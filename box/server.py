"""The web app that runs inside the Sailbox.

It serves the comparison site, receives Parallel monitor webhooks, runs agent
turns, and puts its own Sailbox to sleep when nothing is happening. The next
inbound request (a webhook or a visitor) wakes the box and lands here again.

Local preview, no Sailbox involved:

    SANDBOXWATCH_SELF_SLEEP=0 uvicorn box.server:app --reload
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
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
async def track_activity(request: Request, call_next):
    # Rejected webhook deliveries don't count as activity: Parallel retries
    # failed deliveries with backoff, and anyone can POST to a public URL.
    # Counting those would keep the box awake (and billed) indefinitely.
    is_hook = request.url.path == "/hooks/parallel"
    if not is_hook:
        _touch_activity()
    response = await call_next(request)
    if not is_hook or response.status_code < 400:
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


@app.get("/log", response_class=HTMLResponse)
def log_page(request: Request):
    return _page(request, "log.html", entries=changelog.read())


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
        threading.Thread(target=_drain_pending, daemon=True).start()


def _drain_pending() -> None:
    try:
        while True:
            pending = sorted(_pending_dir().glob("*.json"))
            if not pending:
                return
            for path in pending:
                try:
                    payload = json.loads(path.read_text())
                except json.JSONDecodeError:
                    path.unlink(missing_ok=True)
                    continue
                path.unlink(missing_ok=True)
                try:
                    turn.run_turn(payload)
                except Exception:
                    logger.exception("turn crashed")
    finally:
        _turn_lock.release()
        _touch_activity()


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
