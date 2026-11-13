---
title: Release notes — dprox-app
status: active
updated: 2026-10-08
owner: agent-eco.component.dprox
about: one short entry per release, newest first — what changed and what anyone must do about it
---

# Release notes — dprox-app

Newest first, and the one home for release history (version numbering rule 0.2).
Every built version gets a line under **Built, not yet released**; when it is
tagged on `main` those lines become the tag's entry, with TL;DR and Action, in
the tagging commit.

## Built, not yet released

- **0.2.0** — 2026-09-13 — **BREAKING config change.** Removed three settings
  that were parsed, validated and read by nothing:
  `server.request_timeout_seconds`, `server.max_request_body_bytes`,
  `mtls.cn_to_agent_strategy`. Parsing is strict (`extra="forbid"`), so a config
  still setting any of them fails at boot. Deleted rather than implemented
  because nobody chose the values; a real body limit is proposed separately
  (`architecture/proposals/dprox-body-cap-proposal-v0_1.md`). MINOR not PATCH:
  an understated break is invisible in the one field consumers read to decide
  whether upgrading is safe.
- **0.2.0** — published images move to the organisation namespace
  `ghcr.io/onemorerabbit/dprox`, derived from `github.repository_owner` rather
  than hardcoded (constitution §11). This also fixes the `v0.1.2` push failure.
- **0.2.0** — Atlas wiring refreshed through method 1.34.0.
- **0.2.0** — 2026-11-13 — **request size limit, required in config.** New
  `server.max_body_bytes`, **required with no default**: dprox refuses to
  start if it is absent and names the key (constitution principle 11). A body
  over the limit is refused with **HTTP 413 `request_too_large`** before
  anything parses it, and audited as `request_rejected` with
  `reason="body_too_large"`. Operator ruling 2026-11-13
  (`agenteco-needs-dprox-request-size-limit-v0_1`), answering the body-cap
  proposal.

  **The key is new, not the old one.** v0.1.x had `server.max_request_body_bytes`,
  which was required and **never enforced**; v0.2.0 removes it. Reusing that
  name would have flipped the same config line from inert to enforcing with no
  diff to notice, so a leftover `max_request_body_bytes` now **stops the start
  and names itself**.

  **Value 65536 bytes, chosen rather than inherited:** nomic-embed-text reads
  at most 8192 tokens, about 32,000 characters, doubled for JSON escaping and
  multi-byte text. The reasoning is beside the key in
  `examples/config.example.yml`; changing it is one line of estate config.

**Before this is rolled anywhere**, the deployed config must change. Under
`server:` remove `request_timeout_seconds` and `max_request_body_bytes`, and
**add `max_body_bytes: 65536`**; under `mtls:` remove `cn_to_agent_strategy`.
One edit, and v0.2.0 will not start without the new key.

Order: fix the registry mount (`configure_dprox.yml` currently cannot run —
unrelated failure), run that playbook, confirm the deployed file matches the
above, then bump the estate pin. The estate pins `v0.1.1` explicitly, so
nothing rolls by accident. Tagging and rolling v0.2.0 is a separate step after
the ARC restore.

## v0.1.2 — 2026-09-13

**TL;DR:** source-only tag. **No container image exists for this version — do not
pin it.**

- The release workflow built the image and failed to push it: it targeted
  `ghcr.io/jobcpf/dprox` while this repo is `OneMoreRabbit/dprox-app`, and
  `GITHUB_TOKEN` grants `packages: write` only for the repo owner.
- A namespace mismatch, not a code fault. Fixed for the next release: the
  namespace is now derived from `github.repository_owner` (constitution §11).
- Content is the version bump and an Atlas script refresh. No behaviour change.

**Action:** none, and **do not re-pin.** `v0.1.1` remains the consumable image.

## v0.1.1 — 2026-05-12

**TL;DR:** fixes every `/v1/query` returning `502 upstream_unavailable`.

- `AsyncQdrantClient.search()` was removed in qdrant-client 1.18, and dprox still
  called it — so queries failed before any request reached Qdrant. Migrated to
  `query_points()`, the universal query API.
- Dependency pin tightened to `qdrant-client>=1.13,<2.0` so the removal cannot
  recur silently; two regression-guard tests added.
- Written up in the Atlas vault as
  `components/dprox/docs/provides/dprox-qdrant-client-bug-response-v0_1.md`.

**Action:** re-pin to `v0.1.1`. This is the image currently deployed.

## v0.1.0 — 2026-05-12

**TL;DR:** first public image.

- mTLS termination, CN resolution against `compiled_plan.yml`, RBAC filtering by
  `classification_group`, Ollama embedding and audit logging.
- Real-platform smoke on otter surfaced the qdrant-client API drift fixed in
  `v0.1.1`.

**Action:** superseded — pin `v0.1.1` instead.
