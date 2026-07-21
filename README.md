# sandbox.watch

A comparison site for the cloud sandboxes that run AI agents' code. The site
researches itself, hosts itself, and keeps itself current. Research and web
monitoring come from [Parallel](https://parallel.ai); hosting and the update
agent run in a single [Sailbox](https://sailresearch.com/sailboxes) that
sleeps between updates.

## How it works

One VM, two external APIs.

1. `launch.py` builds a VM image, creates a Sailbox, deploys this repo into
   it, and starts the server.
2. `box/bootstrap.py` runs one Parallel deep-research task per product in
   `providers.json`, writes cited results to `data/providers/`, and creates
   the web monitors that watch for changes.
3. The box then sleeps. A sleeping Sailbox costs nothing and wakes on
   inbound traffic.
4. When a monitor detects a change, Parallel's webhook wakes the box.
   `box/server.py` verifies the signature and hands the event to an
   [opencode](https://opencode.ai) agent running inside the VM. The agent
   edits the data files the event supports and commits.
5. After about a minute of quiet, the box sleeps again. Visitors wake it the
   same way webhooks do.

Every update is one git commit and one entry on the site's `/log` page, so
the whole history is public.

## Run your own

You need two accounts: [Sail](https://app.sailresearch.com) for the Sailbox
and inference, and [Parallel](https://platform.parallel.ai) for research and
monitoring. Both have free credit tiers that cover this demo.

```bash
export SAIL_API_KEY=...                  # app.sailresearch.com
export PARALLEL_API_KEY=...              # platform.parallel.ai
export PARALLEL_WEBHOOK_SECRET=whsec_... # Parallel settings, Webhooks page

pip install 'sail>=0.3.0'
python launch.py --bootstrap
```

The launcher prints the site URL once the box is serving. Bootstrap research
takes a few minutes per product. To give the box a public git remote for its
commits, set `GITHUB_TOKEN` and pass `--github-repo you/your-fork`.

## Local preview

The site runs anywhere Python does; no Sailbox needed while hacking on it.

```bash
uv sync
SANDBOXWATCH_SELF_SLEEP=0 uv run uvicorn box.server:app --reload
uv run pytest
```

## Layout

| Path | What it is |
| --- | --- |
| `launch.py` | Creates the box and deploys everything. The only file that runs on your machine. |
| `box/server.py` | The web app: site pages, the webhook endpoint, idle self-sleep. |
| `box/bootstrap.py` | One-time research and monitor setup. |
| `box/turn.py` | Runs one agent turn per monitor event and commits the result. |
| `box/agent.sh` | The headless opencode invocation. |
| `AGENTS.md` | Instructions the in-box agent follows, data rules and writing style included. |
| `data/providers/` | One JSON file per product. Ships with seed entries; bootstrap replaces them. |
| `site/` | Templates and CSS. |

## License

Apache-2.0.

## Costs and keys

Sail bills compute only while the VM runs, and this VM mostly doesn't. A
month of hosting plus daily monitor updates lands in the low dollars,
Parallel included. API keys live in files inside the VM; they never appear
in this repo or its commits.
