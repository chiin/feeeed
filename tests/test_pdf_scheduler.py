import unittest
from datetime import datetime, timedelta, timezone
import json
import os
import tempfile
from pathlib import Path

from pdf_scheduler import (
    HKT,
    apply_completion_events,
    apply_continuation_events,
    build_snapshot,
    can_continue,
    hkt_day,
    initial_release_at,
    migrate_history,
    next_release_at,
    parse_datetime,
    release_if_due,
)


NOW = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
PDF_IDS = [f"article_{number:03d}.pdf" for number in range(1, 8)]


def pdf_event(event_id, action, pdf_id=None, occurred_at=NOW):
    event = {
        "event_id": event_id,
        "stream_id": "economics",
        "action": action,
        "occurred_at": occurred_at.isoformat(),
    }
    if pdf_id:
        event["pdf_id"] = pdf_id
    return event


def state_for(strategy="sequential"):
    state = {}
    migrate_history("economics", strategy, state, NOW)
    release_if_due(
        "economics", state, PDF_IDS, 3, NOW, force_today=True
    )
    return state


class ReleaseTimingTests(unittest.TestCase):
    def test_initial_release_is_inside_current_hkt_day(self):
        release = initial_release_at("economics", NOW)
        self.assertEqual(hkt_day(release), hkt_day(NOW))
        local_release = release.astimezone(HKT)
        self.assertGreaterEqual(local_release.hour, 1)
        self.assertLessEqual(local_release.hour, 21)

    def test_next_release_is_next_hkt_day_within_three_hours(self):
        released = datetime(2026, 8, 30, 8, 30, tzinfo=timezone.utc)
        following = next_release_at("economics", released)
        self.assertEqual(
            following.astimezone(HKT).date(),
            released.astimezone(HKT).date() + timedelta(days=1),
        )
        previous_minutes = (
            released.astimezone(HKT).hour * 60
            + released.astimezone(HKT).minute
        )
        next_minutes = (
            following.astimezone(HKT).hour * 60
            + following.astimezone(HKT).minute
        )
        self.assertLessEqual(abs(next_minutes - previous_minutes), 180)

    def test_release_happens_only_once_per_hkt_day(self):
        state = {}
        migrate_history("economics", "sequential", state, NOW)
        self.assertTrue(
            release_if_due(
                "economics", state, PDF_IDS, 3, NOW, force_today=True
            )
        )
        first_batch = dict(state["daily_batch"])
        self.assertFalse(
            release_if_due(
                "economics", state, PDF_IDS, 3, NOW, force_today=True
            )
        )
        self.assertEqual(state["daily_batch"], first_batch)


class StrategyTests(unittest.TestCase):
    def test_sequential_carries_unfinished_then_fills_vacancies(self):
        state = state_for()
        self.assertEqual(state["daily_batch"]["active_ids"], PDF_IDS[:3])
        events = [
            pdf_event("complete-1", "complete", PDF_IDS[0]),
            pdf_event("complete-2", "complete", PDF_IDS[1]),
        ]
        apply_completion_events("economics", state, events, NOW)

        tomorrow = NOW + timedelta(days=1)
        release_if_due(
            "economics", state, PDF_IDS, 3, tomorrow, force_today=True
        )
        self.assertEqual(
            state["daily_batch"]["active_ids"],
            [PDF_IDS[2], PDF_IDS[3], PDF_IDS[4]],
        )
        self.assertEqual(
            state["daily_batch"]["release_summary_ids"],
            [PDF_IDS[2], PDF_IDS[3], PDF_IDS[4]],
        )

    def test_random_without_replacement_never_reselects_completed_pdf(self):
        state = state_for("random_without_replacement")
        completed = state["daily_batch"]["active_ids"][0]
        apply_completion_events(
            "economics",
            state,
            [pdf_event("complete-1", "complete", completed)],
            NOW,
        )
        state["daily_batch"]["active_ids"].clear()
        apply_continuation_events(
            "economics",
            state,
            [pdf_event("continue-1", "continue")],
            PDF_IDS,
            3,
            NOW,
        )
        self.assertNotIn(completed, state["daily_batch"]["active_ids"])
        self.assertIn(completed, state["completed_ids"])

    def test_random_with_replacement_avoids_same_day_repeats(self):
        state = state_for("random_with_replacement")
        first_segment = list(state["daily_batch"]["active_ids"])
        apply_completion_events(
            "economics",
            state,
            [
                pdf_event(f"complete-{index}", "complete", pdf_id)
                for index, pdf_id in enumerate(first_segment)
            ],
            NOW,
        )
        apply_continuation_events(
            "economics",
            state,
            [pdf_event("continue-1", "continue")],
            PDF_IDS,
            3,
            NOW,
        )
        second_segment = state["daily_batch"]["active_ids"]
        self.assertTrue(set(first_segment).isdisjoint(second_segment))
        self.assertEqual(state["completed_ids"], [])

        tomorrow = NOW + timedelta(days=1)
        state["daily_batch"]["active_ids"].clear()
        release_if_due(
            "economics", state, first_segment, 3, tomorrow, force_today=True
        )
        self.assertTrue(state["daily_batch"]["active_ids"])
        self.assertTrue(
            set(state["daily_batch"]["active_ids"]).issubset(first_segment)
        )


class EventAndSnapshotTests(unittest.TestCase):
    def test_completion_is_idempotent_and_stale_devices_do_not_advance_twice(self):
        state = state_for()
        first_id = state["daily_batch"]["active_ids"][0]
        event = pdf_event("event-1", "complete", first_id)
        self.assertEqual(
            apply_completion_events("economics", state, [event], NOW)["applied"],
            1,
        )
        self.assertEqual(
            apply_completion_events("economics", state, [event], NOW)["duplicate"],
            1,
        )
        stale = pdf_event("event-2", "complete", first_id)
        self.assertEqual(
            apply_completion_events("economics", state, [stale], NOW)["stale"],
            1,
        )
        self.assertEqual(state["completion_counts"][first_id], 1)

    def test_continuation_is_unlimited_until_eligible_pool_is_exhausted(self):
        state = state_for()
        original_summary = list(state["daily_batch"]["release_summary_ids"])
        state["daily_batch"]["active_ids"].clear()
        first = apply_continuation_events(
            "economics",
            state,
            [pdf_event("continue-1", "continue")],
            PDF_IDS,
            3,
            NOW,
        )
        self.assertEqual(first["applied"], 1)
        self.assertEqual(
            state["daily_batch"]["release_summary_ids"], original_summary
        )
        state["daily_batch"]["active_ids"].clear()
        second = apply_continuation_events(
            "economics",
            state,
            [pdf_event("continue-2", "continue")],
            PDF_IDS,
            3,
            NOW,
        )
        self.assertEqual(second["applied"], 1)
        state["daily_batch"]["active_ids"].clear()
        self.assertFalse(can_continue(state, PDF_IDS))

    def test_snapshot_exposes_authoritative_queue_and_fixed_release_summary(self):
        state = state_for()
        completed = state["daily_batch"]["active_ids"][0]
        apply_completion_events(
            "economics",
            state,
            [pdf_event("complete-1", "complete", completed)],
            NOW,
        )
        snapshot = build_snapshot(
            "economics",
            "Economics",
            "Economics Cards",
            "https://example.test",
            state,
            PDF_IDS,
            3,
            NOW,
        )
        self.assertEqual(len(snapshot["release_summary"]), 3)
        self.assertEqual(len(snapshot["active"]), 2)
        self.assertNotIn(completed, [item["id"] for item in snapshot["active"]])
        self.assertIn("complete-1", snapshot["processed_event_ids"])
        self.assertIn("Economics%20Cards", snapshot["active"][0]["pdf_url"])

    def test_legacy_batch_and_permanent_history_are_migrated(self):
        history = {
            "completed_files": [PDF_IDS[0]],
            "next_update_at": "2026-08-31T12:00:00+08:00",
        }
        legacy = {
            "compiled_at": "2026-08-30T08:00:00Z",
            "batch": [{"id": PDF_IDS[1]}, {"id": PDF_IDS[2]}],
        }
        migrate_history(
            "economics", "sequential", history, NOW, legacy
        )
        self.assertEqual(history["completed_ids"], [PDF_IDS[0]])
        self.assertEqual(
            history["daily_batch"]["active_ids"], [PDF_IDS[1], PDF_IDS[2]]
        )
        self.assertEqual(
            parse_datetime(history["next_release_at"]),
            datetime(2026, 8, 31, 4, 0, tzinfo=timezone.utc),
        )

    def test_legacy_shown_files_are_not_treated_as_confirmed_completions(self):
        history = {"shown_files": [PDF_IDS[0], PDF_IDS[1]]}
        migrate_history(
            "economics", "random_without_replacement", history, NOW
        )
        self.assertEqual(history["completed_ids"], [])

    def test_old_event_ids_remain_idempotent_for_future_random_occurrences(self):
        state = state_for("random_with_replacement")
        pdf_id = state["daily_batch"]["active_ids"][0]
        event = pdf_event("old-event", "complete", pdf_id)
        apply_completion_events("economics", state, [event], NOW)
        for index in range(1100):
            state["processed_events"][f"later-{index}"] = {
                "processed_at": NOW.isoformat(),
                "status": "applied",
            }
        state["daily_batch"]["active_ids"].append(pdf_id)
        result = apply_completion_events("economics", state, [event], NOW)
        self.assertEqual(result["duplicate"], 1)
        self.assertIn(pdf_id, state["daily_batch"]["active_ids"])


class FakeEntry:
    def __init__(self):
        self.values = {}

    def __getattr__(self, name):
        def setter(value=None, **kwargs):
            self.values[name] = kwargs or value
        return setter


class FakeFeed:
    def __init__(self):
        self.entries = []

    def add_entry(self):
        entry = FakeEntry()
        self.entries.append(entry)
        return entry


class ProcessorIntegrationTests(unittest.TestCase):
    def test_processor_keeps_rss_identity_and_summary_while_queue_shrinks(self):
        from generate_feeds import process_pdf_folder

        config = {
            "folder": "Economics_Cards",
            "strategy": "sequential",
            "batch_size": 3,
            "feed_title": "Economics",
        }
        history = {}
        with tempfile.TemporaryDirectory() as directory:
            previous_cwd = os.getcwd()
            os.chdir(directory)
            try:
                folder = Path(config["folder"])
                folder.mkdir()
                for pdf_id in PDF_IDS[:4]:
                    (folder / pdf_id).write_bytes(b"%PDF-test")

                first_feed = FakeFeed()
                process_pdf_folder(
                    "economics",
                    config,
                    history,
                    first_feed,
                    "https://example.test",
                    now=NOW,
                )
                snapshot = json.loads(
                    Path("cards/economics_pdf_batch.json").read_text(
                        encoding="utf-8"
                    )
                )
                first_id = snapshot["active"][0]["id"]
                rss_id = first_feed.entries[0].values["id"]
                rss_title = first_feed.entries[0].values["title"]

                second_feed = FakeFeed()
                process_pdf_folder(
                    "economics",
                    config,
                    history,
                    second_feed,
                    "https://example.test",
                    {
                        "event_type": "pdf_batch_event",
                        "stream_id": "economics",
                        "events": [
                            pdf_event("complete-1", "complete", first_id)
                        ],
                    },
                    NOW,
                )
                updated = json.loads(
                    Path("cards/economics_pdf_batch.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(len(updated["active"]), 2)
                self.assertEqual(
                    second_feed.entries[0].values["id"], rss_id
                )
                self.assertEqual(
                    second_feed.entries[0].values["title"], rss_title
                )
            finally:
                os.chdir(previous_cwd)


if __name__ == "__main__":
    unittest.main()
