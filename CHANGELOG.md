# Changelog

One line per version: version, date, what changed, and why
(`architecture/version-numbering-rule-v0_1.md`, operator ruling 2026-10-08).

Versions are `MAJOR.MINOR.PATCH`, no `-rc` labels, no reused numbers. A version
that is built and tested but not released lives on `dev` with its number and its
line here, untagged; release merges `dev` into `main` and tags that exact
version (ADR-0002 / ADR-0006).

## 0.2.0 — built on `dev`, not released

**BREAKING config change.** Removed three settings that were parsed, validated
and read by nothing: `server.request_timeout_seconds`,
`server.max_request_body_bytes` and `mtls.cn_to_agent_strategy`. Config parsing
is strict (`extra="forbid"`), so a config still setting any of them fails at
boot — the deployed config must be edited before the image is rolled. Deleted
rather than implemented because nobody chose the values; a real body limit is
proposed separately (`architecture/proposals/dprox-body-cap-proposal-v0_1.md`).
MINOR, not PATCH: an understated break is invisible in the one field consumers
read to decide whether upgrading is safe.

Also: published images move to the organisation namespace
`ghcr.io/onemorerabbit/dprox`, derived from `github.repository_owner` rather
than hardcoded (constitution §11). Atlas wiring refreshed to method 1.33.5.

**Not released, and not only awaiting a decision:** the deployed `dprox_arc`
config on otter still sets all three keys, because `configure_dprox.yml` cannot
run (unrelated registry mount failure). Before any rollout: fix the mount, run
that playbook, confirm the keys are gone, then bump the estate pin. The estate
pins `v0.1.1` explicitly, so nothing rolls by accident.

## v0.1.2 — 2026-09-13 — source-only, no image

Tagged on `main`, but **no container image exists for it.** The release workflow
built the image and failed to push: it targeted `ghcr.io/jobcpf/dprox` while the
repo is `OneMoreRabbit/dprox-app`, and `GITHUB_TOKEN` grants `packages: write`
only for the repo owner. A namespace mismatch, not a code fault — fixed in
0.2.0. Do not pin this tag for deployment; `v0.1.1` remains the consumable
image. Content is the version bump and the Atlas 1.27 script refresh.

## v0.1.1 — 2026-05-12

Migrated to `AsyncQdrantClient.query_points()` (the universal query API) after
`.search()` was removed in qdrant-client 1.18. Dependency pin tightened to
`qdrant-client>=1.13,<2.0`, two regression-guard tests added. Deployed to the
platform as `ghcr.io/jobcpf/dprox:v0.1.1`, still the running image.

## v0.1.0 — 2026-05-12

First public image. Real-platform smoke surfaced the `qdrant-client` API drift
above, written up in the Atlas vault as
`components/dprox/docs/provides/dprox-qdrant-client-bug-response-v0_1.md`.
