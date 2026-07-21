"""Provider data model: one JSON file per sandbox product under data/providers/.

The same field set appears in three places on purpose: these files, the JSON
schema Parallel research runs must fill, and the site's comparison table.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import config

# A dollar figure stated per vCPU (or CPU core) per exact time unit. Only
# these normalize honestly: converting seconds or minutes to hours is unit
# arithmetic. Per-credit, per-MCU, per-instance, and per-month prices carry
# assumptions the docs do not state, so they stay unranked. A tilde marks an
# approximation someone derived rather than a rate the provider states, so
# those are skipped too.
_VCPU_RATE = re.compile(
    r"(?<!~)\$([0-9][0-9,]*(?:\.[0-9]+)?)"
    r"\s*(?:per\s+|/\s*)(?:active(?:ly\s+used)?\s+)?v?cpu(?:\s+core)?"
    r"[\s/-]*(sec(?:ond)?|min(?:ute)?|hour|hr)\b",
    re.IGNORECASE,
)
_PER_HOUR_FACTOR = {"s": 3600.0, "m": 60.0, "h": 1.0}


# A stated start or resume time. Bounds count as their value ("<1 s" and
# "under a second" rank as one second), and explicit sub-second wording
# ranks as one second too. Wordier claims with no figure stay unranked.
_TIME_QTY = re.compile(
    r"(\d[\d,]*(?:\.\d+)?)\s*(ms|milliseconds?|secs?\b|seconds?|s\b|min\b|minutes?)",
    re.IGNORECASE,
)
_SUB_SECOND = re.compile(r"sub-?second|millisecond|under a second", re.IGNORECASE)


def start_seconds(cold_start: str | None) -> float | None:
    """Seconds for the first stated time in a start/resume claim, or None."""
    if not cold_start:
        return None
    match = _TIME_QTY.search(cold_start)
    if match:
        value = float(match.group(1).replace(",", ""))
        unit = match.group(2).lower()
        if unit.startswith("ms") or unit.startswith("millisecond"):
            return round(value / 1000, 6)
        if unit.startswith("min"):
            return value * 60
        return value
    if _SUB_SECOND.search(cold_start):
        return 1.0
    return None


def vcpu_hour_rate(price_headline: str | None) -> float | None:
    """Dollars per vCPU-hour when the headline states a per-vCPU time rate.

    Returns None for every other pricing shape, which the table sorts as
    not directly comparable."""
    if not price_headline:
        return None
    match = _VCPU_RATE.search(price_headline)
    if not match:
        return None
    amount = float(match.group(1).replace(",", ""))
    factor = _PER_HOUR_FACTOR[match.group(2)[0].lower()]
    return round(amount * factor, 6)


# Spec fields rendered on the site, in table column order: (key, label,
# visitor tooltip). Booleans render as Yes/No, null renders as "n/a"
# (meaning: no cited public fact yet).
SPEC_FIELDS = [
    (
        "price_headline",
        "Pricing",
        "The headline compute price as the provider states it. Sorting ranks "
        "prices stated per vCPU over time, converted to hourly. Other "
        "pricing models sort after, unranked.",
    ),
    (
        "free_while_idle",
        "Free while idle",
        "Whether a stopped, paused, or sleeping sandbox costs nothing.",
    ),
    (
        "usage_billed",
        "Elastic",
        "Resources flex with what the sandbox actually uses, and the bill "
        "follows: yes when the provider bills on active or observed use, no "
        "when capacity is reserved or allocated and billed while it runs.",
    ),
    (
        "memory_snapshots",
        "Memory snapshots",
        "Whether RAM state survives a pause and resume, not just disk.",
    ),
    (
        "wake_on_request",
        "Wake on request",
        "Whether a stopped sandbox wakes automatically on inbound traffic.",
    ),
    (
        "cold_start",
        "Start / resume",
        "Typical time from create or resume to running. Sorting ranks "
        "stated times, converted to seconds; claims with no figure sort "
        "after, unranked.",
    ),
    ("max_runtime", "Max runtime", "The longest a sandbox may run."),
    ("isolation", "Isolation", "The isolation technology between sandboxes."),
    ("gpu", "GPUs", "Whether GPU instances are available for sandboxes."),
    (
        "docker",
        "Docker",
        "Whether Docker containers can run inside the sandbox.",
    ),
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
        "usage_billed": {
            "type": ["boolean", "null"],
            "description": (
                "True if the sandbox is elastic: resources flex with actual "
                "use and billing follows (active or observed CPU time). "
                "False if capacity is reserved or allocated and billed for "
                "running time regardless of use. Null if the docs do not say."
            ),
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
    # Type checks against the schema. Presence of the schema's required
    # fields is deliberately not enforced: evidence-limited stubs from
    # discovery turns legitimately carry nulls until research fills them.
    for field in (
        "free_while_idle",
        "usage_billed",
        "memory_snapshots",
        "wake_on_request",
        "gpu",
        "docker",
    ):
        if field in data and not isinstance(data[field], (bool, type(None))):
            errors.append(f"{path.name}: '{field}' must be true, false, or null")
    for field in (
        "pricing_model",
        "price_headline",
        "cold_start",
        "max_runtime",
        "isolation",
        "notes",
    ):
        if field in data and not isinstance(data[field], (str, type(None))):
            errors.append(f"{path.name}: '{field}' must be a string or null")
    if "sdks" in data and not (
        isinstance(data["sdks"], list)
        and all(isinstance(item, str) for item in data["sdks"])
    ):
        errors.append(f"{path.name}: 'sdks' must be a list of strings")
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


def validate_census() -> list[str]:
    """Check providers.json's structure: a list of {name, slug, website}
    entries with well-formed, unique slugs. Guards discovery turns, which
    are the one place the census itself gets edited.

    A seed without a data file is fine (research fills it in later), and a
    data file without a seed is fine (a monitor event can add a product
    before anyone seeds it), so neither direction is checked here."""
    path = config.root_dir() / "providers.json"
    try:
        seeds = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return [f"providers.json: {exc}"]
    if not isinstance(seeds, list):
        return ["providers.json: top level must be a list"]
    errors: list[str] = []
    seen: set[str] = set()
    for seed in seeds:
        if not isinstance(seed, dict) or not all(
            isinstance(seed.get(key), str) and seed[key].strip()
            for key in ("name", "slug", "website")
        ):
            errors.append(f"providers.json: entry needs name/slug/website: {seed!r}")
            continue
        slug = seed["slug"]
        if slug in seen:
            errors.append(f"providers.json: duplicate slug {slug!r}")
        seen.add(slug)
        if not all(c.isalnum() or c == "-" for c in slug) or slug != slug.lower():
            errors.append(f"providers.json: malformed slug {slug!r}")
    return errors
