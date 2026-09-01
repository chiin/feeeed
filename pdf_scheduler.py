from __future__ import annotations

import hashlib
import random
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import quote


HKT = timezone(timedelta(hours=8))
SCHEMA_VERSION = 2
STRATEGIES = {
    "sequential",
    "random_without_replacement",
    "random_with_replacement",
}
EVENT_ACTIONS = {"complete", "continue"}
MIN_RELEASE_MINUTE = 60
MAX_RELEASE_MINUTE = 21 * 60


def parse_datetime(value: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("timestamp must be a non-empty ISO 8601 string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def isoformat_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def hkt_day(value: datetime) -> date:
    return value.astimezone(HKT).date()


def _hkt_midnight(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=HKT)


def _stable_number(value: str) -> int:
    return int.from_bytes(hashlib.sha256(value.encode("utf-8")).digest()[:8], "big")


def initial_release_at(stream_key: str, now: datetime) -> datetime:
    day = hkt_day(now)
    minute = MIN_RELEASE_MINUTE + (
        _stable_number(f"{stream_key}:{day.isoformat()}:initial")
        % (MAX_RELEASE_MINUTE - MIN_RELEASE_MINUTE + 1)
    )
    return (_hkt_midnight(day) + timedelta(minutes=minute)).astimezone(timezone.utc)


def next_release_at(stream_key: str, released_at: datetime) -> datetime:
    local_release = released_at.astimezone(HKT)
    target_day = local_release.date() + timedelta(days=1)
    previous_minute = local_release.hour * 60 + local_release.minute
    jitter = (
        _stable_number(f"{stream_key}:{target_day.isoformat()}:jitter") % 361
    ) - 180
    target_minute = min(
        MAX_RELEASE_MINUTE,
        max(MIN_RELEASE_MINUTE, previous_minute + jitter),
    )
    return (
        _hkt_midnight(target_day) + timedelta(minutes=target_minute)
    ).astimezone(timezone.utc)


def _normalize_strategy(strategy: str) -> str:
    if strategy == "random":
        return "random_with_replacement"
    if strategy not in STRATEGIES:
        raise ValueError(f"unsupported PDF selection strategy: {strategy}")
    return strategy


def natural_sort_key(value: str) -> tuple:
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.lower())
        for part in re.split(r"(\d+)", value)
    )


def _legacy_batch(legacy_snapshot: dict | None) -> dict | None:
    if not legacy_snapshot or not legacy_snapshot.get("batch"):
        return None
    try:
        compiled_at = parse_datetime(legacy_snapshot["compiled_at"])
    except (KeyError, TypeError, ValueError):
        return None
    ids = [item["id"] for item in legacy_snapshot["batch"] if item.get("id")]
    return {
        "id": hkt_day(compiled_at).isoformat(),
        "date": hkt_day(compiled_at).isoformat(),
        "released_at": isoformat_utc(compiled_at),
        "release_summary_ids": ids,
        "active_ids": list(ids),
        "selected_ids": list(ids),
        "segments": [{"kind": "initial", "ids": list(ids)}],
    }


def migrate_history(
    stream_key: str,
    strategy: str,
    stream_history: dict,
    now: datetime,
    legacy_snapshot: dict | None = None,
    book_id: str | None = None,
) -> dict:
    strategy = _normalize_strategy(strategy)
    if stream_history.get("pdf_schema_version") == SCHEMA_VERSION:
        if book_id is not None and stream_history.get("book_id") != book_id:
            previous_revision = max(0, int(stream_history.get("revision", 0)))
            stream_history.clear()
            stream_history.update(
                {
                    "pdf_schema_version": SCHEMA_VERSION,
                    "strategy": strategy,
                    "book_id": book_id,
                    "revision": previous_revision + 1,
                    "completed_ids": [],
                    "completion_counts": {},
                    "processed_events": {},
                    "daily_batch": None,
                    "next_release_at": isoformat_utc(now),
                }
            )
            return stream_history
        stream_history["strategy"] = strategy
        return stream_history

    legacy_completed = list(stream_history.get("completed_files", []))
    completed_ids = (
        list(dict.fromkeys(legacy_completed))
        if strategy != "random_with_replacement"
        else []
    )
    batch = stream_history.get("daily_batch") or _legacy_batch(legacy_snapshot)
    release_value = stream_history.get("next_release_at") or stream_history.get(
        "next_update_at"
    )
    try:
        release_at = parse_datetime(release_value) if release_value else None
    except (TypeError, ValueError):
        release_at = None

    for legacy_key in ("completed_files", "next_update_at", "shown_files", "last_index"):
        stream_history.pop(legacy_key, None)
    stream_history.update(
        {
            "pdf_schema_version": SCHEMA_VERSION,
            "strategy": strategy,
            "revision": max(0, int(stream_history.get("revision", 0))),
            "completed_ids": completed_ids,
            "completion_counts": stream_history.get("completion_counts", {}),
            "processed_events": stream_history.get("processed_events", {}),
            "daily_batch": batch,
            "next_release_at": isoformat_utc(
                release_at
                or (
                    now.astimezone(timezone.utc)
                    if book_id
                    else initial_release_at(stream_key, now)
                )
            ),
        }
    )
    if book_id is not None:
        stream_history["book_id"] = book_id
    return stream_history


def _eligible_ids(
    all_ids: list[str],
    strategy: str,
    completed_ids: set[str],
    excluded_ids: set[str],
) -> list[str]:
    candidates = [pdf_id for pdf_id in all_ids if pdf_id not in excluded_ids]
    if strategy != "random_with_replacement":
        candidates = [
            pdf_id for pdf_id in candidates if pdf_id not in completed_ids
        ]
    return candidates


def _select_ids(
    stream_key: str,
    strategy: str,
    candidates: list[str],
    count: int,
    batch_date: str,
    segment_number: int,
) -> list[str]:
    if count <= 0:
        return []
    if strategy == "sequential":
        return candidates[:count]
    shuffled = list(candidates)
    random.Random(
        f"{stream_key}:{batch_date}:{segment_number}:{strategy}"
    ).shuffle(shuffled)
    return shuffled[:count]


def _fill_batch(
    stream_key: str,
    stream_history: dict,
    all_ids: list[str],
    batch_size: int,
    kind: str,
) -> list[str]:
    batch = stream_history["daily_batch"]
    active_ids = batch["active_ids"]
    selected_ids = set(batch["selected_ids"])
    completed_ids = set(stream_history["completed_ids"])
    count = batch_size if kind == "continuation" else batch_size - len(active_ids)
    candidates = _eligible_ids(
        all_ids,
        stream_history["strategy"],
        completed_ids,
        selected_ids | set(active_ids),
    )
    selected = _select_ids(
        stream_key,
        stream_history["strategy"],
        candidates,
        count,
        batch["date"],
        len(batch["segments"]),
    )
    active_ids.extend(selected)
    batch["selected_ids"].extend(selected)
    if selected:
        batch["segments"].append({"kind": kind, "ids": selected})
    return selected


def release_if_due(
    stream_key: str,
    stream_history: dict,
    all_ids: Iterable[str],
    batch_size: int,
    now: datetime,
    force_today: bool = False,
) -> bool:
    all_ids = sorted(all_ids, key=natural_sort_key)
    today = hkt_day(now).isoformat()
    current = stream_history.get("daily_batch")
    if current and current.get("date") == today:
        return False

    due_at = parse_datetime(stream_history["next_release_at"])
    if not force_today and now.astimezone(timezone.utc) < due_at:
        return False

    carryover = [
        pdf_id
        for pdf_id in (current or {}).get("active_ids", [])
        if pdf_id in all_ids
    ][:batch_size]
    released_at = now.astimezone(timezone.utc)
    stream_history["daily_batch"] = {
        "id": today,
        "date": today,
        "released_at": isoformat_utc(released_at),
        "release_summary_ids": [],
        "active_ids": list(carryover),
        "selected_ids": list(carryover),
        "segments": [],
    }
    selected = _fill_batch(
        stream_key, stream_history, all_ids, batch_size, "initial"
    )
    batch = stream_history["daily_batch"]
    batch["release_summary_ids"] = carryover + selected
    if carryover and not selected:
        batch["segments"].append({"kind": "initial", "ids": list(carryover)})
    elif carryover:
        batch["segments"][0]["ids"] = carryover + batch["segments"][0]["ids"]
    stream_history["next_release_at"] = isoformat_utc(
        next_release_at(stream_key, released_at)
    )
    stream_history["revision"] += 1
    return True


def _validate_event(
    stream_key: str,
    event: dict,
    now: datetime,
    book_id: str | None = None,
) -> tuple[str, str, str | None, datetime]:
    if not isinstance(event, dict):
        raise ValueError("PDF event must be an object")
    required = ("event_id", "stream_id", "action", "occurred_at")
    missing = [key for key in required if not event.get(key)]
    if missing:
        raise ValueError(f"PDF event missing: {', '.join(missing)}")
    if event["stream_id"] != stream_key:
        raise ValueError("PDF event stream does not match")
    if book_id is not None and event.get("book_id") != book_id:
        raise ValueError("PDF event book does not match")
    if event["action"] not in EVENT_ACTIONS:
        raise ValueError("PDF event action is invalid")
    pdf_id = event.get("pdf_id")
    if event["action"] == "complete" and not pdf_id:
        raise ValueError("completion event requires pdf_id")
    occurred_at = parse_datetime(event["occurred_at"])
    if occurred_at > now.astimezone(timezone.utc) + timedelta(minutes=5):
        raise ValueError("PDF event timestamp is in the future")
    return event["event_id"], event["action"], pdf_id, occurred_at


def apply_completion_events(
    stream_key: str,
    stream_history: dict,
    events: Iterable[dict],
    now: datetime,
    book_id: str | None = None,
) -> dict[str, int]:
    processed = stream_history["processed_events"]
    batch = stream_history.get("daily_batch")
    counts = {"applied": 0, "duplicate": 0, "stale": 0, "invalid": 0}
    valid_events = []
    for event in events:
        try:
            validated = _validate_event(stream_key, event, now, book_id)
        except (TypeError, ValueError) as error:
            print(f"Ignoring invalid PDF event: {error}")
            counts["invalid"] += 1
            continue
        if validated[1] == "complete":
            valid_events.append(validated)

    valid_events.sort(key=lambda item: item[3])
    for event_id, _action, pdf_id, _occurred_at in valid_events:
        if event_id in processed:
            counts["duplicate"] += 1
            continue
        processed[event_id] = {
            "processed_at": isoformat_utc(now),
            "status": "stale",
        }
        if not batch or pdf_id not in batch["active_ids"]:
            counts["stale"] += 1
            continue

        batch["active_ids"].remove(pdf_id)
        counts_by_id = stream_history["completion_counts"]
        counts_by_id[pdf_id] = counts_by_id.get(pdf_id, 0) + 1
        if (
            stream_history["strategy"] != "random_with_replacement"
            and pdf_id not in stream_history["completed_ids"]
        ):
            stream_history["completed_ids"].append(pdf_id)
        processed[event_id]["status"] = "applied"
        stream_history["revision"] += 1
        counts["applied"] += 1
    return counts


def apply_continuation_events(
    stream_key: str,
    stream_history: dict,
    events: Iterable[dict],
    all_ids: Iterable[str],
    batch_size: int,
    now: datetime,
    book_id: str | None = None,
) -> dict[str, int]:
    processed = stream_history["processed_events"]
    counts = {"applied": 0, "duplicate": 0, "stale": 0, "invalid": 0}
    valid_events = []
    for event in events:
        try:
            validated = _validate_event(stream_key, event, now, book_id)
        except (TypeError, ValueError) as error:
            print(f"Ignoring invalid PDF event: {error}")
            counts["invalid"] += 1
            continue
        if validated[1] == "continue":
            valid_events.append(validated)

    valid_events.sort(key=lambda item: item[3])
    for event_id, _action, _pdf_id, _occurred_at in valid_events:
        if event_id in processed:
            counts["duplicate"] += 1
            continue
        processed[event_id] = {
            "processed_at": isoformat_utc(now),
            "status": "stale",
        }
        batch = stream_history.get("daily_batch")
        if not batch or batch["active_ids"]:
            counts["stale"] += 1
            continue
        selected = _fill_batch(
            stream_key,
            stream_history,
            sorted(all_ids, key=natural_sort_key),
            batch_size,
            "continuation",
        )
        if not selected:
            counts["stale"] += 1
            continue
        processed[event_id]["status"] = "applied"
        stream_history["revision"] += 1
        counts["applied"] += 1
    return counts


def can_continue(stream_history: dict, all_ids: Iterable[str]) -> bool:
    batch = stream_history.get("daily_batch")
    if not batch or batch["active_ids"]:
        return False
    return bool(
        _eligible_ids(
            sorted(all_ids, key=natural_sort_key),
            stream_history["strategy"],
            set(stream_history["completed_ids"]),
            set(batch["selected_ids"]),
        )
    )


def _title(pdf_id: str) -> str:
    return Path(pdf_id).stem.replace("_", " ").title()


def _item(pdf_id: str, folder_name: str, base_url: str) -> dict:
    return {
        "id": pdf_id,
        "title": _title(pdf_id),
        "pdf_url": f"{base_url}/{quote(folder_name)}/{quote(pdf_id)}",
    }


def build_snapshot(
    stream_key: str,
    title: str,
    folder_name: str,
    base_url: str,
    stream_history: dict,
    all_ids: Iterable[str],
    batch_size: int,
    now: datetime,
    book_id: str | None = None,
) -> dict:
    batch = stream_history.get("daily_batch")
    active_ids = batch["active_ids"] if batch else []
    summary_ids = batch["release_summary_ids"] if batch else []
    return {
        "schema_version": SCHEMA_VERSION,
        "stream": stream_key,
        "title": title,
        "compiled_at": isoformat_utc(now),
        "state_revision": stream_history["revision"],
        "batch_id": batch["id"] if batch else None,
        "batch_date": batch["date"] if batch else None,
        "released_at": batch["released_at"] if batch else None,
        "next_release_at": stream_history["next_release_at"],
        "batch_size": batch_size,
        "strategy": stream_history["strategy"],
        "release_summary": [
            _item(pdf_id, folder_name, base_url) for pdf_id in summary_ids
        ],
        "active": [
            _item(pdf_id, folder_name, base_url) for pdf_id in active_ids
        ],
        "batch": [
            _item(pdf_id, folder_name, base_url) for pdf_id in active_ids
        ],
        "can_continue": can_continue(stream_history, all_ids),
        "processed_event_ids": list(stream_history["processed_events"]),
        **({"book_id": book_id} if book_id is not None else {}),
    }
