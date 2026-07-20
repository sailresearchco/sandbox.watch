"""Runtime configuration for the harness.

Everything resolves lazily from environment variables so tests and local runs
can point the harness at temporary directories. Inside the Sailbox, launch.py
writes secrets/runtime.env with the values below before the server starts.
"""

from __future__ import annotations

import os
from pathlib import Path


def root_dir() -> Path:
    env = os.environ.get("SANDBOXWATCH_ROOT")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    return Path(os.environ.get("SANDBOXWATCH_DATA_DIR") or root_dir() / "data")


def providers_dir() -> Path:
    return data_dir() / "providers"


def site_dir() -> Path:
    return root_dir() / "site"


def state_dir() -> Path:
    path = Path(os.environ.get("SANDBOXWATCH_STATE_DIR") or root_dir() / "state")
    path.mkdir(parents=True, exist_ok=True)
    return path


def busy_marker() -> Path:
    """Marker file that pauses idle self-sleep during long non-HTTP work.

    Bootstrap research runs for many minutes without touching the web server,
    and sleeping mid-run would freeze it and sever its API connections."""
    return state_dir() / "busy"


def secrets_dir() -> Path:
    return Path(
        os.environ.get("SANDBOXWATCH_SECRETS_DIR") or "/opt/sandboxwatch/secrets"
    )


def secret(name: str) -> str | None:
    """Read a secret from its file, falling back to the matching env var.

    Files win so the box never depends on process environment for keys.
    """
    path = secrets_dir() / name
    if path.is_file():
        value = path.read_text().strip()
        if value:
            return value
    return os.environ.get(name.upper()) or None


def parallel_api_base() -> str:
    return os.environ.get("PARALLEL_API_BASE") or "https://api.parallel.ai"


def http_port() -> int:
    return int(os.environ.get("SANDBOXWATCH_PORT") or "8080")


def idle_seconds() -> float:
    return float(os.environ.get("SANDBOXWATCH_IDLE_SECONDS") or "60")


def self_sleep_enabled() -> bool:
    if os.environ.get("SANDBOXWATCH_SELF_SLEEP", "1") != "1":
        return False
    return sailbox_id() is not None


def sailbox_id() -> str | None:
    return os.environ.get("SANDBOXWATCH_SAILBOX_ID") or None


def repo_url() -> str | None:
    """Public URL of the site's own git repo, shown in the footer when set."""
    return os.environ.get("SANDBOXWATCH_REPO_URL") or None


def agent_cmd() -> str:
    """Command that runs one headless agent turn. Receives the prompt file path
    as its final argument. Overridable so tests can substitute a stub."""
    return (
        os.environ.get("SANDBOXWATCH_AGENT_CMD")
        or f"bash {root_dir() / 'box' / 'agent.sh'}"
    )


def agent_timeout_seconds() -> float:
    return float(os.environ.get("SANDBOXWATCH_AGENT_TIMEOUT") or "900")


# Cost model for the per-turn estimate shown on /log. One small Sailbox vCPU
# plus a couple GiB of RAM, from docs.sailresearch.com/sailboxes-pricing.
EST_HOURLY_USD = 1 * 0.015 + 2 * 0.008
