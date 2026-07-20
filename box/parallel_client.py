"""Minimal client for the Parallel endpoints this demo uses.

Docs: https://docs.parallel.ai. Auth is an x-api-key header. Webhook
signatures follow the Standard Webhooks spec (webhook-id, webhook-timestamp,
and webhook-signature headers, HMAC-SHA256).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Any, Iterable, Mapping

import httpx

from . import config


class ParallelClient:
    def __init__(self, api_key: str, base_url: str | None = None):
        if not api_key:
            raise ValueError("a Parallel API key is required")
        self._http = httpx.Client(
            base_url=base_url or config.parallel_api_base(),
            headers={"x-api-key": api_key},
            timeout=60.0,
        )

    def create_task_run(
        self,
        *,
        input: str,
        processor: str,
        output_schema: dict | None = None,
        metadata: dict | None = None,
    ) -> str:
        body: dict[str, Any] = {"input": input, "processor": processor}
        if output_schema is not None:
            body["task_spec"] = {
                "output_schema": {"type": "json", "json_schema": output_schema}
            }
        if metadata:
            body["metadata"] = metadata
        resp = self._http.post("/v1/tasks/runs", json=body)
        resp.raise_for_status()
        return resp.json()["run_id"]

    def task_result(self, run_id: str, timeout_seconds: int = 600) -> dict:
        """Block until the run completes and return {run, output}."""
        resp = self._http.get(
            f"/v1/tasks/runs/{run_id}/result",
            params={"timeout": timeout_seconds},
            timeout=timeout_seconds + 30,
        )
        resp.raise_for_status()
        return resp.json()

    def create_monitor(
        self,
        *,
        monitor_type: str,
        frequency: str,
        settings: dict,
        webhook_url: str,
        processor: str = "lite",
        event_types: Iterable[str] = ("monitor.event.detected",),
        metadata: dict | None = None,
    ) -> dict:
        body: dict[str, Any] = {
            "type": monitor_type,
            "frequency": frequency,
            "processor": processor,
            "settings": settings,
            "webhook": {"url": webhook_url, "event_types": list(event_types)},
        }
        if metadata:
            body["metadata"] = metadata
        resp = self._http.post("/v1/monitors", json=body)
        resp.raise_for_status()
        return resp.json()

    def monitor_events(
        self, monitor_id: str, event_group_id: str | None = None
    ) -> list[dict]:
        params = {}
        if event_group_id:
            params["event_group_id"] = event_group_id
        resp = self._http.get(f"/v1/monitors/{monitor_id}/events", params=params)
        resp.raise_for_status()
        payload = resp.json()
        if isinstance(payload, list):
            return payload
        return payload.get("events", [])

    def create_findall_run(
        self,
        *,
        objective: str,
        entity_type: str,
        match_conditions: list[dict],
        generator: str = "base",
        match_limit: int = 40,
        exclude: Iterable[Mapping[str, str]] = (),
    ) -> str:
        """Start a FindAll run (public beta) and return its findall_id.

        ``exclude`` entries need both ``name`` and ``url``."""
        body: dict[str, Any] = {
            "objective": objective,
            "entity_type": entity_type,
            "match_conditions": match_conditions,
            "generator": generator,
            "match_limit": match_limit,
        }
        excludes = [
            {"name": e["name"], "url": e["url"]} for e in exclude if e.get("name")
        ]
        if excludes:
            body["exclude_list"] = excludes
        resp = self._http.post("/v1beta/findall/runs", json=body)
        resp.raise_for_status()
        return resp.json()["findall_id"]

    def findall_result(
        self,
        findall_id: str,
        timeout_seconds: float = 1200,
        poll_seconds: float = 15,
    ) -> dict:
        """Poll a FindAll run until it finishes, then return {run, candidates}."""
        deadline = time.time() + timeout_seconds
        while True:
            resp = self._http.get(f"/v1beta/findall/runs/{findall_id}")
            resp.raise_for_status()
            # The run's status field is itself an object: {status, is_active, ...}.
            status = resp.json().get("status") or {}
            active = status.get("is_active")
            if active is None:
                active = status.get("status") in (
                    "queued",
                    "running",
                    "action_required",
                )
            if not active:
                break
            if time.time() > deadline:
                raise TimeoutError(f"findall run {findall_id} still active")
            time.sleep(poll_seconds)
        resp = self._http.get(f"/v1beta/findall/runs/{findall_id}/result")
        resp.raise_for_status()
        return resp.json()

    def cancel_monitor(self, monitor_id: str) -> None:
        """Cancel a monitor. Cancelled monitors stop executing for good."""
        resp = self._http.post(f"/v1/monitors/{monitor_id}/cancel")
        resp.raise_for_status()

    def trigger_monitor_run(self, monitor_id: str) -> dict:
        """Force a monitor execution now instead of waiting for its cadence."""
        resp = self._http.post(f"/v1/monitors/{monitor_id}/trigger")
        resp.raise_for_status()
        if resp.status_code == 204 or not resp.content:
            return {"monitor_id": monitor_id, "status": "triggered"}
        return resp.json()


def verify_webhook_signature(
    secret: str,
    headers: Mapping[str, str],
    body: bytes,
    *,
    tolerance_seconds: float = 300,
    now: float | None = None,
) -> bool:
    """Check a Standard Webhooks signature. Returns False on any mismatch."""
    msg_id = headers.get("webhook-id")
    timestamp = headers.get("webhook-timestamp")
    signatures = headers.get("webhook-signature")
    if not msg_id or not timestamp or not signatures:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    current = now if now is not None else time.time()
    if abs(current - ts) > tolerance_seconds:
        return False
    try:
        key = base64.b64decode(secret.removeprefix("whsec_"))
    except Exception:
        return False
    message = f"{msg_id}.{ts}.".encode() + body
    expected = base64.b64encode(
        hmac.new(key, message, hashlib.sha256).digest()
    ).decode()
    for part in signatures.split():
        version, _, signature = part.partition(",")
        if version == "v1" and hmac.compare_digest(signature, expected):
            return True
    return False
