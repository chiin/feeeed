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

def create_flashcard_html(stream_key: str, card: dict, box: int, github_pat: str = "") -> str:
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

    async function gradeCard(grade) {{
        const failBtn = document.getElementById('failBtn');
        const passBtn = document.getElementById('passBtn');
        const statusMsg = document.getElementById('statusMsg');

        failBtn.disabled = true;
        passBtn.disabled = true;
        statusMsg.innerText = "Saving grade...";
        statusMsg.style.display = 'block';

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

    function getPAT() {
        let token = localStorage.getItem("feeeed_pat");
        if (!token) {
            token = prompt("Enter your GitHub PAT (saved on your device):");
            if (token) {
                token = token.trim();
                localStorage.setItem("feeeed_pat", token);
            }
        }
        return token;
    }

    async function markFinished() {
        const btn = document.getElementById('finishBtn');
        btn.innerText = "Updating...";
        btn.disabled = true;
    
        const TOKEN = getPAT();
        if (!TOKEN) {
            btn.innerText = "PAT Required";
            btn.disabled = false;
            return;
        }
    
        try {
            const response = await fetch("https://api.github.com/repos/chiin/feeeed/dispatches", {
                method: "POST",
                headers: {
                    "Accept": "application/vnd.github+json",
                    "Authorization": `Bearer ${TOKEN}`,
                    "Content-Type": "application/json"
                },
                body: JSON.stringify({
                    event_type: "advance_chapter",
                    client_payload: { stream: "{stream_key}" }
                })
            });
    
            if (response.ok) {
                btn.innerText = "Done!";
                btn.style.backgroundColor = "#0a84ff";
            } else if (response.status === 401) {
                alert("Invalid PAT. Clearing saved token.");
                localStorage.removeItem("feeeed_pat");
                btn.innerText = "Try Again";
                btn.disabled = false;
            } else {
                btn.innerText = "Error (" + response.status + ")";
                btn.disabled = false;
            }
        } catch (err) {
            btn.innerText = "Network Error";
            btn.disabled = false;
        }
    }
    </script>
</body>
</html>"""

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html_content)

    return f"{BASE_URL}/cards/{filename}"

# --- STREAM PROCESSORS ---

def process_flashcards(stream_key: str, stream_cfg: dict, stream_history: dict, fg, dispatch_payload: dict, github_pat: str):
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

        card_web_url = create_flashcard_html(stream_key, card, box, github_pat)
        item_guid = f"{stream_key}-{cid}"

        fe = fg.add_entry()
        fe.id(item_guid)
        fe.link(href=card_web_url)
        fe.title(f"[{stream_key.title()}] Box {box}")
        fe.description(f"Tap to review flashcard -> {card['prompt']}")
        fe.pubDate(pub_time)


def process_book_queue(stream_key: str, stream_cfg: dict, stream_history: dict, fg, dispatch_payload: dict, github_pat: str):
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

    last_index = stream_history.get("last_index", 0)
    batch = pdfs[last_index : last_index + daily_n]
    stream_history["last_index"] = last_index + len(batch)

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

# --- MAIN CONTROLLER ---

def main():
    config = load_json(CONFIG_PATH)
    master_history = load_json(HISTORY_PATH)
    streams = config.get("streams", {})

    dispatch_payload = get_dispatch_payload()
    github_pat = os.environ.get("GH_PAT", "")

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
            process_flashcards(stream_key, stream_cfg, stream_history, fg, dispatch_payload, github_pat)
        elif stream_type == "book_queue":
            process_book_queue(stream_key, stream_cfg, stream_history, fg, dispatch_payload, github_pat)
        elif stream_type == "pdf_folder":
            process_pdf_folder(stream_key, stream_cfg, stream_history, fg)

        fg.rss_file(str(xml_filename), pretty=True)
        print(f"Generated {xml_filename}")

    save_json(HISTORY_PATH, master_history)

if __name__ == "__main__":
    main()
