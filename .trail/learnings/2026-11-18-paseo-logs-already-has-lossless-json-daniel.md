---
title: "paseo logs already has lossless --json; attach ignores it — use it, don't reimplement"
author: Daniel
date: 2026-11-18
branch: main
cost: { minutes: 1, tokens: 39287 }
id: l-f1af6754
scope: repo
status: active
src: agent
touches:
  - src/herdr4paseo/core.py
---

## Watch out

To read a Paseo agent's structured timeline from the terminal, use `paseo logs <id> --json` (one-shot `{agentId, items}`) and `paseo logs <id> --json -f` (follow: NDJSON, one full `AgentTimelineItem` per line, notices on stderr). It is lossless — full items incl. `tool_call` detail and unknown types — and needs no daemon WebSocket work or upstream dependency. Do NOT reimplement timeline fetching over the WebSocket or re-parse the lossy text log.

Related: `paseo attach` silently ignores the inherited global `--json`/`--format` options, and its text path (`printTimelineItem`) drops unknown item types via a `default: break` and reduces `tool_call` to name+status. The fix (getpaseo/paseo PR #10122) is to honor those selectors, not to add new flags.

Why this matters: an earlier plan assumed `logs` had no JSON mode and proposed adding one plus reimplementing the WebSocket timeline in herdr4paseo; both were wrong. The chat view simply reads `paseo logs --json -f`.

In the code:
- getpaseo/paseo `packages/cli/src/commands/agent/logs.ts` (`formatAgentLogsJson`, `createFollowJsonWriter`, `usesJsonOutput`), `attach.ts` (`printTimelineItem` default-skip; no JSON path), `cli.ts` + `commands/agent/index.ts` (`addJsonAndDaemonHostOptions` registration).
- herdr4paseo `src/herdr4paseo/core.py` `timeline_argv`/`parse_timeline` (the consumer).
