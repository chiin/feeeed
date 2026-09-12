import csv
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from anki_scheduler import (
    FSRSScheduler,
    SCHEMA_VERSION,
    SCHEDULER_NAME,
    SCHEDULER_VERSION,
    apply_review_events,
    build_deck_snapshot,
    ensure_daily_batch,
    migrate_history,
    parse_datetime,
)


NOW = datetime(2026, 8, 30, 2, 0, tzinfo=timezone.utc)
ROLLOVER = datetime(2026, 8, 29, 16, 0, tzinfo=timezone.utc)


def fsrs_history(cards=None):
    return {
        "schema_version": SCHEMA_VERSION,
        "scheduler": {
            "name": SCHEDULER_NAME,
            "version": SCHEDULER_VERSION,
            "desired_retention": 0.9,
        },
        "cards": cards or {},
    }


def fsrs_card_state(
    last_reviewed_at="2026-08-28T16:00:00Z",
    next_due_at="2026-08-29T16:00:00Z",
    state="review",
    step=None,
):
    return {
        "state": state,
        "step": step,
        "stability": 1.2931,
        "difficulty": 5.112170705601056,
        "interval_days": 1,
        "reviews": 1,
        "lapses": 0,
        "last_reviewed_at": last_reviewed_at,
        "next_due_at": next_due_at,
    }


def review_event(event_id, card_id, rating, reviewed_at=NOW):
    return {
        "event_id": event_id,
        "deck_id": "hsk",
        "card_id": card_id,
        "rating": rating,
        "reviewed_at": reviewed_at.isoformat(),
    }


class FSRSSchedulerTests(unittest.TestCase):
    def test_default_fsrs_ratings_are_ordered_and_hkt_anchored(self):
        scheduler = FSRSScheduler()
        new_hard = scheduler.schedule(None, "hard", NOW)
        new_good = scheduler.schedule(None, "good", NOW)
        new_easy = scheduler.schedule(None, "easy", NOW)

        self.assertEqual(new_hard.interval_days, 1)
        self.assertLess(new_hard.interval_days, new_good.interval_days)
        self.assertLess(new_good.interval_days, new_easy.interval_days)
        self.assertLess(new_hard.stability, new_good.stability)
        self.assertLess(new_good.stability, new_easy.stability)
        self.assertEqual(
            new_good.next_due_at,
            datetime(2026, 9, 1, 0, 0, tzinfo=timezone(timedelta(hours=8))),
        )

    def test_again_is_due_in_ten_minutes(self):
        result = FSRSScheduler().schedule(None, "again", NOW)
        self.assertEqual(result.next_due_at, NOW + timedelta(minutes=10))
        self.assertEqual(result.state, "relearning")
        self.assertEqual(result.step, 0)

    def test_repeated_good_increases_stability_and_interval(self):
        scheduler = FSRSScheduler()
        first = scheduler.schedule(None, "good", NOW)
        state = {
            "state": first.state,
            "step": first.step,
            "stability": first.stability,
            "difficulty": first.difficulty,
            "interval_days": first.interval_days,
            "reviews": first.reviews,
            "lapses": first.lapses,
            "last_reviewed_at": first.last_reviewed_at.isoformat(),
            "next_due_at": first.next_due_at.isoformat(),
        }
        second_review = first.next_due_at.astimezone(timezone.utc)
        second = scheduler.schedule(state, "good", second_review)

        self.assertGreater(second.stability, first.stability)
        self.assertGreater(second.interval_days, first.interval_days)

    def test_only_good_and_easy_increment_passing_reviews(self):
        scheduler = FSRSScheduler()

        again = scheduler.schedule(None, "again", NOW)
        hard = scheduler.schedule(None, "hard", NOW)
        good = scheduler.schedule(None, "good", NOW)
        easy = scheduler.schedule(
            {
                **fsrs_card_state(),
                "passing_reviews": 1,
            },
            "easy",
            NOW,
        )

        self.assertEqual(again.passing_reviews, 0)
        self.assertEqual(hard.passing_reviews, 0)
        self.assertEqual(good.passing_reviews, 1)
        self.assertEqual(easy.passing_reviews, 2)

    def test_migration_grandfathers_reviewed_cards_as_passing(self):
        history = fsrs_history({"legacy": fsrs_card_state()})

        migrate_history(history)

        self.assertEqual(history["cards"]["legacy"]["passing_reviews"], 1)
        self.assertIsNone(history["cards"]["legacy"]["last_rating"])

    def test_previews_are_deterministic_and_do_not_mutate_state(self):
        scheduler = FSRSScheduler()
        state = fsrs_card_state()
        original = dict(state)

        first = scheduler.previews(state, NOW)
        second = scheduler.previews(state, NOW)

        self.assertEqual(first, second)
        self.assertEqual(state, original)
        self.assertEqual(first["again"], "10m")


class DailyBatchTests(unittest.TestCase):
    def setUp(self):
        self.cards = [
            {"id": "due", "front": {}, "back": {}},
            {"id": "new-1", "front": {}, "back": {}},
            {"id": "new-2", "front": {}, "back": {}},
        ]
        self.history = fsrs_history({"due": fsrs_card_state()})

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

    def test_again_retry_remains_valid_across_hkt_midnight(self):
        before_midnight = datetime(2026, 8, 29, 15, 50, tzinfo=timezone.utc)
        after_midnight = before_midnight + timedelta(minutes=10)
        self.history["daily_batch"] = {
            "id": "2026-08-29",
            "date": "2026-08-29",
            "created_at": "2026-08-28T16:00:00Z",
            "card_ids": ["due"],
            "active": [
                {
                    "card_id": "due",
                    "available_at": "2026-08-29T15:00:00Z",
                }
            ],
        }

        again = review_event("midnight-again", "due", "again", before_midnight)
        good = review_event("midnight-good", "due", "good", after_midnight)

        self.assertEqual(
            apply_review_events(
                "hsk",
                self.history,
                [again],
                before_midnight,
            )["applied"],
            1,
        )
        self.assertEqual(
            apply_review_events(
                "hsk",
                self.history,
                [good],
                after_midnight,
            )["applied"],
            1,
        )
        self.assertEqual(self.history["cards"]["due"]["last_rating"], "good")

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

    def test_snapshot_exposes_configured_front_text_scale(self):
        ensure_daily_batch(
            self.history, [card["id"] for card in self.cards], 1, NOW
        )
        snapshot = build_deck_snapshot(
            "hsk", "HSK", self.cards, self.history, NOW, front_text_scale=2
        )

        self.assertEqual(snapshot["front_text_scale"], 2)

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
    def test_legacy_scheduler_state_is_reset_but_sync_state_is_preserved(self):
        history = {
            "hsk-1": {
                "box": 2,
                "last_shown": "2026-08-01T00:00:00Z",
                "times_shown": 2,
            },
            "cards": {},
            "revision": 7,
            "processed_events": {"event-1": {"status": "applied"}},
            "daily_batch": {"id": "2026-08-30", "active": []},
        }
        migrate_history(history)
        self.assertNotIn("hsk-1", history)
        self.assertEqual(history["cards"], {})
        self.assertEqual(history["schema_version"], SCHEMA_VERSION)
        self.assertEqual(history["scheduler"]["name"], SCHEDULER_NAME)
        self.assertEqual(history["revision"], 7)
        self.assertIn("event-1", history["processed_events"])
        self.assertEqual(history["daily_batch"]["id"], "2026-08-30")

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

    def test_csv_deck_accepts_utf8_bom(self):
        from generate_feeds import parse_csv_deck

        with tempfile.TemporaryDirectory() as directory:
            csv_path = Path(directory) / "Slovak.csv"
            csv_path.write_text(
                "\ufeffid,front,back,entry_type\n"
                "slovak-0001,jeden,1,term\n",
                encoding="utf-8",
            )

            cards = parse_csv_deck(csv_path, "https://example.test")

        self.assertEqual(cards[0]["id"], "slovak-0001")
        self.assertEqual(cards[0]["front"]["text"], "jeden")
        self.assertEqual(cards[0]["back"]["text"], "1")
        self.assertEqual(cards[0]["entry_type"], "term")

    def test_character_lookup_links_filter_and_deduplicate_han_characters(self):
        from generate_feeds import add_character_lookup_links

        cards = [
            {
                "id": "hsk-1",
                "front": {"text": "打電話3打"},
                "back": {"text": "make a phone call"},
            }
        ]
        config = {
            "character_lookup": {
                "provider": "dong_chinese",
                "placement": "back",
            }
        }

        enriched = add_character_lookup_links("hsk", config, cards)

        self.assertEqual(
            enriched[0]["character_links"],
            [
                {
                    "character": "打",
                    "url": "https://www.dong-chinese.com/wiki/%E6%89%93",
                },
                {
                    "character": "電",
                    "url": "https://www.dong-chinese.com/wiki/%E9%9B%BB",
                },
                {
                    "character": "話",
                    "url": "https://www.dong-chinese.com/wiki/%E8%A9%B1",
                },
            ],
        )
        self.assertNotIn("character_links", cards[0])

    def test_character_lookup_is_absent_for_unconfigured_decks(self):
        from generate_feeds import add_character_lookup_links

        cards = [
            {
                "id": "slovak-1",
                "front": {"text": "jeden"},
                "back": {"text": "one"},
            }
        ]

        self.assertIs(add_character_lookup_links("slovak_vocab", {}, cards), cards)

    def test_character_lookup_applies_to_merged_approved_vocabulary_cards(self):
        from generate_feeds import process_anki_deck

        config = {
            "source_type": "csv",
            "path": "HSK.csv",
            "feed_title": "HSK",
            "new_cards_per_day": 2,
            "character_lookup": {
                "provider": "dong_chinese",
                "placement": "back",
            },
        }
        cards = [
            {
                "id": "hsk-1",
                "entry_type": "term",
                "front": {"text": "電話", "audio": None, "image": None},
                "back": {"text": "telephone", "notes": None},
            },
            {
                "id": "approved-1",
                "entry_type": "term",
                "front": {"text": "圖書館", "audio": None, "image": None},
                "back": {"text": "library", "notes": "approved candidate"},
            },
        ]

        with tempfile.TemporaryDirectory() as directory:
            old_cwd = os.getcwd()
            os.chdir(directory)
            try:
                process_anki_deck(
                    "hsk",
                    config,
                    {},
                    self.make_feed(),
                    "https://example.test",
                    {},
                    NOW,
                    all_cards_override=cards,
                )
                snapshot = json.loads(
                    Path("cards/hsk_deck.json").read_text(encoding="utf-8")
                )
            finally:
                os.chdir(old_cwd)

        self.assertEqual(
            [
                link["character"]
                for link in snapshot["cards"][0]["character_links"]
            ],
            ["電", "話"],
        )
        self.assertEqual(
            [
                link["character"]
                for link in snapshot["cards"][1]["character_links"]
            ],
            ["圖", "書", "館"],
        )

    def test_practice_export_preserves_source_order_and_excludes_boundary(self):
        from generate_feeds import parse_csv_deck, write_practice_export

        config = {
            "practice_export": {
                "path": "hsk_practice.csv",
                "minimum_interval_days": 30,
            }
        }
        history = fsrs_history(
            {
                "mature-2": fsrs_card_state(),
                "boundary": fsrs_card_state(),
                "mature-1": fsrs_card_state(),
            }
        )
        history["cards"]["mature-1"]["interval_days"] = 31
        history["cards"]["boundary"]["interval_days"] = 30
        history["cards"]["mature-2"]["interval_days"] = 90

        with tempfile.TemporaryDirectory() as directory:
            old_cwd = os.getcwd()
            os.chdir(directory)
            try:
                Path("HSK.csv").write_text(
                    "id,front,back\n"
                    'mature-1,你好,"hello, hi"\n'
                    "boundary,再見,goodbye\n"
                    "mature-2,謝謝,thank you\n"
                    "unseen,請,please\n",
                    encoding="utf-8",
                )
                cards = parse_csv_deck(Path("HSK.csv"), "https://example.test")

                exported = write_practice_export(
                    "hsk", config, cards, history
                )

                with Path("hsk_practice.csv").open(
                    mode="r", encoding="utf-8", newline=""
                ) as export_file:
                    rows = list(csv.DictReader(export_file))
            finally:
                os.chdir(old_cwd)

        self.assertEqual(exported, 2)
        self.assertEqual(
            rows,
            [
                {"id": "mature-1", "front": "你好", "back": "hello, hi"},
                {"id": "mature-2", "front": "謝謝", "back": "thank you"},
            ],
        )

    def test_practice_export_writes_header_when_no_cards_qualify(self):
        from generate_feeds import write_practice_export

        config = {
            "practice_export": {
                "path": "hsk_practice.csv",
                "minimum_interval_days": 30,
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            old_cwd = os.getcwd()
            os.chdir(directory)
            try:
                exported = write_practice_export("hsk", config, [], fsrs_history())
                contents = Path("hsk_practice.csv").read_text(encoding="utf-8")
            finally:
                os.chdir(old_cwd)

        self.assertEqual(exported, 0)
        self.assertEqual(contents, "id,front,back\n")

    def test_generated_json_and_rss_follow_authoritative_batch(self):
        from generate_feeds import process_anki_deck

        config = {
            "source_type": "csv",
            "path": "HSK.csv",
            "feed_title": "HSK",
            "new_cards_per_day": 1,
            "practice_export": {
                "path": "hsk_practice.csv",
                "minimum_interval_days": 30,
            },
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
                self.assertEqual(
                    Path("hsk_practice.csv").read_text(encoding="utf-8"),
                    "id,front,back\n",
                )

                history["cards"]["hsk-1"]["interval_days"] = 31
                process_anki_deck(
                    "hsk",
                    config,
                    history,
                    self.make_feed(),
                    "https://example.test",
                    {},
                    NOW,
                )
                with Path("hsk_practice.csv").open(
                    mode="r", encoding="utf-8", newline=""
                ) as export_file:
                    exported_rows = list(csv.DictReader(export_file))
                self.assertEqual(
                    exported_rows,
                    [{"id": "hsk-1", "front": "one", "back": "first"}],
                )
            finally:
                os.chdir(old_cwd)

    def test_program_gate_allows_due_cards_and_only_promoted_new_cards(self):
        from generate_feeds import process_anki_deck

        config = {
            "source_type": "csv",
            "path": "HSK.csv",
            "feed_title": "HSK",
            "new_cards_per_day": 10,
        }
        history = fsrs_history({"due": fsrs_card_state()})
        with tempfile.TemporaryDirectory() as directory:
            old_cwd = os.getcwd()
            os.chdir(directory)
            try:
                Path("HSK.csv").write_text(
                    "id,front,back\n"
                    "due,old,reviewed\n"
                    "promoted,new,eligible\n"
                    "blocked,new,not eligible\n",
                    encoding="utf-8",
                )
                process_anki_deck(
                    "hsk",
                    config,
                    history,
                    self.make_feed(),
                    "https://example.test",
                    {},
                    NOW,
                    eligible_new_card_ids={"promoted"},
                )
            finally:
                os.chdir(old_cwd)

        self.assertEqual(
            history["daily_batch"]["card_ids"],
            ["due", "promoted"],
        )


if __name__ == "__main__":
    unittest.main()
