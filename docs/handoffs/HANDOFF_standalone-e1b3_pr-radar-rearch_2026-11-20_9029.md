---
schema_version: 1
handoff_id: 9029
parent_handoff_ids: [8930]
lineage: deterministic
chain: [standalone-e1b3]
repo: herdr4paseo
workspace: main
branch: main
head_sha: e00eda9
created_at: 2026-11-20T10:39:00-0500
writer: pi
---
# Handoff — PR Radar re-architecture: Fable design + adversarial review done, Argon validation next

## The Goal
Re-architect the `pr-radar` plugin (omercnet/paseo-plugins) to kill ~90 stuck review findings on the merged #618, reusing djbclark-ade pieces (no duplication). Predecessor chain work: paseo4herdr/herdr4paseo TUI bridges (Q2 chat view, B1a attach --json) — see parent handoff 8930; those two PRs are still open and unchanged.

## Where We Are
Two Fable 5.2 passes are complete and durable:
1. **Design record** — `/Users/djbclark/src/.paseo-fanout/pp-rearch/report.md` (+ `client-races.md`). Committed to the fork: `docs/pr-radar-rearchitecture` branch, `pr-radar/docs/rearchitecture-design.md`.
2. **Adversarial review of that design** — `/Users/djbclark/src/.paseo-fanout/pp-rearch-review/report.md`. Committed: `pr-radar/docs/rearchitecture-review.md` (same docs branch).

The proposal (superseded in detail, kept for the finding distribution): `/Users/djbclark/src/.paseo-fanout/pp-followups/pr-radar-rearchitecture-proposal.md` (also `pr-radar/docs/rearchitecture-proposal.md`).

## Key Decisions (already made)
- Reuse, don't fork: routing = `bin/pr-radar-route` (keep the synced `examples/pr-radar-route.py` byte-identical); worktree/ownership = `plugins/ade-claim/` + `bin/ade_claims.py` + `bin/ade-git-guard`. Nothing else owns "route PR/issue → agent in a prepared worktree" end-to-end.
- Fable design says the seam is **Paseo native workspace creation** (`paseo.workspaces.create` with `source.kind:"worktree"` + `checkoutSource`), NOT importing ade-claim.
- Fable review corrected three load-bearing details of that design: `checkoutSource.projectPath` is a no-op; Paseo fetches `origin` before `upstream` (fork PR collision risk); the idempotency key does not dedupe a double press (keep the UI lock).
- Review go/no-go: **Move 2 (routing trust) GO now; Move 3 (client logic) GO with corrections; Move 1 (delegate checkout) NO-GO as written** until a spike answers four questions.

## What We Tried (and ruled out)
- Piecemeal ~90-findings fix — superseded by re-architecture.
- "Ask ade-claim for a worktree" / importing it — impossible (no plugin-to-plugin import; ade-claim only claims what Paseo creates).
- Raising pr-radar's Paseo floor to `^0.15` — unnecessary (everything it calls exists at `v0.12.0`; only ade-claim needs the v0.15 hooks).
- Full monorepo `npm install` on getpaseo/paseo — fails on native `sharp` (node 22 pinned, Homebrew node 26 present); targeted `--workspace` install is the correct scope.

## Operator Feedback
- Never post unvalidated work: install deps + run checks on the working machine unless inordinate (`~/ops/site-private-djbclark/memory/feedback_install_deps_validate_before_pr.md`).
- For THIS session: hand off to Opus 5.5 high, and the successor's FIRST action is a Gemini Argon (gemini-4-argon-high) read-only pass validating the two Fable documents.

## Where We're Going (ordered; item 1 is THE next action)
1. **Dispatch Gemini Argon** (`antigravity/gemini-4-argon-high`, mode `full-access`, `--label ade.kind=frontier`) to read-only validate the Fable design record AND the Fable adversarial review: verify the review's E1 census (12 misclassified findings), the three load-bearing API claims (projectPath no-op, origin-before-upstream fetch, idempotency-key non-dedupe), and the go/no-go. Argon is on the operator's word (read-only review). Write to `/Users/djbclark/src/.paseo-fanout/pp-argon-validation/report.md`.
2. Fold Argon's verdict + the Fable review into a **revised** design record; re-issue the top-5-changes list.
3. **Settle #662** (our own open labels PR, head `9f1f053` which the design treats as "the head"): merge or close. Recommend merge (96 lines, validated).
4. **Draft the upstream design issue** covering Q1/Q2/Q5/Q6/Q7 (behaviour removal: sibling placement, CWD-BASE switching, `pr-radar/pr-N` branches, daemon refresh — all merged days ago; needs `feat!`/major) BEFORE any PR 6.
5. Execute per the review's reordered plan: Move 2 (PRs 1+2 merged into one), Move 3 (PR 3+4, 5 lock state-mirrored, 10 prop-gated), then the Move 1 spike on the lowest-supported daemon + 0.16.2.

## Decisions (indexed)
- [decision] Re-architecture seam = Paseo native workspace creation; rejected: importing/asking ade-claim for a worktree; because Paseo forbids plugin-to-plugin imports and ade-claim only claims what Paseo creates; in [PR 662](https://github.com/omercnet/paseo-plugins/pull/662).
- [decision] Move 1 no-go until a spike answers fork+upstream, branch naming, hook error text, row-link; because `checkoutSource.projectPath` is a no-op and Paseo fetches origin before upstream; in docs branch `pr-radar/docs/rearchitecture-review.md`.
- [decision] Move 2 (routing trust) ships first; because it is the only live security item and independent of Moves 1/3; in docs branch `pr-radar/docs/rearchitecture-design.md`.
- [ruling] "handoff to an Opus 5.5 high agent; that agent should start off by having gemini argon do a pass to validate fable's work" (djbclark, 2026-11-20).

## Relations
- continues [[HANDOFF_standalone-e1b3_paseo-herdr-bridges_2026-11-18_8930]]
- decided_in [[HANDOFF_standalone-e1b3_paseo-herdr-bridges_2026-11-18_8930]]

## Evidence & Data
- Fable design: `/Users/djbclark/src/.paseo-fanout/pp-rearch/report.md` (46 KB) + `client-races.md` (204 lines).
- Fable review: `/Users/djbclark/src/.paseo-fanout/pp-rearch-review/report.md` (352 lines). Findings index F01–F90 in its appendix.
- Findings inventory + raw JSON: `/Users/djbclark/src/.paseo-fanout/pp-followups/{part-b-findings.md, pp618-comments.json}`.
- Fork: `djbclark/paseo-plugins-omercnet` (parent omercnet/paseo-plugins); worktrees `paseo-plugins-omercnet` (main), `paseo-plugins-followup-wt` (branch `feat/pr-radar-routing-labels` @ `9f1f053` = #662 head, deps installed via `bun install`), `paseo-plugins-omercnet-labels-wt`.
- Open PRs (predecessor chain, unchanged): [getpaseo/paseo#10122](https://github.com/getpaseo/paseo/pull/10122) (attach --json, awaiting CI approval), [frdminc/herdr4paseo#2](https://github.com/frdminc/herdr4paseo/pull/2) (chat view).

## Detached jobs
none

## Quick Start
- Argon review inputs: `~/src/.paseo-fanout/pp-rearch/report.md` and `~/src/.paseo-fanout/pp-rearch-review/report.md`; code at `/Users/djbclark/src/paseo-plugins-followup-wt` (`feat/pr-radar-routing-labels`).
- Route for Argon: `~/src/djbclark-ade/bin/route_agent.py plan --kinds frontier --slice argon-validation --cwd /Users/djbclark/src/paseo-plugins-followup-wt` but override to `antigravity/gemini-4-argon-high` (operator's word; plan would otherwise return claude/fable since claude is full).
- pr-radar validation commands (in `pr-radar/`, via `bg`): `bun run typecheck`, `bun run check`, `bun run test`.