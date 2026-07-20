"""One-time setup: research every provider with Parallel's Task API, write the
data files, then create the monitors that keep the site current.

Runs inside the box (launch.py execs it) but works anywhere the secrets are
available:

    python3 -m box.bootstrap --webhook-url https://<box-url>/hooks/parallel
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys

from . import changelog, config, providers, turn
from .parallel_client import ParallelClient

RESEARCH_PROMPT = """\
Research the cloud sandbox product {name} ({website}). These sandboxes run
code for AI agents: isolated compute that an agent or its harness controls.

Fill every field of the output schema from public sources: official docs and
pricing pages first, then changelogs and engineering blog posts. Use null for
anything the public record doesn't state. Do not guess numbers and do not copy
marketing claims; report what the docs actually say.
"""

EVENT_STREAM_QUERY = """\
New cloud sandbox products for AI agents (isolated compute that runs
agent-generated code), or major launches and pricing changes in existing ones.
Report the product name, the company, the website, and what changed, with
sources.
"""

DISCOVER_OBJECTIVE = """\
Find all cloud sandbox products built for running AI agents' code: hosted
isolated compute (VMs, microVMs, or containers) that an agent or its harness
creates and controls programmatically to execute agent-generated or untrusted
code.
"""

DISCOVER_MATCH_CONDITIONS = [
    {
        "name": "agent_sandbox",
        "description": (
            "The product provides hosted isolated compute (VM, microVM, or "
            "container sandboxes) marketed or documented for running code "
            "from AI agents or LLM-driven workflows."
        ),
    },
    {
        "name": "programmatic",
        "description": (
            "Sandboxes can be created and controlled programmatically "
            "through an API, SDK, or CLI."
        ),
    },
    {
        "name": "public_docs",
        "description": "The product has public documentation or a pricing page.",
    },
]


def research_all(
    client: ParallelClient, seeds: list[dict], processor: str
) -> dict[str, str]:
    """Kick off one research run per provider. Returns slug -> run_id."""
    runs = {}
    for seed in seeds:
        run_id = client.create_task_run(
            input=RESEARCH_PROMPT.format(**seed),
            processor=processor,
            output_schema=providers.PROVIDER_OUTPUT_SCHEMA,
            metadata={"site": "sandboxwatch", "slug": seed["slug"]},
        )
        runs[seed["slug"]] = run_id
        print(f"research started: {seed['name']} run_id={run_id}")
    return runs


def write_provider_file(seed: dict, result: dict) -> None:
    output = result.get("output", {})
    content = output.get("content")
    if isinstance(content, str):
        content = json.loads(content)
    citations = []
    for basis in output.get("basis") or []:
        for citation in basis.get("citations") or []:
            url = citation.get("url")
            if url and url not in citations:
                citations.append(url)
    record = {
        "name": seed["name"],
        "slug": seed["slug"],
        "website": seed["website"],
        "last_verified": datetime.date.today().isoformat(),
        "sources": [{"url": url} for url in citations[:12]]
        or [{"url": seed["website"]}],
        **content,
    }
    path = config.providers_dir() / f"{seed['slug']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    print(f"wrote {path}")


def _slugify(name: str) -> str:
    slug = "".join(c if c.isalnum() else "-" for c in name.lower())
    return "-".join(part for part in slug.split("-") if part)


def discover_providers(client: ParallelClient, seeds: list[dict]) -> list[dict]:
    """FindAll products the seed list doesn't cover yet. Returns new seeds."""
    findall_id = client.create_findall_run(
        objective=DISCOVER_OBJECTIVE,
        entity_type="products",
        match_conditions=DISCOVER_MATCH_CONDITIONS,
        generator="base",
        match_limit=40,
        exclude_names=[seed["name"] for seed in seeds],
    )
    print(f"findall run started: {findall_id}")
    result = client.findall_result(findall_id)
    taken = {seed["slug"] for seed in seeds}
    taken.update(seed["name"].lower() for seed in seeds)
    found: list[dict] = []
    for candidate in result.get("candidates", []):
        if candidate.get("match_status") != "matched":
            continue
        name = (candidate.get("name") or "").strip()
        url = (candidate.get("url") or "").strip()
        slug = _slugify(name)
        if not name or not url or not slug:
            continue
        if slug in taken or name.lower() in taken:
            continue
        taken.add(slug)
        found.append({"name": name, "slug": slug, "website": url})
    return found


def run_discovery(client: ParallelClient) -> int:
    """--discover mode: merge FindAll results into providers.json and exit.

    Research for the new seeds stays a separate bootstrap run, so the
    operator sees what was added before paying for deep research."""
    path = config.root_dir() / "providers.json"
    seeds = json.loads(path.read_text())
    found = discover_providers(client, seeds)
    if not found:
        print("discovery found nothing new")
        return 0
    for seed in found:
        print(f"discovered: {seed['name']} ({seed['website']}) slug={seed['slug']}")
    path.write_text(json.dumps(seeds + found, indent=2) + "\n")
    print(
        f"added {len(found)} providers to providers.json; research them with "
        "--only <slug> --skip-monitors, then refresh monitors"
    )
    return 0


def cancel_existing_monitors(client: ParallelClient) -> None:
    """Cancel the monitors recorded in data/monitors.json, if any.

    Re-running bootstrap would otherwise pile up a second full set of
    monitors, and every duplicate fires duplicate webhooks forever."""
    path = config.data_dir() / "monitors.json"
    if not path.is_file():
        return
    try:
        existing = json.loads(path.read_text())
    except json.JSONDecodeError:
        return
    created = list((existing.get("providers") or {}).values())
    created.append(existing.get("new_products"))
    for record in created:
        monitor_id = (record or {}).get("monitor_id")
        if not monitor_id:
            continue
        try:
            client.cancel_monitor(monitor_id)
            print(f"cancelled old monitor {monitor_id}")
        except Exception as exc:
            print(f"could not cancel {monitor_id}: {exc}", file=sys.stderr)


def create_monitors(
    client: ParallelClient, runs: dict[str, str], webhook_url: str, frequency: str
) -> dict:
    """One snapshot monitor per provider (diffs against the research run) plus
    one event-stream monitor that watches for new products."""
    cancel_existing_monitors(client)
    monitors: dict[str, dict] = {"providers": {}, "new_products": None}
    for slug, run_id in runs.items():
        created = client.create_monitor(
            monitor_type="snapshot",
            frequency=frequency,
            settings={"task_run_id": run_id},
            webhook_url=webhook_url,
            metadata={"site": "sandboxwatch", "slug": slug},
        )
        monitors["providers"][slug] = created
        print(
            f"snapshot monitor for {slug}: {created.get('monitor_id') or created.get('id')}"
        )
    # Backfill seeds the first execution with a sample of recent history so
    # launches from before the monitor existed can still surface; base digs
    # deeper than lite and this monitor runs only once a day.
    monitors["new_products"] = client.create_monitor(
        monitor_type="event_stream",
        frequency=frequency,
        settings={"query": EVENT_STREAM_QUERY, "include_backfill": True},
        webhook_url=webhook_url,
        processor="base",
        metadata={"site": "sandboxwatch", "kind": "new_products"},
    )
    print("event-stream monitor created")
    (config.data_dir() / "monitors.json").write_text(
        json.dumps(monitors, indent=2, sort_keys=True) + "\n"
    )
    return monitors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--webhook-url", help="public URL of this box's /hooks/parallel"
    )
    parser.add_argument(
        "--processor", default="core", help="Parallel processor for research"
    )
    parser.add_argument("--frequency", default="1d", help="monitor cadence, e.g. 1d")
    parser.add_argument("--only", help="research a single provider slug")
    parser.add_argument("--skip-monitors", action="store_true")
    parser.add_argument(
        "--trigger-monitor", help="force one monitor execution now and exit"
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help="FindAll new products into providers.json and exit",
    )
    args = parser.parse_args(argv)

    client = turn.default_client()
    if args.trigger_monitor:
        print(json.dumps(client.trigger_monitor_run(args.trigger_monitor), indent=2))
        return 0

    # Hold the busy marker so the server's idle self-sleep doesn't checkpoint
    # the box in the middle of research.
    marker = config.busy_marker()
    marker.touch()
    try:
        if args.discover:
            return run_discovery(client)
        return _run(client, args)
    finally:
        marker.unlink(missing_ok=True)


def _run(client: ParallelClient, args: argparse.Namespace) -> int:
    seeds = json.loads((config.root_dir() / "providers.json").read_text())
    if args.only:
        seeds = [s for s in seeds if s["slug"] == args.only]
        if not seeds:
            print(f"unknown provider slug: {args.only}", file=sys.stderr)
            return 1
        if not args.skip_monitors:
            # create_monitors replaces the full monitor set with one built
            # from this run, which for --only would cancel every other
            # provider's monitor. Refresh monitors with a full bootstrap.
            print("--only implies --skip-monitors; keeping existing monitors")
            args.skip_monitors = True

    runs = research_all(client, seeds, args.processor)
    failures = []
    for seed in seeds:
        run_id = runs[seed["slug"]]
        print(f"waiting for {seed['name']} ({run_id})...")
        try:
            write_provider_file(seed, client.task_result(run_id))
        except Exception as exc:
            failures.append(seed["slug"])
            print(f"research failed for {seed['slug']}: {exc}", file=sys.stderr)

    errors = providers.validate_all()
    if errors:
        print("validation problems:", *errors, sep="\n  ", file=sys.stderr)

    if not args.skip_monitors:
        if not args.webhook_url:
            print("--webhook-url is required to create monitors", file=sys.stderr)
            return 1
        create_monitors(client, runs, args.webhook_url, args.frequency)

    changelog.append(
        {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(
                timespec="seconds"
            ),
            "kind": "bootstrap",
            "status": "applied" if not failures else "partial",
            "summary": f"Researched {len(seeds) - len(failures)} of {len(seeds)} providers "
            "and set up web monitors",
            "details": "",
            "citations": [],
            "commit": None,
            "duration_seconds": None,
            "est_cost_usd": None,
            "validation_errors": errors[:10] + [f"failed: {slug}" for slug in failures],
        }
    )
    turn.commit_and_push("bootstrap: refresh provider research")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
