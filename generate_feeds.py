import csv
import json
import os
import random
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from feedgen.feed import FeedGenerator

CONFIG_PATH = Path("config.json")
HISTORY_PATH = Path("history.json")
CARDS_DIR = Path("cards")
BASE_URL = "https://chiin.github.io/feeeed"

BOX_INTERVALS = {1: 1, 2: 3, 3: 7, 4: 14, 5: 30}

def load_json(path: Path) -> dict:
    if path.exists():
        with open(path, mode="r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_json(path: Path, data: dict):
    with open(path, mode="w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

# --- DISPATCH PAYLOAD DETECTOR ---

def get_dispatched_stream_to_advance() -> str | None:
    """Checks if workflow run was triggered by a 'Finished Chapter' button dispatch."""
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if event_path and os.path.exists(event_path):
        try:
            with open(event_path, "r", encoding="utf-8") as f:
                event_data = json.load(f)
                client_payload = event_data.get("client_payload", {})
                return client_payload.get("stream")
        except Exception as e:
            print(f"Error reading event dispatch payload: {e}")
    return None

# --- HTML CARD GENERATORS ---

def create_flashcard_html(stream_key: str, card: dict) -> str:
    CARDS_DIR.mkdir(exist_ok=True)
    filename = f"{stream_key}-{card['id']}.html"
    filepath = CARDS_DIR / filename
    
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Flashcard</title>
    <style>
        body {{ font-family: -apple-system, sans-serif; display: flex; justify-content: center; align-items: center; min-height: 90vh; background-color: #f4f4f7; margin: 0; padding: 20px; }}
        .card {{ background: white; border-radius: 16px; box-shadow: 0 10px 25px rgba(0,0,0,0.08); padding: 30px; max-width: 400px; width: 100%; text-align: center; }}
        .question {{ font-size: 1.3rem; font-weight: 600; color: #111; margin-bottom: 25px; }}
        .btn {{ background-color: #007aff; color: white; border: none; padding: 12px 24px; font-size: 1rem; font-weight: 600; border-radius: 10px; cursor: pointer; }}
        .answer {{ display: none; margin-top: 25px; padding-top: 20px; border-top: 1px dashed #e0e0e0; font-size: 1.2rem; color: #2c3e50; font-weight: 500; }}
    </style>
</head>
<body>
    <div class="card">
        <div class="question">Q: {card['prompt']}</div>
        <button class="btn" onclick="document.getElementById('ans').style.display='block'; this.style.display='none';">Show Answer</button>
        <div id="ans" class="answer">A: {card['answer']}</div>
    </div>
</body>
</html>"""
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html_content)
    return f"{BASE_URL}/cards/{filename}"


def create_book_reader_html(stream_key: str, chapter_title: str, pdf_url: str, github_pat: str = "") -> str:
    CARDS_DIR.mkdir(exist_ok=True)
    filename = f"{stream_key}-{chapter_title}.html"
    filepath = CARDS_DIR / filename

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>{chapter_title}</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{ margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #1c1c1e; color: white; overflow: hidden; }}
        .header {{ display: flex; justify-content: space-between; align-items: center; padding: 12px 16px; background: #2c2c2e; border-bottom: 1px solid #3a3a3c; height: 55px; }}
        .title {{ font-weight: 600; font-size: 0.95rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 60%; }}
        .btn {{ background-color: #30d158; color: #000; border: none; padding: 8px 14px; font-weight: 600; border-radius: 8px; cursor: pointer; font-size: 0.85rem; }}
        .btn:disabled {{ background-color: #636366; color: #8e8e93; cursor: not-allowed; }}
        .pdf-container {{ height: calc(100vh - 55px); width: 100vw; }}
        iframe {{ width: 100%; height: 100%; border: none; }}
    </style>
</head>
<body>
    <div class="header">
        <div class="title">{chapter_title}</div>
        <button id="finishBtn" class="btn" onclick="markFinished()">Finished Chapter ✓</button>
    </div>
    <div class="pdf-container">
        <iframe src="{pdf_url}"></iframe>
    </div>

    <script>
    async function markFinished() {{
        const btn = document.getElementById('finishBtn');
        btn.innerText = "Updating...";
        btn.disabled = true;

        const TOKEN = "{github_pat}";

        try {{
            const response = await fetch("https://api.github.com/repos/chiin/feeeed/dispatches", {{
                method: "POST",
                headers: {{
                    "Accept": "application/vnd.github+json",
                    "Authorization": `Bearer ${{TOKEN}}`,
                    "Content-Type": "application/json"
                }},
                body: JSON.stringify({{
                    event_type: "advance_chapter",
                    client_payload: {{ stream: "{stream_key}" }}
                }})
            }});

            if (response.ok) {{
                btn.innerText = "Chapter Done! Next tomorrow.";
                btn.style.backgroundColor = "#0a84ff";
                btn.style.color = "#ffffff";
            }} else {{
                btn.innerText = "Error. Try again.";
                btn.disabled = false;
            }}
        }} catch (err) {{
            btn.innerText = "Network Error";
            btn.disabled = false;
        }}
    }}
    </script>
</body>
</html>"""
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html_content)

    return f"{BASE_URL}/cards/{filename}"

# --- STREAM HANDLERS ---

def process_book_queue(stream_key: str, stream_cfg: dict, stream_history: dict, fg, advance_stream: str | None):
    folder = Path(stream_cfg["folder"])
    if not folder.exists():
        print(f"Warning: Folder '{folder}' does not exist.")
        return

    # Sort files alphabetically
    pdf_files = sorted(list(folder.glob("*.pdf")), key=lambda p: p.name.lower())
    if not pdf_files:
        print(f"Warning: No PDF files found in '{folder}'.")
        return

    current_index = stream_history.get("current_index", 0)

    # Check if this stream was triggered to advance via GitHub dispatch
    if advance_stream == stream_key:
        print(f"Advancing stream '{stream_key}' to next chapter!")
        current_index += 1
        stream_history["current_index"] = current_index

    # Check for end-of-book state
    if current_index >= len(pdf_files):
        fe = fg.add_entry()
        fe.id(f"{stream_key}-completed")
        fe.title("🎉 All Done!")
        fe.description("You have completed this book! Upload a new folder of PDFs to start your next reading queue.")
        fe.link(href=f"{BASE_URL}/cards/{stream_key}-done.html")
        return

    active_pdf = pdf_files[current_index]
    pdf_url = f"{BASE_URL}/{folder.name}/{active_pdf.name}"
    github_pat = stream_cfg.get("github_pat", "")

    # Generate viewer card
    card_web_url = create_book_reader_html(stream_key, active_pdf.stem, pdf_url, github_pat)

    item_guid = f"{stream_key}-ch-{current_index:03d}"
    fe = fg.add_entry()
    fe.id(item_guid)
    fe.title(f"[{stream_cfg.get('feed_title', stream_key.title())}] {active_pdf.stem}")
    fe.link(href=card_web_url)
    fe.description(f"Tap to read chapter: {active_pdf.name}")
    fe.enclosure(url=pdf_url, length=str(active_pdf.stat().st_size), type="application/pdf")


def process_pdf_folder(stream_key: str, stream_cfg: dict, stream_history: dict, fg):
    folder = Path(stream_cfg["folder"])
    if not folder.exists():
        return

    strategy = stream_cfg.get("strategy", "sequential")
    daily_n = stream_cfg.get("daily_n", 1)
    pub_time = datetime.now(timezone.utc) - timedelta(minutes=10)

    pdfs = list(folder.glob("*.pdf"))
    if strategy in ["sequential", "alphabetical"]:
        pdfs.sort(key=lambda p: p.name.lower())

    if strategy in ["sequential", "alphabetical"]:
        last_index = stream_history.get("last_index", 0)
        batch = pdfs[last_index : last_index + daily_n]
        stream_history["last_index"] = last_index + len(batch)
    else:
        batch = pdfs[:daily_n]

    for pdf_file in batch:
        pdf_url = f"{BASE_URL}/{folder.name}/{pdf_file.name}"
        item_guid = f"{stream_key}-{pdf_file.stem}"

        fe = fg.add_entry()
        fe.id(item_guid)
        fe.title(f"[{stream_cfg.get('feed_title', stream_key)}] {pdf_file.stem}")
        fe.link(href=pdf_url)
        fe.description(f"Tap to read PDF document: {pdf_file.name}")
        fe.enclosure(url=pdf_url, length=str(pdf_file.stat().st_size), type="application/pdf")
        fe.pubDate(pub_time)


def process_flashcards(stream_key: str, stream_cfg: dict, stream_history: dict, fg):
    csv_path = Path(stream_cfg["csv_file"])
    if not csv_path.exists():
        return

    cards = []
    with open(csv_path, mode="r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            cards.append({"id": row["id"].strip(), "prompt": row["prompt"].strip(), "answer": row["answer"].strip()})

    daily_n = stream_cfg.get("daily_n", 10)
    pub_time = datetime.now(timezone.utc) - timedelta(minutes=10)
    selected_cards = cards[:daily_n]

    for card in selected_cards:
        card_web_url = create_flashcard_html(stream_key, card)
        item_guid = f"{stream_key}-{card['id']}"

        fe = fg.add_entry()
        fe.id(item_guid)
        fe.link(href=card_web_url)
        fe.title(f"{stream_key.title()}")
        fe.description(f"Tap to view flashcard -> {card['prompt']}")
        fe.pubDate(pub_time)

# --- MAIN CONTROLLER ---

def main():
    config = load_json(CONFIG_PATH)
    master_history = load_json(HISTORY_PATH)
    streams = config.get("streams", {})

    advance_stream = get_dispatched_stream_to_advance()
    if advance_stream:
        print(f"Triggered via dispatch for stream: '{advance_stream}'")

    for stream_key, stream_cfg in streams.items():
        # BACKWARDS COMPATIBILITY: Detect stream type automatically if not explicitly defined
        stream_type = stream_cfg.get("type")
        if not stream_type:
            if "csv_file" in stream_cfg:
                stream_type = "flashcard"
            elif "folder" in stream_cfg:
                stream_type = "pdf_folder"
            else:
                continue

        stream_history = master_history.setdefault(stream_key, {})
        xml_filename = Path(f"{stream_key}.xml")
        feed_url = f"{BASE_URL}/{xml_filename}"

        fg = FeedGenerator()
        fg.id(feed_url)
        fg.title(stream_cfg.get("feed_title", stream_key))
        fg.description(f"Daily feed for {stream_key}")
        fg.link(href=feed_url, rel="self")
        fg.language("en")

        if stream_type == "book_queue":
            process_book_queue(stream_key, stream_cfg, stream_history, fg, advance_stream)
        elif stream_type == "pdf_folder":
            process_pdf_folder(stream_key, stream_cfg, stream_history, fg)
        elif stream_type == "flashcard":
            process_flashcards(stream_key, stream_cfg, stream_history, fg)

        fg.rss_file(str(xml_filename), pretty=True)
        print(f"Generated {xml_filename}")

    save_json(HISTORY_PATH, master_history)

if __name__ == "__main__":
    main()
