from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable


HKT = timezone(timedelta(hours=8))
SCHEMA_VERSION = 2
SCHEDULER_NAME = "deterministic-v1"
RATINGS = {"again", "hard", "good", "easy"}
LEGACY_BOX_INTERVALS = {1: 1, 2: 3, 3: 7, 4: 14, 5: 30}
STATE_KEYS = {
    "schema_version",
    "scheduler",
    "revision",
    "cards",
    "processed_events",
    "daily_batch",
    "previous_batch",
}


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


def hkt_midnight(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=HKT)


def next_hkt_rollover(reviewed_at: datetime, days: int) -> datetime:
    return hkt_midnight(hkt_day(reviewed_at) + timedelta(days=days))


def _rounded_days(value: float) -> int:
    return max(1, math.floor(value + 0.5))


@dataclass(frozen=True)
class ScheduleResult:
    state: str
    interval_days: int
    repetitions: int
    next_due_at: datetime


class DeterministicScheduler:
    name = SCHEDULER_NAME

    def schedule(
        self, card_state: dict | None, rating: str, reviewed_at: datetime
    ) -> ScheduleResult:
        if rating not in RATINGS:
            raise ValueError(f"unsupported rating: {rating}")

        current = card_state or {}
        interval = max(0, int(current.get("interval_days", 0)))
        repetitions = max(0, int(current.get("repetitions", 0)))

        if rating == "again":
            return ScheduleResult(
                state="relearning",
                interval_days=0,
                repetitions=0,
                next_due_at=reviewed_at + timedelta(minutes=10),
            )

        if rating == "hard":
            next_interval = 1 if interval == 0 else _rounded_days(interval * 1.2)
            next_repetitions = repetitions
        elif rating == "good":
            if repetitions == 0:
                next_interval = 1
            elif repetitions == 1:
                next_interval = 3
            else:
                next_interval = _rounded_days(interval * 2.5)
            next_repetitions = repetitions + 1
        else:
            next_interval = 4 if repetitions == 0 else _rounded_days(interval * 3.25)
            next_repetitions = repetitions + 1

        return ScheduleResult(
            state="review",
            interval_days=next_interval,
            repetitions=next_repetitions,
            next_due_at=next_hkt_rollover(reviewed_at, next_interval),
        )

    def previews(self, card_state: dict | None) -> dict[str, str]:
        current = card_state or {}
        interval = max(0, int(current.get("interval_days", 0)))
        repetitions = max(0, int(current.get("repetitions", 0)))
        hard = 1 if interval == 0 else _rounded_days(interval * 1.2)
        if repetitions == 0:
            good = 1
        elif repetitions == 1:
            good = 3
        else:
            good = _rounded_days(interval * 2.5)
        easy = 4 if repetitions == 0 else _rounded_days(interval * 3.25)
        return {
            "again": "10m",
            "hard": f"{hard}d",
            "good": f"{good}d",
            "easy": f"{easy}d",
        }


def _migrate_card_state(card_state: dict) -> dict:
    if "next_due_at" in card_state:
        return {
            "state": card_state.get("state", "review"),
            "interval_days": max(0, int(card_state.get("interval_days", 0))),
            "repetitions": max(0, int(card_state.get("repetitions", 0))),
            "last_reviewed_at": card_state.get("last_reviewed_at"),
            "next_due_at": card_state["next_due_at"],
        }

    if "next_due" in card_state:
        return {
            "state": card_state.get("state", "review"),
            "interval_days": max(0, int(card_state.get("interval", 0))),
            "repetitions": max(0, int(card_state.get("repetitions", 0))),
            "last_reviewed_at": card_state.get("last_reviewed"),
            "next_due_at": card_state["next_due"],
        }

    if "last_shown" in card_state:
        last_reviewed = parse_datetime(card_state["last_shown"])
        interval = LEGACY_BOX_INTERVALS.get(int(card_state.get("box", 1)), 1)
        return {
            "state": "review",
            "interval_days": interval,
            "repetitions": max(0, int(card_state.get("times_shown", 0))),
            "last_reviewed_at": isoformat_utc(last_reviewed),
            "next_due_at": isoformat_utc(next_hkt_rollover(last_reviewed, interval)),
        }

    raise ValueError("unrecognized card history record")


def migrate_history(stream_history: dict) -> dict:
    existing_cards = stream_history.get("cards", {})
    legacy_cards = {
        key: value
        for key, value in list(stream_history.items())
        if key not in STATE_KEYS and isinstance(value, dict)
    }
    merged_cards = {**legacy_cards, **existing_cards}
    migrated_cards = {}
    for card_id, card_state in merged_cards.items():
        try:
            migrated_cards[card_id] = _migrate_card_state(card_state)
        except (TypeError, ValueError):
            print(f"Ignoring invalid legacy Anki state for card '{card_id}'.")

    for card_id in legacy_cards:
        del stream_history[card_id]

    stream_history.update(
        {
            "schema_version": SCHEMA_VERSION,
            "scheduler": {"name": SCHEDULER_NAME, "version": 1},
            "revision": max(0, int(stream_history.get("revision", 0))),
            "cards": migrated_cards,
            "processed_events": stream_history.get("processed_events", {}),
        }
    )
    return stream_history


def ensure_daily_batch(
    stream_history: dict,
    all_card_ids: Iterable[str],
    new_limit: int,
    now: datetime,
) -> dict:
    migrate_history(stream_history)
    current_day = hkt_day(now).isoformat()
    existing = stream_history.get("daily_batch")
    if existing and existing.get("date") == current_day:
        return existing

    rollover = hkt_midnight(hkt_day(now)).astimezone(timezone.utc)
    day_end = rollover + timedelta(days=1)
    cards = stream_history["cards"]
    source_ids = list(all_card_ids)
    source_position = {card_id: index for index, card_id in enumerate(source_ids)}

    due_ids = [
        card_id
        for card_id, state in cards.items()
        if card_id in source_position
        and (
            parse_datetime(state["next_due_at"]) <= rollover
            or (
                state.get("state") == "relearning"
                and parse_datetime(state["next_due_at"]) < day_end
            )
        )
    ]
    due_ids.sort(
        key=lambda card_id: (
            parse_datetime(cards[card_id]["next_due_at"]),
            source_position[card_id],
        )
    )
    unseen_ids = [card_id for card_id in source_ids if card_id not in cards]
    member_ids = due_ids + unseen_ids[: max(0, new_limit)]
    if existing:
        stream_history["previous_batch"] = existing
    batch = {
        "id": current_day,
        "date": current_day,
        "created_at": isoformat_utc(now),
        "card_ids": member_ids,
        "active": [
            {
                "card_id": card_id,
                "available_at": isoformat_utc(
                    max(
                        rollover,
                        parse_datetime(cards[card_id]["next_due_at"])
                        if card_id in cards
                        else rollover,
                    )
                ),
            }
            for card_id in member_ids
        ],
    }
    stream_history["daily_batch"] = batch
    stream_history["revision"] += 1
    return batch


def _validate_event(event: dict, stream_key: str, now: datetime) -> tuple:
    if not isinstance(event, dict):
        raise ValueError("review event must be an object")
    required = ("event_id", "deck_id", "card_id", "rating", "reviewed_at")
    missing = [key for key in required if not event.get(key)]
    if missing:
        raise ValueError(f"review event missing: {', '.join(missing)}")
    if event["deck_id"] != stream_key:
        raise ValueError("review event deck does not match stream")
    if event["rating"] not in RATINGS:
        raise ValueError("review event has an invalid rating")
    if len(event["event_id"]) > 128 or len(event["card_id"]) > 128:
        raise ValueError("review event identifier is too long")
    reviewed_at = parse_datetime(event["reviewed_at"])
    if reviewed_at > now.astimezone(timezone.utc) + timedelta(minutes=5):
        raise ValueError("review event timestamp is in the future")
    return (
        event["event_id"],
        event["card_id"],
        event["rating"],
        reviewed_at,
    )


def apply_review_events(
    stream_key: str,
    stream_history: dict,
    events: Iterable[dict],
    now: datetime,
    scheduler: DeterministicScheduler | None = None,
) -> dict[str, int]:
    migrate_history(stream_history)
    scheduler = scheduler or DeterministicScheduler()
    processed = stream_history["processed_events"]
    current_batch = stream_history.get("daily_batch")
    previous_batch = stream_history.get("previous_batch")
    counts = {"applied": 0, "duplicate": 0, "stale": 0, "invalid": 0}

    valid_events = []
    for event in events:
        try:
            validated = _validate_event(event, stream_key, now)
        except (TypeError, ValueError) as error:
            print(f"Ignoring invalid Anki review event: {error}")
            counts["invalid"] += 1
            continue
        valid_events.append((validated, event))

    valid_events.sort(key=lambda item: item[0][3])
    for (event_id, card_id, rating, reviewed_at), _event in valid_events:
        if event_id in processed:
            counts["duplicate"] += 1
            continue

        processed[event_id] = {
            "processed_at": isoformat_utc(now),
            "status": "stale",
        }
        event_day = hkt_day(reviewed_at).isoformat()
        batch = next(
            (
                candidate
                for candidate in (current_batch, previous_batch)
                if candidate and candidate.get("date") == event_day
            ),
            None,
        )
        if not batch:
            counts["stale"] += 1
            continue

        active_index = next(
            (
                index
                for index, item in enumerate(batch["active"])
                if item["card_id"] == card_id
            ),
            None,
        )
        if active_index is None:
            counts["stale"] += 1
            continue

        active_item = batch["active"][active_index]
        if parse_datetime(active_item["available_at"]) > reviewed_at:
            counts["stale"] += 1
            continue

        result = scheduler.schedule(
            stream_history["cards"].get(card_id), rating, reviewed_at
        )
        stream_history["cards"][card_id] = {
            "state": result.state,
            "interval_days": result.interval_days,
            "repetitions": result.repetitions,
            "last_reviewed_at": isoformat_utc(reviewed_at),
            "next_due_at": isoformat_utc(result.next_due_at),
        }
        del batch["active"][active_index]
        if batch is previous_batch and current_batch:
            current_batch["active"] = [
                item
                for item in current_batch["active"]
                if item["card_id"] != card_id
            ]
        target_batch = (
            current_batch
            if current_batch
            and hkt_day(result.next_due_at).isoformat() == current_batch.get("date")
            else batch
        )
        if rating == "again" and target_batch.get("date") == hkt_day(
            result.next_due_at
        ).isoformat():
            target_batch["active"].append(
                {
                    "card_id": card_id,
                    "available_at": isoformat_utc(result.next_due_at),
                }
            )
        processed[event_id]["status"] = "applied"
        stream_history["revision"] += 1
        counts["applied"] += 1

    while len(processed) > 1000:
        del processed[next(iter(processed))]
    return counts


def build_deck_snapshot(
    stream_key: str,
    title: str,
    all_cards: list[dict],
    stream_history: dict,
    now: datetime,
    scheduler: DeterministicScheduler | None = None,
) -> dict:
    scheduler = scheduler or DeterministicScheduler()
    batch = stream_history["daily_batch"]
    cards_by_id = {card["id"]: card for card in all_cards}
    active_cards = []
    for item in batch["active"]:
        card = cards_by_id.get(item["card_id"])
        if card is None:
            continue
        state = stream_history["cards"].get(item["card_id"])
        active_cards.append(
            {
                **card,
                "available_at": item["available_at"],
                "scheduler_state": state,
                "schedule_previews": scheduler.previews(state),
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "deck_id": stream_key,
        "title": title,
        "batch_id": batch["id"],
        "batch_date": batch["date"],
        "compiled_at": isoformat_utc(now),
        "state_revision": stream_history["revision"],
        "processed_event_ids": list(stream_history["processed_events"]),
        "cards": active_cards,
    }
