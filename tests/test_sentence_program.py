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
    apply_vocabulary_candidate_event,
    approved_vocabulary_cards,
    build_candidate_snapshot,
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


def source_card(card_id, surface, entry_type="term"):
    return {
        "id": card_id,
        "entry_type": entry_type,
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
        "passing_reviews": 1,
        "last_rating": "good",
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
            self.sentence_stream_state,
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

    def test_generated_cards_backfill_an_empty_same_day_batch_once(self):
        self.sentence_stream_state.update(
            {
                "revision": 2,
                "daily_batch": {
                    "id": "2026-09-05",
                    "date": "2026-09-05",
                    "created_at": "2026-09-04T16:00:00Z",
                    "card_ids": [],
                    "active": [],
                },
            }
        )
        self.program_state["combined_batch"] = {
            "id": "2026-09-05",
            "date": "2026-09-05",
            "members": [{"deck_id": "hsk", "card_id": "known-1"}],
        }

        first = self.process()
        second = self.process(now=NOW + timedelta(minutes=1))

        sentence_ids = [
            sentence["id"] for sentence in self.content["sentences"]
        ]
        self.assertEqual(first["backfilled"], 2)
        self.assertEqual(second["backfilled"], 0)
        self.assertEqual(
            self.sentence_stream_state["daily_batch"]["card_ids"],
            sentence_ids,
        )
        self.assertEqual(
            [
                item["card_id"]
                for item in self.sentence_stream_state["daily_batch"]["active"]
            ],
            sentence_ids,
        )
        self.assertEqual(self.sentence_stream_state["revision"], 3)
        self.assertNotIn("combined_batch", self.program_state)
        self.assertIn(
            "released_same_day_at",
            self.generation_state["jobs"]["2026-09-05"],
        )
        sentence_snapshot = {
            "deck_id": "mandarin_sentences",
            "title": "Sentences",
            "front_text_scale": 1.5,
            "batch_id": "2026-09-05",
            "state_revision": 3,
            "processed_event_ids": [],
            "cards": [
                {
                    **card,
                    "available_at": self.sentence_stream_state[
                        "daily_batch"
                    ]["active"][index]["available_at"],
                }
                for index, card in enumerate(sentence_cards(self.content))
            ],
        }
        combined = build_combined_snapshot(
            "mandarin_reading",
            "Mandarin",
            self.program_state,
            [
                {
                    "deck_id": "hsk",
                    "title": "HSK",
                    "front_text_scale": 2,
                    "batch_id": "2026-09-05",
                    "state_revision": 1,
                    "processed_event_ids": [],
                    "cards": [
                        {
                            **source_card("known-1", "我"),
                            "available_at": "2026-09-04T16:00:00Z",
                        }
                    ],
                },
                sentence_snapshot,
            ],
            NOW,
        )
        self.assertEqual(len(combined["cards"]), 3)
        self.assertEqual(
            sum(
                card["deck_id"] == "mandarin_sentences"
                for card in combined["cards"]
            ),
            2,
        )

    def test_generated_cards_do_not_change_a_nonempty_batch(self):
        self.sentence_stream_state["daily_batch"] = {
            "id": "2026-09-05",
            "date": "2026-09-05",
            "created_at": "2026-09-04T16:00:00Z",
            "card_ids": ["existing-sentence"],
            "active": [
                {
                    "card_id": "existing-sentence",
                    "available_at": "2026-09-04T16:00:00Z",
                }
            ],
        }

        result = self.process()

        self.assertEqual(result["backfilled"], 0)
        self.assertEqual(
            self.sentence_stream_state["daily_batch"]["card_ids"],
            ["existing-sentence"],
        )

    def test_generated_cards_do_not_backfill_a_prior_day_batch(self):
        self.sentence_stream_state["daily_batch"] = {
            "id": "2026-09-04",
            "date": "2026-09-04",
            "created_at": "2026-09-03T16:00:00Z",
            "card_ids": [],
            "active": [],
        }

        result = self.process()

        self.assertEqual(result["backfilled"], 0)
        self.assertEqual(
            self.sentence_stream_state["daily_batch"]["card_ids"],
            [],
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


class VocabularyFirstSentenceProgramTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            **program_config(daily_target=2),
            "vocabulary_introduction": {"strategy": "vocabulary_first"},
            "sentence_generation": {
                "trigger": "after_first_passing_vocabulary_review",
                "minimum_passing_reviews": 1,
                "allow_inflected_targets": True,
                "familiar_pool_policy": "natural_priority",
            },
            "generation": {"max_attempts": 2},
        }
        self.program_state = {}
        self.generation_state = {}
        self.content = empty_content()
        self.source_state = {"cards": {}}
        self.sentence_state = {"processed_events": {}}

    def process(self, cards, generator_factory=None, now=NOW):
        return prepare_sentence_program(
            "mandarin_reading",
            self.config,
            self.program_state,
            self.generation_state,
            self.content,
            cards,
            self.source_state,
            self.sentence_state,
            now,
            generator_factory,
        )

    def test_sentence_waits_for_first_passing_vocabulary_review(self):
        cards = [source_card("target-1", "mesto")]

        first = self.process(cards, lambda _config: DeterministicSentenceGenerator())
        self.source_state["cards"]["target-1"] = reviewed_state()
        second = self.process(
            cards,
            lambda _config: DeterministicSentenceGenerator(),
            NOW + timedelta(hours=1),
        )

        self.assertEqual(first["generated"], 0)
        self.assertEqual(second["generated"], 1)
        self.assertEqual(
            self.program_state["vocabulary"]["target-1"]["status"],
            "sentence_reinforcement",
        )

    def test_parallel_relearning_targets_catalog_without_vocabulary_review(self):
        class CapturingGenerator:
            def __init__(self):
                self.requests = []

            def generate(self, request):
                self.requests.append(request)
                return DeterministicSentenceGenerator().generate(request)

        self.config["prior_knowledge"] = {
            "source_catalog": "assumed_familiar",
        }
        self.config["sentence_generation"].update(
            {
                "trigger": "catalog_order",
                "context_pool": "source_catalog",
            }
        )
        cards = [
            source_card("target-1", "mesto"),
            source_card("target-2", "kniha"),
            source_card("target-3", "stôl"),
        ]
        generator = CapturingGenerator()

        result = self.process(cards, lambda _config: generator)

        self.assertEqual(result["generated"], 2)
        self.assertEqual(
            [target["id"] for target in generator.requests[0]["targets"]],
            ["target-1", "target-2"],
        )
        self.assertEqual(
            set(generator.requests[0]["known_words"]),
            {"mesto", "kniha", "stôl"},
        )

    def test_again_or_hard_only_card_is_not_a_sentence_target(self):
        cards = [source_card("target-1", "mesto")]
        self.source_state["cards"]["target-1"] = {
            **reviewed_state(),
            "passing_reviews": 0,
            "last_rating": "hard",
        }

        result = self.process(
            cards,
            lambda _config: DeterministicSentenceGenerator(),
        )

        self.assertEqual(result["generated"], 0)
        self.assertEqual(self.program_state["vocabulary"], {})

    def test_source_sentence_becomes_direct_sentence_practice(self):
        cards = [
            source_card(
                "sentence-1",
                "Sedím na stoličke.",
                entry_type="sentence",
            )
        ]
        self.source_state["cards"]["sentence-1"] = reviewed_state()

        result = self.process(
            cards,
            lambda _config: self.fail("generator should not be created"),
        )

        self.assertEqual(result["generated"], 1)
        sentence = self.content["sentences"][0]
        self.assertEqual(sentence["source"], "vocabulary_sentence")
        self.assertEqual(sentence["payload"]["primary_text"], "Sedím na stoličke.")

    def test_inflected_target_occurrence_is_accepted(self):
        class InflectedGenerator:
            def generate(self, _request):
                return [
                    {
                        "target_word_id": "target-1",
                        "target_occurrence": "meste",
                        "primary_text": "Bývam v malom meste.",
                        "transliteration": "ˈmes.ce",
                        "translation": "I live in a small town.",
                        "cloze_text": "Bývam v malom […].",
                        "target_breakdown": "mesto → meste, locative singular",
                    }
                ]

        cards = [source_card("target-1", "mesto")]
        self.source_state["cards"]["target-1"] = reviewed_state()

        result = self.process(cards, lambda _config: InflectedGenerator())

        self.assertEqual(result["generated"], 1)
        self.assertEqual(
            self.content["sentences"][0]["payload"]["primary_text"],
            "Bývam v malom meste.",
        )

    def test_reinforcement_completion_does_not_promote_vocabulary(self):
        cards = [source_card("target-1", "mesto")]
        self.source_state["cards"]["target-1"] = reviewed_state()
        self.process(cards, lambda _config: DeterministicSentenceGenerator())
        sentence = self.content["sentences"][0]

        for index in range(3):
            event = review_event(
                f"reinforcement-{index}",
                sentence["id"],
                "good",
                NOW + timedelta(minutes=index),
            )
            self.sentence_state["processed_events"][event["event_id"]] = {
                "status": "applied"
            }
            apply_sentence_review_results(
                "mandarin_reading",
                "mandarin_sentences",
                self.program_state,
                self.sentence_state,
                self.content,
                {
                    "event_type": "anki_review",
                    "deck_id": "mandarin_sentences",
                    "events": [event],
                },
                3,
                NOW + timedelta(minutes=index),
            )

        self.assertEqual(
            self.program_state["vocabulary"]["target-1"]["status"],
            "sentence_complete",
        )
        self.assertNotIn("target-1", promoted_word_ids(self.program_state))
        self.assertEqual(sentence["status"], "archived")

    def test_list_like_output_is_rejected_and_retried(self):
        class RetryGenerator:
            def __init__(self):
                self.calls = 0

            def generate(self, _request):
                self.calls += 1
                if self.calls == 1:
                    primary = "Dobrý deň, tri, nie dva, nie sedem, nie osem."
                    translation = "Good day, three, not two, not seven, not eight."
                else:
                    primary = "Na stole sú tri knihy."
                    translation = "There are three books on the table."
                return [
                    {
                        "target_word_id": "target-1",
                        "target_occurrence": "tri",
                        "primary_text": primary,
                        "transliteration": "tri",
                        "translation": translation,
                        "cloze_text": "Na stole sú […] knihy.",
                        "target_breakdown": "tri: cardinal number used with books",
                    }
                ]

        cards = [source_card("target-1", "tri")]
        self.source_state["cards"]["target-1"] = reviewed_state()
        generator = RetryGenerator()

        result = self.process(cards, lambda _config: generator)

        self.assertEqual(result["generated"], 1)
        self.assertEqual(generator.calls, 2)
        self.assertEqual(
            self.content["sentences"][0]["payload"]["primary_text"],
            "Na stole sú tri knihy.",
        )

    def test_quality_version_refreshes_active_sentence_in_place(self):
        cards = [source_card("target-1", "tri")]
        self.source_state["cards"]["target-1"] = reviewed_state()
        sentence_id = "mandarin_reading-2026-09-05-target-1"
        self.content["sentences"] = [
            {
                "id": sentence_id,
                "card_type": "text_reading",
                "target_word_ids": ["target-1"],
                "lifecycle": "disposable_scaffold",
                "status": "active",
                "created_at": "2026-09-05T14:00:00Z",
                "introduction_strategy": "vocabulary_first",
                "source": "generated",
                "payload": {
                    "primary_text": "Tri, dva, jeden, nie štyri.",
                    "transliteration": "tri",
                    "translation": "Three, two, one, not four.",
                    "cloze_text": "[…], dva, jeden, nie štyri.",
                    "target_breakdown": "tri: three",
                },
            }
        ]
        self.program_state["vocabulary"] = {
            "target-1": {
                "status": "sentence_reinforcement",
                "sentence_pass_count": 1,
                "introduced_at": "2026-09-05T14:00:00Z",
            }
        }
        self.generation_state["jobs"] = {
            "2026-09-05": {
                "status": "completed",
                "generated_count": 1,
                "sentence_ids": [sentence_id],
            }
        }
        self.config["generation"] = {
            "quality_version": 2,
            "max_attempts": 2,
        }

        class NaturalGenerator:
            def generate(self, _request):
                return [
                    {
                        "target_word_id": "target-1",
                        "target_occurrence": "tri",
                        "primary_text": "Na stole sú tri knihy.",
                        "transliteration": "tri",
                        "translation": "There are three books on the table.",
                        "cloze_text": "Na stole sú […] knihy.",
                        "target_breakdown": "tri: cardinal number used with books",
                    }
                ]

        result = self.process(cards, lambda _config: NaturalGenerator())

        refreshed = self.content["sentences"][0]
        self.assertEqual(result["refreshed"], 1)
        self.assertEqual(refreshed["id"], sentence_id)
        self.assertEqual(refreshed["quality_version"], 2)
        self.assertEqual(
            refreshed["payload"]["primary_text"],
            "Na stole sú tri knihy.",
        )
        self.assertEqual(
            self.program_state["vocabulary"]["target-1"]["sentence_pass_count"],
            1,
        )

    def test_discovered_vocabulary_is_pending_until_approved(self):
        class DiscoveryGenerator:
            def generate(self, _request):
                return [
                    {
                        "target_word_id": "target-1",
                        "target_occurrence": "meste",
                        "primary_text": "V meste je nová knižnica.",
                        "transliteration": "ˈmes.ce",
                        "translation": "There is a new library in the town.",
                        "cloze_text": "V […] je nová knižnica.",
                        "target_breakdown": "mesto → meste, locative singular",
                        "discovered_vocabulary": [
                            {
                                "surface_form": "knižnica",
                                "observed_form": "knižnica",
                                "translation": "library",
                            }
                        ],
                    }
                ]

        self.config["daily_sentence_target"] = 1
        self.config["vocabulary_discovery"] = {
            "enabled": True,
            "approval": "manual",
        }
        cards = [source_card("target-1", "mesto")]
        self.source_state["cards"]["target-1"] = reviewed_state()

        result = self.process(cards, lambda _config: DiscoveryGenerator())

        self.assertEqual(result["discovered"], 1)
        candidate_id, candidate = next(
            iter(self.program_state["vocabulary_candidates"].items())
        )
        self.assertEqual(candidate["status"], "pending")
        snapshot = build_candidate_snapshot(
            "mandarin_reading",
            "Test",
            self.program_state,
            NOW,
        )
        self.assertEqual(snapshot["candidates"][0]["id"], candidate_id)

        payload = {
            "event_type": "vocabulary_candidate",
            "event_id": "approve-1",
            "program_id": "mandarin_reading",
            "candidate_id": candidate_id,
            "action": "approve",
            "occurred_at": NOW.isoformat(),
        }
        applied = apply_vocabulary_candidate_event(
            "mandarin_reading",
            self.program_state,
            payload,
            NOW,
        )
        duplicate = apply_vocabulary_candidate_event(
            "mandarin_reading",
            self.program_state,
            payload,
            NOW,
        )

        self.assertEqual(applied["applied"], 1)
        self.assertEqual(duplicate["duplicate"], 1)
        self.assertEqual(
            approved_vocabulary_cards(self.program_state)[0]["front"]["text"],
            "knižnica",
        )
        self.assertEqual(
            self.program_state["vocabulary"][candidate_id]["status"],
            "sentence_complete",
        )


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
            "familiar_pool_policy": "natural_priority",
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
        self.assertIn(
            "Never output a word list",
            payload["messages"][1]["content"],
        )

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
                candidate_snapshot = json.loads(
                    Path(
                        "cards/mandarin_reading_candidates.json"
                    ).read_text(encoding="utf-8")
                )
                self.assertEqual(candidate_snapshot["candidates"], [])
            finally:
                os.chdir(old_cwd)

    def test_vocabulary_review_immediately_generates_reinforcement_sentence(self):
        from anki_scheduler import hkt_day, isoformat_utc
        from generate_feeds import main

        now = datetime.now(timezone.utc)
        day = hkt_day(now).isoformat()
        reviewed_at = now - timedelta(seconds=1)
        config = {
            "streams": {
                "slovak_vocab": {
                    "type": "anki_deck",
                    "source_type": "csv",
                    "path": "Slovak.csv",
                    "feed_title": "Slovak Vocabulary",
                    "new_cards_per_day": 20,
                },
                "slovak_sentences": {
                    "type": "anki_deck",
                    "source_type": "generated_sentences",
                    "program_id": "slovak_reading",
                    "path": "generated/slovak_reading/sentences.json",
                    "feed_title": "Slovak Sentences",
                    "new_cards_per_day": 10,
                },
            },
            "programs": {
                "slovak_reading": {
                    **program_config(daily_target=1),
                    "enabled": True,
                    "source_stream": "slovak_vocab",
                    "sentence_stream": "slovak_sentences",
                    "content_path": "generated/slovak_reading/sentences.json",
                    "vocabulary_introduction": {
                        "strategy": "vocabulary_first",
                    },
                    "sentence_generation": {
                        "trigger": "after_first_passing_vocabulary_review",
                        "minimum_passing_reviews": 1,
                        "allow_inflected_targets": True,
                    },
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
                    json.dumps(config),
                    encoding="utf-8",
                )
                Path("history.json").write_text("{}", encoding="utf-8")
                Path("Slovak.csv").write_text(
                    "id,front,back,entry_type\n"
                    "slovak-1,mesto,town,term\n",
                    encoding="utf-8",
                )
                source_state_path = Path("state/streams/slovak_vocab.json")
                source_state_path.parent.mkdir(parents=True)
                source_state_path.write_text(
                    json.dumps(
                        {
                            "schema_version": 3,
                            "scheduler": {
                                "name": "fsrs-6.3.2",
                                "version": 1,
                                "desired_retention": 0.9,
                            },
                            "revision": 1,
                            "cards": {},
                            "processed_events": {},
                            "daily_batch": {
                                "id": day,
                                "date": day,
                                "created_at": isoformat_utc(
                                    reviewed_at - timedelta(minutes=1)
                                ),
                                "card_ids": ["slovak-1"],
                                "active": [
                                    {
                                        "card_id": "slovak-1",
                                        "available_at": isoformat_utc(
                                            reviewed_at - timedelta(minutes=1)
                                        ),
                                    }
                                ],
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                event_path = Path("event.json")
                event_path.write_text(
                    json.dumps(
                        {
                            "action": "anki_review",
                            "client_payload": {
                                "event_type": "anki_review",
                                "deck_id": "slovak_vocab",
                                "events": [
                                    {
                                        "event_id": "slovak-pass",
                                        "deck_id": "slovak_vocab",
                                        "card_id": "slovak-1",
                                        "rating": "good",
                                        "reviewed_at": isoformat_utc(reviewed_at),
                                    }
                                ],
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                with (
                    patch.dict(
                        os.environ,
                        {"GITHUB_EVENT_PATH": str(event_path)},
                        clear=False,
                    ),
                    patch(
                        "sentence_program._openrouter_generator",
                        return_value=DeterministicSentenceGenerator(),
                    ),
                ):
                    main()

                source_state = json.loads(
                    source_state_path.read_text(encoding="utf-8")
                )
                program_state = json.loads(
                    Path("state/programs/slovak_reading.json").read_text(
                        encoding="utf-8"
                    )
                )
                content = json.loads(
                    Path(
                        "generated/slovak_reading/sentences.json"
                    ).read_text(encoding="utf-8")
                )
                self.assertEqual(
                    source_state["cards"]["slovak-1"]["passing_reviews"],
                    1,
                )
                self.assertEqual(source_state["cards"]["slovak-1"]["reviews"], 1)
                self.assertEqual(len(content["sentences"]), 1)
                self.assertEqual(
                    program_state["vocabulary"]["slovak-1"]["status"],
                    "sentence_reinforcement",
                )
            finally:
                os.chdir(old_cwd)


if __name__ == "__main__":
    unittest.main()
