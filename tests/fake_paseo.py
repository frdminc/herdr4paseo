#!/usr/bin/env python3
"""Stand-in for the `paseo` CLI. Records every call to $FAKE_PASEO_CALLS (one JSON argv per line)."""

import json
import os
import sys
import time

AGENTS = [
    {"id": "aaaaaaa1-0000-0000-0000-000000000001", "shortId": "aaaaaaa", "name": os.environ.get("FAKE_PASEO_NAME", "alpha"),
     "provider": "claude/x", "status": os.environ.get("FAKE_PASEO_STATUS", "running"), "cwd": "~/src/a", "created": "1 days ago"},
    {"id": "bbbbbbb2-0000-0000-0000-000000000002", "shortId": "bbbbbbb", "name": "beta",
     "provider": "codex/y", "status": "idle", "cwd": "~/src/b", "created": "2 days ago"},
]


def pending_permits() -> str:
    out = os.environ.get("FAKE_PASEO_PERMITS", "[]")
    if os.environ.get("FAKE_PASEO_PERMITS_STATEFUL"):  # an answered request leaves the list, as in the real daemon
        with open(os.environ["FAKE_PASEO_CALLS"]) as seen:
            answered = {c[3] for c in map(json.loads, seen) if c[:2] in (["permit", "allow"], ["permit", "deny"]) and len(c) > 3}
        out = json.dumps([q for q in json.loads(out) if q["id"] not in answered])
    return out


with open(os.environ["FAKE_PASEO_CALLS"], "a") as calls:
    calls.write(json.dumps(sys.argv[1:]) + "\n")

cmd = sys.argv[1]
if cmd == "ls":
    print(json.dumps(AGENTS))
elif cmd == "logs":
    if "--json" in sys.argv:
        # Follow-mode records are deltas: three fragments with one messageId concatenate.
        mode = os.environ.get("FAKE_PASEO_JSON_MODE", "")
        items = [
            {"type": "user_message", "text": "hello chat"},
            {"type": "assistant_message", "text": "Hel", "messageId": "m1"},
            {"type": "assistant_message", "text": "lo ", "messageId": "m1"},
            {"type": "assistant_message", "text": "**world**", "messageId": "m1"},
            {"type": "tool_call", "callId": "c1", "name": "Shell", "status": "completed", "detail": {"output": "done"}},
        ]
        if mode == "big":  # one record far over asyncio's 64 KiB StreamReader limit, then a normal one
            items = [
                {"type": "tool_call", "callId": "big", "name": "Shell", "status": "completed", "detail": {"output": "x" * 300_000}},
                {"type": "assistant_message", "text": "after the big record", "messageId": "m2"},
            ]
        if mode == "escape":  # untrusted text carrying terminal control sequences
            items = [{"type": "assistant_message", "text": "hi \x1b]52;c;cHduZWQ=\x1b\\ \x1b[2J \x1b]0;pwned\x07 \x9b31m done", "messageId": "e1"},
                     {"type": "tool_call", "callId": "e2", "name": "Sh\x1b]8;;http://x\x1b\\ell", "status": "ok\x1b[31m"}]
        if mode == "many":  # 300 one-character deltas, written back to back
            items = [{"type": "assistant_message", "text": "ab"[i % 2], "messageId": "mm"} for i in range(300)]
        if mode == "stderrbig":  # a stderr line over asyncio's 64 KiB limit
            sys.stderr.write("e" * 200_000 + "\n")
            sys.stderr.flush()
        if mode == "deep":  # a record nested far past Python's recursion limit, then a normal one
            depth = int(os.environ.get("FAKE_PASEO_DEPTH", "100000"))
            deep = "[" * depth + "]" * depth
            items = []
            if "-f" in sys.argv:
                print(deep, flush=True)
            items = [{"type": "assistant_message", "text": "after the deep record", "messageId": "d1"}]
        if mode == "bigtools":  # more rendered text than one redraw's budget
            items = [{"type": "tool_call", "callId": f"t{i}", "name": "Shell", "status": "ok", "detail": {"out": "z" * 200_000}} for i in range(12)]
        if mode == "badtype":  # a wrong-typed field must be skipped, not kill the app
            items = [
                {"type": "reasoning", "text": "a"},
                {"type": "reasoning", "text": 1},
                {"type": "assistant_message", "text": "still alive", "messageId": "m3"},
            ]
        if "-f" in sys.argv:
            for item in items:
                print(json.dumps(item), flush=True)
            time.sleep(30)
        else:
            print(json.dumps({"agentId": sys.argv[2], "items": items}, indent=2))
        sys.exit(0)
    tools_only = "--filter" in sys.argv
    log_mode = os.environ.get("FAKE_PASEO_LOG_MODE", "")
    if log_mode == "longline":  # one line over asyncio's 64 KiB StreamReader limit, then a normal one
        print("[Shell] " + "x" * 200_000)
        print("after the long line")
        sys.stdout.flush()
        time.sleep(30)
        sys.exit(0)
    if log_mode == "escape":
        print("[User] hello \x1b]52;c;cHduZWQ=\x1b\\ \x1b[2J \x1b]0;pwned\x07 \x9b31m end")
        print("plain assistant line")
        sys.stdout.flush()
        time.sleep(30)
        sys.exit(0)
    print("[Shell] ls" if tools_only else "[User] hello there")
    if not tools_only:
        print("plain assistant line")
    sys.stdout.flush()
    if os.environ.get("FAKE_PASEO_LOG_LATE"):  # new output a moment after the replayed tail
        time.sleep(float(os.environ["FAKE_PASEO_LOG_LATE"]))
        print("late line", flush=True)
    if os.environ.get("FAKE_PASEO_LOGS_EXIT"):
        sys.exit(0)
    time.sleep(30)  # `-f` follows until killed
elif cmd == "permit" and sys.argv[2] == "ls":
    print(pending_permits())
elif cmd == "agent" and sys.argv[2] == "inspect":  # one agent, capitalised keys, as the real CLI prints it
    if os.environ.get("FAKE_PASEO_INSPECT_FAIL"):
        print("unknown command", file=sys.stderr)
        sys.exit(1)
    agent = next(a for a in AGENTS if a["id"] == sys.argv[3])
    if agent["shortId"] == "aaaaaaa":
        time.sleep(float(os.environ.get("FAKE_PASEO_INSPECT_SLEEP_A", "0")))  # a slow probe for alpha
    queue = [q for q in json.loads(pending_permits()) if q["agentId"] == agent["id"]]
    print(json.dumps({"Id": agent["id"], "Name": agent["name"], "Status": agent["status"],
                      "PendingPermissions": [{"id": q["id"], "tool": q["name"]} for q in queue]}, indent=2))
elif cmd == "run":
    if os.environ.get("FAKE_PASEO_RUN_FAIL"):
        print("no provider", file=sys.stderr)
        sys.exit(1)
    with open(os.environ["FAKE_PASEO_CALLS"], "a") as more:
        more.write(json.dumps(["cwd", os.getcwd()]) + "\n")
    print("bbbbbbb2-0000-0000-0000-000000000002")
elif cmd == "send":
    if os.environ.get("FAKE_PASEO_SEND_FAIL"):
        print("boom", file=sys.stderr)
        sys.exit(1)
