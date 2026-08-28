#!/usr/bin/env python3
"""
Repository-owned HTTP client for the IAC Bus.

Used by swarm tooling (`scripts/swarm_harness.py`), deployment verification
(`scripts/bus_conformance.py`), and tests. Keeping one client here avoids each
agent re-implementing polling cursors and lease handling incorrectly.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Iterator, List, Mapping, Optional

import requests

DEFAULT_TIMEOUT = 15.0
DEFAULT_POLL_INTERVAL = 0.5


class BusError(RuntimeError):
    """Raised when the bus returns an unexpected response."""

    def __init__(self, message: str, status_code: Optional[int] = None, body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class BusClient:
    """Thin, thread-safe-enough client. Create one per worker thread."""

    def __init__(
        self,
        base_url: str,
        token: str = "",
        timeout: float = DEFAULT_TIMEOUT,
        session: Optional[requests.Session] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token or ""
        self.timeout = timeout
        self.session = session or requests.Session()

    # ------------------------------------------------------------------
    # plumbing
    # ------------------------------------------------------------------
    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.base_url}{path}"
        kwargs.setdefault("timeout", self.timeout)
        return self.session.request(method, url, headers=self._headers(), **kwargs)

    @staticmethod
    def _json_or_raise(resp: requests.Response, expected: Iterator[int]) -> Any:
        if resp.status_code not in tuple(expected):
            raise BusError(
                f"{resp.request.method} {resp.request.path_url} -> {resp.status_code}",
                status_code=resp.status_code,
                body=_safe_json(resp),
            )
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    # ------------------------------------------------------------------
    # core API
    # ------------------------------------------------------------------
    def health(self) -> Dict[str, Any]:
        resp = self._request("GET", "/health")
        return self._json_or_raise(resp, (200,))

    def post(
        self,
        message: Any,
        channel: str = "default",
        sender: str = "agent",
        type: str = "event",
        queue: str = "",
        priority: int = 0,
        **extra: Any,
    ) -> Dict[str, Any]:
        """Publish a message. Returns the stored message as the bus recorded it.

        Note the return value is the *server's* view, which may not echo every
        field that was sent; callers that care should compare explicitly.
        """
        payload: Dict[str, Any] = {
            "channel": channel,
            "sender": sender,
            "message": message,
            "type": type,
        }
        if queue:
            payload["queue"] = queue
        if priority:
            payload["priority"] = priority
        payload.update({k: v for k, v in extra.items() if v is not None})
        resp = self._request("POST", "/bus/messages", json=payload)
        return self._json_or_raise(resp, (201,))["message"]

    def poll(
        self,
        channel: str = "",
        since_id: str = "",
        limit: int = 50,
        include_queue: bool = False,
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"limit": limit}
        if channel:
            params["channel"] = channel
        if since_id:
            params["since_id"] = since_id
        if include_queue:
            params["include_queue"] = "true"
        resp = self._request("GET", "/bus/messages", params=params)
        return self._json_or_raise(resp, (200,))["messages"]

    def wait_for_messages(
        self,
        channel: str = "",
        since_id: str = "",
        timeout: float = 10.0,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Client-side long-poll.

        The bus has no server-side `wait_seconds` support yet (see
        `docs/SWARM_DEV_PLAN.md`, phase S2), so blocking reads are emulated by
        polling. Swap the body for a single long-poll request once the server
        implements it.
        """
        deadline = time.time() + timeout
        while True:
            messages = self.poll(channel=channel, since_id=since_id, limit=limit)
            if messages:
                return messages
            if time.time() >= deadline:
                return []
            time.sleep(poll_interval)

    def subscribe(
        self,
        channel: str = "",
        since_id: str = "",
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        stop_event: Optional[threading.Event] = None,
        limit: int = 50,
    ) -> Iterator[Dict[str, Any]]:
        """Yield messages forever, advancing the `since_id` cursor as it goes."""
        cursor = since_id
        while not (stop_event and stop_event.is_set()):
            messages = self.poll(channel=channel, since_id=cursor, limit=limit)
            for message in messages:
                yield message
            if messages:
                cursor = messages[-1]["id"]
            else:
                time.sleep(poll_interval)

    # ------------------------------------------------------------------
    # queue / work leasing
    # ------------------------------------------------------------------
    def claim(
        self, queue: str, worker: str, lease_seconds: Optional[float] = None
    ) -> Optional[Dict[str, Any]]:
        """Claim one task. Returns None when the queue has nothing pending."""
        payload: Dict[str, Any] = {"queue": queue, "worker": worker}
        if lease_seconds is not None:
            payload["lease_seconds"] = lease_seconds
        resp = self._request("POST", "/bus/queues/claim", json=payload)
        body = self._json_or_raise(resp, (200, 204))
        return body["message"] if body else None

    def ack(self, queue: str, message_id: str, worker: str, lease_id: str) -> Dict[str, Any]:
        resp = self._request(
            "POST",
            "/bus/queues/ack",
            json={
                "queue": queue,
                "message_id": message_id,
                "worker": worker,
                "lease_id": lease_id,
            },
        )
        return self._json_or_raise(resp, (200,))

    def nack(
        self,
        queue: str,
        message_id: str,
        worker: str,
        lease_id: str,
        requeue: bool = True,
    ) -> Dict[str, Any]:
        resp = self._request(
            "POST",
            "/bus/queues/nack",
            json={
                "queue": queue,
                "message_id": message_id,
                "worker": worker,
                "lease_id": lease_id,
                "requeue": requeue,
            },
        )
        return self._json_or_raise(resp, (200,))

    # ------------------------------------------------------------------
    # orchestration
    # ------------------------------------------------------------------
    def submit_job(self, job: Mapping[str, Any], sender: str = "orchestrator") -> Dict[str, Any]:
        return self.post(
            message={"job": dict(job)},
            channel="orchestration",
            sender=sender,
            type="orchestration.job",
        )

    def report_step(
        self,
        job_id: str,
        step_id: str,
        status: str,
        sender: str = "worker",
    ) -> Dict[str, Any]:
        return self.post(
            message={"job_id": job_id, "step_id": step_id, "status": status},
            channel="orchestration",
            sender=sender,
            type="orchestration.step.status",
        )

    def get_job(self, job_id: str) -> Dict[str, Any]:
        resp = self._request("GET", f"/bus/orchestration/jobs/{job_id}")
        return self._json_or_raise(resp, (200,))

    def get_ready_steps(self, job_id: str) -> List[Dict[str, Any]]:
        resp = self._request("GET", f"/bus/orchestration/jobs/{job_id}/ready")
        return self._json_or_raise(resp, (200,))["steps"]


def _safe_json(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return resp.text[:500]
