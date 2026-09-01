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

        self.assertIn('src="reviewer_state.js?v=4"', html)
        self.assertIn('src="reviewer_app.js?v=4"', html)

    def test_pdf_reader_loads_book_aware_assets(self):
        html = (ROOT / "pdf_reader.html").read_text(encoding="utf-8")
        app = (ROOT / "pdf_reader_app.js").read_text(encoding="utf-8")

        self.assertIn('src="reviewer_state.js?v=4"', html)
        self.assertIn('src="pdf_reader_app.js?v=4"', html)
        self.assertIn("data.book_id", app)
        self.assertIn("pdf_outbox_v3", app)


if __name__ == "__main__":
    unittest.main()
