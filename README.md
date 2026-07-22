# sandbox.watch

A comparison site for AI agent sandboxes and cloud environments. The site
researches itself, hosts itself, and keeps itself current. Research and web
monitoring come from [Parallel](https://parallel.ai); hosting and the update
agent run in a single [Sailbox](https://sailresearch.com/sailboxes) that
sleeps between updates.

## How it works

1. `launch.py` builds a VM image, creates a Sailbox, deploys this repo into
   it, and starts the server.
2. `box/bootstrap.py` runs one Parallel deep-research task per product in
   `providers.json`, writes cited results to `data/providers/`, and creates
   the web monitors that watch for changes: one per product, plus one that
   watches for new sandbox launches.
3. The box then sleeps. A sleeping Sailbox costs nothing and wakes on
   inbound traffic.
4. When a monitor detects a change, Parallel's webhook wakes the box.
   `box/server.py` verifies the signature and hands the event to an
   [opencode](https://opencode.ai) agent running inside the VM. The agent
   edits the data files the event supports and commits.
5. New products enter the list the same way. When the new-launch monitor
   fires, the agent adds a row for the product it names. A separate
   discovery sweep (`bootstrap --discover --propose`) uses Parallel's
   FindAll to surface candidates the seed list misses, and the agent judges
   each one against the site's bar before it joins the list.
6. After about a minute of quiet, the box sleeps again. Visitors wake it the
   same way webhooks do.

Every update is one git commit and one entry on the site's `/log` page, so
the whole history is public.

The list is not a fixed set. It booted from a short starter list in
`providers.json`, then grows on its own: the discovery sweep in step 5 pulls
candidate products from across the web and the agent vets each one against a
single bar (a hosted sandbox an agent can drive, with public docs). A fresh
sweep turns up most of the starter products alongside new ones, so the table
tracks what's out there rather than a hand-picked few. Run a sweep yourself
with `python3 -m box.bootstrap --discover --propose`.

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
| `data/providers/` | One JSON file per product. A starter set ships in the repo; research fills each file and discovery grows the set. |
| `site/` | Templates and CSS. |

## License

Apache-2.0.

## Costs and keys

Sail bills compute only while the VM runs, and this VM mostly doesn't. A
month of hosting plus daily monitor updates lands in the low dollars,
Parallel included. API keys live in files inside the VM; they never appear
in this repo or its commits.
