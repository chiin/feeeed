import csv
import json
import random
from datetime import datetime, timezone, timedelta
from pathlib import Path
from feedgen.feed import FeedGenerator

# File Paths
CONFIG_PATH = Path("flashcard_config.json")
HISTORY_PATH = Path("history.json")
BASE_URL = "https://chiin.github.io/feeeed"

# Leitner Box intervals (in days)
BOX_INTERVALS = {1: 1, 2: 3, 3: 7, 4: 14, 5: 30}

def load_json(path: Path) -> dict:
    if path.exists():
        with open(path, mode="r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_json(path: Path, data: dict):
    with open(path, mode="w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def load_csv_cards(csv_path: Path) -> list[dict]:
    if not csv_path.exists():
        print(f"Warning: CSV file {csv_path} not found. Skipping stream.")
        return []
    cards = []
    with open(csv_path, mode="r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            cards.append({
                "id": row["id"].strip(),
                "prompt": row["prompt"].strip(),
                "answer": row["answer"].strip()
            })
    return cards

def select_batch(cards: list[dict], stream_history: dict, batch_size: int) -> list[dict]:
    now = datetime.now(timezone.utc)
    eligible_candidates = []
    
    for card in cards:
        card_id = card["id"]
        card_state = stream_history.get(card_id)
        
        if not card_state:
            eligible_candidates.append({"card": card, "urgency": 999.0})
            continue

        box = card_state.get("box", 1)
        last_shown = datetime.fromisoformat(card_state["last_shown"])
        required_interval = timedelta(days=BOX_INTERVALS.get(box, 30))
        time_elapsed = now - last_shown
        
        if time_elapsed >= required_interval:
            urgency = time_elapsed.total_seconds() / required_interval.total_seconds()
            eligible_candidates.append({"card": card, "urgency": urgency})

    if len(eligible_candidates) < batch_size:
        all_candidates = []
        for card in cards:
            card_id = card["id"]
            card_state = stream_history.get(card_id, {})
            box = card_state.get("box", 1)
            last_shown_str = card_state.get("last_shown")
            
            if not last_shown_str:
                urgency = 999.0
            else:
                last_shown = datetime.fromisoformat(last_shown_str)
                required_interval = timedelta(days=BOX_INTERVALS.get(box, 30))
                urgency = (now - last_shown).total_seconds() / required_interval.total_seconds()
            all_candidates.append({"card": card, "urgency": urgency})
            
        all_candidates.sort(key=lambda x: x["urgency"], reverse=True)
        selected_units = all_candidates[:batch_size]
    else:
        eligible_candidates.sort(key=lambda x: x["urgency"], reverse=True)
        selected_units = eligible_candidates[:batch_size]

    selected_cards = [unit["card"] for unit in selected_units]
    random.shuffle(selected_cards)
    return selected_cards

def generate_stream_xml(stream_key: str, stream_config: dict, batch: list[dict], stream_history: dict) -> Path:
    now = datetime.now(timezone.utc)
    # Use a slight offset (10 mins ago) to avoid client clock sync issues
    pub_time = now - timedelta(minutes=10)
    
    xml_filename = Path(f"{stream_key}.xml")
    feed_url = f"{BASE_URL}/{xml_filename}"

    fg = FeedGenerator()
    fg.id(feed_url)
    fg.title(stream_config.get("feed_title", f"Flashcards - {stream_key}"))
    fg.description(f"Daily Leitner deck for {stream_key}.")
    fg.link(href=feed_url, rel="self")
    fg.language("en")

    for card in batch:
        card_id = card["id"]
        card_state = stream_history.get(card_id, {"box": 1, "times_shown": 0})
        
        current_box = card_state.get("box", 1)
        times_shown = card_state.get("times_shown", 0) + 1
        next_box = min(current_box + 1, 5) if times_shown > 1 else current_box

        stream_history[card_id] = {
            "box": next_box,
            "last_shown": now.isoformat(),
            "times_shown": times_shown
        }

        # Unique identifier version per run
        item_guid = f"{stream_key}-{card_id}-v{times_shown}"

        fe = fg.add_entry()
        # 1. Unique ID / GUID
        fe.id(item_guid)
        
        # 2. Explicit Item Link (REQUIRED by feeeed parser)
        fe.link(href=f"{feed_url}#{item_guid}")
        
        # 3. Card Title
        fe.title(f"{stream_key.title()}")
        
        # 4. Content / Description Body
        card_html = (
            f"<p><strong>Q: {card['prompt']}</strong></p>"
            f"<details><summary>💡 Click to reveal answer</summary><p>{card['answer']}</p></details>"
        )
        fe.description(card_html)
        
        # 5. Timestamp
        fe.pubDate(pub_time)

    fg.rss_file(str(xml_filename), pretty=True)
    return xml_filename

def main():
    config = load_json(CONFIG_PATH)
    master_history = load_json(HISTORY_PATH)
    streams = config.get("streams", {})

    if not streams:
        print("No streams found in flashcard_config.json")
        return

    for stream_key, stream_config in streams.items():
        csv_path = Path(stream_config["csv_file"])
        daily_n = stream_config.get("daily_n", 20)
        
        cards = load_csv_cards(csv_path)
        if not cards:
            continue

        # Get or create isolated history namespace for this stream
        stream_history = master_history.setdefault(stream_key, {})
        
        batch = select_batch(cards, stream_history, daily_n)
        xml_file = generate_stream_xml(stream_key, stream_config, batch, stream_history)
        
        print(f"Generated {len(batch)} cards -> {BASE_URL}/{xml_file.name}")

    save_json(HISTORY_PATH, master_history)

if __name__ == "__main__":
    main()
