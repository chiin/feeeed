import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from anki_scheduler import (
    DeterministicScheduler,
    apply_review_events,
    build_deck_snapshot,
    ensure_daily_batch,
    migrate_history,
    parse_datetime,
)


NOW = datetime(2026, 8, 30, 2, 0, tzinfo=timezone.utc)
ROLLOVER = datetime(2026, 8, 29, 16, 0, tzinfo=timezone.utc)


def review_event(event_id, card_id, rating, reviewed_at=NOW):
    return {
        "event_id": event_id,
        "deck_id": "hsk",
        "card_id": card_id,
        "rating": rating,
        "reviewed_at": reviewed_at.isoformat(),
    }


class DeterministicSchedulerTests(unittest.TestCase):
    def test_confirmed_rating_defaults(self):
        scheduler = DeterministicScheduler()
        new_hard = scheduler.schedule(None, "hard", NOW)
        new_good = scheduler.schedule(None, "good", NOW)
        new_easy = scheduler.schedule(None, "easy", NOW)
        mature = {"interval_days": 10, "repetitions": 3}

        self.assertEqual(new_hard.interval_days, 1)
        self.assertEqual(new_good.interval_days, 1)
        self.assertEqual(new_easy.interval_days, 4)
        self.assertEqual(scheduler.schedule(mature, "hard", NOW).interval_days, 12)
        self.assertEqual(scheduler.schedule(mature, "good", NOW).interval_days, 25)
        self.assertEqual(scheduler.schedule(mature, "easy", NOW).interval_days, 33)
        self.assertEqual(
            new_good.next_due_at,
            datetime(2026, 8, 30, 16, 0, tzinfo=timezone.utc),
        )

    def test_again_is_due_in_ten_minutes(self):
        result = DeterministicScheduler().schedule(None, "again", NOW)
        self.assertEqual(result.next_due_at, NOW + timedelta(minutes=10))
        self.assertEqual(result.state, "relearning")


class DailyBatchTests(unittest.TestCase):
    def setUp(self):
        self.cards = [
            {"id": "due", "front": {}, "back": {}},
            {"id": "new-1", "front": {}, "back": {}},
            {"id": "new-2", "front": {}, "back": {}},
        ]
        self.history = {
            "cards": {
                "due": {
                    "state": "review",
                    "interval_days": 1,
                    "repetitions": 1,
                    "last_reviewed_at": "2026-08-28T16:00:00Z",
                    "next_due_at": "2026-08-29T16:00:00Z",
                }
            }
        }

    def test_midnight_batch_has_all_due_then_limited_unseen_and_is_frozen(self):
        batch = ensure_daily_batch(
            self.history, [card["id"] for card in self.cards], 1, NOW
        )
        self.assertEqual(batch["card_ids"], ["due", "new-1"])

        apply_review_events(
            "hsk", self.history, [review_event("good-1", "new-1", "good")], NOW
        )
        same_batch = ensure_daily_batch(
            self.history, [card["id"] for card in self.cards], 1, NOW
        )
        self.assertEqual(same_batch["card_ids"], ["due", "new-1"])
        self.assertEqual(
            [item["card_id"] for item in same_batch["active"]], ["due"]
        )
        self.assertNotIn("new-2", same_batch["card_ids"])

    def test_again_delays_card_then_allows_a_later_rating(self):
        ensure_daily_batch(
            self.history, [card["id"] for card in self.cards], 1, NOW
        )
        result = apply_review_events(
            "hsk", self.history, [review_event("again-1", "due", "again")], NOW
        )
        self.assertEqual(result["applied"], 1)
        active = self.history["daily_batch"]["active"][-1]
        self.assertEqual(active["card_id"], "due")
        self.assertEqual(parse_datetime(active["available_at"]), NOW + timedelta(minutes=10))

        too_early = review_event(
            "good-early", "due", "good", NOW + timedelta(minutes=9)
        )
        later = review_event(
            "good-later", "due", "good", NOW + timedelta(minutes=10)
        )
        self.assertEqual(
            apply_review_events(
                "hsk", self.history, [too_early], NOW + timedelta(minutes=9)
            )["stale"],
            1,
        )
        self.assertEqual(
            apply_review_events(
                "hsk", self.history, [later], NOW + timedelta(minutes=10)
            )["applied"],
            1,
        )
        self.assertNotIn(
            "due",
            [item["card_id"] for item in self.history["daily_batch"]["active"]],
        )

    def test_duplicate_and_stale_device_events_do_not_reschedule(self):
        ensure_daily_batch(
            self.history, [card["id"] for card in self.cards], 1, NOW
        )
        first = review_event("device-a", "due", "easy")
        stale = review_event("device-b", "due", "hard", NOW + timedelta(seconds=1))
        self.assertEqual(
            apply_review_events("hsk", self.history, [first], NOW)["applied"], 1
        )
        first_state = dict(self.history["cards"]["due"])
        self.assertEqual(
            apply_review_events("hsk", self.history, [first], NOW)["duplicate"], 1
        )
        self.assertEqual(
            apply_review_events(
                "hsk", self.history, [stale], NOW + timedelta(seconds=1)
            )["stale"],
            1,
        )
        self.assertEqual(self.history["cards"]["due"], first_state)

    def test_snapshot_is_cross_device_authority_and_acknowledges_events(self):
        ensure_daily_batch(
            self.history, [card["id"] for card in self.cards], 1, NOW
        )
        apply_review_events(
            "hsk", self.history, [review_event("event-1", "due", "good")], NOW
        )
        snapshot = build_deck_snapshot(
            "hsk", "HSK", self.cards, self.history, NOW
        )
        self.assertNotIn("due", [card["id"] for card in snapshot["cards"]])
        self.assertIn("event-1", snapshot["processed_event_ids"])

    def test_rollover_replaces_yesterdays_batch(self):
        self.history["daily_batch"] = {
            "id": "2026-08-29",
            "date": "2026-08-29",
            "created_at": "2026-08-28T16:00:00Z",
            "card_ids": ["new-2"],
            "active": [{"card_id": "new-2", "available_at": "2026-08-28T16:00:00Z"}],
        }
        batch = ensure_daily_batch(
            self.history, [card["id"] for card in self.cards], 1, NOW
        )
        self.assertEqual(batch["date"], "2026-08-30")
        self.assertEqual(batch["card_ids"], ["due", "new-1"])

    def test_relearning_due_after_rollover_is_available_at_its_due_time(self):
        self.history["cards"]["due"].update(
            {
                "state": "relearning",
                "next_due_at": "2026-08-30T02:10:00Z",
            }
        )
        batch = ensure_daily_batch(
            self.history, [card["id"] for card in self.cards], 1, NOW
        )
        self.assertEqual(batch["card_ids"], ["due", "new-1"])
        self.assertEqual(batch["active"][0]["available_at"], "2026-08-30T02:10:00Z")

    def test_review_from_previous_batch_is_accepted_after_rollover(self):
        previous_review_time = ROLLOVER - timedelta(minutes=1)
        self.history["daily_batch"] = {
            "id": "2026-08-29",
            "date": "2026-08-29",
            "created_at": "2026-08-28T16:00:00Z",
            "card_ids": ["due"],
            "active": [{"card_id": "due", "available_at": "2026-08-28T16:00:00Z"}],
        }
        ensure_daily_batch(
            self.history, [card["id"] for card in self.cards], 1, NOW
        )
        event = review_event("late-delivery", "due", "good", previous_review_time)
        result = apply_review_events("hsk", self.history, [event], NOW)
        self.assertEqual(result["applied"], 1)
        self.assertNotIn(
            "due",
            [item["card_id"] for item in self.history["daily_batch"]["active"]],
        )

    def test_malformed_event_is_rejected_without_crashing(self):
        ensure_daily_batch(
            self.history, [card["id"] for card in self.cards], 1, NOW
        )
        result = apply_review_events("hsk", self.history, ["not-an-object"], NOW)
        self.assertEqual(result["invalid"], 1)


class MigrationAndDispatchTests(unittest.TestCase):
    def test_legacy_records_move_under_cards(self):
        history = {
            "hsk-1": {
                "box": 2,
                "last_shown": "2026-08-01T00:00:00Z",
                "times_shown": 2,
            },
            "cards": {},
        }
        migrate_history(history)
        self.assertNotIn("hsk-1", history)
        self.assertIn("hsk-1", history["cards"])
        self.assertEqual(history["schema_version"], 2)

    def test_repository_dispatch_action_is_normalized(self):
        event = {
            "action": "anki_review",
            "client_payload": {"deck_id": "hsk", "events": []},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "event.json"
            path.write_text(json.dumps(event), encoding="utf-8")
            with patch.dict(os.environ, {"GITHUB_EVENT_PATH": str(path)}):
                from generate_feeds import get_dispatch_payload

                payload = get_dispatch_payload()
        self.assertEqual(payload["event_type"], "anki_review")
        self.assertEqual(payload["deck_id"], "hsk")


class FeedIntegrationTests(unittest.TestCase):
    def make_feed(self):
        from feedgen.feed import FeedGenerator

        feed = FeedGenerator()
        feed.id("https://example.test/hsk.xml")
        feed.title("HSK")
        feed.description("Test feed")
        feed.link(href="https://example.test/hsk.xml", rel="self")
        return feed

    def test_generated_json_and_rss_follow_authoritative_batch(self):
        from generate_feeds import process_anki_deck

        config = {
            "source_type": "csv",
            "path": "HSK.csv",
            "feed_title": "HSK",
            "new_cards_per_day": 1,
        }
        history = {}
        with tempfile.TemporaryDirectory() as directory:
            old_cwd = os.getcwd()
            os.chdir(directory)
            try:
                Path("HSK.csv").write_text(
                    "id,front,back\nhsk-1,one,first\nhsk-2,two,second\n",
                    encoding="utf-8",
                )
                feed = self.make_feed()
                process_anki_deck(
                    "hsk", config, history, feed, "https://example.test", {}, NOW
                )
                first_snapshot = json.loads(
                    Path("cards/hsk_deck.json").read_text(encoding="utf-8")
                )
                self.assertEqual(
                    [card["id"] for card in first_snapshot["cards"]], ["hsk-1"]
                )
                self.assertIn(
                    "hsk-anki-batch-2026-08-30",
                    feed.rss_str(pretty=True).decode("utf-8"),
                )

                event = review_event("event-1", "hsk-1", "good")
                process_anki_deck(
                    "hsk",
                    config,
                    history,
                    self.make_feed(),
                    "https://example.test",
                    {
                        "event_type": "anki_review",
                        "deck_id": "hsk",
                        "events": [event],
                    },
                    NOW,
                )
                updated_snapshot = json.loads(
                    Path("cards/hsk_deck.json").read_text(encoding="utf-8")
                )
                self.assertEqual(updated_snapshot["cards"], [])
                self.assertEqual(
                    history["daily_batch"]["card_ids"], ["hsk-1"]
                )
                self.assertNotIn("hsk-2", history["daily_batch"]["card_ids"])
            finally:
                os.chdir(old_cwd)


if __name__ == "__main__":
    unittest.main()
