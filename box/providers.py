"""Provider data model: one JSON file per sandbox product under data/providers/.

The same field set appears in three places on purpose: these files, the JSON
schema Parallel research runs must fill, and the site's comparison table.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import config

# Spec fields rendered on the site, in table column order. Booleans render as
# Yes/No, null renders as "n/a" (meaning: no cited public fact yet).
SPEC_FIELDS = [
    ("price_headline", "Pricing"),
    ("free_while_idle", "Free while idle"),
    ("memory_snapshots", "Memory snapshots"),
    ("wake_on_request", "Wake on request"),
    ("cold_start", "Cold start"),
    ("max_runtime", "Max runtime"),
    ("isolation", "Isolation"),
    ("gpu", "GPUs"),
    ("docker", "Docker"),
]

# Schema handed to Parallel task runs and snapshot monitors. Descriptions are
# written for the researcher: they define what each field means.
PROVIDER_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {
            "type": "string",
            "description": (
                "Two or three plain sentences on what the product is and who uses it. "
                "No marketing language."
            ),
        },
        "pricing_model": {
            "type": "string",
            "description": (
                "How billing works, in one short phrase. "
                "Example: 'Per-second, billed on reserved vCPU and RAM'."
            ),
        },
        "price_headline": {
            "type": "string",
            "description": "The headline compute price. Example: '$0.05 per vCPU-hour'.",
        },
        "free_while_idle": {
            "type": ["boolean", "null"],
            "description": "True if a stopped, paused, or sleeping sandbox costs nothing.",
        },
        "memory_snapshots": {
            "type": ["boolean", "null"],
            "description": "True if RAM state survives a pause/resume cycle, not just disk.",
        },
        "wake_on_request": {
            "type": ["boolean", "null"],
            "description": "True if a stopped sandbox wakes automatically on inbound traffic.",
        },
        "cold_start": {
            "type": ["string", "null"],
            "description": "Typical time from create or resume to running. Example: '~150 ms'.",
        },
        "max_runtime": {
            "type": ["string", "null"],
            "description": "Longest a sandbox may run. Example: '24 h' or 'No fixed limit'.",
        },
        "isolation": {
            "type": ["string", "null"],
            "description": "Isolation technology. Example: 'Firecracker microVM'.",
        },
        "sdks": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Official SDK languages, for example ['Python', 'TypeScript'].",
        },
        "gpu": {"type": ["boolean", "null"], "description": "GPU instances available."},
        "docker": {
            "type": ["boolean", "null"],
            "description": "True if Docker containers can run inside the sandbox.",
        },
        "notes": {
            "type": ["string", "null"],
            "description": "One factual caveat worth knowing, or null.",
        },
    },
    "required": ["summary", "pricing_model", "price_headline"],
}


def load_providers(directory: Path | None = None) -> list[dict]:
    directory = directory or config.providers_dir()
    items = []
    for path in sorted(directory.glob("*.json")):
        with path.open() as f:
            items.append(json.load(f))
    return sorted(items, key=lambda p: p.get("name", "").lower())


def load_provider(slug: str) -> dict | None:
    path = config.providers_dir() / f"{slug}.json"
    if not path.is_file():
        return None
    with path.open() as f:
        return json.load(f)


def validate_provider_file(path: Path) -> list[str]:
    """Return human-readable problems with one provider file, empty if fine."""
    errors: list[str] = []
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        return [f"{path.name}: invalid JSON ({exc})"]
    if not isinstance(data, dict):
        return [f"{path.name}: top level must be an object"]
    for field in ("name", "slug", "website", "summary", "last_verified"):
        if not isinstance(data.get(field), str) or not data[field].strip():
            errors.append(f"{path.name}: missing or empty '{field}'")
    if data.get("slug") != path.stem:
        errors.append(
            f"{path.name}: slug {data.get('slug')!r} does not match the filename"
        )
    sources = data.get("sources")
    if not isinstance(sources, list) or not sources:
        errors.append(f"{path.name}: 'sources' must be a non-empty list")
    else:
        for i, source in enumerate(sources):
            if not isinstance(source, dict) or not str(
                source.get("url", "")
            ).startswith("http"):
                errors.append(f"{path.name}: sources[{i}] needs an http(s) 'url'")
    return errors


def validate_all(directory: Path | None = None) -> list[str]:
    directory = directory or config.providers_dir()
    errors: list[str] = []
    for path in sorted(directory.glob("*.json")):
        errors.extend(validate_provider_file(path))
    return errors
