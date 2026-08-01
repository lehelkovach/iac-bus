#!/usr/bin/env python3
"""
OpenClaw-compatible adapter for the IAC Bus REST API.

The adapter intentionally uses only the Python standard library so it can be
vendored into agent runtimes without installing additional dependencies.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Optional, Tuple


Transport = Callable[[str, str, Dict[str, str], Optional[bytes], int], Tuple[int, str, bytes]]


class IacBusError(RuntimeError):
    """Raised when the IAC Bus API returns an error response."""

    def __init__(self, status: int, message: str):
        super().__init__(f"IAC Bus request failed ({status}): {message}")
        self.status = status
        self.message = message


def _default_transport(
    method: str,
    url: str,
    headers: Dict[str, str],
    body: Optional[bytes],
    timeout: int,
) -> Tuple[int, str, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content_type = response.headers.get("Content-Type", "")
            return response.status, content_type, response.read()
    except urllib.error.HTTPError as exc:
        content_type = exc.headers.get("Content-Type", "") if exc.headers else ""
        return exc.code, content_type, exc.read()


def _clean_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}


class IacBusClient:
    """Small typed wrapper around the IAC Bus REST endpoints."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        timeout: int = 30,
        transport: Optional[Transport] = None,
    ):
        base_url = base_url or os.environ.get("IAC_BUS_URL", "")
        if not base_url:
            raise ValueError("IAC_BUS_URL or base_url is required")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.transport = transport or _default_transport
        self.headers = {"Content-Type": "application/json"}
        token = token if token is not None else os.environ.get("IAC_BUS_TOKEN", "")
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[dict] = None,
        query: Optional[dict] = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        if query:
            clean_query = {key: value for key, value in query.items() if value not in (None, "")}
            if clean_query:
                url = f"{url}?{urllib.parse.urlencode(clean_query)}"
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        status, content_type, data = self.transport(
            method,
            url,
            dict(self.headers),
            body,
            self.timeout,
        )
        if status >= 400:
            message = data.decode("utf-8", "replace") if data else ""
            raise IacBusError(status, message)
        if status == 204 or not data:
            return None
        if "application/json" in content_type or data[:1] in (b"{", b"["):
            return json.loads(data.decode("utf-8"))
        return data

    def health(self) -> dict:
        return self._request("GET", "/health")

    def post_message(
        self,
        message: Any,
        channel: str = "default",
        sender: str = "agent",
        message_type: str = "event",
        protocol: str = "iac-bus/1.0",
        conversation_id: Optional[str] = None,
        reply_to: Optional[str] = None,
        recipient: Optional[str] = None,
        group: Optional[str] = None,
        queue: str = "",
        priority: int = 0,
        headers: Optional[dict] = None,
        ttl_seconds: Optional[float] = None,
    ) -> dict:
        payload = _clean_payload(
            {
                "protocol": protocol,
                "channel": channel,
                "sender": sender,
                "message": message,
                "type": message_type,
                "conversation_id": conversation_id,
                "reply_to": reply_to,
                "recipient": recipient,
                "group": group,
                "queue": queue,
                "priority": priority,
                "headers": headers,
                "ttl_seconds": ttl_seconds,
            }
        )
        return self._request("POST", "/bus/messages", payload)

    def list_messages(
        self,
        channel: str = "",
        since_id: str = "",
        limit: int = 50,
        include_queue: bool = False,
    ) -> dict:
        return self._request(
            "GET",
            "/bus/messages",
            query={
                "channel": channel,
                "since_id": since_id,
                "limit": limit,
                "include_queue": str(include_queue).lower(),
            },
        )

    poll_messages = list_messages

    def claim_queue(self, queue: str, worker: str, lease_seconds: Optional[float] = None) -> Optional[dict]:
        return self._request(
            "POST",
            "/bus/queues/claim",
            _clean_payload({"queue": queue, "worker": worker, "lease_seconds": lease_seconds}),
        )

    def ack_queue(self, queue: str, message_id: str, worker: str, lease_id: str) -> dict:
        return self._request(
            "POST",
            "/bus/queues/ack",
            {"queue": queue, "message_id": message_id, "worker": worker, "lease_id": lease_id},
        )

    def nack_queue(
        self,
        queue: str,
        message_id: str,
        worker: str,
        lease_id: str,
        requeue: bool = True,
    ) -> dict:
        return self._request(
            "POST",
            "/bus/queues/nack",
            {
                "queue": queue,
                "message_id": message_id,
                "worker": worker,
                "lease_id": lease_id,
                "requeue": requeue,
            },
        )

    def session_progress(
        self,
        session_id: str,
        sender: str,
        message: str,
        channel: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> dict:
        return self._post_session_event("progress", session_id, sender, message, channel, metadata)

    def session_blocker(
        self,
        session_id: str,
        sender: str,
        message: str,
        channel: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> dict:
        return self._post_session_event("blocker", session_id, sender, message, channel, metadata)

    def session_done(
        self,
        session_id: str,
        sender: str,
        message: str,
        channel: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> dict:
        return self._post_session_event("done", session_id, sender, message, channel, metadata)

    def _post_session_event(
        self,
        event_type: str,
        session_id: str,
        sender: str,
        text: str,
        channel: Optional[str],
        metadata: Optional[dict],
    ) -> dict:
        return self.post_message(
            {
                "text": text,
                "session_id": session_id,
                "metadata": metadata or {},
            },
            channel=channel or f"session.{session_id}",
            sender=sender,
            message_type=event_type,
            conversation_id=session_id,
        )


def _json_value(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _json_object(value: Optional[str]) -> Optional[dict]:
    if not value:
        return None
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("value must be a JSON object")
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="IAC Bus OpenClaw skill adapter")
    parser.add_argument("--base-url", default=os.environ.get("IAC_BUS_URL", ""), help="IAC Bus URL")
    parser.add_argument("--token", default=os.environ.get("IAC_BUS_TOKEN", ""), help="Bearer token")
    parser.add_argument("--timeout", type=int, default=30)

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("health")

    post = sub.add_parser("post")
    post.add_argument("--message", required=True, help="JSON value or plain text")
    post.add_argument("--channel", default="default")
    post.add_argument("--sender", default="agent")
    post.add_argument("--type", default="event", dest="message_type")
    post.add_argument("--protocol", default="iac-bus/1.0")
    post.add_argument("--conversation-id", default=None)
    post.add_argument("--queue", default="")
    post.add_argument("--priority", type=int, default=0)
    post.add_argument("--headers", type=_json_object)

    list_cmd = sub.add_parser("list")
    list_cmd.add_argument("--channel", default="")
    list_cmd.add_argument("--since-id", default="")
    list_cmd.add_argument("--limit", type=int, default=50)
    list_cmd.add_argument("--include-queue", action="store_true")

    claim = sub.add_parser("claim")
    claim.add_argument("--queue", required=True)
    claim.add_argument("--worker", required=True)
    claim.add_argument("--lease-seconds", type=float, default=None)

    for name in ("ack", "nack"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--queue", required=True)
        cmd.add_argument("--message-id", required=True)
        cmd.add_argument("--worker", required=True)
        cmd.add_argument("--lease-id", required=True)
        if name == "nack":
            cmd.add_argument("--drop", action="store_true", help="Do not requeue the message")

    for name in ("progress", "blocker", "done"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--session-id", required=True)
        cmd.add_argument("--sender", required=True)
        cmd.add_argument("--message", required=True)
        cmd.add_argument("--channel", default=None)
        cmd.add_argument("--metadata", type=_json_object)

    return parser


def main() -> int:
    args = _build_parser().parse_args()
    bus = IacBusClient(args.base_url, token=args.token, timeout=args.timeout)

    if args.command == "health":
        result = bus.health()
    elif args.command == "post":
        result = bus.post_message(
            _json_value(args.message),
            channel=args.channel,
            sender=args.sender,
            message_type=args.message_type,
            protocol=args.protocol,
            conversation_id=args.conversation_id,
            queue=args.queue,
            priority=args.priority,
            headers=args.headers,
        )
    elif args.command == "list":
        result = bus.list_messages(
            channel=args.channel,
            since_id=args.since_id,
            limit=args.limit,
            include_queue=args.include_queue,
        )
    elif args.command == "claim":
        result = bus.claim_queue(args.queue, args.worker, lease_seconds=args.lease_seconds)
    elif args.command == "ack":
        result = bus.ack_queue(args.queue, args.message_id, args.worker, args.lease_id)
    elif args.command == "nack":
        result = bus.nack_queue(args.queue, args.message_id, args.worker, args.lease_id, requeue=not args.drop)
    elif args.command == "progress":
        result = bus.session_progress(args.session_id, args.sender, args.message, args.channel, args.metadata)
    elif args.command == "blocker":
        result = bus.session_blocker(args.session_id, args.sender, args.message, args.channel, args.metadata)
    elif args.command == "done":
        result = bus.session_done(args.session_id, args.sender, args.message, args.channel, args.metadata)
    else:
        raise AssertionError(f"unhandled command {args.command}")

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
