---
schema_version: 1
handoff_id: 9130
parent_handoff_ids: [9029]
lineage: deterministic
chain: [standalone-e1b3]
repo: herdr4paseo
workspace: main
branch: main
head_sha: 4ee1d23
created_at: 2026-11-20T11:20:00-0500
writer: claude-opus-5.5 (e1979690-b0cc-4140-a061-a1ca9d673c46)
---
# Handoff: upstream review debt + PR Radar rearch, 6 workers in flight

## The Goal
Clear every open upstream review ask on the chain's PRs (omercnet/paseo-plugins #662, the follow-ups omercnet invited when he merged #618, frdminc/herdr4paseo#2) and carry the PR Radar re-architecture forward (design v2, upstream design issue, Move 2).

## Where We Are
- Argon validation is done: `/Users/djbclark/src/.paseo-fanout/pp-argon-validation/report.md` (agent archived). It confirms the Fable review on 11 of 12 census corrections and all three API claims. It adds: `outcomeUnknown` poisons a reused idempotency key, so mint the key fresh per click; `getPullRequestCheckoutTarget` ignores the fork parent; don't re-sync `examples/pr-radar-route.py`; F52 and F06/F24 are live.
- #662: my first comment wrongly said "ready, please merge" while omercnet had two blocking asks. I edited it into a correction: https://github.com/omercnet/paseo-plugins/pull/662#issuecomment-7043783089. djbclark has only `pull` access on omercnet/paseo-plugins.
- herdr4paseo#2: Codex P1/P2 are fixed (`d70302d`, `291d741`, CI green), and replies are posted (https://github.com/frdminc/herdr4paseo/pull/2#discussion_r4644740659, https://github.com/frdminc/herdr4paseo/pull/2#discussion_r4644741016). Round 2 is running: the adversary's three findings.
- herdr4paseo `4ee1d23`: the ack for handoff 9029 is committed.

## Key Decisions (operator, 2026-11-20)
- #662 label scope: **both shapes**. Labels matching an allowed prefix (setting, empty by default) keep their name; every other valid label becomes `pr-radar.route.<key>`. `pr-radar`, `pr-radar.*`, `paseo.*` are always reserved.
- #662 invalid labels: **block the launch by default** (modal + auto-launch), naming each rejected key. Operator-made **short-term fallback rules** (`{repository, until ≤ 7 days, note}`) allow launching with no script labels plus a warning.
- #662 settle: "ping and stack" (comment posted, now a correction); Move 2 stacks on #662's branch.
- The half-created PR worktree and F52 go into the #618 follow-up PR; F41 is its own PR.
- herdr4paseo#2: fix **all three** adversary findings in the same PR (terminal escapes HIGH, readline() 64 KiB in log view/stderr, render coalescing).
- Upstream design issue: draft locally; post only with the operator's approval.

## Detached jobs (Paseo workers; lead = whoever holds this chain, send updates their way)
| slice | agent | report / done | ack status |
|---|---|---|---|
| pp662-fix (#662 rework, MSG 1 decisions + MSG 2 load-watch path) | b6f5d430-52c8-4109-9a15-394a5347f1e9 | `/Users/djbclark/src/.paseo-fanout/pp662-fix/report.md` / `report.done` | acked msg1, msg2 |
| pp618-followup (#618 follow-ups + half-created PR worktree + F52; new wt `/Users/djbclark/src/paseo-plugins-618fu-wt`) | 546994ea-97e5-4bad-8031-899d0301f40d | `/Users/djbclark/src/.paseo-fanout/pp618-followup/report.md` / `report.done` | msg1/msg2 sent |
| pp-f41 (issue cache wipe; wt `/Users/djbclark/src/paseo-plugins-f41-wt`) | 005b0d9b-7451-4569-9789-400d322f5944 | `/Users/djbclark/src/.paseo-fanout/pp-f41/report.md` / `report.done` | acked msg1 |
| ade-f39 (djbclark-ade: F39 age_s, pr-radar-route docstring, dispatch-footer load-watch full path) | f011f1a1-947b-4237-b272-15cec84a853f | `/Users/djbclark/src/.paseo-fanout/ade-f39/report.md` / `report.done` | msg1 sent |
| h4p-pr2-fix round 2 | f5fe55a3-22d5-4660-a7cb-107f9d139d67 | `/Users/djbclark/src/.paseo-fanout/h4p-pr2-fix/report2.md` / `report2.done` | round-1 acked (done.ack written) |
| pp-rearch-v2 (Opus: design-v2.md + upstream-issue-draft.md) | 25838034-3133-4fae-afb3-799be296c79e | `/Users/djbclark/src/.paseo-fanout/pp-rearch-v2/report.md` / `report.done` | — |

**First, after acking:** every worker's completion-notice targets the old lead `e1979690-b0cc-4140-a061-a1ca9d673c46`. Send each running worker a plain `paseo send` saying `LEAD <your id> took over from e1979690; send completion notices and BLOCKED messages to <your id>`, and also re-arm `wait-file` on each done path above. On each completion-notice: verify the worker is idle, `paseo send` `ack <worker> <msg-id>`, and write `<done>.ack` (JSON `{ack, msg_id, timestamp}`, atomic). Then verify the diff and tests (and CI where a PR exists), and archive the idle worker.

## Where We're Going (ordered)
1. Collect each worker as it finishes. Verify the diff, tests and adversary verdict.
2. #662: once pushed and verified, post the worker's drafted replies to omercnet's review comment and the two Baz threads (radar.ts:957, tests/workspace-ops.test.ts:114). **Read every comment on the PR again before posting** (memory `feedback_read_all_reviews_before_pr_comment.md`).
3. Dispatch Move 2: brief `/Users/djbclark/src/.paseo-fanout/pp-move2/brief.md` (already updated with Argon's C4 refinement, #618 LOW item 3 and the load-watch path) via `route_agent.py plan --kinds code --slice pp-move2`. Do it after the #662 rework is pushed; it stacks on `feat/pr-radar-routing-labels`.
4. Open the #618 follow-up PR and the F41 PR from the workers' drafted titles and bodies (to omercnet/paseo-plugins main, head `djbclark:<branch>` on the fork `djbclark/paseo-plugins-omercnet`).
5. Read design v2 + the issue draft; show the operator the draft as wrapped prose; post only on their yes. Commit design-v2 to the fork's `docs/pr-radar-rearchitecture` branch (`pr-radar/docs/`).
6. herdr4paseo#2 round 2: verify, then the PR is ready for the operator to merge.

## What We Tried (ruled out)
- A big brief through a Bash heredoc: the secret-read-gate hook blocked it ("glob analysis budget exceeded"). Write briefs with the Write tool.
- `~/ops/site-private-djbclark/bin/load-watch` does not exist; it is `/Users/djbclark/src/djbclark-ade/bin/load-watch`.

## Evidence
- Design v1: `/Users/djbclark/src/.paseo-fanout/pp-rearch/report.md`; review: `/Users/djbclark/src/.paseo-fanout/pp-rearch-review/report.md`; Argon: `/Users/djbclark/src/.paseo-fanout/pp-argon-validation/report.md`.
- Fork remotes in `/Users/djbclark/src/paseo-plugins-followup-wt`: `fork-omercnet` = djbclark/paseo-plugins-omercnet (the PR head repo), `upstream`/`omercnet` = omercnet/paseo-plugins.
- Unchanged: getpaseo/paseo#10122 → tracked by paseo-bot as #10136 (nothing to do).
