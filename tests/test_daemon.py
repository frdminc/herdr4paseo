import json

import pytest

from herdr4paseo.core import Command, match_commands, parse_commands
from herdr4paseo.daemon import DaemonError, daemon_url, fetch_commands, local_credential

from fake_daemon import COMMANDS, serve


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PASEO_HOME", str(tmp_path))
    monkeypatch.delenv("PASEO_HOST", raising=False)
    (tmp_path / "local-credential").write_text("s3cret\n")
    return tmp_path


def test_daemon_url_prefers_host_then_config_then_default(home, monkeypatch):
    assert daemon_url() == "ws://127.0.0.1:6767/ws"
    (home / "config.json").write_text(json.dumps({"daemon": {"listen": "127.0.0.1:7263"}}))
    assert daemon_url() == "ws://127.0.0.1:7263/ws"
    monkeypatch.setenv("PASEO_HOST", "box.example:6767")
    assert daemon_url() == "ws://box.example:6767/ws"
    monkeypatch.setenv("PASEO_HOST", "https://box.example")
    assert daemon_url() == "wss://box.example/ws"


def test_local_credential_is_never_offered_to_a_remote_daemon(home):
    assert local_credential("ws://127.0.0.1:7263/ws") == "s3cret"
    assert local_credential("ws://localhost:7263/ws") == "s3cret"
    assert local_credential("ws://box.example:6767/ws") is None


def test_parse_and_match_commands():
    commands = parse_commands(COMMANDS)
    assert [c.name for c in commands] == ["code-review", "compact", "context", "steps"]
    assert commands[1] == Command("compact", "Compact the conversation", "[focus]", "command")
    assert [c.name for c in match_commands(commands, "/co")] == ["code-review", "compact", "context"]
    assert [c.name for c in match_commands(commands, "/review")] == ["code-review"]  # substring after prefixes
    assert [c.name for c in match_commands(commands, "/")] == [c.name for c in commands]
    assert match_commands(commands, "/compact now") == []  # a space ends the command name
    assert match_commands(commands, "hello") == []


async def test_fetch_commands_sends_credential_and_ignores_other_requests(home, monkeypatch):
    async with serve() as daemon:
        monkeypatch.setenv("PASEO_HOST", f"127.0.0.1:{daemon.port}")
        commands = await fetch_commands("agent-1", timeout=5)
    assert [c.name for c in commands] == ["code-review", "compact", "context", "steps"]
    assert daemon.hellos[0]["auth"] == {"kind": "localCredential", "token": "s3cret"}
    assert daemon.requests[0]["agentId"] == "agent-1"


async def test_fetch_commands_reports_rejection_error_and_unreachable(home, monkeypatch):
    async with serve(reject="bad credential") as daemon:
        monkeypatch.setenv("PASEO_HOST", f"127.0.0.1:{daemon.port}")
        with pytest.raises(DaemonError, match="bad credential"):
            await fetch_commands("agent-1", timeout=5)
    async with serve(error="agent not found") as daemon:
        monkeypatch.setenv("PASEO_HOST", f"127.0.0.1:{daemon.port}")
        with pytest.raises(DaemonError, match="agent not found"):
            await fetch_commands("agent-1", timeout=5)
    monkeypatch.setenv("PASEO_HOST", "127.0.0.1:1")
    with pytest.raises(DaemonError):
        await fetch_commands("agent-1", timeout=5)


# -- DaemonSession: one persistent connection for `fetch_agent_request` (in-process fake, no socket) --

import asyncio  # noqa: E402

from herdr4paseo import daemon as daemon_module  # noqa: E402
from herdr4paseo.daemon import AgentState, DaemonSession, daemon_status_enabled, parse_agent_response  # noqa: E402

from fake_ws import FakeLink  # noqa: E402


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def link(home, monkeypatch):
    monkeypatch.setenv("PASEO_HOST", "127.0.0.1:7263")
    return FakeLink()


def session_for(link, clock=None, timeout=2.0) -> DaemonSession:
    return DaemonSession(connect=link.connect, clock=clock or Clock(), timeout=timeout)


async def test_session_reuses_one_connection_and_sends_the_credential_once_per_connect(link):
    session = session_for(link)
    assert await session.fetch_agent("agent-1") == AgentState("running", False)
    link.status, link.pending = "idle", [{"id": "p1"}]
    assert await session.fetch_agent("agent-1") == AgentState("idle", True)
    assert session.connects == 1 and len(link.connections) == 1
    assert link.hellos[0]["auth"] == {"kind": "localCredential", "token": "s3cret"}
    assert [r["agentId"] for r in link.requests] == ["agent-1", "agent-1"]
    sent = link.last.sent[1]
    assert sent["type"] == "session" and sent["message"]["type"] == "fetch_agent_request"
    assert link.urls == ["ws://127.0.0.1:7263/ws"]
    await session.close()


async def test_session_never_offers_the_credential_to_a_remote_daemon(link, monkeypatch):
    monkeypatch.setenv("PASEO_HOST", "box.example:6767")
    session = session_for(link)
    await session.fetch_agent("agent-1")
    assert "auth" not in link.hellos[0]
    assert "s3cret" not in json.dumps(link.last.sent)
    await session.close()


async def test_down_at_start_backs_off_with_bounded_growth_and_recovers(link):
    clock = Clock()
    session = session_for(link, clock)
    link.down = True
    with pytest.raises(DaemonError):
        await session.fetch_agent("agent-1")
    for _ in range(5):  # inside the back-off window nobody dials out again
        with pytest.raises(DaemonError, match="backing off"):
            await session.fetch_agent("agent-1")
    assert session.connects == 1
    clock.now += daemon_module.BACKOFF_FIRST + 0.1
    with pytest.raises(DaemonError):
        await session.fetch_agent("agent-1")
    assert session.connects == 2
    clock.now += daemon_module.BACKOFF_FIRST + 0.1  # the window doubled: one first-interval is not enough
    with pytest.raises(DaemonError, match="backing off"):
        await session.fetch_agent("agent-1")
    for _ in range(12):  # many failures later the wait is capped, not unbounded
        clock.now += daemon_module.BACKOFF_MAX + 0.1
        with pytest.raises(DaemonError):
            await session.fetch_agent("agent-1")
    assert session._retry_at - clock.now <= daemon_module.BACKOFF_MAX
    link.down = False
    clock.now += daemon_module.BACKOFF_MAX + 0.1
    assert (await session.fetch_agent("agent-1")).status == "running"
    assert session._delay == daemon_module.BACKOFF_FIRST  # a good answer forgives the past
    await session.close()


async def test_daemon_dropping_mid_session_fails_the_next_call_then_reconnects(link):
    clock = Clock()
    session = session_for(link, clock)
    await session.fetch_agent("agent-1")
    link.last.drop()
    await asyncio.sleep(0.05)
    assert not session.connected
    with pytest.raises(DaemonError, match="backing off"):  # the caller's CLI fallback does this poll
        await session.fetch_agent("agent-1")
    clock.now += daemon_module.BACKOFF_FIRST + 0.1
    assert (await session.fetch_agent("agent-1")).status == "running"
    assert session.connects == 2 and len(link.connections) == 2
    await session.close()


async def test_a_request_in_flight_fails_at_once_when_the_daemon_drops(link):
    session = session_for(link, timeout=5)
    await session.fetch_agent("agent-1")
    link.delays["agent-1"] = 5
    task = asyncio.ensure_future(session.fetch_agent("agent-1"))
    await asyncio.sleep(0.05)
    link.last.drop(ConnectionResetError("gone"))
    with pytest.raises(DaemonError):
        await asyncio.wait_for(task, 1)
    await session.close()


async def test_malformed_and_foreign_frames_are_ignored(link):
    link.junk = ["not json", "[1, 2]", "null", json.dumps({"type": "session"}),
                 json.dumps({"type": "session", "message": "str"}),
                 json.dumps({"type": "session", "message": {"type": "fetch_agent_response", "payload": "str"}}),
                 json.dumps({"type": "session", "message": {"type": "fetch_agent_response", "payload": {"requestId": ["x"]}}}),
                 json.dumps({"type": "pong"}), b"\xff\xfe"]
    session = session_for(link)
    assert await session.fetch_agent("agent-1") == AgentState("running", False)  # the real answer got through
    assert await session.fetch_agent("agent-1") == AgentState("running", False)
    assert session.connects == 1
    await session.close()


async def test_a_flood_of_unreadable_frames_drops_the_connection(link):
    link.junk = ["garbage"] * (daemon_module.MAX_BAD_FRAMES + 5)
    link.silent = True
    session = session_for(link, timeout=2)
    with pytest.raises(DaemonError):
        await session.fetch_agent("agent-1")
    await asyncio.sleep(0.05)
    assert not session.connected and link.last.closed
    await session.close()


@pytest.mark.parametrize("tweak, match", [
    (lambda l: setattr(l, "reply_id", "someone-else"), "did not return that agent"),
    (lambda l: setattr(l, "null_agent", True), "did not return that agent"),
    (lambda l: setattr(l, "response_error", "ambiguous agent"), "ambiguous agent"),
    (lambda l: setattr(l, "rpc_error", "boom " * 200), "boom"),
    (lambda l: setattr(l, "reject", "bad credential"), "bad credential"),
    (lambda l: setattr(l, "silent_hello", True), "admit"),
])
async def test_errors_and_odd_answers_raise_daemon_error(link, tweak, match):
    tweak(link)
    session = session_for(link, timeout=0.3)
    with pytest.raises(DaemonError, match=match) as err:
        await session.fetch_agent("agent-1")
    assert len(str(err.value)) < 400  # a daemon-supplied reason is clipped
    await session.close()


async def test_a_silent_daemon_times_out_and_the_socket_is_dropped(link):
    link.silent = True
    session = session_for(link, timeout=0.1)
    with pytest.raises(DaemonError, match="in time"):
        await session.fetch_agent("agent-1")
    await asyncio.sleep(0.05)  # the close runs as its own task
    assert not session.connected and link.last.closed
    await session.close()


async def test_a_late_answer_to_an_abandoned_request_is_ignored(link):
    clock = Clock()
    session = session_for(link, clock, timeout=0.2)
    link.delays["slow"] = 0.5
    with pytest.raises(DaemonError):
        await session.fetch_agent("slow")  # times out; its answer will arrive after the drop
    clock.now += daemon_module.BACKOFF_FIRST + 0.1
    assert (await session.fetch_agent("agent-2")).status == "running"
    await asyncio.sleep(0.5)  # the old connection's late frame goes nowhere
    assert session.connected
    await session.close()


async def test_close_stops_the_reader_is_idempotent_and_final(link):
    session = session_for(link)
    await session.fetch_agent("agent-1")
    reader = session._reader
    await session.close()
    await session.close()
    assert reader.done() and not session.connected and not session._closers
    assert link.last.closed
    with pytest.raises(DaemonError, match="closed"):  # a poll after unmount must not open a new socket
        await session.fetch_agent("agent-1")
    assert len(link.connections) == 1


async def test_hello_declares_a_modern_client_so_the_daemon_does_not_stream_other_agents(link):
    session = session_for(link)
    await session.fetch_agent("agent-1")
    caps = link.hellos[0]["capabilities"]
    assert caps["owned_subscriptions"] is True and caps["all_providers"] is True and caps["selective_agent_timeline"] is True
    assert link.streamed == 0
    await session.close()
    # the guard against a regression: a hello without the capabilities is what makes the fake stream the fleet
    link2 = FakeLink()
    conn = await link2.connect("ws://x", 1)
    await conn.send(json.dumps({"type": "hello", "clientId": "x", "clientType": "cli", "protocolVersion": 1}))
    await asyncio.sleep(0.05)
    assert link2.streamed == 50


async def test_no_request_is_sent_before_the_daemon_admits_the_hello(link):
    link.admit_delay = 0.2
    session = session_for(link)
    assert (await session.fetch_agent("agent-1")).status == "running"
    assert link.premature == 0
    await session.close()


async def test_a_session_frame_before_admission_is_what_the_fake_daemon_punishes(link):
    link.admit_delay = 0.2
    conn = await link.connect("ws://x", 1)
    await conn.send(json.dumps({"type": "hello", "clientId": "x", "clientType": "cli", "protocolVersion": 1}))
    await conn.send(json.dumps({"type": "session", "message": {"type": "fetch_agent_request", "requestId": "r", "agentId": "a"}}))
    assert link.premature == 1 and conn.closed


async def test_websocket_is_opened_without_a_proxy(monkeypatch):
    seen = {}

    async def fake_connect(url, **kwargs):
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(daemon_module.websockets, "connect", fake_connect)
    await daemon_module._open("ws://127.0.0.1:6767/ws", 3.0)
    assert seen["proxy"] is None and seen["max_size"] == daemon_module.MAX_FRAME_BYTES


async def test_a_status_string_is_clipped(link):
    link.status = "x" * 5000
    session = session_for(link)
    assert len((await session.fetch_agent("agent-1")).status) == daemon_module.MAX_STATUS_CHARS
    await session.close()


def test_daemon_url_survives_a_nonsense_config(home, monkeypatch):
    (home / "config.json").write_text(json.dumps({"daemon": {"listen": None}}))
    assert daemon_url() == "ws://127.0.0.1:6767/ws"
    (home / "config.json").write_text(json.dumps({"daemon": {"listen": 7}}))
    assert daemon_url() == "ws://127.0.0.1:6767/ws"


async def test_too_many_requests_in_flight_are_refused(link):
    session = session_for(link, timeout=5)
    await session.fetch_agent("agent-1")
    link.delays["agent-1"] = 5
    tasks = [asyncio.ensure_future(session.fetch_agent("agent-1")) for _ in range(daemon_module.MAX_IN_FLIGHT)]
    await asyncio.sleep(0.05)
    with pytest.raises(DaemonError, match="too many"):
        await session.fetch_agent("agent-1")
    await session.close()
    for task in tasks:
        with pytest.raises(DaemonError):
            await task


def test_parse_agent_response_checks_every_field():
    good = {"requestId": "r", "agent": {"id": "a", "status": "idle", "pendingPermissions": []}, "error": None}
    assert parse_agent_response(good, "a") == AgentState("idle", False)
    for bad in (None, [], {"agent": None}, {"agent": {"id": "b", "status": "idle", "pendingPermissions": []}},
                {"agent": {"id": "a", "status": 3, "pendingPermissions": []}},
                {"agent": {"id": "a", "status": "idle", "pendingPermissions": None}},
                {"agent": {"id": "a", "status": "idle", "pendingPermissions": []}, "error": "no"}):
        with pytest.raises(DaemonError):
            parse_agent_response(bad, "a")


def test_daemon_status_can_be_switched_off():
    assert daemon_status_enabled({})
    assert not daemon_status_enabled({"HERDR4PASEO_DAEMON_STATUS": "0"})
    assert not daemon_status_enabled({"HERDR4PASEO_DAEMON_STATUS": "off"})
