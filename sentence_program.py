from __future__ import annotations

import json
import os
import hashlib
import random
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Protocol

from anki_scheduler import hkt_day, isoformat_utc, parse_datetime


PROGRAM_SCHEMA_VERSION = 1
GENERATION_SCHEMA_VERSION = 1
CONTENT_SCHEMA_VERSION = 1
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
PASSING_RATINGS = {"good", "easy"}
INTRODUCTION_STRATEGIES = {"sentence_first", "vocabulary_first"}


class SentenceGenerator(Protocol):
    def generate(self, request: dict) -> list[dict]:
        ...


class DeterministicSentenceGenerator:
    """Test generator; production configuration never selects this provider."""

    def generate(self, request: dict) -> list[dict]:
        sentences = []
        for target in request["targets"]:
            surface = target["surface_form"]
            sentences.append(
                {
                    "target_word_id": target["id"],
                    "target_occurrence": surface,
                    "primary_text": f"我會在句子中使用「{surface}」。",
                    "transliteration": f"Mock transliteration for {surface}",
                    "translation": f"I will use “{surface}” in a sentence.",
                    "cloze_text": "我會在句子中使用「[…]」。",
                    "target_breakdown": (
                        f"{surface}: {target['translation']}"
                    ),
                    "discovered_vocabulary": [],
                }
            )
        return sentences


class OpenRouterSentenceGenerator:
    def __init__(
        self,
        api_key: str,
        model: str,
        timeout_seconds: int = 90,
    ) -> None:
        if not api_key:
            raise ValueError("OpenRouter API key must not be empty")
        if not model:
            raise ValueError("OpenRouter model must not be empty")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds

    def generate(self, request: dict) -> list[dict]:
        targets = request["targets"]
        target_lines = "\n".join(
            f"- {item['id']}: {item['surface_form']} ({item['translation']})"
            for item in targets
        )
        known_words = ", ".join(request["known_words"])
        familiar_pool_policy = request.get("familiar_pool_policy", "restricted")
        if familiar_pool_policy == "natural_priority":
            vocabulary_instruction = f"""
Use familiar vocabulary where it fits naturally:
{known_words}

You may freely use basic vocabulary outside that pool. Natural, grammatical,
communicative language is more important than familiar-word coverage. Write one
short everyday sentence with a clear meaning. Never output a word list,
enumeration, greeting used as filler, or a chain of contrasts added only to use
known words.
""".strip()
        else:
            vocabulary_instruction = f"""
At least 85 percent of the surrounding vocabulary should come from this
familiar pool:
{known_words}
""".strip()
        target_instruction = (
            "Use a grammatically natural inflected form of each target when "
            "appropriate. Return that exact substring as \"target_occurrence\"."
            if request.get("allow_inflected_targets", False)
            else "Each sentence must contain its target surface form exactly. "
            "Return that form as \"target_occurrence\"."
        )
        system_prompt = (
            "You are an expert language-pedagogy engine. Generate natural, "
            "grammatical, communicative practice sentences. Return JSON only."
            if familiar_pool_policy == "natural_priority"
            else "You are an expert language-pedagogy engine. Generate natural "
            "practice sentences using restricted vocabulary. Return JSON only."
        )
        user_prompt = f"""
Language: {request['language_code']}
Style: {request['prompt_style']}
Orthography: {request['orthography']}
Transliteration/pronunciation field: {request.get('transliteration_style', 'helpful pronunciation guidance')}

Create exactly one sentence for each target:
{target_lines}

{vocabulary_instruction}

{target_instruction} Return an object with a
"sentences" array. Every item must contain exactly these string fields:
"target_word_id", "target_occurrence", "primary_text", "transliteration", "translation",
"cloze_text", and "target_breakdown".

The translation must express the same coherent sentence. The target breakdown
must explain only the target's meaning and grammar in this sentence; do not add
etymology.

Also return "discovered_vocabulary" as an array containing at most one useful
content word introduced outside the familiar pool, or an empty array. A
candidate must contain exactly "surface_form" (dictionary form),
"observed_form" (the exact form in the sentence), and "translation". Do not
propose function words, proper names, the target itself, or familiar words.
""".strip()
        schema = {
            "name": "sentence_batch",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["sentences"],
                "properties": {
                    "sentences": {
                        "type": "array",
                        "minItems": len(targets),
                        "maxItems": len(targets),
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": [
                                "target_word_id",
                                "target_occurrence",
                                "primary_text",
                                "transliteration",
                                "translation",
                                "cloze_text",
                                "target_breakdown",
                                "discovered_vocabulary",
                            ],
                            "properties": {
                                **{
                                    key: {"type": "string"}
                                    for key in (
                                        "target_word_id",
                                        "target_occurrence",
                                        "primary_text",
                                        "transliteration",
                                        "translation",
                                        "cloze_text",
                                        "target_breakdown",
                                    )
                                },
                                "discovered_vocabulary": {
                                    "type": "array",
                                    "maxItems": 1,
                                    "items": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "required": [
                                            "surface_form",
                                            "observed_form",
                                            "translation",
                                        ],
                                        "properties": {
                                            key: {"type": "string"}
                                            for key in (
                                                "surface_form",
                                                "observed_form",
                                                "translation",
                                            )
                                        },
                                    },
                                },
                            },
                        },
                    }
                },
            },
        }
        payload = {
            "model": self.model,
            "temperature": 0.3,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": schema,
            },
        }
        http_request = urllib.request.Request(
            OPENROUTER_URL,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/chiin/feeeed",
                "X-Title": "Feeeed Sentence Generator",
            },
        )
        try:
            with urllib.request.urlopen(
                http_request, timeout=self.timeout_seconds
            ) as response:
                response_data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read(1000).decode("utf-8", errors="replace")
            raise RuntimeError(
                f"OpenRouter request failed with HTTP {error.code}: {detail}"
            ) from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"OpenRouter request failed: {error.reason}") from error
        except json.JSONDecodeError as error:
            raise ValueError("OpenRouter returned invalid response JSON") from error

        try:
            message_content = response_data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise ValueError("OpenRouter response did not contain message content") from error
        if not isinstance(message_content, str):
            raise ValueError("OpenRouter message content must be a JSON string")
        try:
            generated = json.loads(message_content)
        except json.JSONDecodeError as error:
            raise ValueError("OpenRouter message content was not valid JSON") from error
        if not isinstance(generated, dict):
            raise ValueError("OpenRouter generated content must be an object")
        return generated.get("sentences")


def load_sentence_content(path: Path, program_id: str) -> dict:
    if not path.exists():
        return {
            "schema_version": CONTENT_SCHEMA_VERSION,
            "program_id": program_id,
            "sentences": [],
        }
    with path.open(mode="r", encoding="utf-8") as file:
        content = json.load(file)
    if not isinstance(content, dict):
        raise ValueError(f"sentence content must be an object: {path}")
    if content.get("schema_version") != CONTENT_SCHEMA_VERSION:
        raise ValueError(f"unsupported sentence content schema: {path}")
    if content.get("program_id") != program_id:
        raise ValueError(f"sentence content belongs to another program: {path}")
    if not isinstance(content.get("sentences"), list):
        raise ValueError(f"sentence content must contain a sentences array: {path}")
    return content


def resolve_sentence_content_path(
    repository_root: Path,
    configured_path: str,
) -> Path:
    if not isinstance(configured_path, str) or not configured_path:
        raise ValueError("sentence content_path must be a non-empty string")
    relative_path = Path(configured_path)
    if relative_path.is_absolute():
        raise ValueError("sentence content_path must be repository-relative")
    root = repository_root.resolve()
    path = (root / relative_path).resolve()
    try:
        within_repository = path.relative_to(root)
    except ValueError as error:
        raise ValueError("sentence content_path escapes repository root") from error
    if (
        not within_repository.parts
        or within_repository.parts[0] != "generated"
        or path.suffix.lower() != ".json"
    ):
        raise ValueError("sentence content_path must be a JSON file inside generated/")
    return path


def save_sentence_content(path: Path, content: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.tmp")
    try:
        with temporary_path.open(mode="w", encoding="utf-8") as file:
            json.dump(content, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def sentence_cards(content: dict) -> list[dict]:
    cards = []
    for sentence in content["sentences"]:
        if sentence.get("status") != "active":
            continue
        payload = sentence["payload"]
        notes = "\n\n".join(
            item
            for item in (
                payload.get("transliteration"),
                payload.get("target_breakdown"),
            )
            if item
        ) or None
        cards.append(
            {
                "id": sentence["id"],
                "front": {
                    "text": payload["primary_text"],
                    "audio": payload.get("audio_url"),
                    "image": None,
                },
                "back": {
                    "text": payload["translation"],
                    "notes": notes,
                },
            }
        )
    return cards


def promoted_word_ids(program_state: dict) -> set[str]:
    vocabulary = program_state.get("vocabulary", {})
    return {
        word_id
        for word_id, item in vocabulary.items()
        if item.get("status") in {"active_anki", "mastered"}
    }


def eligible_source_word_ids(program_state: dict) -> set[str]:
    return promoted_word_ids(program_state) | set(
        program_state.get("grandfathered_word_ids", [])
    )


def ensure_combined_batch(
    program_id: str,
    program_state: dict,
    source_snapshots: list[dict],
    now: datetime,
) -> dict:
    _migrate_program_state(program_state)
    day = hkt_day(now).isoformat()
    existing = program_state.get("combined_batch")
    if existing and existing.get("date") == day:
        return existing

    members = [
        {
            "deck_id": snapshot["deck_id"],
            "card_id": card["id"],
        }
        for snapshot in source_snapshots
        for card in snapshot.get("cards", [])
    ]
    seed_material = f"{program_id}:{day}".encode("utf-8")
    seed = int.from_bytes(hashlib.sha256(seed_material).digest(), "big")
    random.Random(seed).shuffle(members)
    if existing:
        program_state["previous_combined_batch"] = existing
    combined_batch = {
        "id": day,
        "date": day,
        "created_at": isoformat_utc(now),
        "source_batch_ids": {
            snapshot["deck_id"]: snapshot["batch_id"]
            for snapshot in source_snapshots
        },
        "members": members,
    }
    program_state["combined_batch"] = combined_batch
    program_state["revision"] += 1
    return combined_batch


def build_combined_snapshot(
    program_id: str,
    title: str,
    program_state: dict,
    source_snapshots: list[dict],
    now: datetime,
) -> dict:
    batch = ensure_combined_batch(
        program_id,
        program_state,
        source_snapshots,
        now,
    )
    source_cards = {
        (snapshot["deck_id"], card["id"]): {
            **card,
            "deck_id": snapshot["deck_id"],
            "deck_title": snapshot["title"],
            "front_text_scale": snapshot.get("front_text_scale", 1),
        }
        for snapshot in source_snapshots
        for card in snapshot.get("cards", [])
    }
    cards = [
        source_cards[(member["deck_id"], member["card_id"])]
        for member in batch["members"]
        if (member["deck_id"], member["card_id"]) in source_cards
    ]
    processed_event_ids = []
    seen_event_ids = set()
    for snapshot in source_snapshots:
        for event_id in snapshot.get("processed_event_ids", []):
            if event_id not in seen_event_ids:
                seen_event_ids.add(event_id)
                processed_event_ids.append(event_id)
    return {
        "schema_version": 1,
        "program_id": program_id,
        "title": title,
        "batch_id": batch["id"],
        "batch_date": batch["date"],
        "compiled_at": isoformat_utc(now),
        "state_revision": program_state["revision"],
        "source_revisions": {
            snapshot["deck_id"]: snapshot["state_revision"]
            for snapshot in source_snapshots
        },
        "processed_event_ids": processed_event_ids,
        "cards": cards,
    }


def _migrate_program_state(program_state: dict) -> None:
    schema_version = program_state.get("schema_version")
    if schema_version not in (None, PROGRAM_SCHEMA_VERSION):
        raise ValueError("unsupported sentence program schema version")
    program_state.setdefault("schema_version", PROGRAM_SCHEMA_VERSION)
    program_state.setdefault("revision", 0)
    program_state.setdefault("vocabulary", {})
    program_state.setdefault("processed_events", {})
    program_state.setdefault("vocabulary_candidates", {})
    program_state.setdefault("processed_candidate_events", {})
    program_state.setdefault("grandfathered_word_ids", [])
    program_state.setdefault("source_gate_initialized", False)
    if not isinstance(program_state["vocabulary"], dict):
        raise ValueError("program vocabulary state must be an object")
    if not isinstance(program_state["processed_events"], dict):
        raise ValueError("program processed_events state must be an object")
    if not isinstance(program_state["vocabulary_candidates"], dict):
        raise ValueError("program vocabulary_candidates state must be an object")
    if not isinstance(program_state["processed_candidate_events"], dict):
        raise ValueError("program processed_candidate_events state must be an object")
    if not (
        isinstance(program_state["grandfathered_word_ids"], list)
        and all(
            isinstance(word_id, str)
            for word_id in program_state["grandfathered_word_ids"]
        )
    ):
        raise ValueError("program grandfathered_word_ids must be a string array")
    if not isinstance(program_state["source_gate_initialized"], bool):
        raise ValueError("program source_gate_initialized must be a boolean")


def _migrate_generation_state(generation_state: dict) -> None:
    schema_version = generation_state.get("schema_version")
    if schema_version not in (None, GENERATION_SCHEMA_VERSION):
        raise ValueError("unsupported sentence generation schema version")
    generation_state.setdefault("schema_version", GENERATION_SCHEMA_VERSION)
    generation_state.setdefault("jobs", {})
    if not isinstance(generation_state["jobs"], dict):
        raise ValueError("generation jobs state must be an object")


def _source_vocabulary(source_cards: list[dict]) -> dict[str, dict]:
    vocabulary = {}
    for card in source_cards:
        word_id = card.get("id")
        surface = card.get("front", {}).get("text")
        translation = card.get("back", {}).get("text")
        if not all(isinstance(value, str) and value for value in (
            word_id,
            surface,
            translation,
        )):
            raise ValueError("source vocabulary cards require id, front text, and back text")
        if word_id in vocabulary:
            raise ValueError(f"duplicate source vocabulary ID: {word_id}")
        entry_type = card.get("entry_type", "term")
        if entry_type not in {"term", "word", "phrase", "sentence"}:
            raise ValueError(f"unsupported vocabulary entry type: {entry_type}")
        vocabulary[word_id] = {
            "id": word_id,
            "surface_form": surface,
            "translation": translation,
            "entry_type": entry_type,
            "notes": card.get("back", {}).get("notes"),
        }
    return vocabulary


def introduction_strategy(config: dict) -> str:
    policy = config.get("vocabulary_introduction", {})
    if not isinstance(policy, dict):
        raise ValueError("vocabulary_introduction must be an object")
    strategy = policy.get("strategy")
    if strategy is None:
        strategy = (
            "sentence_first"
            if config.get("control_source_new_cards", True)
            else "vocabulary_first"
        )
    if strategy not in INTRODUCTION_STRATEGIES:
        raise ValueError(f"unsupported vocabulary introduction strategy: {strategy}")
    return strategy


def controls_source_new_cards(config: dict) -> bool:
    return introduction_strategy(config) == "sentence_first"


def _passing_review_count(card_state: dict | None) -> int:
    if not card_state:
        return 0
    if "passing_reviews" in card_state:
        return max(0, int(card_state["passing_reviews"]))
    if card_state.get("state") == "review" and int(card_state.get("reviews", 0)) > 0:
        return 1
    return 0


def _known_words(
    vocabulary: dict[str, dict],
    source_scheduler_cards: dict,
    tracked_vocabulary: dict,
    minimum_passing_reviews: int,
    limit: int,
) -> list[str]:
    return [
        item["surface_form"]
        for word_id, item in vocabulary.items()
        if (
            _passing_review_count(source_scheduler_cards.get(word_id))
            >= minimum_passing_reviews
            or tracked_vocabulary.get(word_id, {}).get("status")
            in {"active_anki", "mastered"}
        )
    ][:limit]


def _catalog_context_words(
    program_id: str,
    day: str,
    vocabulary: dict[str, dict],
    limit: int,
) -> list[str]:
    items = list(vocabulary.values())
    seed_material = f"{program_id}:{day}:context".encode("utf-8")
    seed = int.from_bytes(hashlib.sha256(seed_material).digest(), "big")
    random.Random(seed).shuffle(items)
    return [item["surface_form"] for item in items[:limit]]


def _direct_sentence(
    program_id: str,
    day: str,
    target: dict,
    created_at: str,
    strategy: str,
) -> dict:
    return {
        "id": f"{program_id}-{day}-{target['id']}",
        "card_type": "text_reading",
        "target_word_ids": [target["id"]],
        "lifecycle": "disposable_scaffold",
        "status": "active",
        "created_at": created_at,
        "payload": {
            "primary_text": target["surface_form"],
            "transliteration": target.get("notes") or "",
            "translation": target["translation"],
            "cloze_text": "",
            "target_breakdown": "",
        },
        "introduction_strategy": strategy,
        "source": "vocabulary_sentence",
    }


def approved_vocabulary_cards(program_state: dict) -> list[dict]:
    _migrate_program_state(program_state)
    return [
        {
            "id": candidate_id,
            "entry_type": "term",
            "front": {
                "text": candidate["surface_form"],
                "audio": None,
                "image": None,
            },
            "back": {
                "text": candidate["translation"],
                "notes": (
                    f"Discovered in: {candidate['source_sentence_text']}"
                ),
            },
        }
        for candidate_id, candidate in program_state[
            "vocabulary_candidates"
        ].items()
        if candidate.get("status") == "approved"
    ]


def apply_vocabulary_candidate_event(
    program_id: str,
    program_state: dict,
    payload: dict,
    now: datetime,
) -> dict[str, int]:
    counts = {"applied": 0, "duplicate": 0, "stale": 0, "invalid": 0}
    if payload.get("event_type") != "vocabulary_candidate":
        return counts
    _migrate_program_state(program_state)
    required = ("event_id", "program_id", "candidate_id", "action", "occurred_at")
    if any(not payload.get(key) for key in required):
        counts["invalid"] += 1
        return counts
    if payload["program_id"] != program_id:
        return counts
    event_id = payload["event_id"]
    if not isinstance(event_id, str) or len(event_id) > 128:
        counts["invalid"] += 1
        return counts
    processed = program_state["processed_candidate_events"]
    if event_id in processed:
        counts["duplicate"] += 1
        return counts
    processed[event_id] = {
        "processed_at": isoformat_utc(now),
        "status": "invalid",
    }
    try:
        occurred_at = parse_datetime(payload["occurred_at"])
    except (TypeError, ValueError):
        counts["invalid"] += 1
        return counts
    if occurred_at > now.astimezone(timezone.utc) + timedelta(minutes=5):
        counts["invalid"] += 1
        return counts
    action = payload["action"]
    if action not in {"approve", "reject"}:
        counts["invalid"] += 1
        return counts
    candidate = program_state["vocabulary_candidates"].get(
        payload["candidate_id"]
    )
    if not candidate or candidate.get("status") != "pending":
        processed[event_id]["status"] = "stale"
        counts["stale"] += 1
        return counts
    candidate["status"] = "approved" if action == "approve" else "rejected"
    candidate["decided_at"] = isoformat_utc(occurred_at)
    if action == "approve":
        program_state["vocabulary"].setdefault(
            payload["candidate_id"],
            {
                "status": "sentence_complete",
                "sentence_pass_count": 0,
                "introduced_at": candidate["created_at"],
                "introduction_strategy": "sentence_discovery",
            },
        )
    processed[event_id]["status"] = "applied"
    program_state["revision"] += 1
    counts["applied"] += 1
    while len(processed) > 1000:
        del processed[next(iter(processed))]
    return counts


def build_candidate_snapshot(
    program_id: str,
    title: str,
    program_state: dict,
    now: datetime,
) -> dict:
    _migrate_program_state(program_state)
    pending = [
        {"id": candidate_id, **candidate}
        for candidate_id, candidate in program_state[
            "vocabulary_candidates"
        ].items()
        if candidate.get("status") == "pending"
    ]
    return {
        "schema_version": 1,
        "program_id": program_id,
        "title": title,
        "compiled_at": isoformat_utc(now),
        "state_revision": program_state["revision"],
        "processed_event_ids": list(
            program_state["processed_candidate_events"]
        ),
        "candidates": pending,
    }


def _record_discovered_vocabulary(
    program_id: str,
    program_state: dict,
    vocabulary: dict[str, dict],
    sentence_id: str,
    sentence_text: str,
    discovered: list[dict],
    now: datetime,
) -> int:
    known_surfaces = {
        item["surface_form"].strip().casefold() for item in vocabulary.values()
    }
    candidates = program_state["vocabulary_candidates"]
    known_surfaces.update(
        item["surface_form"].strip().casefold() for item in candidates.values()
    )
    added = 0
    for item in discovered:
        normalized = item["surface_form"].strip().casefold()
        if normalized in known_surfaces:
            continue
        digest = hashlib.sha256(
            f"{program_id}:{normalized}".encode("utf-8")
        ).hexdigest()[:16]
        candidate_id = f"{program_id}-discovered-{digest}"
        candidates[candidate_id] = {
            "surface_form": item["surface_form"].strip(),
            "observed_form": item["observed_form"].strip(),
            "translation": item["translation"].strip(),
            "source_sentence_id": sentence_id,
            "source_sentence_text": sentence_text,
            "status": "pending",
            "created_at": isoformat_utc(now),
        }
        known_surfaces.add(normalized)
        added += 1
    if added:
        program_state["revision"] += 1
    return added


def _refresh_mastery(
    program_state: dict,
    source_stream_state: dict,
    mastery_interval_days: int,
    now: datetime,
) -> None:
    source_states = source_stream_state.get("cards", {})
    for word_id, item in program_state["vocabulary"].items():
        card_state = source_states.get(word_id)
        if (
            item.get("status") == "active_anki"
            and card_state
            and int(card_state.get("interval_days", 0)) >= mastery_interval_days
        ):
            item["status"] = "mastered"
            item["mastered_at"] = isoformat_utc(now)
            program_state["revision"] += 1


def _reconcile_content_state(
    program_state: dict,
    generation_state: dict,
    content: dict,
    vocabulary: dict[str, dict],
    promotion_threshold: int,
    now: datetime,
) -> None:
    changed = False
    today = hkt_day(now).isoformat()
    today_sentence_ids = []
    for sentence in content["sentences"]:
        sentence_id = sentence.get("id")
        target_ids = sentence.get("target_word_ids")
        created_at = sentence.get("created_at")
        if (
            not isinstance(sentence_id, str)
            or not isinstance(target_ids, list)
            or not target_ids
            or not all(isinstance(word_id, str) for word_id in target_ids)
        ):
            raise ValueError("stored sentence has invalid identity metadata")
        for word_id in target_ids:
            if word_id not in vocabulary:
                raise ValueError(
                    f"stored sentence references unknown source word: {word_id}"
                )
            if word_id not in program_state["vocabulary"]:
                archived = sentence.get("status") == "archived"
                program_state["vocabulary"][word_id] = {
                    "status": "active_anki" if archived else "sentence_preview",
                    "sentence_pass_count": promotion_threshold if archived else 0,
                    "introduced_at": created_at,
                }
                changed = True
        try:
            if hkt_day(parse_datetime(created_at)).isoformat() == today:
                today_sentence_ids.append(sentence_id)
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"stored sentence has invalid created_at: {sentence_id}"
            ) from error

    if today_sentence_ids and today not in generation_state["jobs"]:
        generation_state["jobs"][today] = {
            "status": "completed",
            "completed_at": isoformat_utc(now),
            "generated_count": len(today_sentence_ids),
            "sentence_ids": today_sentence_ids,
            "recovered_from_content": True,
        }
        changed = True
    if changed:
        program_state["revision"] += 1


def apply_sentence_review_results(
    program_id: str,
    sentence_stream_id: str,
    program_state: dict,
    sentence_stream_state: dict,
    content: dict,
    dispatch_payload: dict,
    promotion_threshold: int,
    now: datetime,
) -> dict[str, int]:
    counts = {"applied": 0, "duplicate": 0, "stale": 0, "invalid": 0}
    if (
        dispatch_payload.get("event_type") != "anki_review"
        or dispatch_payload.get("deck_id") != sentence_stream_id
    ):
        return counts
    raw_events = dispatch_payload.get("events")
    if raw_events is None:
        raw_events = [dispatch_payload]
    if not isinstance(raw_events, list):
        raise ValueError(f"[{program_id}] sentence review events must be a list")

    sentences = {
        sentence["id"]: sentence
        for sentence in content["sentences"]
        if isinstance(sentence, dict) and isinstance(sentence.get("id"), str)
    }
    processed_events = program_state["processed_events"]
    scheduler_events = sentence_stream_state.get("processed_events", {})
    for event in sorted(
        raw_events,
        key=lambda item: item.get("reviewed_at", "") if isinstance(item, dict) else "",
    ):
        if not isinstance(event, dict):
            counts["invalid"] += 1
            continue
        required = ("event_id", "deck_id", "card_id", "rating", "reviewed_at")
        if any(not event.get(key) for key in required):
            counts["invalid"] += 1
            continue
        event_id = event["event_id"]
        if not isinstance(event_id, str) or len(event_id) > 128:
            counts["invalid"] += 1
            continue
        if event_id in processed_events:
            counts["duplicate"] += 1
            continue
        scheduler_event = scheduler_events.get(event_id)
        if not scheduler_event:
            counts["invalid"] += 1
            continue
        try:
            reviewed_at = parse_datetime(event["reviewed_at"])
        except (TypeError, ValueError):
            counts["invalid"] += 1
            continue
        if reviewed_at > now.astimezone(timezone.utc) + timedelta(minutes=5):
            counts["invalid"] += 1
            continue
        sentence = sentences.get(event["card_id"])
        processed_events[event_id] = {
            "processed_at": isoformat_utc(now),
            "status": "stale",
        }
        if (
            scheduler_event.get("status") != "applied"
            or not sentence
            or sentence.get("status") != "active"
        ):
            counts["stale"] += 1
            continue

        rating = event["rating"]
        if rating not in {"again", "hard", "good", "easy"}:
            processed_events[event_id]["status"] = "invalid"
            counts["invalid"] += 1
            continue
        if rating in PASSING_RATINGS:
            for word_id in sentence["target_word_ids"]:
                word_state = program_state["vocabulary"].get(word_id)
                if not word_state or word_state.get("status") not in {
                    "sentence_preview",
                    "sentence_reinforcement",
                }:
                    continue
                word_state["sentence_pass_count"] += 1
                word_state["last_sentence_pass_at"] = isoformat_utc(reviewed_at)
                if word_state["sentence_pass_count"] >= promotion_threshold:
                    if word_state["status"] == "sentence_preview":
                        word_state["status"] = "active_anki"
                        word_state["promoted_at"] = isoformat_utc(reviewed_at)
                    else:
                        word_state["status"] = "sentence_complete"
                        word_state["completed_at"] = isoformat_utc(reviewed_at)
                    for candidate in content["sentences"]:
                        if (
                            candidate.get("status") == "active"
                            and word_id in candidate.get("target_word_ids", [])
                        ):
                            candidate["status"] = "archived"
                            candidate["archived_at"] = isoformat_utc(reviewed_at)
            program_state["revision"] += 1
        processed_events[event_id]["status"] = "applied"
        counts["applied"] += 1
    return counts


def _validate_generated_sentences(
    generated: list[dict],
    targets: list[dict],
    allow_inflected_targets: bool = False,
    familiar_pool_policy: str = "restricted",
) -> list[dict]:
    if not isinstance(generated, list):
        raise ValueError("sentence generator must return a list")
    target_by_id = {target["id"]: target for target in targets}
    if len(generated) != len(targets):
        raise ValueError("sentence generator returned the wrong number of sentences")
    validated = []
    seen_ids = set()
    required_fields = {
        "target_word_id",
        "primary_text",
        "transliteration",
        "translation",
        "cloze_text",
        "target_breakdown",
    }
    for sentence in generated:
        if not isinstance(sentence, dict):
            raise ValueError("generated sentence must be an object")
        allowed_fields = required_fields | {
            "target_occurrence",
            "discovered_vocabulary",
        }
        if not required_fields.issubset(sentence) or not set(sentence).issubset(
            allowed_fields
        ):
            raise ValueError("generated sentence has unexpected or missing fields")
        if not all(
            isinstance(sentence[field], str) and sentence[field].strip()
            for field in required_fields
        ):
            raise ValueError("generated sentence fields must be non-empty strings")
        target_id = sentence["target_word_id"]
        target = target_by_id.get(target_id)
        if not target or target_id in seen_ids:
            raise ValueError("generated sentence has an unknown or duplicate target")
        occurrence = sentence.get("target_occurrence", target["surface_form"])
        if not isinstance(occurrence, str) or not occurrence.strip():
            raise ValueError("generated sentence target occurrence must be non-empty")
        if occurrence not in sentence["primary_text"]:
            raise ValueError(
                f"generated sentence does not contain target occurrence {occurrence}"
            )
        if not allow_inflected_targets and occurrence != target["surface_form"]:
            raise ValueError("generated sentence changed an exact-form target")
        if familiar_pool_policy == "natural_priority":
            primary_text = sentence["primary_text"]
            translation = sentence["translation"]
            if primary_text.count(",") > 2 or translation.count(",") > 2:
                raise ValueError("generated sentence is list-like")
        discovered = sentence.get("discovered_vocabulary", [])
        if not isinstance(discovered, list) or len(discovered) > 1:
            raise ValueError("discovered_vocabulary must contain at most one item")
        for candidate in discovered:
            if not isinstance(candidate, dict) or set(candidate) != {
                "surface_form",
                "observed_form",
                "translation",
            }:
                raise ValueError("discovered vocabulary has an invalid shape")
            if not all(
                isinstance(candidate[key], str) and candidate[key].strip()
                for key in candidate
            ):
                raise ValueError("discovered vocabulary fields must be non-empty")
            if candidate["observed_form"] not in sentence["primary_text"]:
                raise ValueError(
                    "discovered vocabulary occurrence is absent from sentence"
                )
            if candidate["surface_form"].casefold() == target[
                "surface_form"
            ].casefold():
                raise ValueError("discovered vocabulary duplicates the target")
        seen_ids.add(target_id)
        validated.append(sentence)
    if seen_ids != set(target_by_id):
        raise ValueError("sentence generator omitted a target")
    return validated


def _generation_request(
    program_id: str,
    config: dict,
    known_words: list[str],
    targets: list[dict],
    allow_inflected_targets: bool,
    familiar_pool_policy: str,
) -> dict:
    return {
        "program_id": program_id,
        "language_code": config.get("language_code", "zh-CN"),
        "prompt_style": config.get("prompt_style", "formal_mandarin"),
        "orthography": config.get("orthography", "Traditional Chinese"),
        "transliteration_style": config.get(
            "transliteration_style",
            "helpful pronunciation guidance",
        ),
        "known_words": known_words,
        "targets": targets,
        "allow_inflected_targets": allow_inflected_targets,
        "familiar_pool_policy": familiar_pool_policy,
    }


def _generate_with_retries(
    generator: SentenceGenerator,
    request: dict,
    targets: list[dict],
    allow_inflected_targets: bool,
    familiar_pool_policy: str,
    max_attempts: int,
) -> list[dict]:
    last_error = None
    for _attempt in range(max_attempts):
        try:
            return _validate_generated_sentences(
                generator.generate(request),
                targets,
                allow_inflected_targets,
                familiar_pool_policy,
            )
        except ValueError as error:
            last_error = error
    raise ValueError(
        f"sentence generator failed quality validation after {max_attempts} attempts: "
        f"{last_error}"
    ) from last_error


def _openrouter_generator(config: dict) -> SentenceGenerator:
    generation_config = config.get("generation", {})
    provider = generation_config.get("provider")
    if provider != "openrouter":
        raise ValueError(f"unsupported sentence generation provider: {provider}")
    key_variable = generation_config.get("api_key_env", "OPENROUTER_API_KEY")
    api_key = os.environ.get(key_variable)
    if not api_key:
        raise RuntimeError(
            f"sentence generation requires the {key_variable} environment variable"
        )
    timeout_seconds = int(generation_config.get("timeout_seconds", 90))
    if not 10 <= timeout_seconds <= 300:
        raise ValueError("OpenRouter timeout_seconds must be between 10 and 300")
    return OpenRouterSentenceGenerator(
        api_key,
        generation_config.get("model", ""),
        timeout_seconds,
    )


def _backfill_empty_sentence_batch(
    program_state: dict,
    generation_state: dict,
    sentence_stream_state: dict,
    content: dict,
    now: datetime,
) -> int:
    day = hkt_day(now).isoformat()
    job = generation_state["jobs"].get(day)
    batch = sentence_stream_state.get("daily_batch")
    if (
        not job
        or int(job.get("generated_count", 0)) < 1
        or not batch
        or batch.get("date") != day
        or batch.get("card_ids")
        or batch.get("active")
    ):
        return 0

    active_sentence_ids = {
        sentence["id"]
        for sentence in content["sentences"]
        if sentence.get("status") == "active"
    }
    sentence_ids = [
        sentence_id
        for sentence_id in job.get("sentence_ids", [])
        if sentence_id in active_sentence_ids
    ]
    if not sentence_ids:
        return 0

    available_at = isoformat_utc(now)
    batch["card_ids"] = sentence_ids
    batch["active"] = [
        {
            "card_id": sentence_id,
            "available_at": available_at,
        }
        for sentence_id in sentence_ids
    ]
    sentence_stream_state["revision"] = (
        max(0, int(sentence_stream_state.get("revision", 0))) + 1
    )
    job["released_same_day_at"] = available_at
    if program_state.get("combined_batch", {}).get("date") == day:
        del program_state["combined_batch"]
    program_state["revision"] += 1
    return len(sentence_ids)


def prepare_sentence_program(
    program_id: str,
    config: dict,
    program_state: dict,
    generation_state: dict,
    content: dict,
    source_cards: list[dict],
    source_stream_state: dict,
    sentence_stream_state: dict,
    now: datetime,
    generator_factory: Callable[[dict], SentenceGenerator] | None = None,
) -> dict:
    _migrate_program_state(program_state)
    _migrate_generation_state(generation_state)
    vocabulary = _source_vocabulary(source_cards)
    strategy = introduction_strategy(config)
    sentence_generation = config.get("sentence_generation", {})
    if not isinstance(sentence_generation, dict):
        raise ValueError("sentence_generation must be an object")
    trigger = sentence_generation.get(
        "trigger",
        (
            "before_vocabulary"
            if strategy == "sentence_first"
            else "after_first_passing_vocabulary_review"
        ),
    )
    valid_triggers = (
        {"before_vocabulary"}
        if strategy == "sentence_first"
        else {
            "after_first_passing_vocabulary_review",
            "catalog_order",
        }
    )
    if trigger not in valid_triggers:
        raise ValueError(f"{strategy} does not support sentence trigger {trigger}")
    minimum_passing_reviews = int(
        sentence_generation.get("minimum_passing_reviews", 1)
    )
    if minimum_passing_reviews < 1:
        raise ValueError("minimum_passing_reviews must be positive")
    allow_inflected_targets = bool(
        sentence_generation.get("allow_inflected_targets", False)
    )
    familiar_pool_policy = sentence_generation.get(
        "familiar_pool_policy",
        "restricted",
    )
    if familiar_pool_policy not in {"restricted", "natural_priority"}:
        raise ValueError(
            f"unsupported familiar pool policy: {familiar_pool_policy}"
        )
    context_pool = sentence_generation.get(
        "context_pool",
        "reviewed_vocabulary",
    )
    if context_pool not in {"reviewed_vocabulary", "source_catalog"}:
        raise ValueError(f"unsupported sentence context pool: {context_pool}")
    prior_knowledge = config.get("prior_knowledge", {})
    if not isinstance(prior_knowledge, dict):
        raise ValueError("prior_knowledge must be an object")
    if trigger == "catalog_order" and prior_knowledge.get(
        "source_catalog"
    ) != "assumed_familiar":
        raise ValueError(
            "catalog_order requires an assumed-familiar source catalog"
        )
    discovery_config = config.get("vocabulary_discovery", {})
    if not isinstance(discovery_config, dict):
        raise ValueError("vocabulary_discovery must be an object")
    discovery_enabled = bool(discovery_config.get("enabled", False))
    if discovery_enabled and discovery_config.get("approval") != "manual":
        raise ValueError("vocabulary discovery currently requires manual approval")
    generation_config = config.get("generation", {})
    if not isinstance(generation_config, dict):
        raise ValueError("generation must be an object")
    quality_version = int(generation_config.get("quality_version", 1))
    refresh_quality = "quality_version" in generation_config
    max_generation_attempts = int(generation_config.get("max_attempts", 1))
    if quality_version < 1 or not 1 <= max_generation_attempts <= 5:
        raise ValueError("sentence generation quality settings are invalid")
    promotion_threshold = int(config.get("promotion_threshold_sentence_passes", 3))
    daily_target = int(config.get("daily_sentence_target", 10))
    max_buffer = int(config.get("max_active_word_buffer", 30))
    mastery_interval = int(config.get("mastery_interval_days", 21))
    if promotion_threshold < 1:
        raise ValueError("promotion threshold must be positive")
    if daily_target < 0 or max_buffer < 1 or mastery_interval < 1:
        raise ValueError("sentence program limits are invalid")
    if config.get("sentence_lifecycle", "disposable_scaffold") != (
        "disposable_scaffold"
    ):
        raise ValueError("only disposable_scaffold sentence lifecycle is supported")

    _reconcile_content_state(
        program_state,
        generation_state,
        content,
        vocabulary,
        promotion_threshold,
        now,
    )
    if not program_state["source_gate_initialized"]:
        exposed_ids = set()
        for batch_key in ("daily_batch", "previous_batch"):
            batch = source_stream_state.get(batch_key) or {}
            exposed_ids.update(batch.get("card_ids", []))
        program_state["grandfathered_word_ids"] = sorted(
            word_id for word_id in exposed_ids if word_id in vocabulary
        )
        program_state["source_gate_initialized"] = True
        program_state["revision"] += 1

    _refresh_mastery(program_state, source_stream_state, mastery_interval, now)

    day = hkt_day(now).isoformat()
    tracked = program_state["vocabulary"]
    source_scheduler_cards = source_stream_state.get("cards", {})
    known_pool_limit = int(config.get("known_pool_limit", 200))
    known_words = (
        _catalog_context_words(
            program_id,
            day,
            vocabulary,
            known_pool_limit,
        )
        if context_pool == "source_catalog"
        else _known_words(
            vocabulary,
            source_scheduler_cards,
            tracked,
            minimum_passing_reviews,
            known_pool_limit,
        )
    )
    discovered_count = 0
    outdated_sentences = [
        sentence
        for sentence in content["sentences"]
        if (
            refresh_quality
            and sentence.get("status") == "active"
            and sentence.get("source") == "generated"
            and int(sentence.get("quality_version", 0)) < quality_version
        )
    ]
    refreshed_count = 0
    if outdated_sentences:
        refresh_targets = [
            vocabulary[sentence["target_word_ids"][0]]
            for sentence in outdated_sentences
        ]
        refresh_request = _generation_request(
            program_id,
            config,
            known_words,
            refresh_targets,
            allow_inflected_targets,
            familiar_pool_policy,
        )
        refresh_generator = (generator_factory or _openrouter_generator)(config)
        refreshed = _generate_with_retries(
            refresh_generator,
            refresh_request,
            refresh_targets,
            allow_inflected_targets,
            familiar_pool_policy,
            max_generation_attempts,
        )
        refreshed_by_target = {
            sentence["target_word_id"]: sentence for sentence in refreshed
        }
        refreshed_at = isoformat_utc(now)
        for stored_sentence in outdated_sentences:
            target_id = stored_sentence["target_word_ids"][0]
            replacement = refreshed_by_target[target_id]
            stored_sentence["payload"] = {
                key: replacement[key]
                for key in (
                    "primary_text",
                    "transliteration",
                    "translation",
                    "cloze_text",
                    "target_breakdown",
                )
            }
            stored_sentence["quality_version"] = quality_version
            stored_sentence["refreshed_at"] = refreshed_at
            if discovery_enabled:
                discovered_count += _record_discovered_vocabulary(
                    program_id,
                    program_state,
                    vocabulary,
                    stored_sentence["id"],
                    stored_sentence["payload"]["primary_text"],
                    replacement.get("discovered_vocabulary", []),
                    now,
                )
        refreshed_count = len(outdated_sentences)
        program_state["revision"] += 1

    existing_job = generation_state["jobs"].get(day)
    generated_count = 0
    generation_pending = (
        not existing_job
        or existing_job.get("generated_count") == 0
    )
    if generation_pending:
        active_buffer = sum(
            1
            for item in tracked.values()
            if item.get("status")
            in (
                {"sentence_preview", "active_anki"}
                if strategy == "sentence_first"
                else {"sentence_preview", "sentence_reinforcement"}
            )
        )
        available_slots = max(0, max_buffer - active_buffer)
        target_count = min(daily_target, available_slots)
        previously_exposed = set(program_state["grandfathered_word_ids"])
        if trigger == "catalog_order":
            candidates = [
                item
                for word_id, item in vocabulary.items()
                if word_id not in tracked
            ][:target_count]
        elif strategy == "sentence_first":
            candidates = [
                item
                for word_id, item in vocabulary.items()
                if (
                    word_id not in tracked
                    and word_id not in source_scheduler_cards
                    and word_id not in previously_exposed
                )
            ][:target_count]
        else:
            candidates = [
                item
                for word_id, item in vocabulary.items()
                if (
                    word_id not in tracked
                    and _passing_review_count(source_scheduler_cards.get(word_id))
                    >= minimum_passing_reviews
                )
            ][:target_count]

        if candidates:
            generated_targets = [
                item for item in candidates if item["entry_type"] != "sentence"
            ]
            if generated_targets and not known_words:
                raise RuntimeError(
                    f"[{program_id}] no familiar vocabulary is available for generation"
                )
            generated_by_target = {}
            if generated_targets:
                request = _generation_request(
                    program_id,
                    config,
                    known_words,
                    generated_targets,
                    allow_inflected_targets,
                    familiar_pool_policy,
                )
                generator = (generator_factory or _openrouter_generator)(config)
                generated = _generate_with_retries(
                    generator,
                    request,
                    generated_targets,
                    allow_inflected_targets,
                    familiar_pool_policy,
                    max_generation_attempts,
                )
                generated_by_target = {
                    sentence["target_word_id"]: sentence for sentence in generated
                }
            created_at = isoformat_utc(now)
            generated_sentence_ids = []
            word_status = (
                "sentence_preview"
                if strategy == "sentence_first"
                else "sentence_reinforcement"
            )
            for target in candidates:
                target_id = target["id"]
                sentence_id = f"{program_id}-{day}-{target_id}"
                if any(
                    existing.get("id") == sentence_id
                    for existing in content["sentences"]
                ):
                    raise ValueError(f"duplicate generated sentence ID: {sentence_id}")
                if target["entry_type"] == "sentence":
                    stored_sentence = _direct_sentence(
                        program_id,
                        day,
                        target,
                        created_at,
                        strategy,
                    )
                else:
                    sentence = generated_by_target[target_id]
                    stored_sentence = {
                        "id": sentence_id,
                        "card_type": config.get("mode", "text_reading"),
                        "target_word_ids": [target_id],
                        "lifecycle": config.get(
                            "sentence_lifecycle", "disposable_scaffold"
                        ),
                        "status": "active",
                        "created_at": created_at,
                        "introduction_strategy": strategy,
                        "source": "generated",
                        "quality_version": quality_version,
                        "payload": {
                            key: sentence[key]
                            for key in (
                                "primary_text",
                                "transliteration",
                                "translation",
                                "cloze_text",
                                "target_breakdown",
                            )
                        },
                    }
                content["sentences"].append(stored_sentence)
                if discovery_enabled and target["entry_type"] != "sentence":
                    discovered_count += _record_discovered_vocabulary(
                        program_id,
                        program_state,
                        vocabulary,
                        sentence_id,
                        stored_sentence["payload"]["primary_text"],
                        sentence.get("discovered_vocabulary", []),
                        now,
                    )
                generated_sentence_ids.append(sentence_id)
                tracked[target_id] = {
                    "status": word_status,
                    "sentence_pass_count": 0,
                    "introduced_at": created_at,
                    "introduction_strategy": strategy,
                }
            generated_count = len(candidates)
            program_state["revision"] += 1

        if candidates or not existing_job:
            generation_state["jobs"][day] = {
                "status": "completed",
                "completed_at": isoformat_utc(now),
                "generated_count": generated_count,
                "sentence_ids": generated_sentence_ids if candidates else [],
            }

    backfilled_count = _backfill_empty_sentence_batch(
        program_state,
        generation_state,
        sentence_stream_state,
        content,
        now,
    )
    return {
        "generated": generated_count,
        "refreshed": refreshed_count,
        "discovered": discovered_count,
        "backfilled": backfilled_count,
        "active_sentences": len(sentence_cards(content)),
        "promoted_words": len(promoted_word_ids(program_state)),
    }
