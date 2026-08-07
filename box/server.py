"""The web app that runs inside the Sailbox.

It serves the comparison site, receives Parallel monitor webhooks, runs agent
turns, and puts its own Sailbox to sleep when nothing is happening. The next
inbound request (a webhook or a visitor) wakes the box and lands here again.

Local preview, no Sailbox involved:

    SANDBOXWATCH_SELF_SLEEP=0 uvicorn box.server:app --reload
"""

from __future__ import annotations

import contextlib
import datetime
import json
import logging
import os
import threading
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from . import changelog, config, providers, turn
from .parallel_client import verify_webhook_signature

logger = logging.getLogger("sandboxwatch")
logging.basicConfig(level=logging.INFO)


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI):
    _maybe_start_worker()  # drain anything queued before a restart
    if config.self_sleep_enabled():
        threading.Thread(target=_sleep_when_idle, daemon=True).start()
    else:
        logger.info("self-sleep disabled (no SANDBOXWATCH_SAILBOX_ID or turned off)")
    yield


app = FastAPI(title="sandbox.watch", lifespan=_lifespan)
app.mount(
    "/static", StaticFiles(directory=str(config.site_dir() / "static")), name="static"
)
templates = Jinja2Templates(directory=str(config.site_dir() / "templates"))


_NA_CELL = Markup(
    '<span class="g na" role="img" aria-label="no cited public fact">–</span>'
)


def _cell(value) -> Markup:
    """Render a spec value: booleans and gaps as labelled glyphs, text as is."""
    if value is True:
        return Markup('<span class="g yes" role="img" aria-label="yes">✓</span>')
    if value is False:
        return Markup('<span class="g no" role="img" aria-label="no">✕</span>')
    if isinstance(value, list):
        return escape(", ".join(str(v) for v in value)) if value else _NA_CELL
    if value in (None, ""):
        return _NA_CELL
    return escape(str(value))


templates.env.filters["cell"] = _cell

_last_activity = time.monotonic()
_turn_lock = threading.Lock()


def _touch_activity() -> None:
    global _last_activity
    _last_activity = time.monotonic()


@app.middleware("http")
async def canonical_host(request: Request, call_next):
    """Send www to the bare domain, so the site has one canonical URL.

    Done here rather than at a CDN so nothing sits in front of the box."""
    host = (request.headers.get("host") or "").split(":")[0]
    if host.startswith("www.") and len(host) > 4:
        # TLS terminates at the edge, so the request arrives as plain http.
        # Redirect straight to https, or the visitor pays a second hop
        # through the http-to-https redirect.
        scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
        target = request.url.replace(scheme=scheme, netloc=host[4:])
        return RedirectResponse(str(target), status_code=308)
    return await call_next(request)


@app.middleware("http")
async def track_activity(request: Request, call_next):
    # Only served requests count as activity. Failed ones don't: Parallel
    # retries rejected webhook deliveries with backoff, and a public
    # hostname draws a constant trickle of scanner probes for paths that
    # 404. Counting either would keep the box awake (and billed) around
    # the clock.
    response = await call_next(request)
    if response.status_code < 400:
        _touch_activity()
    return response


def _page(request: Request, template: str, **context) -> HTMLResponse:
    context.update(request=request, repo_url=config.repo_url())
    return templates.TemplateResponse(request, template, context)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    items = providers.load_providers()
    # Sort keys for the pricing column: dollars per vCPU-hour where the
    # provider states such a rate, None (unranked) everywhere else. The same
    # figure doubles as the compact display; other rows show their raw
    # wording, clamped by CSS until the column is expanded.
    price_keys = {
        p.get("slug"): providers.vcpu_hour_rate(p.get("price_headline")) for p in items
    }
    price_compact = {
        slug: f"${key:.4g}/vCPU-hr"
        for slug, key in price_keys.items()
        if key is not None
    }
    # Same idea for start/resume: seconds where a time is stated.
    start_keys = {
        p.get("slug"): providers.start_seconds(p.get("cold_start")) for p in items
    }
    return _page(
        request,
        "index.html",
        providers=items,
        spec_fields=providers.SPEC_FIELDS,
        price_keys=price_keys,
        price_compact=price_compact,
        start_keys=start_keys,
    )


@app.get("/p/{slug}", response_class=HTMLResponse)
def provider_detail(request: Request, slug: str):
    item = providers.load_provider(slug)
    if item is None:
        return HTMLResponse("Not found", status_code=404)
    return _page(request, "provider.html", p=item, spec_fields=providers.SPEC_FIELDS)


# Turn outcomes hidden from the public log. The raw changelog
# (data/changelog.jsonl) and the git history keep every turn as the audit
# trail; the page shows only the turns that changed something. A failed turn
# reverts and changes nothing, so it is noise here.
_HIDDEN_LOG_STATUSES = {"failed"}

# When the monitors last ran, shown on /log so a quiet day (monitors ran,
# nothing changed) is distinguishable from a stalled one. The box never hears
# about no-change runs (it only wakes on detected events), so the value comes
# from Parallel's own last_run_at. Cached in memory and fetched only while the
# box is already awake serving the page, so it adds no wakes.
_monitors_last_ran = {"display": None, "at": 0.0}
_MONITORS_LAST_RAN_TTL = 900


def _monitors_last_ran_display() -> str | None:
    now = time.time()
    if now - _monitors_last_ran["at"] < _MONITORS_LAST_RAN_TTL:
        return _monitors_last_ran["display"]
    display = _monitors_last_ran["display"]  # keep last good value on failure
    try:
        path = config.data_dir() / "monitors.json"
        recorded = json.loads(path.read_text())
        ids = {
            m.get("monitor_id") for m in (recorded.get("providers") or {}).values() if m
        }
        ids.add((recorded.get("new_products") or {}).get("monitor_id"))
        runs = [
            m.get("last_run_at")
            for m in turn.default_client().list_monitors()
            if m.get("monitor_id") in ids and m.get("last_run_at")
        ]
        latest = max(runs, default=None)
        if latest:
            # Hand the page an ISO instant, not a formatted string: the
            # template renders it in the reader's own zone.
            dt = datetime.datetime.fromisoformat(latest.replace("Z", "+00:00"))
            display = dt.isoformat(timespec="seconds")
    except Exception:
        logger.info("could not refresh monitors-last-ran", exc_info=True)
    _monitors_last_ran["display"] = display
    _monitors_last_ran["at"] = now
    return display


@app.get("/log", response_class=HTMLResponse)
def log_page(request: Request):
    entries = [
        e for e in changelog.read() if e.get("status") not in _HIDDEN_LOG_STATUSES
    ]
    return _page(
        request,
        "log.html",
        entries=entries,
        monitors_last_ran=_monitors_last_ran_display(),
    )


@app.get("/about", response_class=HTMLResponse)
def about_page(request: Request):
    return _page(request, "about.html")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/hooks/parallel")
async def parallel_hook(request: Request):
    secret = config.secret("parallel_webhook_secret")
    if not secret:
        # Fail closed: without a secret we can't tell Parallel from anyone else.
        return JSONResponse({"error": "webhook secret not configured"}, status_code=503)
    body = await request.body()
    if not verify_webhook_signature(secret, request.headers, body):
        return JSONResponse({"error": "bad signature"}, status_code=401)
    payload = json.loads(body)
    if payload.get("type") != "monitor.event.detected":
        return Response(status_code=204)
    _enqueue_turn(payload, dedupe_key=request.headers.get("webhook-id"))
    return {"ok": True}


def _pending_dir():
    path = config.state_dir() / "pending"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _enqueue_turn(payload: dict, dedupe_key: str | None = None) -> None:
    """Queue the event on disk, then make sure a worker is draining the queue.

    Disk first so an event survives a restart between ack and processing.
    Redeliveries of the same webhook reuse their id, so keying the queue file
    on it collapses retries into one turn."""
    if dedupe_key:
        safe = "".join(c for c in dedupe_key if c.isalnum() or c in "-_.")[:80]
        name = f"wh-{safe}.json"
    else:
        name = (
            f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
            f"-{uuid.uuid4().hex[:8]}.json"
        )
    (_pending_dir() / name).write_text(json.dumps(payload))
    _maybe_start_worker()


def _maybe_start_worker() -> None:
    if _turn_lock.acquire(blocking=False):
        try:
            threading.Thread(target=_drain_pending, daemon=True).start()
        except Exception:
            # A failed thread start must not wedge the queue forever.
            _turn_lock.release()
            raise


def _drain_pending() -> None:
    try:
        _drain_loop()
        # New products an agent turn created arrive as near-empty stubs.
        # Research them and start monitoring them before going back to sleep.
        for slug, status in turn.drain_pending_research():
            logger.info("first research for %s: %s", slug, status)
    finally:
        _turn_lock.release()
        _touch_activity()
    # A webhook that landed between the final empty scan and the lock
    # release found no worker to start; pick its file up now.
    if sorted(_pending_dir().glob("*.json")):
        _maybe_start_worker()


def _drain_loop() -> None:
    while True:
        pending = sorted(_pending_dir().glob("*.json"))
        if not pending:
            return
        for path in pending:
            # Claim the file first so a crash mid-turn cannot lose the
            # event: success deletes the claim, a first failure requeues
            # once, and a final failure parks the payload under
            # state/failed for inspection.
            claimed = path.with_suffix(".working")
            try:
                path.rename(claimed)
            except OSError:
                continue
            try:
                payload = json.loads(claimed.read_text())
            except json.JSONDecodeError:
                claimed.unlink(missing_ok=True)
                continue
            try:
                entry = turn.run_turn(payload)
                failed = entry.get("status") == "failed"
            except Exception:
                logger.exception("turn crashed")
                failed = True
            # One automatic retry, so a transiently slow model or a
            # crash does not silently drop the event until the next
            # daily diff happens to re-detect it.
            if failed and payload.get("_retries", 0) < 1:
                payload["_retries"] = payload.get("_retries", 0) + 1
                retry_name = f"retry-{path.name}"
                (_pending_dir() / retry_name).write_text(json.dumps(payload))
                claimed.unlink(missing_ok=True)
            elif failed:
                parked = config.state_dir() / "failed"
                parked.mkdir(parents=True, exist_ok=True)
                claimed.rename(parked / path.name)
            else:
                claimed.unlink(missing_ok=True)


def _sleep_self() -> None:
    # The SDK authenticates from the environment; secrets live in files here,
    # so surface the key before first use.
    key = config.secret("sail_api_key")
    if key:
        os.environ.setdefault("SAIL_API_KEY", key)
    # Imported lazily: the SDK is only installed inside the box.
    import sail

    box_id = config.sailbox_id()
    sail.Sailbox(
        sailbox_id=box_id,
        name="sandboxwatch",
        status="running",
        worker_address="",
        exec_endpoint="",
    ).sleep()


def _busy_hold() -> bool:
    """True while any long non-HTTP job (bootstrap research, an agent turn)
    holds a busy marker. Holds older than two hours are treated as stale so
    a killed job cannot keep the box awake forever."""
    paths = [config.busy_marker()]
    try:
        paths.extend(config.busy_holds_dir().iterdir())
    except OSError:
        pass
    now = time.time()
    for path in paths:
        try:
            if now - path.stat().st_mtime < 7200:
                return True
        except OSError:
            continue
    return False


def _sleep_when_idle() -> None:
    """Sleep the box once the server has been idle long enough.

    sleep() checkpoints the whole VM, this thread included. When ingress wakes
    the box the call returns and the loop continues where it left off."""
    while True:
        time.sleep(5)
        if _turn_lock.locked() or _busy_hold():
            continue
        if time.monotonic() - _last_activity < config.idle_seconds():
            continue
        before = time.time()
        try:
            logger.info("idle for %.0fs, sleeping the box", config.idle_seconds())
            _sleep_self()
        except Exception:
            logger.warning("self-sleep failed, retrying later", exc_info=True)
            time.sleep(30)
        else:
            slept = time.time() - before
            if slept > 5:
                logger.info("woke after %.0fs asleep", slept)
        _touch_activity()
