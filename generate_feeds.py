import csv
import json
import os
import random
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from feedgen.feed import FeedGenerator
from anki_scheduler import (
    FSRSScheduler,
    apply_review_events,
    build_deck_snapshot,
    ensure_daily_batch,
)
from pdf_scheduler import (
    apply_completion_events,
    apply_continuation_events,
    build_snapshot as build_pdf_snapshot,
    migrate_history as migrate_pdf_history,
    release_if_due,
)

CONFIG_PATH = Path("config.json")
HISTORY_PATH = Path("history.json")
CARDS_DIR = Path("cards")
BASE_URL = "https://chiin.github.io/feeeed"

# Leitner Box Intervals (in days)
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

def get_dispatch_payload() -> dict:
    """Reads payload if workflow was triggered by a repository_dispatch event."""
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if event_path and os.path.exists(event_path):
        try:
            with open(event_path, "r", encoding="utf-8") as f:
                event_data = json.load(f)
                payload = event_data.get("client_payload", {})
                if not isinstance(payload, dict):
                    raise ValueError("client_payload must be an object")
                normalized = dict(payload)
                normalized.setdefault(
                    "event_type",
                    event_data.get("action") or event_data.get("event_type"),
                )
                return normalized
        except (OSError, ValueError, json.JSONDecodeError) as e:
            print(f"Error reading event dispatch payload: {e}")
    return {}

# --- HTML CARD GENERATORS ---

def create_flashcard_html(stream_key: str, card: dict, box: int) -> str:
    CARDS_DIR.mkdir(exist_ok=True)
    filename = f"{stream_key}-{card['id']}.html"
    filepath = CARDS_DIR / filename

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Flashcard: {card['prompt']}</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; display: flex; justify-content: center; align-items: center; min-height: 100vh; background-color: #1c1c1e; color: white; margin: 0; padding: 20px; }}
        .card {{ background: #2c2c2e; border-radius: 16px; box-shadow: 0 10px 30px rgba(0,0,0,0.5); padding: 28px; max-width: 420px; width: 100%; text-align: center; border: 1px solid #3a3a3c; }}
        .box-badge {{ display: inline-block; background: #3a3a3c; color: #0a84ff; font-size: 0.8rem; font-weight: 700; padding: 4px 10px; border-radius: 12px; margin-bottom: 16px; text-transform: uppercase; letter-spacing: 0.5px; }}
        .question {{ font-size: 1.35rem; font-weight: 600; color: #ffffff; margin-bottom: 24px; line-height: 1.4; }}
        .btn {{ background-color: #0a84ff; color: white; border: none; padding: 12px 24px; font-size: 1rem; font-weight: 600; border-radius: 10px; cursor: pointer; transition: opacity 0.2s; width: 100%; }}
        .btn:disabled {{ opacity: 0.5; cursor: not-allowed; }}
        .answer-box {{ display: none; margin-top: 24px; padding-top: 20px; border-top: 1px dashed #48484a; }}
        .answer {{ font-size: 1.25rem; color: #30d158; font-weight: 600; margin-bottom: 24px; }}
        .controls {{ display: flex; gap: 12px; margin-top: 16px; }}
        .btn-fail {{ background-color: #ff453a; flex: 1; }}
        .btn-pass {{ background-color: #30d158; color: #000; flex: 1; }}
        .status-msg {{ display: none; margin-top: 16px; font-size: 0.95rem; font-weight: 500; color: #8e8e93; }}
    </style>
</head>
<body>
    <div class="card">
        <div class="box-badge">Leitner Box {box}</div>
        <div class="question">{card['prompt']}</div>
        
        <button id="showBtn" class="btn" onclick="revealAnswer()">Show Answer</button>
        
        <div id="answerBox" class="answer-box">
            <div class="answer">{card['answer']}</div>
            <div id="controls" class="controls">
                <button id="failBtn" class="btn btn-fail" onclick="gradeCard('incorrect')">❌ Incorrect</button>
                <button id="passBtn" class="btn btn-pass" onclick="gradeCard('correct')">✅ Correct</button>
            </div>
        </div>
        
        <div id="statusMsg" class="status-msg"></div>
    </div>

    <script>
    function revealAnswer() {{
        document.getElementById('answerBox').style.display = 'block';
        document.getElementById('showBtn').style.display = 'none';
    }}

    function getPAT() {{
        let token = localStorage.getItem("feeeed_pat");
        if (!token) {{
            token = prompt("Enter your GitHub PAT (saved on your device):");
            if (token) {{
                token = token.trim();
                localStorage.setItem("feeeed_pat", token);
            }}
        }}
        return token;
    }}

    async function gradeCard(grade) {{
        const failBtn = document.getElementById('failBtn');
        const passBtn = document.getElementById('passBtn');
        const statusMsg = document.getElementById('statusMsg');

        failBtn.disabled = true;
        passBtn.disabled = true;
        statusMsg.innerText = "Saving grade...";
        statusMsg.style.display = 'block';

        const TOKEN = getPAT();
        if (!TOKEN) {{
            statusMsg.innerText = "PAT required to submit.";
            failBtn.disabled = false;
            passBtn.disabled = false;
            return;
        }}

        try {{
            const response = await fetch("https://api.github.com/repos/chiin/feeeed/dispatches", {{
                method: "POST",
                headers: {{
                    "Accept": "application/vnd.github+json",
                    "Authorization": `Bearer ${{TOKEN}}`,
                    "Content-Type": "application/json"
                }},
                body: JSON.stringify({{
                    event_type: "flashcard_grade",
                    client_payload: {{
                        stream: "{stream_key}",
                        card_id: "{card['id']}",
                        grade: grade
                    }}
                }})
            }});

            if (response.ok) {{
                document.getElementById('controls').style.display = 'none';
                statusMsg.innerText = grade === 'correct' 
                    ? "✅ Marked Correct! Moved to next Leitner box." 
                    : "❌ Marked Incorrect. Reset to Box 1 for tomorrow.";
                statusMsg.style.color = grade === 'correct' ? '#30d158' : '#ff453a';
            }} else if (response.status === 401) {{
                alert("Invalid PAT. Clearing saved token.");
                localStorage.removeItem("feeeed_pat");
                statusMsg.innerText = "Invalid PAT. Tap button again.";
                failBtn.disabled = false;
                passBtn.disabled = false;
            }} else {{
                statusMsg.innerText = "Error saving grade (" + response.status + ")";
                failBtn.disabled = false;
                passBtn.disabled = false;
            }}
        }} catch (err) {{
            statusMsg.innerText = "Network error. Check connection.";
            failBtn.disabled = false;
            passBtn.disabled = false;
        }}
    }}
    </script>
</body>
</html>"""

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html_content)

    return f"{BASE_URL}/cards/{filename}"


def create_book_reader_html(stream_key: str, chapter_title: str, pdf_url: str) -> str:
    CARDS_DIR.mkdir(exist_ok=True)
    filename = f"{stream_key}-{chapter_title}.html"
    filepath = CARDS_DIR / filename

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>{chapter_title}</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js"></script>
    <style>
        * {{ box-sizing: border-box; }}
        body {{ margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: #1c1c1e; color: white; }}
        .header {{ position: sticky; top: 0; z-index: 100; display: flex; justify-content: space-between; align-items: center; padding: 12px 16px; background: #2c2c2e; border-bottom: 1px solid #3a3a3c; height: 55px; }}
        .title {{ font-weight: 600; font-size: 0.95rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 60%; }}
        .btn {{ background-color: #30d158; color: #000; border: none; padding: 8px 14px; font-weight: 600; border-radius: 8px; cursor: pointer; font-size: 0.85rem; }}
        .btn:disabled {{ background-color: #636366; color: #8e8e93; cursor: not-allowed; }}
        .pdf-container {{ padding: 10px; width: 100%; max-width: 800px; margin: 0 auto; min-height: 100vh; }}
        canvas {{ width: 100% !important; height: auto !important; margin-bottom: 12px; border-radius: 6px; box-shadow: 0 4px 12px rgba(0,0,0,0.5); }}
        .loading {{ text-align: center; padding: 40px; color: #8e8e93; font-size: 0.9rem; }}
    </style>
</head>
<body>
    <div class="header">
        <div class="title">{chapter_title}</div>
        <button id="finishBtn" class="btn" onclick="markFinished()">Finished Chapter ✓</button>
    </div>

    <div id="loading" class="loading">Loading PDF chapter...</div>
    <div id="pdf-viewer" class="pdf-container"></div>

    <script>
    pdfjsLib.GlobalWorkerOptions.workerSrc = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';

    pdfjsLib.getDocument('{pdf_url}').promise.then(pdf => {{
        document.getElementById('loading').style.display = 'none';
        const viewer = document.getElementById('pdf-viewer');
        
        for (let pageNum = 1; pageNum <= pdf.numPages; pageNum++) {{
            pdf.getPage(pageNum).then(page => {{
                const viewport = page.getViewport({{ scale: 2.0 }});
                const canvas = document.createElement('canvas');
                const context = canvas.getContext('2d');
                canvas.height = viewport.height;
                canvas.width = viewport.width;
                
                viewer.appendChild(canvas);

                page.render({{
                    canvasContext: context,
                    viewport: viewport
                }});
            }});
        }}
    }}).catch(err => {{
        document.getElementById('loading').innerText = 'Failed to load PDF: ' + err.message;
    }});

    function getPAT() {{
        let token = localStorage.getItem("feeeed_pat");
        if (!token) {{
            token = prompt("Enter your GitHub PAT (saved on your device):");
            if (token) {{
                token = token.trim();
                localStorage.setItem("feeeed_pat", token);
            }}
        }}
        return token;
    }}

    async function markFinished() {{
        const btn = document.getElementById('finishBtn');
        btn.innerText = "Updating...";
        btn.disabled = true;

        const TOKEN = getPAT();
        if (!TOKEN) {{
            btn.innerText = "PAT Required";
            btn.disabled = false;
            return;
        }}

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
                btn.innerText = "Done!";
                btn.style.backgroundColor = "#0a84ff";
                btn.style.color = "#ffffff";
            }} else if (response.status === 401) {{
                alert("Invalid PAT. Clearing saved token.");
                localStorage.removeItem("feeeed_pat");
                btn.innerText = "Invalid PAT. Try again.";
                btn.disabled = false;
            }} else {{
                btn.innerText = "Error (" + response.status + ")";
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

# --- STREAM PROCESSORS ---

def process_flashcards(stream_key: str, stream_cfg: dict, stream_history: dict, fg, dispatch_payload: dict):
    csv_path = Path(stream_cfg["csv_file"])
    if not csv_path.exists():
        print(f"Warning: CSV file '{csv_path}' not found.")
        return

    cards_history = stream_history.setdefault("cards", {})

    # 1. Update Leitner score if this stream was triggered via flashcard grading
    if dispatch_payload.get("stream") == stream_key and "card_id" in dispatch_payload:
        card_id = dispatch_payload["card_id"]
        grade = dispatch_payload.get("grade", "correct")

        card_stat = cards_history.setdefault(card_id, {"box": 1})
        current_box = card_stat.get("box", 1)

        if grade == "correct":
            new_box = min(current_box + 1, 5)
        else:
            new_box = 1  # Reset back to Box 1 on error

        now_utc = datetime.now(timezone.utc)
        days_ahead = BOX_INTERVALS.get(new_box, 1)
        next_due = now_utc + timedelta(days=days_ahead)

        card_stat["box"] = new_box
        card_stat["last_reviewed"] = now_utc.isoformat()
        card_stat["next_due"] = next_due.isoformat()
        print(f"[{stream_key}] Rated '{card_id}' as '{grade}'. Box: {current_box} -> {new_box}. Next due in {days_ahead} days.")

    # 2. Read CSV cards
    cards = []
    with open(csv_path, mode="r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            cards.append({"id": row["id"].strip(), "prompt": row["prompt"].strip(), "answer": row["answer"].strip()})

    # 3. Filter due cards (next_due <= now or unreviewed)
    now_iso = datetime.now(timezone.utc).isoformat()
    due_cards = []

    for card in cards:
        cid = card["id"]
        c_stat = cards_history.get(cid, {})
        next_due_iso = c_stat.get("next_due")

        if not next_due_iso or next_due_iso <= now_iso:
            due_cards.append(card)

    daily_n = stream_cfg.get("daily_n", 10)
    selected_cards = due_cards[:daily_n]
    pub_time = datetime.now(timezone.utc) - timedelta(minutes=10)

    for card in selected_cards:
        cid = card["id"]
        c_stat = cards_history.get(cid, {})
        box = c_stat.get("box", 1)

        card_web_url = create_flashcard_html(stream_key, card, box)
        item_guid = f"{stream_key}-{cid}"

        fe = fg.add_entry()
        fe.id(item_guid)
        fe.link(href=card_web_url)
        fe.title(f"[{stream_key.title()}] Box {box}")
        fe.description(f"Tap to review flashcard -> {card['prompt']}")
        fe.pubDate(pub_time)


def process_book_queue(stream_key: str, stream_cfg: dict, stream_history: dict, fg, dispatch_payload: dict):
    folder = Path(stream_cfg["folder"])
    if not folder.exists():
        return

    pdf_files = sorted(list(folder.glob("*.pdf")), key=lambda p: p.name.lower())
    if not pdf_files:
        return

    current_index = stream_history.get("current_index", 0)

    # Advance chapter if triggered via dispatch
    if dispatch_payload.get("stream") == stream_key and "card_id" not in dispatch_payload:
        print(f"Advancing stream '{stream_key}' to next chapter!")
        current_index += 1
        stream_history["current_index"] = current_index

    if current_index >= len(pdf_files):
        fe = fg.add_entry()
        fe.id(f"{stream_key}-completed")
        fe.title("🎉 All Done!")
        fe.description("You have completed this book! Upload a new folder of PDFs to start your next reading queue.")
        fe.link(href=f"{BASE_URL}/cards/{stream_key}-done.html")
        return

    active_pdf = pdf_files[current_index]
    pdf_url = f"{BASE_URL}/{folder.name}/{active_pdf.name}"

    card_web_url = create_book_reader_html(stream_key, active_pdf.stem, pdf_url)

    now_utc = datetime.now(timezone.utc)
    if stream_history.get("active_chapter_index") != current_index:
        stream_history["active_chapter_index"] = current_index
        stream_history["chapter_started_at"] = now_utc.isoformat()
    chapter_started_at = datetime.fromisoformat(
        stream_history["chapter_started_at"].replace("Z", "+00:00")
    )

    item_guid = f"{stream_key}-ch-{current_index:03d}"
    fe = fg.add_entry()
    fe.id(item_guid)
    fe.title(f"[{stream_cfg.get('feed_title', stream_key.title())}] {active_pdf.stem}")
    fe.link(href=card_web_url)
    fe.description(f"Tap to read chapter: {active_pdf.name}")
    fe.pubDate(chapter_started_at)
    fe.enclosure(url=pdf_url, length=str(active_pdf.stat().st_size), type="application/pdf")


def process_pdf_folder(
    stream_key: str,
    stream_cfg: dict,
    stream_history: dict,
    fg,
    base_url: str,
    dispatch_payload: dict | None = None,
    now: datetime | None = None,
):
    now = now or datetime.now(timezone.utc)
    dispatch_payload = dispatch_payload or {}
    is_current_book = stream_cfg.get("type") == "current_book"
    book_id = stream_cfg.get("book_id") if is_current_book else None
    if is_current_book and (not isinstance(book_id, str) or not book_id.strip()):
        raise ValueError(f"[{stream_key}] current_book requires a non-empty book_id.")
    if book_id is not None:
        book_id = book_id.strip()
    folder_path = Path(stream_cfg.get("folder", ""))
    if not folder_path.exists():
        print(f"[{stream_key}] Folder '{folder_path}' does not exist.")
        return

    cards_dir = Path("cards")
    cards_dir.mkdir(exist_ok=True)
    batch_json_path = cards_dir / f"{stream_key}_pdf_batch.json"
    legacy_snapshot = None
    if batch_json_path.exists():
        try:
            legacy_snapshot = load_json(batch_json_path)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            print(f"[{stream_key}] Could not read legacy PDF batch: {error}")

    all_pdf_ids = [path.name for path in folder_path.glob("*.pdf")]
    strategy = (
        "sequential"
        if is_current_book
        else stream_cfg.get("strategy", "sequential")
    )
    batch_size = 1 if is_current_book else stream_cfg.get("batch_size", 5)
    migrate_pdf_history(
        stream_key, strategy, stream_history, now, legacy_snapshot, book_id
    )

    events = []
    if (
        dispatch_payload.get("event_type") == "pdf_batch_event"
        and dispatch_payload.get("stream_id") == stream_key
    ):
        raw_events = dispatch_payload.get("events", [])
        if isinstance(raw_events, list):
            events = raw_events
        else:
            print(f"[{stream_key}] Ignoring PDF dispatch: events must be a list.")
    elif (
        dispatch_payload.get("event_type") == "pdf_completed"
        and dispatch_payload.get("stream") == stream_key
        and dispatch_payload.get("pdf_id")
    ):
        events = [{
            "event_id": (
                f"legacy:{stream_key}:{dispatch_payload['pdf_id']}:"
                f"{now.date().isoformat()}"
            ),
            "stream_id": stream_key,
            "action": "complete",
            "pdf_id": dispatch_payload["pdf_id"],
            "occurred_at": now.isoformat(),
        }]

    completion_result = apply_completion_events(
        stream_key, stream_history, events, now, book_id
    )
    continuation_requested = any(
        isinstance(event, dict) and event.get("action") == "continue"
        for event in events
    )
    current_batch = stream_history.get("daily_batch")
    force_today = bool(
        continuation_requested
        and current_batch
        and not current_batch["active_ids"]
    )
    released = release_if_due(
        stream_key,
        stream_history,
        all_pdf_ids,
        batch_size,
        now,
        force_today=force_today,
    )
    continuation_result = apply_continuation_events(
        stream_key,
        stream_history,
        events,
        all_pdf_ids,
        batch_size,
        now,
        book_id,
    )
    if events:
        print(
            f"[{stream_key}] Processed PDF events: "
            f"completions={completion_result}, continuations={continuation_result}."
        )
    if released:
        print(f"[{stream_key}] Released HKT batch {stream_history['daily_batch']['id']}.")

    compiled_payload = build_pdf_snapshot(
        stream_key,
        stream_cfg.get("feed_title", stream_key.title()),
        folder_path.name,
        base_url,
        stream_history,
        all_pdf_ids,
        batch_size,
        now,
        book_id,
    )
    save_json(batch_json_path, compiled_payload)

    release_summary = compiled_payload["release_summary"]
    if not release_summary:
        return
    titles_summary = ", ".join(item["title"] for item in release_summary)
    web_reader_url = f"{base_url}/pdf_reader.html?stream={stream_key}"

    fe = fg.add_entry()
    identity = f"{stream_key}-{book_id}" if book_id else stream_key
    fe.id(f"{identity}-pdf-batch-{compiled_payload['batch_id']}")
    fe.title(
        f"[{stream_cfg.get('feed_title', stream_key.title())}] {titles_summary}"
    )
    fe.link(href=web_reader_url)
    fe.description(
        f"{len(release_summary)} PDFs released; "
        f"{len(compiled_payload['active'])} remaining."
    )
    fe.pubDate(
        datetime.fromisoformat(
            compiled_payload["released_at"].replace("Z", "+00:00")
        )
    )

# --- MAIN CONTROLLER ---

def main():
    config = load_json(CONFIG_PATH)
    master_history = load_json(HISTORY_PATH)
    streams = config.get("streams", {})

    dispatch_payload = get_dispatch_payload()

    if dispatch_payload:
        print(f"Triggered via dispatch with payload: {dispatch_payload}")

    for stream_key, stream_cfg in streams.items():
        # Autodetect stream type for backwards compatibility
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

        if stream_type == "flashcard":
            process_flashcards(stream_key, stream_cfg, stream_history, fg, dispatch_payload)
        elif stream_type == "book_queue":
            process_book_queue(stream_key, stream_cfg, stream_history, fg, dispatch_payload)
        elif stream_type in {"pdf_folder", "current_book"}:
            process_pdf_folder(stream_key, stream_cfg, stream_history, fg, BASE_URL, dispatch_payload)
        elif stream_type == "anki_deck":
            process_anki_deck(stream_key, stream_cfg, stream_history, fg, BASE_URL, dispatch_payload)

        fg.rss_file(str(xml_filename), pretty=True)
        print(f"Generated {xml_filename}")

    save_json(HISTORY_PATH, master_history)

# --- DECK PARSERS ---

def parse_csv_deck(csv_path: Path, base_url: str) -> list[dict]:
    cards = []
    with open(csv_path, mode="r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            card_id = row["id"].strip()
            audio_url = row.get("audio_url", "").strip()
            if audio_url and not audio_url.startswith("http"):
                audio_url = f"{base_url}/{audio_url}"

            cards.append({
                "id": card_id,
                "front": {
                    "text": row.get("front", "").strip(),
                    "audio": audio_url if audio_url else None,
                    "image": row.get("image_url", "").strip() or None
                },
                "back": {
                    "text": row.get("back", "").strip(),
                    "notes": row.get("notes", "").strip() or None
                }
            })
    return cards

def parse_media_folder(folder_path: Path, base_url: str) -> list[dict]:
    cards = []
    audio_extensions = {".mp3", ".m4a", ".wav", ".ogg"}
    
    # Pair audio files with matching .txt description files
    for audio_file in sorted(folder_path.glob("*")):
        if audio_file.suffix.lower() in audio_extensions:
            card_id = audio_file.stem
            txt_file = folder_path / f"{card_id}.txt"
            
            back_text = ""
            if txt_file.exists():
                with open(txt_file, "r", encoding="utf-8") as f:
                    back_text = f.read().strip()

            cards.append({
                "id": card_id,
                "front": {
                    "text": f"🔊 Audio Prompt: {card_id}",
                    "audio": f"{base_url}/{folder_path.name}/{audio_file.name}",
                    "image": None
                },
                "back": {
                    "text": back_text if back_text else card_id,
                    "notes": None
                }
            })
    return cards

# --- PROCESSOR FOR ANKI DECKS ---

def process_anki_deck(
    stream_key: str,
    stream_cfg: dict,
    stream_history: dict,
    fg,
    base_url: str,
    dispatch_payload: dict,
    now: datetime | None = None,
):
    now = now or datetime.now(timezone.utc)
    source_type = stream_cfg.get("source_type", "csv")
    path = Path(stream_cfg["path"])
    
    if not path.exists():
        print(f"Warning: Deck path '{path}' does not exist.")
        return

    # 1. Parse Card Data
    if source_type == "csv":
        all_cards = parse_csv_deck(path, base_url)
    elif source_type == "media_folder":
        all_cards = parse_media_folder(path, base_url)
    else:
        return

    # The HKT rollover freezes membership before any reviews mutate the batch.
    batch = ensure_daily_batch(
        stream_history,
        [card["id"] for card in all_cards],
        stream_cfg.get("new_cards_per_day", 50),
        now,
    )

    if (
        dispatch_payload.get("event_type") == "anki_review"
        and dispatch_payload.get("deck_id") == stream_key
    ):
        raw_events = dispatch_payload.get("events")
        if raw_events is None:
            raw_events = [dispatch_payload]
        if not isinstance(raw_events, list):
            print(f"[{stream_key}] Ignoring Anki dispatch: events must be a list.")
        else:
            result = apply_review_events(
                stream_key, stream_history, raw_events, now
            )
            print(f"[{stream_key}] Processed Anki reviews: {result}.")

    scheduler = FSRSScheduler()
    compiled_payload = build_deck_snapshot(
        stream_key,
        stream_cfg.get("feed_title", stream_key.title()),
        all_cards,
        stream_history,
        now,
        scheduler,
    )
    if not compiled_payload["cards"]:
        print(f"[{stream_key}] No cards due today!")

    # Save the authoritative active batch snapshot for the reviewer.
    cards_dir = Path("cards")
    cards_dir.mkdir(exist_ok=True)
    deck_json_path = cards_dir / f"{stream_key}_deck.json"
    
    with open(deck_json_path, "w", encoding="utf-8") as f:
        json.dump(compiled_payload, f, indent=2)

    # Reuse one GUID for the HKT day so review updates do not create unread items.
    web_reviewer_url = f"{base_url}/reviewer.html?deck={stream_key}"
    card_count = len(compiled_payload["cards"])
    
    fe = fg.add_entry()
    fe.id(f"{stream_key}-anki-batch-{batch['date']}")
    fe.title(f"[{stream_cfg.get('feed_title', stream_key.title())}] {card_count} Cards Due")
    fe.link(href=web_reviewer_url)
    fe.description(f"Tap to start today's review session ({card_count} cards pending).")
    fe.pubDate(datetime.fromisoformat(batch["created_at"].replace("Z", "+00:00")))

    # 6. Export RSS file
    rss_filepath = Path(f"{stream_key}.xml")
    # fg.rss_file(rss_filepath, pretty=True)
    print(f"[{stream_key}] Saved feed to {rss_filepath}")

if __name__ == "__main__":
    main()

