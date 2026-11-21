"""An in-process stand-in for a WebSocket connection to the Paseo daemon: no socket, no port.

`FakeLink.connect` has the signature `DaemonSession(connect=...)` wants and returns a
`FakeConnection` (send, close, async iteration). The link plays the daemon's side: it answers a
`fetch_agent_request` with an agent snapshot built from `link.status` / `link.pending`, and the
knobs below inject the faults the session must survive. Like the real daemon it admits a hello
asynchronously (`admit_delay`), closes a socket whose session frame arrives before that, answers a
rejected hello with `hello.rejected` and a close, and streams unrelated `agent_stream` frames to a
client that did not declare `owned_subscriptions` (what a legacy client gets).
"""

from __future__ import annotations

import asyncio
import json


class FakeConnection:
    def __init__(self, link: "FakeLink") -> None:
        self.link = link
        self.sent: list[dict] = []
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.closed = False
        self.admitted = False
        self._tasks: set[asyncio.Task] = set()

    async def send(self, text: str) -> None:
        if self.closed:
            raise ConnectionResetError("fake connection is closed")
        message = json.loads(text)
        self.sent.append(message)
        if message.get("type") == "hello":
            if self.link.reject:
                self.push(json.dumps({"type": "hello.rejected", "reason": self.link.reject, "accepts": ["password"]}))
                self.drop()
            else:
                self._spawn(self._admit(message))
            return
        if not self.admitted:  # the real daemon: "Rejected pending message before hello"
            self.link.premature += 1
            self.drop()
            return
        inner = message.get("message") or {}
        if inner.get("type") == "fetch_agent_request":
            self._spawn(self._answer(inner))

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _admit(self, hello: dict) -> None:
        if self.link.admit_delay:
            await asyncio.sleep(self.link.admit_delay)
        if self.closed:
            return
        self.admitted = True
        if not self.link.silent_hello:
            self.push(json.dumps({"type": "session", "message": {"type": "status", "payload": {"status": "server_info"}}}))
        if not (hello.get("capabilities") or {}).get("owned_subscriptions"):
            for n in range(self.link.legacy_frames):  # a legacy client is sent every agent's events
                self.link.streamed += 1
                self.push(json.dumps({"type": "session", "message": {"type": "agent_stream", "payload": {"agentId": f"other-{n}"}}}))

    async def _answer(self, request: dict) -> None:
        self.link.requests.append(request)
        agent_id = request["agentId"]
        delay = self.link.delays.get(agent_id, 0.0)
        if delay:
            await asyncio.sleep(delay)
        for junk in self.link.junk:  # frames the session must shrug off before the real answer
            self.push(junk)
        if self.link.silent:
            return
        if self.link.rpc_error:
            self.push(json.dumps({"type": "session", "message": {"type": "rpc_error", "payload": {
                "requestId": request["requestId"], "requestType": "fetch_agent_request", "error": self.link.rpc_error}}}))
            return
        self.push(json.dumps({"type": "session", "message": {"type": "fetch_agent_response", "payload": {
            "requestId": "someone-elses", "agent": self.link.snapshot(agent_id), "error": None}}}))
        self.push(json.dumps({"type": "session", "message": {"type": "fetch_agent_response", "payload": {
            "requestId": request["requestId"], "agent": self.link.snapshot(self.link.reply_id or agent_id),
            "error": self.link.response_error}}}))

    def push(self, raw) -> None:
        self.inbox.put_nowait(raw)

    def drop(self, exc: BaseException | None = None) -> None:
        """The daemon goes away: iteration ends (or raises `exc`)."""
        self.closed = True
        self.inbox.put_nowait(exc if exc is not None else StopAsyncIteration())

    async def close(self) -> None:
        self.closed = True
        self.inbox.put_nowait(StopAsyncIteration())

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.inbox.get()
        if isinstance(item, BaseException):
            raise item
        return item


class FakeLink:
    def __init__(self) -> None:
        self.status = "running"
        self.pending: list[dict] = []
        self.statuses: dict[str, str] = {}  # per agent id, over `status`
        self.pendings: dict[str, list[dict]] = {}
        self.delays: dict[str, float] = {}  # seconds before answering a request about that agent
        self.junk: list = []
        self.reject: str | None = None
        self.rpc_error: str | None = None
        self.response_error: str | None = None
        self.reply_id: str | None = None  # answer with another agent's id
        self.silent = False  # never answers
        self.silent_hello = False  # never admits the hello
        self.admit_delay = 0.0
        self.legacy_frames = 50
        self.streamed = 0  # unrelated frames pushed to a legacy client
        self.premature = 0  # session frames that arrived before the hello was admitted
        self.down = False  # connect fails
        self.null_agent = False
        self.connections: list[FakeConnection] = []
        self.urls: list[str] = []
        self.requests: list[dict] = []

    def snapshot(self, agent_id: str) -> dict | None:
        if self.null_agent:
            return None
        return {"id": agent_id, "status": self.statuses.get(agent_id, self.status),
                "pendingPermissions": self.pendings.get(agent_id, self.pending)}

    async def connect(self, url: str, timeout: float) -> FakeConnection:
        self.urls.append(url)
        if self.down:
            raise ConnectionRefusedError("fake daemon is down")
        conn = FakeConnection(self)
        self.connections.append(conn)
        return conn

    @property
    def last(self) -> FakeConnection:
        return self.connections[-1]

    @property
    def hellos(self) -> list[dict]:
        return [m for c in self.connections for m in c.sent if m.get("type") == "hello"]
