from __future__ import annotations

import json
import os
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
                    "primary_text": f"我會在句子中使用「{surface}」。",
                    "transliteration": f"Mock transliteration for {surface}",
                    "translation": f"I will use “{surface}” in a sentence.",
                    "cloze_text": "我會在句子中使用「[…]」。",
                    "target_breakdown": (
                        f"{surface}: {target['translation']}"
                    ),
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
        system_prompt = (
            "You are an expert language-pedagogy engine. Generate natural "
            "practice sentences using restricted vocabulary. Return JSON only."
        )
        user_prompt = f"""
Language: {request['language_code']}
Style: {request['prompt_style']}
Orthography: {request['orthography']}

Create exactly one sentence for each target:
{target_lines}

At least 85 percent of the surrounding vocabulary should come from this
familiar pool:
{known_words}

Each sentence must contain its target surface form. Return an object with a
"sentences" array. Every item must contain exactly these string fields:
"target_word_id", "primary_text", "transliteration", "translation",
"cloze_text", and "target_breakdown".
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
                                "primary_text",
                                "transliteration",
                                "translation",
                                "cloze_text",
                                "target_breakdown",
                            ],
                            "properties": {
                                key: {"type": "string"}
                                for key in (
                                    "target_word_id",
                                    "primary_text",
                                    "transliteration",
                                    "translation",
                                    "cloze_text",
                                    "target_breakdown",
                                )
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
        notes = payload["transliteration"]
        if payload["target_breakdown"]:
            notes = f"{notes}\n\n{payload['target_breakdown']}"
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


def _migrate_program_state(program_state: dict) -> None:
    schema_version = program_state.get("schema_version")
    if schema_version not in (None, PROGRAM_SCHEMA_VERSION):
        raise ValueError("unsupported sentence program schema version")
    program_state.setdefault("schema_version", PROGRAM_SCHEMA_VERSION)
    program_state.setdefault("revision", 0)
    program_state.setdefault("vocabulary", {})
    program_state.setdefault("processed_events", {})
    program_state.setdefault("grandfathered_word_ids", [])
    program_state.setdefault("source_gate_initialized", False)
    if not isinstance(program_state["vocabulary"], dict):
        raise ValueError("program vocabulary state must be an object")
    if not isinstance(program_state["processed_events"], dict):
        raise ValueError("program processed_events state must be an object")
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
        vocabulary[word_id] = {
            "id": word_id,
            "surface_form": surface,
            "translation": translation,
        }
    return vocabulary


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
                if not word_state or word_state.get("status") != "sentence_preview":
                    continue
                word_state["sentence_pass_count"] += 1
                word_state["last_sentence_pass_at"] = isoformat_utc(reviewed_at)
                if word_state["sentence_pass_count"] >= promotion_threshold:
                    word_state["status"] = "active_anki"
                    word_state["promoted_at"] = isoformat_utc(reviewed_at)
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
) -> list[dict]:
    if not isinstance(generated, list):
        raise ValueError("sentence generator must return a list")
    target_by_id = {target["id"]: target for target in targets}
    if len(generated) != len(targets):
        raise ValueError("sentence generator returned the wrong number of sentences")
    validated = []
    seen_ids = set()
    required_fields = (
        "target_word_id",
        "primary_text",
        "transliteration",
        "translation",
        "cloze_text",
        "target_breakdown",
    )
    for sentence in generated:
        if not isinstance(sentence, dict):
            raise ValueError("generated sentence must be an object")
        if set(sentence) != set(required_fields):
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
        if target["surface_form"] not in sentence["primary_text"]:
            raise ValueError(
                f"generated sentence does not contain target {target['surface_form']}"
            )
        seen_ids.add(target_id)
        validated.append(sentence)
    if seen_ids != set(target_by_id):
        raise ValueError("sentence generator omitted a target")
    return validated


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


def prepare_sentence_program(
    program_id: str,
    config: dict,
    program_state: dict,
    generation_state: dict,
    content: dict,
    source_cards: list[dict],
    source_stream_state: dict,
    now: datetime,
    generator_factory: Callable[[dict], SentenceGenerator] | None = None,
) -> dict:
    _migrate_program_state(program_state)
    _migrate_generation_state(generation_state)
    vocabulary = _source_vocabulary(source_cards)
    sentence_stream_id = config["sentence_stream"]
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
    existing_job = generation_state["jobs"].get(day)
    generated_count = 0
    if not existing_job:
        tracked = program_state["vocabulary"]
        active_buffer = sum(
            1
            for item in tracked.values()
            if item.get("status") in {"sentence_preview", "active_anki"}
        )
        available_slots = max(0, max_buffer - active_buffer)
        target_count = min(daily_target, available_slots)
        source_scheduler_cards = source_stream_state.get("cards", {})
        previously_exposed = set(program_state["grandfathered_word_ids"])
        candidates = [
            item
            for word_id, item in vocabulary.items()
            if (
                word_id not in tracked
                and word_id not in source_scheduler_cards
                and word_id not in previously_exposed
            )
        ][:target_count]

        if candidates:
            minimum_reviews = int(config.get("familiar_min_reviews", 1))
            known_pool_limit = int(config.get("known_pool_limit", 200))
            known_words = [
                item["surface_form"]
                for word_id, item in vocabulary.items()
                if (
                    int(source_scheduler_cards.get(word_id, {}).get("reviews", 0))
                    >= minimum_reviews
                    or tracked.get(word_id, {}).get("status")
                    in {"active_anki", "mastered"}
                )
            ][:known_pool_limit]
            if not known_words:
                raise RuntimeError(
                    f"[{program_id}] no familiar vocabulary is available for generation"
                )
            request = {
                "program_id": program_id,
                "language_code": config.get("language_code", "zh-CN"),
                "prompt_style": config.get("prompt_style", "formal_mandarin"),
                "orthography": config.get("orthography", "Traditional Chinese"),
                "known_words": known_words,
                "targets": candidates,
            }
            generator = (generator_factory or _openrouter_generator)(config)
            generated = _validate_generated_sentences(
                generator.generate(request),
                candidates,
            )
            created_at = isoformat_utc(now)
            generated_sentence_ids = []
            for sentence in generated:
                target_id = sentence["target_word_id"]
                sentence_id = f"{program_id}-{day}-{target_id}"
                if any(
                    existing.get("id") == sentence_id
                    for existing in content["sentences"]
                ):
                    raise ValueError(f"duplicate generated sentence ID: {sentence_id}")
                content["sentences"].append(
                    {
                        "id": sentence_id,
                        "card_type": config.get("mode", "text_reading"),
                        "target_word_ids": [target_id],
                        "lifecycle": config.get(
                            "sentence_lifecycle", "disposable_scaffold"
                        ),
                        "status": "active",
                        "created_at": created_at,
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
                )
                generated_sentence_ids.append(sentence_id)
                tracked[target_id] = {
                    "status": "sentence_preview",
                    "sentence_pass_count": 0,
                    "introduced_at": created_at,
                }
            generated_count = len(generated)
            program_state["revision"] += 1

        generation_state["jobs"][day] = {
            "status": "completed",
            "completed_at": isoformat_utc(now),
            "generated_count": generated_count,
            "sentence_ids": generated_sentence_ids if candidates else [],
        }

    return {
        "generated": generated_count,
        "active_sentences": len(sentence_cards(content)),
        "promoted_words": len(promoted_word_ids(program_state)),
    }
