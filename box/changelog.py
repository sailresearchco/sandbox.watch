"""Append-only record of what the agent did and when. Rendered at /log."""

from __future__ import annotations

import json
from pathlib import Path

from . import config


def path() -> Path:
    return config.data_dir() / "changelog.jsonl"


def append(entry: dict) -> None:
    file = path()
    file.parent.mkdir(parents=True, exist_ok=True)
    with file.open("a") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")


def read(limit: int = 200) -> list[dict]:
    """Entries, newest first. Malformed lines are skipped, not fatal."""
    file = path()
    if not file.is_file():
        return []
    entries = []
    for line in file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    entries.reverse()
    return entries[:limit]
