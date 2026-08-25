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

# Leitner Box Intervals (in days)
BOX_INTERVALS = {1: 1, 2: 3, 3: 7, 4: 14, 5: 30}


from datetime import datetime, timezone, timedelta

def get_next_update_time(last_update_iso: str = None) -> str:
    hkt = timezone(timedelta(hours=8))
    now_hkt = datetime.now(hkt)

    if last_update_iso:
        last_dt = datetime.fromisoformat(last_update_iso).astimezone(hkt)
    else:
        last_dt = now_hkt

    # 24h + rand(-3h, +3h)
    random_offset_hours = random.uniform(-3.0, 3.0)
    next_dt = last_dt + timedelta(hours=24 + random_offset_hours)

    # Floor at 00:00 HKT and cap at 23:59 HKT for that target day
    target_date = next_dt.date()
    day_start = datetime.combine(target_date, datetime.min.time(), tzinfo=hkt)
    day_end = datetime.combine(target_date, datetime.max.time(), tzinfo=hkt)

    clamped_dt = max(day_start, min(next_dt, day_end))
    return clamped_dt.isoformat()
    

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
                return event_data.get("client_payload", {})
        except Exception as e:
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

    item_guid = f"{stream_key}-ch-{current_index:03d}"
    fe = fg.add_entry()
    fe.id(item_guid)
    fe.title(f"[{stream_cfg.get('feed_title', stream_key.title())}] {active_pdf.stem}")
    fe.link(href=card_web_url)
    fe.description(f"Tap to read chapter: {active_pdf.name}")
    fe.enclosure(url=pdf_url, length=str(active_pdf.stat().st_size), type="application/pdf")


def process_pdf_folder(stream_key: str, stream_cfg: dict, stream_history: dict, fg, base_url: str, dispatch_payload: dict = None):
    folder_path = Path(stream_cfg.get("folder", ""))
    if not folder_path.exists():
        print(f"[{stream_key}] Folder '{folder_path}' does not exist.")
        return

    # 1. Process incoming completion dispatches
    completed_files = set(stream_history.get("completed_files", []))
    is_dispatch = bool(dispatch_payload and dispatch_payload.get("stream") == stream_key)

    if is_dispatch and dispatch_payload.get("event_type") == "pdf_completed":
        completed_id = dispatch_payload.get("pdf_id")
        if completed_id:
            completed_files.add(completed_id)
            stream_history["completed_files"] = list(completed_files)
            print(f"[{stream_key}] Marked PDF completed: {completed_id}")

    # 2. Check stagger timing
    now_hkt = datetime.now(timezone(timedelta(hours=8)))
    next_update_str = stream_history.get("next_update_at")

    cards_dir = Path("cards")
    cards_dir.mkdir(exist_ok=True)
    batch_json_path = cards_dir / f"{stream_key}_pdf_batch.json"

    # If skipping build, re-publish current batch to RSS so feed stays populated
    if next_update_str and not is_dispatch:
        next_update_dt = datetime.fromisoformat(next_update_str)
        if now_hkt < next_update_dt and batch_json_path.exists():
            try:
                with open(batch_json_path, "r", encoding="utf-8") as f:
                    existing_data = json.load(f)
                batch_items = existing_data.get("batch", [])
                card_count = len(batch_items)
                titles_summary = ", ".join([item["title"] for item in batch_items]) if batch_items else "All PDFs completed!"
                web_reader_url = f"{base_url}/pdf_reader.html?stream={stream_key}"
                now_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

                fe = fg.add_entry()
                fe.id(f"{stream_key}-pdf-batch-{now_date}")
                fe.title(f"[{stream_cfg.get('feed_title', stream_key.title())}] {card_count} PDFs Queued")
                fe.link(href=web_reader_url)
                fe.description(f"Today's queue ({card_count} remaining): {titles_summary}")
                fe.pubDate(datetime.now(timezone.utc) - timedelta(minutes=5))
                return
            except Exception as e:
                print(f"[{stream_key}] Error reading batch JSON: {e}")

    # 3. Gather unread PDFs and generate batch
    all_pdfs = sorted([f for f in folder_path.glob("*.pdf")])
    unread_pdfs = [f for f in all_pdfs if f.name not in completed_files]

    mode = stream_cfg.get("mode", "sequential")
    batch_size = stream_cfg.get("batch_size", 5)

    if mode == "random_without_replacement":
        today_seed = datetime.now(timezone.utc).strftime("%Y%m%d") + stream_key
        rng = random.Random(today_seed)
        shuffled = unread_pdfs.copy()
        rng.shuffle(shuffled)
        batch = shuffled[:batch_size]
    else:
        batch = unread_pdfs[:batch_size]

    batch_items = [
        {
            "id": f.name,
            "title": f.stem.replace("_", " ").title(),
            "pdf_url": f"{base_url}/{folder_path.name}/{f.name}"
        }
        for f in batch
    ]

    compiled_payload = {
        "stream": stream_key,
        "title": stream_cfg.get("feed_title", stream_key.title()),
        "compiled_at": datetime.now(timezone.utc).isoformat(),
        "total_unread_remaining": len(unread_pdfs),
        "batch": batch_items
    }

    with open(batch_json_path, "w", encoding="utf-8") as json_out:
        json.dump(compiled_payload, json_out, indent=2)

    now_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    card_count = len(batch_items)
    titles_summary = ", ".join([item["title"] for item in batch_items]) if batch_items else "All PDFs completed!"
    web_reader_url = f"{base_url}/pdf_reader.html?stream={stream_key}"

    fe = fg.add_entry()
    fe.id(f"{stream_key}-pdf-batch-{now_date}")
    fe.title(f"[{stream_cfg.get('feed_title', stream_key.title())}] {card_count} PDFs Queued")
    fe.link(href=web_reader_url)
    fe.description(f"Today's queue ({card_count} remaining): {titles_summary}")
    fe.pubDate(datetime.now(timezone.utc) - timedelta(minutes=5))

    stream_history["next_update_at"] = get_next_update_time(now_hkt.isoformat())

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
        elif stream_type == "pdf_folder":
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

def process_anki_deck(stream_key: str, stream_cfg: dict, stream_history: dict, fg, base_url: str, dispatch_payload: dict):
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

    # 2. Process Session Batch Dispatch from Web App
    if dispatch_payload.get("stream") == stream_key and dispatch_payload.get("event_type") == "anki_session":
        session_results = dispatch_payload.get("results", [])
        for res in session_results:
            cid = res["id"]
            stream_history[cid] = {
                "state": res["state"],
                "easiness_factor": res["easiness_factor"],
                "interval": res["interval"],
                "repetitions": res["repetitions"],
                "last_reviewed": datetime.now(timezone.utc).isoformat(),
                "next_due": res["next_due"]
            }
        print(f"[{stream_key}] Processed Anki review session: {len(session_results)} cards updated.")

    # 3. Filter Due Cards via SM-2 History
    now_iso = datetime.now(timezone.utc).isoformat()
    due_cards = []
    new_cards = []

    for card in all_cards:
        cid = card["id"]
        c_history = stream_history.get(cid)
        
        if not c_history:
            new_cards.append(card)
        elif c_history.get("next_due", "") <= now_iso:
            card["sm2"] = c_history
            due_cards.append(card)

    # Limit new cards per day according to config
    new_limit = stream_cfg.get("new_cards_per_day", 5)
    selected_new = new_cards[:new_limit]
    for c in selected_new:
        c["sm2"] = {
            "state": "new",
            "easiness_factor": 2.5,
            "interval": 0,
            "repetitions": 0
        }

    session_queue = due_cards + selected_new
    if not session_queue:
        print(f"[{stream_key}] No cards due today!")

    # Shuffle the queue so cards don't appear in CSV order
    random.shuffle(session_queue)

    # 4. Save Compiled Deck JSON for the Web App Reviewer
    cards_dir = Path("cards")
    cards_dir.mkdir(exist_ok=True)
    deck_json_path = cards_dir / f"{stream_key}_deck.json"
    
    compiled_payload = {
        "deck_id": stream_key,
        "title": stream_cfg.get("feed_title", stream_key.title()),
        "compiled_at": datetime.now(timezone.utc).isoformat(),
        "cards": session_queue
    }
    
    with open(deck_json_path, "w", encoding="utf-8") as f:
        json.dump(compiled_payload, f, indent=2)

    # 5. Generate Single Daily RSS Item
    web_reviewer_url = f"{base_url}/reviewer.html?deck={stream_key}"
    card_count = len(session_queue)
    
    fe = fg.add_entry()
    fe.id(f"{stream_key}-session-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}")
    fe.title(f"[{stream_cfg.get('feed_title', stream_key.title())}] {card_count} Cards Due")
    fe.link(href=web_reviewer_url)
    fe.description(f"Tap to start today's review session ({card_count} cards pending).")
    fe.pubDate(datetime.now(timezone.utc) - timedelta(minutes=5))

    # 6. Export RSS file
    rss_filepath = Path(f"{stream_key}.xml")
    # fg.rss_file(rss_filepath, pretty=True)
    print(f"[{stream_key}] Saved feed to {rss_filepath}")

if __name__ == "__main__":
    main()
