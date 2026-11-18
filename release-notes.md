---
title: Release notes — dprox-app
status: active
updated: 2026-11-18
owner: agent-eco.component.dprox
about: one short entry per release, newest first — what changed and what anyone must do about it
---

# Release notes — dprox-app

Newest first, and the one home for release history (version numbering rule 0.2).
Every built version gets a line under **Built, not yet released**; when it is
tagged on `main` those lines become the tag's entry, with TL;DR and Action, in
the tagging commit.

## v0.3.0 — 2026-11-18 (built, not yet released)

**TL;DR:** one dprox answers every org, and there is a search command,
`dprox-query`, for seats and OpenClaw agents. **Breaking for deployment:**
`config.yml` must be rewritten before the image is rolled, and every caller's
plan must carry `groups[]`.

- **One dprox for every org.** The accepted design in one sentence: dprox
  searches only the collections of the caller's orgs, and inside each keeps
  only passages of the caller's groups. If one check is wrong, the other still
  holds. The caller's orgs come from the plan's `groups[]` section; a group's
  org is never parsed out of its name (constitution principle 10).
- **A result now carries `org`.** A merged list spans orgs and
  `source_path_rel` is relative to an org's own root, so without it two orgs'
  passages at the same path are indistinguishable. Response metadata gains
  `orgs_applied`.
- **The audit log records `collections_searched` and `orgs_applied`,** so a
  wrong org check is readable afterwards and not only inferable from what came
  back.
- **Refusals, not quiet shortenings.** A caller holding groups in an org this
  dprox has no collection for gets 502 and nothing is searched; one collection
  failing fails the whole search; two orgs naming one collection is refused at
  startup. In each case the alternative returns a short answer that reads
  exactly like a complete one.
- **`/healthz` checks every collection,** not one. One org's collection
  rebuilt with the wrong metric or the wrong model is the fault that is
  otherwise invisible, and checking one would clear all of them. `dprox health`
  prints a line for each org, pass or fail.
- **`dprox-query`,** a distribution of its own in `tools/dprox-query`, released
  from this repo by this tag and attached to the release as a wheel. It is
  installed with pipx at image build, never while an agent runs. Its address,
  CA, certificate and key are all required with no defaults, written by the
  estate into the seat's persistent home. It states which dprox versions it
  works with — 0.3.0 up to but not including 0.4.0 — and checks before every
  question, not only in `dprox-query check`.

**Action — the estate, before rolling the image:**

1. Rewrite `config.yml`. Remove the top-level `org:`. Replace
   `qdrant.collection: <name>` with a collection for each org:

   ```yaml
   qdrant:
     collections:
       arc: arc_documents
       job: job_documents
   ```

   Both old keys fail at startup rather than being ignored, because the schema
   forbids unknown keys. `examples/config.example.yml` is the full shape.
2. Roll rbac-compiler v0.7.3 or later first, so the plan carries `groups[]`.
   dprox 0.3.0 refuses a plan without it, and refuses a plan where a caller
   holds a group `groups[]` does not list.
3. Place `dprox-query`'s configuration, certificate and key in each seat's
   persistent home. `dprox-query check` says whether all three are in place.

## v0.2.0 — 2026-11-13

**TL;DR:** a real request size limit, images in the organisation namespace, and
three dead configuration keys removed. **Breaking for deployment:** the config
must be edited before the image is rolled.

- **New `server.max_body_bytes`, required, no default.** dprox refuses to start
  if it is absent and names the key (constitution principle 11). A body over
  the limit is refused with **HTTP 413 `request_too_large`** before anything
  parses it, and audited as `request_rejected` with `reason="body_too_large"`.
  Operator ruling 2026-11-13.
- **Three keys removed**, all parsed and read by nothing on `0.1.x`:
  `server.request_timeout_seconds`, `server.max_request_body_bytes`,
  `mtls.cn_to_agent_strategy`. Parsing is strict, so a config still setting any
  of them fails at boot and names it.
- **`max_body_bytes` is a new name, not the old one reused.** The old key was
  required and never enforced; reusing it would have flipped that config line
  from inert to enforcing with no diff to notice. A leftover
  `max_request_body_bytes` now stops the start and names itself.
- **Images publish to `ghcr.io/onemorerabbit/dprox`**, derived from
  `github.repository_owner` rather than hardcoded (constitution §11). This is
  what `v0.1.2` failed to do, which is why that tag has no image.
- Atlas wiring refreshed through method 1.35.0.

**Action — edit the config before rolling the image; they are two acts.**

```diff
 server:
-  request_timeout_seconds: 30
-  max_request_body_bytes: 65536
+  max_body_bytes: 65536
 mtls:
-  cn_to_agent_strategy: cn_equals_name
```

Editing is harmless on `0.1.x` because nothing read those keys; rolling the
image first is a boot failure. Order: fix the registry mount, run
`configure_dprox.yml`, confirm the deployed file matches the above, then bump
the estate pin. The estate pins `v0.1.1` explicitly, so nothing rolls by
accident.

**The value 65536 is chosen, not inherited:** nomic-embed-text reads at most
8192 tokens, about 32,000 characters, doubled for JSON escaping and multi-byte
text. It should be revisited if the embedding model changes. The reasoning sits
beside the key in `examples/config.example.yml`.

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
