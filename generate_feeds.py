import csv
import json
import random
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

# --- PDF SELECTION LOGIC ---

def get_pdf_files(folder_path: Path, strategy: str) -> list[Path]:
    if not folder_path.exists():
        print(f"Warning: Folder {folder_path} does not exist.")
        return []
    
    pdfs = list(folder_path.glob("*.pdf"))
    if strategy in ["sequential", "alphabetical"]:
        pdfs.sort(key=lambda p: p.name.lower())
    return pdfs

def select_pdf_batch(pdfs: list[Path], stream_key: str, stream_history: dict, strategy: str, daily_n: int) -> list[Path]:
    if not pdfs:
        return []

    shown_set = set(stream_history.get("shown_files", []))

    if strategy in ["sequential", "alphabetical"]:
        last_index = stream_history.get("last_index", 0)
        selected = pdfs[last_index : last_index + daily_n]
        stream_history["last_index"] = last_index + len(selected)
        return selected

    elif strategy == "random_without_replacement":
        remaining = [p for p in pdfs if p.name not in shown_set]
        # Reset pool if all files have been read
        if len(remaining) < daily_n:
            shown_set.clear()
            remaining = pdfs
        
        selected = random.sample(remaining, min(daily_n, len(remaining)))
        for p in selected:
            shown_set.add(p.name)
        stream_history["shown_files"] = list(shown_set)
        return selected

    elif strategy == "random_with_replacement":
        return random.choices(pdfs, k=min(daily_n, len(pdfs)))

    return pdfs[:daily_n]

# --- HTML FLASHCARD MAKER ---

def create_html_card(stream_key: str, card: dict) -> str:
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

# --- MAIN GENERATOR ---

def main():
    config = load_json(CONFIG_PATH)
    master_history = load_json(HISTORY_PATH)
    streams = config.get("streams", {})

    for stream_key, stream_cfg in streams.items():
        stream_type = stream_cfg.get("type", "flashcard")
        stream_history = master_history.setdefault(stream_key, {})
        now = datetime.now(timezone.utc)
        pub_time = now - timedelta(minutes=10)

        xml_filename = Path(f"{stream_key}.xml")
        feed_url = f"{BASE_URL}/{xml_filename}"

        fg = FeedGenerator()
        fg.id(feed_url)
        fg.title(stream_cfg.get("feed_title", stream_key))
        fg.description(f"Daily feed for {stream_key}")
        fg.link(href=feed_url, rel="self")
        fg.language("en")

        if stream_type == "pdf_folder":
            folder = Path(stream_cfg["folder"])
            strategy = stream_cfg.get("strategy", "sequential")
            daily_n = stream_cfg.get("daily_n", 1)
            
            pdfs = get_pdf_files(folder, strategy)
            batch = select_pdf_batch(pdfs, stream_key, stream_history, strategy, daily_n)

            for pdf_file in batch:
                pdf_url = f"{BASE_URL}/{folder.name}/{pdf_file.name}"
                item_guid = f"{stream_key}-{pdf_file.stem}"

                fe = fg.add_entry()
                fe.id(item_guid)
                fe.title(f"[{stream_cfg.get('feed_title')}] {pdf_file.stem}")
                fe.link(href=pdf_url)
                fe.description(f"Tap to read PDF document: {pdf_file.name}")
                fe.enclosure(url=pdf_url, length=str(pdf_file.stat().st_size), type="application/pdf")
                fe.pubDate(pub_time)

        elif stream_type == "flashcard":
            # Handles CSV Flashcards
            csv_path = Path(stream_cfg["csv_file"])
            if not csv_path.exists():
                continue
            
            cards = []
            with open(csv_path, mode="r", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    cards.append({"id": row["id"].strip(), "prompt": row["prompt"].strip(), "answer": row["answer"].strip()})

            daily_n = stream_cfg.get("daily_n", 10)
            # Pick simple candidate batch
            selected_cards = cards[:daily_n]  

            for card in selected_cards:
                card_web_url = create_html_card(stream_key, card)
                item_guid = f"{stream_key}-{card['id']}"

                fe = fg.add_entry()
                fe.id(item_guid)
                fe.link(href=card_web_url)
                fe.title(f"{stream_key.title()}")
                fe.description(f"Tap to view flashcard -> {card['prompt']}")
                fe.pubDate(pub_time)

        fg.rss_file(str(xml_filename), pretty=True)
        print(f"Generated {stream_key}.xml")

    save_json(HISTORY_PATH, master_history)

if __name__ == "__main__":
    main()