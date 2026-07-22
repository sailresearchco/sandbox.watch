#!/usr/bin/env python3
"""Create the Sailbox that runs sandboxwatch, deploy this repo into it, and
start the server. Run from your machine, not from inside a box:

    export SAIL_API_KEY=... PARALLEL_API_KEY=... PARALLEL_WEBHOOK_SECRET=...
    python launch.py --bootstrap

Requires the Sail SDK locally (pip install 'sail>=0.3.0'). Everything else
runs inside the box.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shlex
import sys
from pathlib import Path

import sail

HERE = Path(__file__).resolve().parent
REMOTE_ROOT = "/opt/sandboxwatch"
SECRETS_DIR = f"{REMOTE_ROOT}/secrets"
PORT = 8080
UPLOAD_DIRS = ("box", "site", "data")
UPLOAD_FILES = (
    "AGENTS.md",
    "LICENSE",
    "README.md",
    "launch.py",
    "providers.json",
    "pyproject.toml",
    ".gitignore",
)
SECRET_ENV_VARS = (
    "SAIL_API_KEY",
    "PARALLEL_API_KEY",
    "PARALLEL_WEBHOOK_SECRET",
    "GITHUB_TOKEN",
)
PATH_ENV = (
    "/root/.opencode/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
)


def _exec_checked(sb: sail.Sailbox, command: str, *, timeout: int = 120):
    result = sb.exec(command, timeout=timeout).wait()
    if result.exit_code != 0:
        raise RuntimeError(
            f"remote command failed exit_code={result.exit_code} "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
    return result


def _upload_bytes(sb: sail.Sailbox, remote_path: str, data: bytes) -> None:
    """Chunked base64 upload with a checksum, same pattern as the other Sail
    example hosts. Keeps every exec argv well under proxy limits."""
    encoded = base64.b64encode(data).decode("ascii")
    digest = hashlib.sha256(data).hexdigest()
    remote = shlex.quote(remote_path)
    tmp = shlex.quote(f"{remote_path}.tmp")
    parent = shlex.quote(str(Path(remote_path).parent))
    _exec_checked(sb, f"mkdir -p {parent} && : > {tmp}")
    for offset in range(0, len(encoded), 24000):
        chunk = encoded[offset : offset + 24000]
        _exec_checked(sb, f"base64 -d >> {tmp} <<'B64EOF'\n{chunk}\nB64EOF")
    _exec_checked(
        sb,
        f"test $(sha256sum {tmp} | awk '{{print $1}}') = {shlex.quote(digest)} && mv {tmp} {remote}",
    )


def build_image():
    print("building image (a few minutes on the first run)...")
    return (
        sail.Image.debian_arm64.apt_install(
            "curl", "git", "python3", "python3-pip", "ca-certificates"
        )
        .run_commands(
            "curl -fsSL https://opencode.ai/install | bash",
            "pip install --break-system-packages "
            "'sail>=0.3.0' 'fastapi>=0.111' 'uvicorn>=0.30' 'jinja2>=3.1' 'httpx>=0.27'",
        )
        .env({"PATH": PATH_ENV})
        .build(timeout=1800)
    )


def upload_tree(sb: sail.Sailbox, *, include_data: bool = True) -> None:
    # data/ and providers.json ship seed content for a new box. An existing
    # box owns both (bootstrap research, agent turns, census decisions), so
    # redeploys must not clobber them.
    dirs = UPLOAD_DIRS if include_data else tuple(d for d in UPLOAD_DIRS if d != "data")
    files = (
        UPLOAD_FILES
        if include_data
        else tuple(f for f in UPLOAD_FILES if f != "providers.json")
    )
    paths: list[Path] = [HERE / name for name in files]
    for dirname in dirs:
        paths.extend(p for p in sorted((HERE / dirname).rglob("*")) if p.is_file())
    for path in paths:
        if "__pycache__" in path.parts or not path.exists():
            continue
        rel = path.relative_to(HERE).as_posix()
        print(f"  uploading {rel}")
        _upload_bytes(sb, f"{REMOTE_ROOT}/{rel}", path.read_bytes())


def write_secrets(sb: sail.Sailbox) -> None:
    _exec_checked(sb, f"mkdir -p {SECRETS_DIR} && chmod 700 {SECRETS_DIR}")
    for var in SECRET_ENV_VARS:
        value = os.environ.get(var)
        if not value:
            continue
        target = f"{SECRETS_DIR}/{var.lower()}"
        _upload_bytes(sb, target, value.encode())
        _exec_checked(sb, f"chmod 600 {shlex.quote(target)}")


def write_runtime_env(sb: sail.Sailbox, repo_url: str | None) -> None:
    lines = [
        f"export SANDBOXWATCH_ROOT={REMOTE_ROOT}",
        f"export SANDBOXWATCH_SECRETS_DIR={SECRETS_DIR}",
        f"export SANDBOXWATCH_SAILBOX_ID={shlex.quote(sb.sailbox_id)}",
        f"export PATH={PATH_ENV}",
        "export HOME=/root",
    ]
    # Operator tuning passes through: a public hostname draws steady crawler
    # traffic, and a shorter idle window lets the box sleep in the gaps.
    for var in ("SANDBOXWATCH_IDLE_SECONDS", "SANDBOXWATCH_AGENT_TIMEOUT"):
        value = os.environ.get(var)
        if value:
            lines.append(f"export {var}={shlex.quote(value)}")
    # A bare --attach must not drop config the box already owns: keep the
    # existing public repo URL when this run doesn't set a new one.
    if not repo_url:
        current = sb.exec(
            f"sed -n 's/^export SANDBOXWATCH_REPO_URL=//p' {SECRETS_DIR}/runtime.env"
            " 2>/dev/null | head -1",
            timeout=30,
        ).wait()
        existing = (current.stdout or "").strip().strip("'\"")
        if existing:
            repo_url = existing
    if repo_url:
        lines.append(f"export SANDBOXWATCH_REPO_URL={shlex.quote(repo_url)}")
    _upload_bytes(sb, f"{SECRETS_DIR}/runtime.env", ("\n".join(lines) + "\n").encode())
    _exec_checked(sb, f"chmod 600 {SECRETS_DIR}/runtime.env")


def write_opencode_config(sb: sail.Sailbox) -> None:
    api_key = os.environ["SAIL_API_KEY"]
    config = {
        "$schema": "https://opencode.ai/config.json",
        "model": "sail/zai-org/GLM-5.2-FP8",
        "provider": {
            "sail": {
                "npm": "@ai-sdk/openai-compatible",
                "options": {
                    "baseURL": "https://api.sailresearch.com/v1",
                    "apiKey": api_key,
                },
                "models": {"zai-org/GLM-5.2-FP8": {"name": "GLM-5.2"}},
            }
        },
    }
    _upload_bytes(
        sb,
        "/root/.config/opencode/opencode.jsonc",
        json.dumps(config, indent=2).encode(),
    )
    _exec_checked(sb, "chmod 600 /root/.config/opencode/opencode.jsonc")


def init_git(sb: sail.Sailbox, github_repo: str | None) -> None:
    _exec_checked(
        sb,
        f"cd {REMOTE_ROOT} && (git rev-parse --git-dir >/dev/null 2>&1 || git init -q -b main)"
        " && git config user.name sandboxwatch"
        " && git config user.email sandboxwatch@users.noreply.github.com"
        " && git add -A && (git diff --cached --quiet || git commit -qm 'deploy: sandboxwatch')",
    )
    # If the box already has a public remote, publish the deploy right away
    # instead of waiting for the next agent turn's push.
    sb.exec(
        f"cd {REMOTE_ROOT} && git remote get-url origin >/dev/null 2>&1"
        " && git push -q origin HEAD || true",
        timeout=120,
    ).wait()
    if not github_repo:
        return
    if not os.environ.get("GITHUB_TOKEN"):
        print(
            "warning: --github-repo set but GITHUB_TOKEN is missing; commits stay local"
        )
        return
    credentials = f"https://x-access-token:{os.environ['GITHUB_TOKEN']}@github.com\n"
    _upload_bytes(sb, f"{SECRETS_DIR}/git-credentials", credentials.encode())
    _exec_checked(
        sb,
        f"chmod 600 {SECRETS_DIR}/git-credentials && cd {REMOTE_ROOT}"
        f" && git config credential.helper 'store --file {SECRETS_DIR}/git-credentials'"
        f" && (git remote get-url origin >/dev/null 2>&1"
        f"     || git remote add origin https://github.com/{github_repo}.git)",
    )
    push = sb.exec(f"cd {REMOTE_ROOT} && git push -u origin main", timeout=120).wait()
    if push.exit_code != 0:
        print(f"warning: initial push failed, commits stay local: {push.stderr[-300:]}")


def start_server(sb: sail.Sailbox) -> str:
    # Kill any server from a previous deploy in its own exec. A pkill inside
    # the start command would match that command's own shell (its command line
    # contains the uvicorn invocation text) and kill the loop before it runs;
    # here the brackets keep the patterns from matching this exec itself.
    # Stray agent turns die too: a restart orphans them with no harness left
    # to validate or commit, and they keep editing the shared tree.
    sb.exec(
        "pkill -f '[u]vicorn box.server' 2>/dev/null; "
        "pkill -f '[o]pencode run' 2>/dev/null; true",
        timeout=30,
    ).wait()
    command = (
        "set -eu; "
        f"cd {REMOTE_ROOT}; mkdir -p /var/log; "
        "while true; do "
        f"  set -a; . {SECRETS_DIR}/runtime.env; set +a; "
        f"  python3 -m uvicorn box.server:app --host 0.0.0.0 --port {PORT} "
        "    >> /var/log/sandboxwatch.log 2>&1 || true; "
        "  sleep 5; "
        "done"
    )
    sb.exec(command, background=True, cwd=REMOTE_ROOT)
    listener = sb.wait_for_listener(PORT, timeout=120)
    return listener.endpoint.url.rstrip("/")


def run_bootstrap(sb: sail.Sailbox, url: str, processor: str, frequency: str) -> None:
    print("running bootstrap research (this can take a while)...")
    command = (
        f"cd {REMOTE_ROOT} && set -a && . {SECRETS_DIR}/runtime.env && set +a && "
        f"python3 -m box.bootstrap --webhook-url {shlex.quote(url + '/hooks/parallel')} "
        f"--processor {shlex.quote(processor)} --frequency {shlex.quote(frequency)}"
    )
    result = sb.exec(command, timeout=3600).wait()
    print(result.stdout)
    if result.exit_code != 0:
        print(result.stderr, file=sys.stderr)
        raise SystemExit("bootstrap failed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default="sandboxwatch")
    parser.add_argument("--app", default="sandboxwatch")
    parser.add_argument("--size", default="s", choices=["s", "m"])
    parser.add_argument(
        "--bootstrap", action="store_true", help="run research after launch"
    )
    parser.add_argument(
        "--processor", default="core", help="Parallel processor for research"
    )
    parser.add_argument("--frequency", default="1d", help="monitor cadence")
    parser.add_argument("--github-repo", help="owner/name remote for the box's commits")
    parser.add_argument(
        "--attach", help="redeploy into an existing Sailbox id instead of creating"
    )
    args = parser.parse_args()

    if not os.environ.get("SAIL_API_KEY"):
        raise SystemExit("SAIL_API_KEY is required")
    for var in ("PARALLEL_API_KEY", "PARALLEL_WEBHOOK_SECRET"):
        if not os.environ.get(var):
            print(f"warning: {var} is not set; research and webhooks need it")

    if args.attach:
        sb = sail.Sailbox.get(args.attach)
        print(f"attaching to existing sailbox {sb.sailbox_id}")
    else:
        image = build_image()
        app = sail.App.find(name=args.app, mint_if_missing=True)
        print(
            f"creating sailbox name={args.name!r} app={args.app!r} size={args.size!r}"
        )
        sb = sail.Sailbox.create(
            image=image,
            app=app,
            name=args.name,
            size=args.size,
            ingress_ports=[PORT],
        )

    print("deploying the repo into the box")
    upload_tree(sb, include_data=not args.attach)
    write_secrets(sb)
    repo_url = f"https://github.com/{args.github_repo}" if args.github_repo else None
    write_runtime_env(sb, repo_url)
    write_opencode_config(sb)
    init_git(sb, args.github_repo)

    url = start_server(sb)
    webhook_url = f"{url}/hooks/parallel"
    print(
        "\nsandboxwatch is up:",
        f"sailbox:  {sb.sailbox_id}",
        f"site:     {url}",
        f"log:      {url}/log",
        f"webhook:  {webhook_url}",
        f"logs:     sail box exec {args.name} -- tail -f /var/log/sandboxwatch.log",
        sep="\n  ",
    )

    if args.bootstrap:
        run_bootstrap(sb, url, args.processor, args.frequency)
        print("bootstrap complete. The box will sleep when idle and wake on webhooks.")
    else:
        print(
            "\nnext: run the research and create monitors:\n"
            f"  python launch.py --attach {sb.sailbox_id} --bootstrap"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
