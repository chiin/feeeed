import json
import os
import tempfile
import unittest
from pathlib import Path

from state_store import StateStore


class StateStoreTests(unittest.TestCase):
    def test_streams_migrate_independently_from_legacy_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = {
                "hsk": {"revision": 7, "cards": {"hsk-1": {}}},
                "economics": {"revision": 3, "completed_ids": ["one.pdf"]},
            }
            history_path = root / "history.json"
            history_path.write_text(json.dumps(legacy), encoding="utf-8")
            store = StateStore(root)

            hsk = store.load_stream("hsk")
            economics = store.load_stream("economics")
            hsk["revision"] += 1
            store.save_all()

            self.assertEqual(
                json.loads((root / "state/streams/hsk.json").read_text()),
                {"revision": 8, "cards": {"hsk-1": {}}},
            )
            self.assertEqual(
                json.loads((root / "state/streams/economics.json").read_text()),
                legacy["economics"],
            )
            self.assertEqual(json.loads(history_path.read_text()), legacy)
            manifest = json.loads((root / "state/migration.json").read_text())
            self.assertEqual(
                manifest["migrated_streams"], ["economics", "hsk"]
            )

    def test_existing_stream_state_takes_precedence_over_legacy_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "history.json").write_text(
                json.dumps({"hsk": {"revision": 1}}), encoding="utf-8"
            )
            state_path = root / "state/streams/hsk.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps({"revision": 9}), encoding="utf-8"
            )

            store = StateStore(root)
            state = store.load_stream("hsk")
            store.save_all()

            self.assertEqual(state["revision"], 9)
            manifest = json.loads((root / "state/migration.json").read_text())
            self.assertEqual(manifest["migrated_streams"], ["hsk"])

    def test_program_and_generation_state_have_separate_namespaces(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = StateStore(root)
            program = store.load_program("mandarin_reading")
            generation = store.load_generation("mandarin_reading")
            program["phase"] = "active"
            generation["last_job_id"] = "job-1"
            store.save_all()

            self.assertEqual(
                json.loads(
                    (root / "state/programs/mandarin_reading.json").read_text()
                ),
                {"phase": "active"},
            )
            self.assertEqual(
                json.loads(
                    (root / "state/generation/mandarin_reading.json").read_text()
                ),
                {"last_job_id": "job-1"},
            )

    def test_custom_state_path_is_repository_relative(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = StateStore(root)
            state = store.load_stream("hsk", "state/custom/hsk-state.json")
            state["revision"] = 1
            store.save_all()

            self.assertTrue((root / "state/custom/hsk-state.json").exists())
            with self.assertRaises(ValueError):
                store.load_stream("other", "../outside.json")
            with self.assertRaises(ValueError):
                store.load_stream("other", "custom/outside-state.json")

    def test_invalid_state_identifiers_and_shared_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory))
            with self.assertRaises(ValueError):
                store.load_stream("../hsk")
            store.load_stream("hsk", "state/shared.json")
            with self.assertRaises(ValueError):
                store.load_stream("slovak", "state/shared.json")
            with self.assertRaises(ValueError):
                store.load_stream("slovak", "state/SHARED.json")
            with self.assertRaises(ValueError):
                store.load_stream("reserved", "state/MIGRATION.JSON")

    def test_missing_state_after_completed_migration_is_not_recreated_from_legacy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "history.json").write_text(
                json.dumps({"hsk": {"revision": 1}}), encoding="utf-8"
            )
            store = StateStore(root)
            store.load_stream("hsk")
            store.save_all()
            (root / "state/streams/hsk.json").unlink()

            with self.assertRaises(FileNotFoundError):
                StateStore(root).load_stream("hsk")

    def test_invalid_existing_state_file_is_not_silently_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / "state/streams/hsk.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text("[]", encoding="utf-8")

            with self.assertRaises(ValueError):
                StateStore(root).load_stream("hsk")


class GeneratorStateIntegrationTests(unittest.TestCase):
    def test_generator_migrates_configured_stream_without_rewriting_history(self):
        from generate_feeds import main

        legacy = {
            "economics": {
                "completed_files": ["article_001.pdf"],
                "next_update_at": "2026-08-31T12:00:00+08:00",
            },
            "unconfigured": {"keep": True},
        }
        config = {
            "streams": {
                "economics": {
                    "type": "pdf_folder",
                    "folder": "Economics_Cards",
                    "strategy": "sequential",
                    "batch_size": 1,
                    "feed_title": "Economics",
                }
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            previous_directory = os.getcwd()
            os.chdir(directory)
            try:
                Path("config.json").write_text(
                    json.dumps(config), encoding="utf-8"
                )
                Path("history.json").write_text(
                    json.dumps(legacy), encoding="utf-8"
                )
                folder = Path("Economics_Cards")
                folder.mkdir()
                (folder / "article_001.pdf").write_bytes(b"%PDF-test")

                main()

                migrated = json.loads(
                    Path("state/streams/economics.json").read_text()
                )
                self.assertEqual(migrated["completed_ids"], ["article_001.pdf"])
                self.assertEqual(
                    json.loads(Path("history.json").read_text()), legacy
                )
                self.assertFalse(Path("state/streams/unconfigured.json").exists())
            finally:
                os.chdir(previous_directory)


if __name__ == "__main__":
    unittest.main()
