import json

import pytest

from herdr4paseo.poll import PollConfig, PollSchedule, inspect_argv, parse_inspect

AGENT = "aaaaaaa1-0000-0000-0000-000000000001"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def schedule(clock: Clock, **overrides) -> PollSchedule:
    return PollSchedule(PollConfig(**overrides), clock)


def test_defaults_are_much_slower_than_the_old_three_second_tick():
    cfg = PollConfig()
    assert (cfg.active, cfg.idle, cfg.idle_max, cfg.fast, cfg.burst) == (5.0, 15.0, 30.0, 3.0, 15.0)


def test_fresh_attach_polls_fast_for_one_burst_then_settles():
    clock = Clock()
    poll = schedule(clock)
    assert poll.delay() == 3.0
    poll.observe("idle", 0)
    clock.now += 15.0
    assert poll.delay() == 15.0


def test_idle_agent_backs_off_while_nothing_changes_up_to_the_ceiling():
    clock = Clock()
    poll = schedule(clock)
    clock.now += 100
    delays = []
    for _ in range(6):
        poll.observe("idle", 0)
        delays.append(poll.delay())
    assert delays == [15.0, 30.0, 30.0, 30.0, 30.0, 30.0]


def test_running_agent_polls_at_the_active_rate_and_never_backs_off():
    clock = Clock()
    poll = schedule(clock)
    clock.now += 100
    for _ in range(10):
        poll.observe("running", 0)
        assert poll.delay() == 5.0


def test_pending_request_keeps_the_active_rate_even_when_idle():
    clock = Clock()
    poll = schedule(clock)
    clock.now += 100
    for _ in range(5):
        poll.observe("idle", 1)
    assert poll.delay() == 5.0


def test_a_change_snaps_back_to_fast_then_decays():
    clock = Clock()
    poll = schedule(clock)
    clock.now += 100
    for _ in range(4):
        poll.observe("idle", 0)
    assert poll.delay() == 30.0
    assert poll.observe("running", 0) is True
    assert poll.delay() == 3.0
    clock.now += 15.0
    assert poll.delay() == 5.0  # burst over: running rate
    assert poll.observe("running", 0) is False
    assert poll.observe("idle", 0) is True
    clock.now += 15.0
    assert poll.delay() == 15.0  # backoff restarted from the base


def test_user_action_bumps_to_fast_and_restarts_the_backoff():
    clock = Clock()
    poll = schedule(clock)
    clock.now += 100
    for _ in range(4):
        poll.observe("idle", 0)
    poll.bump()
    assert poll.delay() == 3.0
    clock.now += 15.0
    assert poll.delay() == 15.0


def test_log_activity_wakes_a_settled_agent_at_most_once_per_idle_interval():
    clock = Clock()
    poll = schedule(clock)
    clock.now += 100
    poll.observe("idle", 0)
    poll.observe("idle", 0)
    assert poll.delay() == 30.0
    assert poll.log_activity() is True
    assert poll.delay() == 3.0
    clock.now += 15.0  # burst over, but the hint limit (idle = 15 s) has just elapsed too
    assert poll.log_activity() is True
    assert poll.log_activity() is False  # inside the burst/limit again


def test_log_activity_is_free_while_running_or_a_request_waits():
    clock = Clock()
    poll = schedule(clock)
    clock.now += 100
    poll.observe("running", 0)
    assert poll.log_activity() is False
    poll.observe("idle", 1)
    assert poll.log_activity() is False


def test_polls_inside_a_burst_do_not_count_towards_the_backoff():
    clock = Clock()
    poll = schedule(clock)
    for _ in range(5):  # five fast polls while the attach burst is still on
        poll.observe("idle", 0)
        clock.now += 3.0
    clock.now += 100
    poll.observe("idle", 0)
    assert poll.delay() == 15.0  # starts from `idle`, not from the 30 s ceiling


def test_log_hint_throttle_never_outlasts_a_short_burst():
    clock = Clock()
    poll = schedule(clock, burst=2.0)
    clock.now += 100
    poll.observe("idle", 0)
    assert poll.log_activity() is True
    clock.now += 2.5  # burst (2 s) over; idle is 15 s but the throttle is min(idle, burst)
    assert poll.log_activity() is True


def test_environment_overrides_clamp_and_ignore_garbage():
    cfg = PollConfig.from_env({"HERDR4PASEO_POLL_ACTIVE": "9", "HERDR4PASEO_POLL_IDLE": "0",
                               "HERDR4PASEO_POLL_FAST": "soon", "HERDR4PASEO_POLL_BURST": "nan",
                               "HERDR4PASEO_POLL_IDLE_MAX": "1"})
    assert PollConfig.from_env({"HERDR4PASEO_POLL_ACTIVE": "inf"}).active == 5.0  # inf would stop polling a running agent
    assert cfg.active == 9.0
    assert cfg.idle == 0.5  # never below the floor, so a typo cannot spin the CLI
    assert cfg.fast == 3.0 and cfg.burst == 15.0  # unparsable: default
    assert cfg.idle_max >= cfg.idle  # the ceiling cannot sit under the base


def test_inspect_argv_names_the_agent():
    assert inspect_argv(AGENT)[1:] == ["agent", "inspect", AGENT, "--json"]


def test_parse_inspect_reads_status_and_pending():
    raw = json.dumps({"Id": AGENT, "Status": "running", "PendingPermissions": [{"id": "p1", "tool": "Bash"}]})
    assert parse_inspect(raw, AGENT) == ("running", True)
    assert parse_inspect(json.dumps({"Id": AGENT, "Status": "idle", "PendingPermissions": []}), AGENT) == ("idle", False)


@pytest.mark.parametrize("raw", [
    "[]", "null", "not json", "{}",
    json.dumps({"Id": "other", "Status": "idle", "PendingPermissions": []}),
    json.dumps({"Id": AGENT, "Status": 3, "PendingPermissions": []}),
    json.dumps({"Id": AGENT, "Status": "idle"}),
    json.dumps({"Id": AGENT, "Status": "idle", "PendingPermissions": "none"}),
])
def test_parse_inspect_rejects_anything_else_so_the_caller_falls_back(raw):
    with pytest.raises(ValueError):
        parse_inspect(raw, AGENT)
