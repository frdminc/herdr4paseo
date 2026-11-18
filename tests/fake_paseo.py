#!/usr/bin/env python3
"""Stand-in for the `paseo` CLI. Records every call to $FAKE_PASEO_CALLS (one JSON argv per line)."""

import json
import os
import sys
import time

AGENTS = [
    {"id": "aaaaaaa1-0000-0000-0000-000000000001", "shortId": "aaaaaaa", "name": "alpha",
     "provider": "claude/x", "status": os.environ.get("FAKE_PASEO_STATUS", "running"), "cwd": "~/src/a", "created": "1 days ago"},
    {"id": "bbbbbbb2-0000-0000-0000-000000000002", "shortId": "bbbbbbb", "name": "beta",
     "provider": "codex/y", "status": "idle", "cwd": "~/src/b", "created": "2 days ago"},
]

with open(os.environ["FAKE_PASEO_CALLS"], "a") as calls:
    calls.write(json.dumps(sys.argv[1:]) + "\n")

cmd = sys.argv[1]
if cmd == "ls":
    print(json.dumps(AGENTS))
elif cmd == "logs":
    if "--json" in sys.argv:
        items = [
            {"type": "user_message", "text": "hello chat"},
            {"type": "assistant_message", "text": "Hello", "messageId": "m1"},
            {"type": "assistant_message", "text": "Hello **world**", "messageId": "m1"},
            {"type": "tool_call", "callId": "c1", "name": "Shell", "status": "completed", "detail": {"output": "done"}},
        ]
        if "-f" in sys.argv:
            for item in items:
                print(json.dumps(item), flush=True)
            time.sleep(30)
        else:
            print(json.dumps({"agentId": sys.argv[2], "items": items}, indent=2))
        sys.exit(0)
    tools_only = "--filter" in sys.argv
    print("[Shell] ls" if tools_only else "[User] hello there")
    if not tools_only:
        print("plain assistant line")
    sys.stdout.flush()
    if os.environ.get("FAKE_PASEO_LOGS_EXIT"):
        sys.exit(0)
    time.sleep(30)  # `-f` follows until killed
elif cmd == "permit" and sys.argv[2] == "ls":
    print(os.environ.get("FAKE_PASEO_PERMITS", "[]"))
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
