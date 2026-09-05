import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sentence_program import (
    DeterministicSentenceGenerator,
    OpenRouterSentenceGenerator,
    apply_sentence_review_results,
    build_combined_snapshot,
    eligible_source_word_ids,
    load_sentence_content,
    prepare_sentence_program,
    promoted_word_ids,
    resolve_sentence_content_path,
    save_sentence_content,
    sentence_cards,
)


NOW = datetime(2026, 9, 5, 14, 0, tzinfo=timezone.utc)


def source_card(card_id, surface):
    return {
        "id": card_id,
        "front": {"text": surface, "audio": None, "image": None},
        "back": {"text": f"meaning of {surface}", "notes": None},
    }


def reviewed_state(interval_days=3):
    return {
        "state": "review",
        "step": None,
        "stability": 2.0,
        "difficulty": 5.0,
        "interval_days": interval_days,
        "reviews": 2,
        "lapses": 0,
        "last_reviewed_at": "2026-09-01T16:00:00Z",
        "next_due_at": "2026-09-08T16:00:00Z",
    }


def program_config(daily_target=2):
    return {
        "sentence_stream": "mandarin_sentences",
        "language_code": "zh-CN",
        "orthography": "Traditional Chinese",
        "prompt_style": "formal written Mandarin",
        "promotion_threshold_sentence_passes": 3,
        "daily_sentence_target": daily_target,
        "max_active_word_buffer": 30,
        "mastery_interval_days": 21,
        "familiar_min_reviews": 1,
        "known_pool_limit": 200,
        "sentence_lifecycle": "disposable_scaffold",
        "mode": "text_reading",
    }


def empty_content():
    return {
        "schema_version": 1,
        "program_id": "mandarin_reading",
        "sentences": [],
    }


def review_event(event_id, card_id, rating, reviewed_at=NOW):
    return {
        "event_id": event_id,
        "deck_id": "mandarin_sentences",
        "card_id": card_id,
        "rating": rating,
        "reviewed_at": reviewed_at.isoformat(),
    }


class SentenceProgramTests(unittest.TestCase):
    def setUp(self):
        self.cards = [
            source_card("known-1", "我"),
            source_card("known-2", "你"),
            source_card("target-1", "事情"),
            source_card("target-2", "重要"),
            source_card("target-3", "處理"),
        ]
        self.source_state = {
            "cards": {
                "known-1": reviewed_state(),
                "known-2": reviewed_state(interval_days=30),
            }
        }
        self.program_state = {}
        self.generation_state = {}
        self.sentence_stream_state = {"processed_events": {}}
        self.content = empty_content()
        self.factory = lambda _config: DeterministicSentenceGenerator()

    def process(self, payload=None, now=NOW):
        result = prepare_sentence_program(
            "mandarin_reading",
            program_config(),
            self.program_state,
            self.generation_state,
            self.content,
            self.cards,
            self.source_state,
            now,
            self.factory,
        )
        if payload:
            for event in payload.get("events", []):
                self.sentence_stream_state["processed_events"][event["event_id"]] = {
                    "status": "applied"
                }
            result["reviews"] = apply_sentence_review_results(
                "mandarin_reading",
                "mandarin_sentences",
                self.program_state,
                self.sentence_stream_state,
                self.content,
                payload,
                3,
                now,
            )
        return result

    def test_daily_generation_is_batched_and_idempotent(self):
        first = self.process()
        second = self.process(now=NOW + timedelta(hours=1))

        self.assertEqual(first["generated"], 2)
        self.assertEqual(second["generated"], 0)
        self.assertEqual(len(self.content["sentences"]), 2)
        self.assertEqual(
            set(self.program_state["vocabulary"]),
            {"target-1", "target-2"},
        )
        job = self.generation_state["jobs"]["2026-09-05"]
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["generated_count"], 2)
        self.assertEqual(len(job["sentence_ids"]), 2)

    def test_zero_result_job_retries_when_new_vocabulary_arrives(self):
        initial_cards = self.cards
        self.cards = self.cards[:2]
        first = self.process()
        first_job = dict(self.generation_state["jobs"]["2026-09-05"])

        self.cards = initial_cards
        second = self.process(now=NOW + timedelta(hours=1))

        self.assertEqual(first["generated"], 0)
        self.assertEqual(first_job["generated_count"], 0)
        self.assertEqual(second["generated"], 2)
        self.assertEqual(
            self.generation_state["jobs"]["2026-09-05"]["generated_count"],
            2,
        )

    def test_repeated_zero_result_does_not_rewrite_job(self):
        self.cards = self.cards[:2]
        self.process()
        first_job = dict(self.generation_state["jobs"]["2026-09-05"])

        self.process(now=NOW + timedelta(hours=1))

        self.assertEqual(
            self.generation_state["jobs"]["2026-09-05"],
            first_job,
        )

    def test_good_reviews_promote_word_and_archive_disposable_sentence(self):
        self.process()
        sentence = self.content["sentences"][0]
        target_id = sentence["target_word_ids"][0]
        for index, rating in enumerate(("hard", "good", "easy", "good")):
            result = self.process(
                {
                    "event_type": "anki_review",
                    "deck_id": "mandarin_sentences",
                    "events": [
                        review_event(
                            f"event-{index}",
                            sentence["id"],
                            rating,
                            NOW + timedelta(minutes=index),
                        )
                    ],
                },
                NOW + timedelta(minutes=index),
            )
            self.assertEqual(result["reviews"]["applied"], 1)

        word = self.program_state["vocabulary"][target_id]
        self.assertEqual(word["sentence_pass_count"], 3)
        self.assertEqual(word["status"], "active_anki")
        self.assertEqual(sentence["status"], "archived")
        self.assertIn(target_id, promoted_word_ids(self.program_state))
        self.assertNotIn(sentence["id"], [card["id"] for card in sentence_cards(self.content)])

    def test_duplicate_review_does_not_increment_pass_count(self):
        self.process()
        sentence = self.content["sentences"][0]
        event = review_event("same-event", sentence["id"], "good")
        payload = {
            "event_type": "anki_review",
            "deck_id": "mandarin_sentences",
            "events": [event],
        }
        self.process(payload)
        result = self.process(payload)

        target_id = sentence["target_word_ids"][0]
        self.assertEqual(
            self.program_state["vocabulary"][target_id]["sentence_pass_count"],
            1,
        )
        self.assertEqual(result["reviews"]["duplicate"], 1)

    def test_scheduler_rejected_review_cannot_promote_a_word(self):
        self.process()
        sentence = self.content["sentences"][0]
        event = review_event("stale-event", sentence["id"], "good")
        self.sentence_stream_state["processed_events"]["stale-event"] = {
            "status": "stale"
        }

        result = apply_sentence_review_results(
            "mandarin_reading",
            "mandarin_sentences",
            self.program_state,
            self.sentence_stream_state,
            self.content,
            {
                "event_type": "anki_review",
                "deck_id": "mandarin_sentences",
                "events": [event],
            },
            3,
            NOW,
        )

        target_id = sentence["target_word_ids"][0]
        self.assertEqual(result["stale"], 1)
        self.assertEqual(
            self.program_state["vocabulary"][target_id]["sentence_pass_count"],
            0,
        )

    def test_active_word_becomes_mastered_at_configured_interval(self):
        self.process()
        target_id = self.content["sentences"][0]["target_word_ids"][0]
        self.program_state["vocabulary"][target_id]["status"] = "active_anki"
        self.source_state["cards"][target_id] = reviewed_state(interval_days=21)

        self.process()

        self.assertEqual(
            self.program_state["vocabulary"][target_id]["status"],
            "mastered",
        )

    def test_existing_unreviewed_batch_cards_are_grandfathered(self):
        self.source_state["daily_batch"] = {
            "card_ids": ["target-1"],
        }

        self.process()

        self.assertIn("target-1", eligible_source_word_ids(self.program_state))
        self.assertNotIn("target-1", self.program_state["vocabulary"])
        self.assertEqual(
            set(self.program_state["vocabulary"]),
            {"target-2", "target-3"},
        )

    def test_content_recovers_missing_program_and_generation_state(self):
        self.process()
        original_sentence_ids = [
            sentence["id"] for sentence in self.content["sentences"]
        ]
        self.program_state.clear()
        self.generation_state.clear()

        result = self.process(now=NOW + timedelta(hours=1))

        self.assertEqual(result["generated"], 0)
        self.assertEqual(
            [sentence["id"] for sentence in self.content["sentences"]],
            original_sentence_ids,
        )
        self.assertEqual(
            self.generation_state["jobs"]["2026-09-05"]["sentence_ids"],
            original_sentence_ids,
        )
        self.assertTrue(
            self.generation_state["jobs"]["2026-09-05"][
                "recovered_from_content"
            ]
        )

    def test_content_round_trip_and_card_projection(self):
        self.process()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "generated/sentences.json"
            save_sentence_content(path, self.content)
            loaded = load_sentence_content(path, "mandarin_reading")

        cards = sentence_cards(loaded)
        self.assertEqual(len(cards), 2)
        self.assertIn("Mock transliteration", cards[0]["back"]["notes"])

    def test_content_path_must_be_json_inside_generated_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            resolved = resolve_sentence_content_path(
                root, "generated/mandarin/sentences.json"
            )
            self.assertEqual(
                resolved,
                root.resolve() / "generated/mandarin/sentences.json",
            )
            with self.assertRaises(ValueError):
                resolve_sentence_content_path(root, "../sentences.json")
            with self.assertRaises(ValueError):
                resolve_sentence_content_path(root, "state/sentences.json")

    def test_combined_batch_is_a_frozen_deterministic_union(self):
        snapshots = [
            {
                "deck_id": "hsk",
                "title": "HSK",
                "front_text_scale": 2,
                "batch_id": "2026-09-05",
                "state_revision": 4,
                "processed_event_ids": ["word-event"],
                "cards": [
                    {
                        **source_card("shared", "我"),
                        "available_at": "2026-09-04T16:00:00Z",
                    },
                    {
                        **source_card("word-2", "你"),
                        "available_at": "2026-09-04T16:00:00Z",
                    },
                ],
            },
            {
                "deck_id": "mandarin_sentences",
                "title": "Sentences",
                "front_text_scale": 1.5,
                "batch_id": "2026-09-05",
                "state_revision": 2,
                "processed_event_ids": ["sentence-event"],
                "cards": [
                    {
                        **source_card("shared", "我是學生。"),
                        "available_at": "2026-09-04T16:00:00Z",
                    }
                ],
            },
        ]
        first_state = {}
        first = build_combined_snapshot(
            "mandarin_reading",
            "Mandarin",
            first_state,
            snapshots,
            NOW,
        )
        second = build_combined_snapshot(
            "mandarin_reading",
            "Mandarin",
            first_state,
            list(reversed(snapshots)),
            NOW + timedelta(hours=1),
        )
        independent = build_combined_snapshot(
            "mandarin_reading",
            "Mandarin",
            {},
            snapshots,
            NOW,
        )

        first_refs = [
            (card["deck_id"], card["id"]) for card in first["cards"]
        ]
        self.assertCountEqual(
            first_refs,
            [
                ("hsk", "shared"),
                ("hsk", "word-2"),
                ("mandarin_sentences", "shared"),
            ],
        )
        self.assertEqual(first_refs, [
            (card["deck_id"], card["id"]) for card in second["cards"]
        ])
        self.assertEqual(first_refs, [
            (card["deck_id"], card["id"]) for card in independent["cards"]
        ])
        self.assertEqual(
            first["processed_event_ids"],
            ["word-event", "sentence-event"],
        )

    def test_combined_snapshot_filters_card_removed_by_source_scheduler(self):
        snapshots = [
            {
                "deck_id": "hsk",
                "title": "HSK",
                "batch_id": "2026-09-05",
                "state_revision": 1,
                "processed_event_ids": [],
                "cards": [
                    {
                        **source_card("word-1", "我"),
                        "available_at": "2026-09-04T16:00:00Z",
                    }
                ],
            },
            {
                "deck_id": "mandarin_sentences",
                "title": "Sentences",
                "batch_id": "2026-09-05",
                "state_revision": 1,
                "processed_event_ids": [],
                "cards": [
                    {
                        **source_card("sentence-1", "我是學生。"),
                        "available_at": "2026-09-04T16:00:00Z",
                    }
                ],
            },
        ]
        state = {}
        build_combined_snapshot(
            "mandarin_reading", "Mandarin", state, snapshots, NOW
        )
        snapshots[0]["cards"] = []

        updated = build_combined_snapshot(
            "mandarin_reading",
            "Mandarin",
            state,
            snapshots,
            NOW + timedelta(minutes=1),
        )

        self.assertEqual(
            [(card["deck_id"], card["id"]) for card in updated["cards"]],
            [("mandarin_sentences", "sentence-1")],
        )
        self.assertEqual(len(state["combined_batch"]["members"]), 2)


class OpenRouterGeneratorTests(unittest.TestCase):
    class FakeResponse:
        def __init__(self, data):
            self.data = json.dumps(data).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return self.data

    def test_openrouter_request_uses_bearer_key_and_parses_json(self):
        generated = {
            "sentences": [
                {
                    "target_word_id": "target-1",
                    "primary_text": "這件事情很重要。",
                    "transliteration": "zhè jiàn shìqing hěn zhòngyào",
                    "translation": "This matter is important.",
                    "cloze_text": "這件[…]很重要。",
                    "target_breakdown": "事情: matter",
                }
            ]
        }
        api_response = {
            "choices": [{"message": {"content": json.dumps(generated)}}]
        }
        request_data = {
            "language_code": "zh-CN",
            "prompt_style": "formal",
            "orthography": "Traditional Chinese",
            "known_words": ["我", "你"],
            "targets": [
                {
                    "id": "target-1",
                    "surface_form": "事情",
                    "translation": "matter",
                }
            ],
        }
        with patch(
            "urllib.request.urlopen",
            return_value=self.FakeResponse(api_response),
        ) as urlopen:
            result = OpenRouterSentenceGenerator(
                "secret-test-key", "qwen/test"
            ).generate(request_data)

        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-test-key")
        self.assertEqual(result, generated["sentences"])
        payload = json.loads(request.data)
        self.assertEqual(payload["model"], "qwen/test")
        self.assertEqual(payload["response_format"]["type"], "json_schema")

    def test_invalid_openrouter_shape_is_rejected(self):
        response = self.FakeResponse({"choices": []})
        with patch("urllib.request.urlopen", return_value=response):
            with self.assertRaisesRegex(ValueError, "message content"):
                OpenRouterSentenceGenerator("key", "model").generate(
                    {
                        "language_code": "zh-CN",
                        "prompt_style": "formal",
                        "orthography": "Traditional Chinese",
                        "known_words": ["我"],
                        "targets": [
                            {
                                "id": "target",
                                "surface_form": "事情",
                                "translation": "matter",
                            }
                        ],
                    }
                )


class SentenceProgramIntegrationTests(unittest.TestCase):
    def test_feed_engine_generates_independent_sentence_stream_with_mocked_api(self):
        from generate_feeds import main

        config = {
            "streams": {
                "hsk": {
                    "type": "anki_deck",
                    "source_type": "csv",
                    "path": "HSK.csv",
                    "feed_title": "HSK",
                    "new_cards_per_day": 10,
                },
                "mandarin_sentences": {
                    "type": "anki_deck",
                    "source_type": "generated_sentences",
                    "program_id": "mandarin_reading",
                    "path": "generated/mandarin_reading/sentences.json",
                    "feed_title": "Mandarin Sentences",
                    "new_cards_per_day": 10,
                },
            },
            "programs": {
                "mandarin_reading": {
                    **program_config(daily_target=1),
                    "enabled": True,
                    "source_stream": "hsk",
                    "content_path": "generated/mandarin_reading/sentences.json",
                    "control_source_new_cards": True,
                    "generation": {
                        "provider": "openrouter",
                        "model": "qwen/test",
                    },
                }
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            old_cwd = os.getcwd()
            os.chdir(directory)
            try:
                Path("config.json").write_text(
                    json.dumps(config), encoding="utf-8"
                )
                Path("history.json").write_text("{}", encoding="utf-8")
                Path("HSK.csv").write_text(
                    "id,front,back\nknown,我,I\ntarget,事情,matter\n",
                    encoding="utf-8",
                )
                state_path = Path("state/streams/hsk.json")
                state_path.parent.mkdir(parents=True)
                state_path.write_text(
                    json.dumps(
                        {
                            "schema_version": 3,
                            "scheduler": {
                                "name": "fsrs-6.3.2",
                                "version": 1,
                                "desired_retention": 0.9,
                            },
                            "revision": 1,
                            "cards": {"known": reviewed_state()},
                            "processed_events": {},
                        }
                    ),
                    encoding="utf-8",
                )
                with patch(
                    "sentence_program._openrouter_generator",
                    return_value=DeterministicSentenceGenerator(),
                ):
                    main()

                generated = json.loads(
                    Path(
                        "generated/mandarin_reading/sentences.json"
                    ).read_text(encoding="utf-8")
                )
                snapshot = json.loads(
                    Path("cards/mandarin_sentences_deck.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(len(generated["sentences"]), 1)
                self.assertEqual(len(snapshot["cards"]), 1)
                combined = json.loads(
                    Path("cards/mandarin_reading_program.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(len(combined["cards"]), 1)
                self.assertEqual(
                    combined["cards"][0]["deck_id"],
                    "mandarin_sentences",
                )
                self.assertTrue(Path("mandarin_reading.xml").exists())
                self.assertTrue(
                    Path("state/programs/mandarin_reading.json").exists()
                )
                self.assertTrue(
                    Path("state/generation/mandarin_reading.json").exists()
                )
            finally:
                os.chdir(old_cwd)


if __name__ == "__main__":
    unittest.main()
