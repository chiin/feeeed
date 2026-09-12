import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WebAssetSmokeTests(unittest.TestCase):
    def test_anki_app_is_not_the_shared_state_module(self):
        app = (ROOT / "reviewer_app.js").read_text(encoding="utf-8")
        state = (ROOT / "reviewer_state.js").read_text(encoding="utf-8")

        self.assertIn("async function loadDeck()", app)
        self.assertIn('event_type: "anki_review"', app)
        self.assertNotEqual(app, state)

    def test_reviewer_loads_versioned_anki_assets(self):
        html = (ROOT / "reviewer.html").read_text(encoding="utf-8")
        app = (ROOT / "reviewer_app.js").read_text(encoding="utf-8")

        self.assertIn('src="reviewer_state.js?v=6"', html)
        self.assertIn('src="reviewer_app.js?v=8"', html)
        self.assertIn('id="homeScreen"', html)
        self.assertIn('id="candidateScreen"', html)
        self.assertIn('id="characterLinks"', html)
        self.assertIn("loadDeckIndex", app)
        self.assertIn("programId", app)
        self.assertIn("groupReviewEventsByDeck", app)
        self.assertIn("flushCandidateSync", app)
        self.assertIn("renderCharacterLinks", app)
        self.assertIn('anchor.target = "_blank"', app)
        self.assertIn('anchor.rel = "noopener noreferrer"', app)

    def test_pdf_reader_loads_book_aware_assets(self):
        html = (ROOT / "pdf_reader.html").read_text(encoding="utf-8")
        reviewer_html = (ROOT / "reviewer.html").read_text(encoding="utf-8")
        app = (ROOT / "pdf_reader_app.js").read_text(encoding="utf-8")

        self.assertIn("<title>PDF Queue Reader</title>", html)
        self.assertIn('id="pdfViewer"', html)
        self.assertNotEqual(html, reviewer_html)
        self.assertIn('src="reviewer_state.js?v=4"', html)
        self.assertIn('src="pdf_reader_app.js?v=5"', html)
        self.assertIn("data.book_id", app)
        self.assertIn("pdf_outbox_v3", app)
        self.assertIn("loadReaderIndex", app)
        self.assertIn('"BOOKS"', app)

    def test_workflow_exposes_optional_audio_generation_secrets(self):
        workflow = (
            ROOT / ".github/workflows/daily_flashcards.yaml"
        ).read_text(encoding="utf-8")

        self.assertIn("AZURE_SPEECH_KEY", workflow)
        self.assertIn("AZURE_SPEECH_REGION", workflow)


if __name__ == "__main__":
    unittest.main()
