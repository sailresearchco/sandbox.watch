"""Run one agent turn: take a Parallel monitor event, let the coding agent
update the site data, then commit the result and record it on /log.

The harness owns structure (validation, git, the changelog format). The agent
owns judgment (which fields the event supports changing, and the prose).
"""

from __future__ import annotations

import datetime
import json
import logging
import shlex
import subprocess
import time
from pathlib import Path

from . import changelog, config, providers
from .parallel_client import ParallelClient

logger = logging.getLogger("sandboxwatch.turn")

MAX_CITATIONS = 10


def default_client() -> ParallelClient:
    key = config.secret("parallel_api_key")
    if not key:
        raise RuntimeError("parallel_api_key secret is not configured")
    return ParallelClient(key)


def fetch_event_details(client: ParallelClient, payload: dict) -> dict:
    """Resolve the webhook pointer into full event content with citations."""
    data = payload.get("data", {})
    monitor_id = data.get("monitor_id", "")
    event_group_id = (data.get("event") or {}).get("event_group_id")
    events = client.monitor_events(monitor_id, event_group_id) if monitor_id else []
    citations: list[str] = []
    for event in events:
        # Citations live on the basis entries of the event's output block:
        # `output` on event-stream events, `changed_output` on snapshot diffs.
        for block_key in ("output", "changed_output"):
            block = event.get(block_key)
            if not isinstance(block, dict):
                continue
            for basis in block.get("basis") or []:
                for citation in basis.get("citations") or []:
                    url = citation.get("url")
                    if url and url not in citations:
                        citations.append(url)
    return {
        "monitor_id": monitor_id,
        "event_group_id": event_group_id,
        "metadata": data.get("metadata") or {},
        "events": events,
        "citations": citations[:MAX_CITATIONS],
    }


def build_prompt(event_path: Path) -> str:
    template = (config.root_dir() / "box" / "prompts" / "update_prompt.md").read_text()
    return template.format(event_path=event_path)


def run_agent(prompt_path: Path) -> bool:
    cmd = shlex.split(config.agent_cmd()) + [str(prompt_path)]
    logger.info("running agent turn: %s", cmd)
    try:
        result = subprocess.run(
            cmd,
            cwd=config.root_dir(),
            timeout=config.agent_timeout_seconds(),
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired:
        logger.warning("agent turn timed out")
        return False
    if result.returncode != 0:
        logger.warning(
            "agent turn failed rc=%d stdout=%r stderr=%r",
            result.returncode,
            result.stdout[-2000:],
            result.stderr[-2000:],
        )
    return result.returncode == 0


def _git(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=config.root_dir(),
        capture_output=True,
        text=True,
        check=check,
        timeout=120,
    )


def _provider_files() -> set[str]:
    return {path.name for path in config.providers_dir().glob("*.json")}


def queue_research(slugs: set[str]) -> None:
    """Mark newly created products for their first full research pass.

    A turn writes only what its event cited, which is usually a name, a URL
    and little else. Without this the row would sit half empty forever and
    nothing would watch the product for changes."""
    if not slugs:
        return
    directory = config.pending_research_dir()
    directory.mkdir(parents=True, exist_ok=True)
    for slug in slugs:
        (directory / slug).touch()
    logger.info("queued first research for %s", sorted(slugs))


def complete_new_provider(slug: str, processor: str = "pro") -> str:
    """Research a newly discovered product, then start monitoring it.

    Returns a short status string. Imports bootstrap lazily because bootstrap
    imports this module."""
    from . import bootstrap

    path = config.providers_dir() / f"{slug}.json"
    if not path.is_file():
        return "gone"
    stub = json.loads(path.read_text())
    seed = {"name": stub["name"], "slug": slug, "website": stub["website"]}
    client = default_client()

    runs = bootstrap.research_all(client, [seed], processor)
    run_id = runs[slug]
    before = path.read_text()
    bootstrap.write_provider_file(
        seed, client.task_result(run_id, timeout_seconds=3600)
    )
    errors = providers.validate_all()
    if errors:
        # Keep the stub rather than publish a malformed row.
        path.write_text(before)
        logger.warning("research for %s failed validation: %s", slug, errors)
        return "invalid"

    # Census entry, so the product is a first-class row rather than a file
    # that happens to exist.
    census_path = config.root_dir() / "providers.json"
    census = json.loads(census_path.read_text())
    if not any(entry.get("slug") == slug for entry in census):
        census.append(seed)
        census_path.write_text(json.dumps(census, indent=2) + "\n")

    monitors_path = config.data_dir() / "monitors.json"
    monitors = json.loads(monitors_path.read_text())
    # Reuse the webhook the existing monitors already point at, so a new
    # monitor can never drift onto a stale URL.
    webhook_url = ""
    for record in list((monitors.get("providers") or {}).values()) + [
        monitors.get("new_products")
    ]:
        url = ((record or {}).get("webhook") or {}).get("url")
        if url:
            webhook_url = url
            break
    if not webhook_url:
        logger.warning("no webhook url on record; skipping monitor for %s", slug)
        commit_and_push(
            f"{slug}: research the new entry",
            paths=("data", "site", "providers.json"),
        )
        return "researched-unmonitored"
    created = client.create_monitor(
        monitor_type="snapshot",
        frequency="1d",
        settings={"task_run_id": run_id},
        webhook_url=webhook_url,
        processor="base",
        metadata={"site": "sandboxwatch", "slug": slug},
    )
    monitors.setdefault("providers", {})[slug] = created
    monitors_path.write_text(json.dumps(monitors, indent=2, sort_keys=True) + "\n")

    commit_and_push(
        f"{slug}: research the new entry and start monitoring it",
        paths=("data", "site", "providers.json"),
    )
    return "researched"


def drain_pending_research(limit: int = 3) -> list[tuple[str, str]]:
    """Complete queued new products, a few per wake to bound cost and how
    long the box stays awake. Anything left waits for the next wake."""
    directory = config.pending_research_dir()
    if not directory.is_dir():
        return []
    results = []
    for marker in sorted(directory.iterdir())[:limit]:
        slug = marker.name
        try:
            status = complete_new_provider(slug)
        except Exception:
            logger.exception("first research failed for %s", slug)
            status = "error"
        # Drop the marker either way: a retry loop here would re-spend money
        # on every wake. An operator can requeue with bootstrap --only.
        marker.unlink(missing_ok=True)
        results.append((slug, status))
    return results


def revert_working_tree() -> None:
    _git("checkout", "--", ".")
    _git("clean", "-fdq", "data", "site")


def _finish_turn(
    *,
    started: float,
    turn_dir: Path,
    kind: str,
    agent_ok: bool,
    errors: list[str],
    extra: dict | None = None,
    commit_paths: tuple[str, ...] = ("data", "site"),
) -> dict:
    """Shared tail of every turn: commit or revert, then record on /log."""
    if agent_ok and not errors:
        title, summary_text = read_turn_summary()
        commit = commit_and_push(title, paths=commit_paths)
        status = "applied" if commit else "no_change"
    else:
        revert_working_tree()
        title, summary_text = "Turn failed, changes reverted", ""
        commit = None
        status = "failed"

    duration = time.time() - started
    entry = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec="seconds"
        ),
        "kind": kind,
        "status": status,
        "summary": title,
        "details": summary_text,
        "citations": [],
        "commit": commit,
        "duration_seconds": round(duration, 1),
        "est_cost_usd": round(duration * config.EST_HOURLY_USD / 3600, 6),
        "validation_errors": errors[:10],
        **(extra or {}),
    }
    changelog.append(entry)
    # Record every outcome in git history, failures included: the changelog
    # is the audit trail, and an uncommitted line would be discarded by the
    # next turn's revert.
    commit_and_push(f"log: {title}", paths=commit_paths)
    if summary_text:
        (turn_dir / "turn_summary.md").write_text(summary_text)
    logger.info(
        "turn finished status=%s commit=%s duration=%.1fs", status, commit, duration
    )
    return entry


def commit_and_push(
    message: str, paths: tuple[str, ...] = ("data", "site")
) -> str | None:
    """Commit changes under the given paths. Returns the short hash, or
    None if there was nothing to commit. Pushing is best-effort.

    The census (providers.json) is only in scope for discovery turns, so
    callers that may commit it pass it explicitly."""
    _git("add", "-A", *paths)
    if _git("diff", "--cached", "--quiet").returncode == 0:
        return None
    result = _git("commit", "-m", message)
    if result.returncode != 0:
        logger.warning("git commit failed: %s", result.stderr[-500:])
        return None
    commit = _git("rev-parse", "--short", "HEAD").stdout.strip()
    if _git("remote", "get-url", "origin").returncode == 0:
        push = _git("push", "origin", "HEAD")
        if push.returncode != 0:
            logger.info("git push failed (kept local commit): %s", push.stderr[-500:])
    return commit


def read_turn_summary() -> tuple[str, str]:
    """(first line, full text) of the summary the agent wrote, with fallbacks."""
    path = config.state_dir() / "turn_summary.md"
    if not path.is_file():
        return "Update from a web monitor event", ""
    text = path.read_text().strip()
    first = text.splitlines()[0].strip() if text else "Update from a web monitor event"
    return first, text


def run_turn(payload: dict, client: ParallelClient | None = None) -> dict:
    """Handle one monitor.event.detected payload end to end."""
    started = time.time()
    turn_dir = (
        config.state_dir() / "turns" / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    )
    turn_dir.mkdir(parents=True, exist_ok=True)
    summary_path = config.state_dir() / "turn_summary.md"
    summary_path.unlink(missing_ok=True)

    details = fetch_event_details(client or default_client(), payload)
    event_path = turn_dir / "event.json"
    event_path.write_text(json.dumps(details, indent=2, sort_keys=True))
    prompt_path = turn_dir / "prompt.md"
    prompt_path.write_text(build_prompt(event_path))

    files_before = _provider_files()
    agent_ok = run_agent(prompt_path)
    errors = providers.validate_all() if agent_ok else []
    if agent_ok:
        # A monitor event never justifies dropping a product outright.
        errors += sorted(
            f"deleted provider file: {name}"
            for name in files_before - _provider_files()
        )
    if agent_ok and errors:
        logger.warning("agent output failed validation: %s", errors)

    created_slugs = {n[:-5] for n in _provider_files() - files_before}
    entry = _finish_turn(
        started=started,
        turn_dir=turn_dir,
        kind="monitor_event",
        agent_ok=agent_ok,
        errors=errors,
        extra={
            "citations": details["citations"],
            "monitor_id": details["monitor_id"],
        },
    )
    if entry["status"] == "applied":
        queue_research(created_slugs)
    return entry


def run_discovery_turn(discovery_path: Path | None = None) -> dict:
    """Judge the FindAll candidates in state/discovery.json.

    The agent promotes candidates whose evidence clears the census bar
    (providers.json entry plus an evidence-limited data file) and rejects
    the rest with reasons. The resulting commit is the approval record."""
    started = time.time()
    source = discovery_path or config.state_dir() / "discovery.json"
    turn_dir = (
        config.state_dir() / "turns" / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    )
    turn_dir.mkdir(parents=True, exist_ok=True)
    summary_path = config.state_dir() / "turn_summary.md"
    summary_path.unlink(missing_ok=True)

    # Snapshot the candidates into the turn dir so the prompt references an
    # immutable copy even if a later sweep rewrites state/discovery.json.
    candidates_path = turn_dir / "discovery.json"
    candidates_path.write_text(source.read_text())
    template = (
        config.root_dir() / "box" / "prompts" / "discovery_prompt.md"
    ).read_text()
    prompt_path = turn_dir / "prompt.md"
    prompt_path.write_text(template.format(discovery_path=candidates_path))

    files_before = _provider_files()
    agent_ok = run_agent(prompt_path)
    errors = providers.validate_all() + providers.validate_census() if agent_ok else []
    if agent_ok:
        # Discovery adds products; removals are an operator decision.
        errors += sorted(
            f"deleted provider file: {name}"
            for name in files_before - _provider_files()
        )
    if agent_ok and errors:
        logger.warning("discovery turn failed validation: %s", errors)

    created_slugs = {n[:-5] for n in _provider_files() - files_before}
    entry = _finish_turn(
        started=started,
        turn_dir=turn_dir,
        kind="discovery",
        agent_ok=agent_ok,
        errors=errors,
        commit_paths=("data", "site", "providers.json"),
    )
    if entry["status"] == "applied":
        queue_research(created_slugs)
    return entry
