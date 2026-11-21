"""Read-only WebSocket client for what the `paseo` CLI does not expose or makes expensive.

`list_commands_request` returns an agent's slash commands and skills (the Paseo app
fills its `/` menu from it). `DaemonSession` keeps one connection open and asks
`fetch_agent_request` for the agent's status and pending-permission count, so a status poll
does not start a Node `paseo` process. Everything else herdr4paseo does still goes through the
`paseo` CLI. This module only reads: it never sends prompts or answers permissions.

Wire format (getpaseo/paseo, packages/protocol/src/messages.ts): the client opens `/ws`, sends
`{"type": "hello", ...}`, then `{"type": "session", "message": {...}}` frames. The daemon answers
with `{"type": "session", "message": {"type": "fetch_agent_response", "payload": {"requestId",
"agent", "error"}}}`, or an `rpc_error` carrying the same `requestId`; a refused hello is
`hello.rejected` followed by a close. The daemon admits a hello asynchronously and answers it with a
`status` frame (`server_info`); a session frame sent before that is rejected and the socket closed,
so `DaemonSession` waits for it. A hello without `capabilities` makes the daemon treat the socket as
a legacy client and stream it every agent's events, so the session says it is a modern one.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import urlsplit

import websockets

from .core import Command, parse_commands

DEFAULT_LISTEN = "127.0.0.1:6767"
LOOPBACK = ("127.0.0.1", "localhost", "::1")
MAX_FRAME_BYTES = 4 << 20  # an agent snapshot is a few KiB; refuse anything bigger than this on the wire
MAX_BAD_FRAMES = 20  # unparseable or unrelated-looking frames tolerated per connection before dropping it
MAX_IN_FLIGHT = 8
MAX_STATUS_CHARS = 32  # the longest lifecycle status is "initializing"; a hostile peer cannot fill the header
# packages/protocol/src/client-capabilities.ts. owned_subscriptions is the one that matters here: without it the
# daemon streams every agent's timeline and events into this socket for as long as it stays open.
CAPABILITIES = {
    "hello_rejection": True,
    "owned_subscriptions": True,
    "explicit_event_subscriptions": True,
    "selective_agent_timeline": True,
    "all_providers": True,
}
BACKOFF_FIRST = 1.0  # seconds before the first reconnect attempt after a failure
BACKOFF_MAX = 60.0
HELLO_REJECTED = ("hello.rejected", "hello_rejected")  # the daemon sends the dotted form
ENV_DAEMON_STATUS = "HERDR4PASEO_DAEMON_STATUS"  # "0" / "off" keeps every status poll on the CLI


class DaemonError(Exception):
    """The daemon could not be reached or refused the request."""


def paseo_home() -> Path:
    return Path(os.environ.get("PASEO_HOME") or Path.home() / ".paseo")


def daemon_url() -> str:
    """ws:// URL of the daemon: PASEO_HOST, else the home's config.json listen address."""
    host = os.environ.get("PASEO_HOST")
    if not host:
        try:
            host = json.loads((paseo_home() / "config.json").read_text())["daemon"]["listen"]
        except (OSError, ValueError, KeyError, TypeError):
            host = DEFAULT_LISTEN
        if not isinstance(host, str) or not host:  # a config the daemon itself would refuse
            host = DEFAULT_LISTEN
    if "://" not in host:
        host = f"ws://{host}"
    parts = urlsplit(host)
    scheme = {"http": "ws", "https": "wss"}.get(parts.scheme, parts.scheme)
    return f"{scheme}://{parts.netloc}{parts.path or '/ws'}"


def local_credential(url: str) -> str | None:
    """The daemon's local credential, only ever for a loopback daemon: never sent to a remote host."""
    if urlsplit(url).hostname not in LOOPBACK:
        return None
    try:
        return (paseo_home() / "local-credential").read_text().strip() or None
    except OSError:
        return None


def hello_message(url: str, *, modern: bool = False) -> dict[str, object]:
    hello: dict[str, object] = {
        "type": "hello",
        "clientId": "herdr4paseo",
        "clientType": "cli",
        "protocolVersion": 1,
    }
    if modern:
        hello["capabilities"] = dict(CAPABILITIES)
    token = local_credential(url)
    if token:
        hello["auth"] = {"kind": "localCredential", "token": token}
    return hello


async def fetch_commands(agent_id: str, *, timeout: float = 30.0) -> list[Command]:
    url = daemon_url()
    hello = hello_message(url)
    request_id = str(uuid.uuid4())
    request = {
        "type": "session",
        "message": {"type": "list_commands_request", "agentId": agent_id, "requestId": request_id},
    }
    try:
        async with asyncio.timeout(timeout), websockets.connect(url, open_timeout=timeout, proxy=None) as ws:
            await ws.send(json.dumps(hello))
            await ws.send(json.dumps(request))
            async for raw in ws:
                message = json.loads(raw)
                if message.get("type") in HELLO_REJECTED:
                    raise DaemonError(f"daemon rejected the connection: {clip_reason(message.get('reason', 'unknown'))}")
                inner = message.get("message") if message.get("type") == "session" else None
                if not isinstance(inner, dict) or inner.get("type") != "list_commands_response":
                    continue
                payload = inner.get("payload")
                if not isinstance(payload, dict) or payload.get("requestId") != request_id:
                    continue
                if payload.get("error"):
                    raise DaemonError(str(payload["error"]))
                commands = payload.get("commands")
                return parse_commands(commands if isinstance(commands, list) else [])
    except DaemonError:
        raise
    except (OSError, TimeoutError, ValueError, TypeError, AttributeError, websockets.WebSocketException) as exc:
        raise DaemonError(f"{type(exc).__name__}: {exc}") from exc
    raise DaemonError("daemon closed the connection before answering")


class AgentState(NamedTuple):
    status: str
    pending: bool  # at least one permission request waits on an answer


def daemon_status_enabled(env: dict[str, str] | None = None) -> bool:
    value = (os.environ if env is None else env).get(ENV_DAEMON_STATUS, "").strip().lower()
    return value not in ("0", "off", "no", "false")


def parse_agent_response(payload: object, agent_id: str) -> AgentState:
    """The `payload` of a `fetch_agent_response` -> AgentState; DaemonError for anything else.

    An unknown agent comes back as `agent: null` with no error; that is an error here too, so the
    caller falls back to the CLI, which says what is wrong.
    """
    if not isinstance(payload, dict):
        raise DaemonError("fetch_agent_response has no payload")
    if payload.get("error"):
        raise DaemonError(clip_reason(payload["error"]))
    agent = payload.get("agent")
    if not isinstance(agent, dict) or agent.get("id") != agent_id:
        raise DaemonError("daemon did not return that agent")
    status, pending = agent.get("status"), agent.get("pendingPermissions")
    if not isinstance(status, str) or not isinstance(pending, list):
        raise DaemonError("agent snapshot lacks status or pendingPermissions")
    return AgentState(status[:MAX_STATUS_CHARS], bool(pending))


def clip_reason(value: object, limit: int = 200) -> str:
    return str(value)[:limit]


async def _open(url: str, timeout: float) -> Any:
    # proxy=None: websockets would otherwise tunnel a loopback daemon through HTTPS_PROXY/ALL_PROXY, token and all
    return await websockets.connect(url, open_timeout=timeout, max_size=MAX_FRAME_BYTES, proxy=None)


class DaemonSession:
    """One long-lived daemon connection that answers `fetch_agent_request`s.

    Opened on the first request: every method is a coroutine the app awaits and the reader is a task
    on the same loop, so nothing here blocks the UI. The connection counts as open only once the
    daemon has admitted the hello (its `status` frame); requests are never sent before that. A failure
    (refused, rejected hello, a dropped or silent socket, an `rpc_error`) raises DaemonError and starts
    a back-off (1 s, doubling to 60 s) during which `fetch_agent` raises at once, so the caller's CLI
    fallback does the work and a down daemon is never hammered. Only a complete, valid answer resets
    the back-off, so a daemon that accepts and then drops cannot cause a tight reconnect loop.

    `connect(url, timeout)` is injectable: tests pass an in-process fake connection with `send`,
    `close` and async iteration.
    """

    def __init__(
        self,
        *,
        connect: Callable[[str, float], Awaitable[Any]] = _open,
        clock: Callable[[], float] = time.monotonic,
        timeout: float = 10.0,
    ) -> None:
        self._connect = connect
        self._clock = clock
        self._timeout = timeout
        self._ws: Any = None
        self._reader: asyncio.Task[None] | None = None
        self._ready: asyncio.Future[None] | None = None  # resolved by the daemon's reply to the hello
        self._closers: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()
        self._waiting: dict[str, asyncio.Future[object]] = {}
        self._delay = BACKOFF_FIRST
        self._retry_at = float("-inf")
        self._closed = False
        self.connects = 0  # connection attempts so far (tests and diagnostics)

    @property
    def connected(self) -> bool:
        return self._ws is not None

    async def fetch_agent(self, agent_id: str) -> AgentState:
        ws = await self._ensure()
        if len(self._waiting) >= MAX_IN_FLIGHT:
            raise DaemonError("too many daemon requests in flight")
        request_id = str(uuid.uuid4())
        future: asyncio.Future[object] = asyncio.get_running_loop().create_future()
        self._waiting[request_id] = future
        try:
            frame = {"type": "session", "message": {"type": "fetch_agent_request", "requestId": request_id, "agentId": agent_id}}
            try:
                async with asyncio.timeout(self._timeout):  # the send and the wait share one bound
                    await ws.send(json.dumps(frame))
                    payload = await future
            except TimeoutError as exc:
                if self._ws is ws:  # not a newer connection another request opened meanwhile
                    self._drop("daemon did not answer in time")
                raise DaemonError("daemon did not answer in time") from exc
            except (OSError, websockets.WebSocketException) as exc:
                if self._ws is ws:
                    self._drop(f"{type(exc).__name__}")
                raise DaemonError(f"{type(exc).__name__}: {exc}") from exc
            state = parse_agent_response(payload, agent_id)
        finally:
            self._waiting.pop(request_id, None)
            if future.done() and not future.cancelled():
                future.exception()  # mark it retrieved: a drop may have failed it while we raised something else
        self._delay = BACKOFF_FIRST  # a full answer proves the link: forgive earlier failures
        return state

    async def close(self) -> None:
        """Final: a request after this raises DaemonError instead of opening a new socket."""
        self._closed = True
        self._drop("closed", backoff=False)
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reader
        if self._closers:
            await asyncio.gather(*self._closers, return_exceptions=True)

    async def _ensure(self) -> Any:
        async with self._lock:
            if self._closed:
                raise DaemonError("daemon session is closed")
            if self._ws is not None:
                return self._ws
            if self._clock() < self._retry_at:
                raise DaemonError("daemon connection is backing off")
            url = daemon_url()
            self.connects += 1
            try:
                async with asyncio.timeout(self._timeout + 1):
                    ws = await self._connect(url, self._timeout)
                    await ws.send(json.dumps(hello_message(url, modern=True)))
            except (OSError, TimeoutError, ValueError, websockets.WebSocketException) as exc:
                self._arm_backoff()
                raise DaemonError(f"{type(exc).__name__}: {exc}") from exc
            ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            self._ws, self._ready = ws, ready
            self._reader = asyncio.create_task(self._read(ws))
            try:
                await asyncio.wait_for(ready, self._timeout)  # a session frame sent before the admission closes the socket
            except TimeoutError as exc:
                if self._ws is ws:
                    self._drop("daemon did not admit the connection in time")
                raise DaemonError("daemon did not admit the connection in time") from exc
            finally:
                self._ready = None
            return ws

    def _arm_backoff(self) -> None:
        self._retry_at = self._clock() + self._delay
        self._delay = min(BACKOFF_MAX, self._delay * 2)

    def _drop(self, reason: str, *, backoff: bool = True) -> None:
        """Forget the connection and fail every waiting request; the next request reconnects after the back-off."""
        ws, self._ws = self._ws, None
        if ws is not None:
            if backoff:
                self._arm_backoff()
            task = asyncio.ensure_future(self._close_quietly(ws))
            self._closers.add(task)
            task.add_done_callback(self._closers.discard)
        failed = [self._ready, *self._waiting.values()]
        for future in failed:
            if future is not None and not future.done():
                future.set_exception(DaemonError(reason))

    @staticmethod
    async def _close_quietly(ws: Any) -> None:
        with contextlib.suppress(Exception):
            await ws.close()

    async def _read(self, ws: Any) -> None:
        bad = 0
        try:
            async for raw in ws:
                handled = self._handle(raw)
                bad = 0 if handled else bad + 1
                if bad > MAX_BAD_FRAMES:
                    if self._ws is ws:
                        self._drop("daemon sent too many unreadable frames")
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a broken socket must end the session, never crash the app
            if self._ws is ws:
                self._drop(type(exc).__name__)
            return
        if self._ws is ws:
            self._drop("daemon closed the connection")

    def _handle(self, raw: str | bytes) -> bool:
        """Route one frame; False for one that could not be read at all."""
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            return False
        if not isinstance(message, dict):
            return False
        kind = message.get("type")
        if kind in HELLO_REJECTED:
            self._drop(f"daemon rejected the connection: {clip_reason(message.get('reason', 'unknown'))}")
            return True
        inner = message.get("message") if kind == "session" else None
        if not isinstance(inner, dict):
            return True  # pong and other frames this client has no use for
        if inner.get("type") == "status":
            if self._ready is not None and not self._ready.done():
                self._ready.set_result(None)  # the hello was admitted (`server_info`)
            return True
        payload = inner.get("payload")
        if inner.get("type") not in ("fetch_agent_response", "rpc_error") or not isinstance(payload, dict):
            return True
        request_id = payload.get("requestId")
        future = self._waiting.get(request_id) if isinstance(request_id, str) else None
        if future is None or future.done():
            return True  # a late or foreign answer
        if inner["type"] == "rpc_error":
            future.set_exception(DaemonError(clip_reason(payload.get("error", "rpc error"))))
        else:
            future.set_result(payload)
        return True
