from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable

from fsrs import Card, Rating, Scheduler, State


HKT = timezone(timedelta(hours=8))
SCHEMA_VERSION = 3
SCHEDULER_NAME = "fsrs-6.3.2"
SCHEDULER_VERSION = 1
DESIRED_RETENTION = 0.9
RATINGS = {"again", "hard", "good", "easy"}
RATING_MAP = {
    "again": Rating.Again,
    "hard": Rating.Hard,
    "good": Rating.Good,
    "easy": Rating.Easy,
}
STATE_MAP = {
    "learning": State.Learning,
    "review": State.Review,
    "relearning": State.Relearning,
}
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


@dataclass(frozen=True)
class ScheduleResult:
    state: str
    step: int | None
    stability: float
    difficulty: float
    interval_days: int
    reviews: int
    lapses: int
    last_reviewed_at: datetime
    next_due_at: datetime


class FSRSScheduler:
    name = SCHEDULER_NAME

    def __init__(self) -> None:
        self.scheduler = Scheduler(
            desired_retention=DESIRED_RETENTION,
            learning_steps=(),
            relearning_steps=(),
            enable_fuzzing=False,
        )

    def _card(self, card_state: dict | None, reviewed_at: datetime) -> Card:
        if not card_state:
            return Card(card_id=0, due=reviewed_at)
        return Card(
            card_id=0,
            state=STATE_MAP[card_state["state"]],
            step=card_state.get("step"),
            stability=float(card_state["stability"]),
            difficulty=float(card_state["difficulty"]),
            due=parse_datetime(card_state["next_due_at"]),
            last_review=parse_datetime(card_state["last_reviewed_at"]),
        )

    def schedule(
        self, card_state: dict | None, rating: str, reviewed_at: datetime
    ) -> ScheduleResult:
        if rating not in RATINGS:
            raise ValueError(f"unsupported rating: {rating}")

        reviewed_at = reviewed_at.astimezone(timezone.utc)
        card = self._card(card_state, reviewed_at)
        updated, _review_log = self.scheduler.review_card(
            card, RATING_MAP[rating], review_datetime=reviewed_at
        )
        reviews = max(0, int((card_state or {}).get("reviews", 0))) + 1
        lapses = max(0, int((card_state or {}).get("lapses", 0)))
        if rating == "again":
            next_due_at = reviewed_at + timedelta(minutes=10)
            state = "relearning"
            step = 0
            if card_state:
                lapses += 1
            interval_days = 0
        else:
            interval_days = max(1, (updated.due - reviewed_at).days)
            next_due_at = next_hkt_rollover(reviewed_at, interval_days)
            state = "review"
            step = None

        return ScheduleResult(
            state=state,
            step=step,
            stability=float(updated.stability),
            difficulty=float(updated.difficulty),
            interval_days=interval_days,
            reviews=reviews,
            lapses=lapses,
            last_reviewed_at=reviewed_at,
            next_due_at=next_due_at,
        )

    def previews(
        self, card_state: dict | None, reviewed_at: datetime
    ) -> dict[str, str]:
        previews = {"again": "10m"}
        for rating in ("hard", "good", "easy"):
            result = self.schedule(card_state, rating, reviewed_at)
            previews[rating] = f"{result.interval_days}d"
        return previews


def migrate_history(stream_history: dict) -> dict:
    legacy_cards = {
        key: value
        for key, value in list(stream_history.items())
        if key not in STATE_KEYS and isinstance(value, dict)
    }
    for card_id in legacy_cards:
        del stream_history[card_id]

    current_scheduler = stream_history.get("scheduler", {})
    reset_cards = (
        stream_history.get("schema_version") != SCHEMA_VERSION
        or current_scheduler.get("name") != SCHEDULER_NAME
        or current_scheduler.get("version") != SCHEDULER_VERSION
    )
    if reset_cards and (legacy_cards or stream_history.get("cards")):
        print("Resetting incompatible Anki card state for FSRS migration.")

    stream_history.update(
        {
            "schema_version": SCHEMA_VERSION,
            "scheduler": {
                "name": SCHEDULER_NAME,
                "version": SCHEDULER_VERSION,
                "desired_retention": DESIRED_RETENTION,
            },
            "revision": max(0, int(stream_history.get("revision", 0))),
            "cards": {} if reset_cards else stream_history.get("cards", {}),
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
    scheduler: FSRSScheduler | None = None,
) -> dict[str, int]:
    migrate_history(stream_history)
    scheduler = scheduler or FSRSScheduler()
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

        current_state = stream_history["cards"].get(card_id)
        if (
            current_state
            and parse_datetime(current_state["last_reviewed_at"]) > reviewed_at
        ):
            counts["stale"] += 1
            continue

        result = scheduler.schedule(current_state, rating, reviewed_at)
        stream_history["cards"][card_id] = {
            "state": result.state,
            "step": result.step,
            "stability": result.stability,
            "difficulty": result.difficulty,
            "interval_days": result.interval_days,
            "reviews": result.reviews,
            "lapses": result.lapses,
            "last_reviewed_at": isoformat_utc(result.last_reviewed_at),
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
    scheduler: FSRSScheduler | None = None,
    front_text_scale: float = 1.0,
) -> dict:
    scheduler = scheduler or FSRSScheduler()
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
                "schedule_previews": scheduler.previews(state, now),
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "deck_id": stream_key,
        "title": title,
        "front_text_scale": front_text_scale,
        "batch_id": batch["id"],
        "batch_date": batch["date"],
        "compiled_at": isoformat_utc(now),
        "state_revision": stream_history["revision"],
        "processed_event_ids": list(stream_history["processed_events"]),
        "cards": active_cards,
    }

