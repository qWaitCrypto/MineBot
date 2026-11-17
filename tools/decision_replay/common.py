"""Shared JSONL format. Never sort a payload, including its nested maps."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

FORMAT = "minebot-decision-replay/1"
LIVE_TIMEOUT_S = 2.0


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    """Order-sensitive identity, used to detect changes when resuming."""
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def payload_key(payload: dict) -> str:
    """Content-only lookup of sorted trace copies; never sent to the model."""
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def complete_answers(payload: dict, response: Any) -> bool:
    answers = response.get("answers") if isinstance(response, dict) else None
    questions = payload.get("questions")
    return bool(questions) and isinstance(answers, dict) and all(
        isinstance(answers.get(key), dict)
        and isinstance(answers[key].get("choice"), str)
        and answers[key]["choice"] in question.get("criteria", {})
        for key, question in questions.items()
    )


def frame_choice(payload: dict, answers: dict) -> str:
    """The idle sampling stratum uses the danger-gated action, as the Body does.

    Agreement is always measured separately for every question, including p
    when d says safe. A standalone intent/place question has its own choice.
    """
    questions = payload["questions"]
    key = "p" if "d" in questions and answers["d"]["choice"] == "in_danger" else "q"
    if key not in questions:
        key = next(iter(questions))
    return answers[key]["choice"]


def read_jsonl(path: Path) -> tuple[dict, list[dict]]:
    with path.open(encoding="utf-8") as stream:
        header = json.loads(next(stream))
        if header.get("type") != "header" or header.get("format") != FORMAT:
            raise ValueError(f"Unsupported replay file: {path}")
        records = [json.loads(line) for line in stream if line.strip()]
    ids = [record["id"] for record in records]
    if len(ids) != len(set(ids)):
        raise ValueError(f"Duplicate record IDs: {path}")
    return header, records


def write_jsonl(path: Path, header: dict, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic replacement leaves an existing set intact if harvesting fails.
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        for record in (header, *records):
            stream.write(dumps(record) + "\n")
    temporary.replace(path)
