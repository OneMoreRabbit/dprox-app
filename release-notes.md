---
title: Release notes — dprox-app
status: active
updated: 2026-11-23
owner: agent-eco.component.dprox
about: one short entry per release, newest first — what changed and what anyone must do about it
---

# Release notes — dprox-app

Newest first, and the one home for release history (version numbering rule 0.2).
Every built version gets a line under **Built, not yet released**; when it is
tagged on `main` those lines become the tag's entry, with TL;DR and Action, in
the tagging commit.

A version marked **built, never released** has no image and never will: it was
tagged locally, overtaken, and publishing it would add something nobody can
deploy. The entry stays because the changes are real and shipped in a later
release.

**There is no v0.1.2 image either**, for a different reason: its release build
failed on 2026-09-13 pushing to a personal namespace after the repository had
moved to OneMoreRabbit. The only image before v0.3.0 is
`ghcr.io/jobcpf/dprox:v0.1.1`.

## v0.3.0 — 2026-11-23

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
- **A conversation passage keeps its attribution.**
  `qdrant-collection-contract` 0.4 lets a collection hold turns of an agent's
  conversation, chunked by turn so that a result is attributable. dprox read
  one without error before this and dropped all seven fields that say who
  said it, so a result shape gains `conversation`: the agent, the runtime, the
  conversation and turn ids, the role, the turn time and whether it was a
  sub-agent. Null on a document passage, so there is one shape and not two. A
  conversation passage missing the four fields that attribute it is refused,
  as one missing `text` already was. `dprox-query` prints the speaker rather
  than the session file's path.
- **A passage dprox cannot read is dropped and counted, not raised.** Before
  this, one unreadable point made the whole search 502 — so a caller's search
  would fail on some questions and work on others, which reads as flakiness
  rather than as a fault in the index. Now the passage is dropped (fail-closed:
  nothing is shown whose group could not be read), the response metadata
  carries `dropped_count` so a shortened answer does not read like a complete
  one, and each drop is logged at WARNING as `passage_dropped` naming the
  collection and the missing field. `dprox-query` says so on stderr. The same
  rule applies to a missing `text`, a missing `classification_group` and an
  unattributable turn — one rule, not three.
- **`dprox-query`,** a distribution of its own in `tools/dprox-query`, released
  from this repo by this tag and attached to the release as a wheel. It is
  installed with pipx at image build, never while an agent runs. Its address,
  CA, certificate and key are all required with no defaults, written by the
  estate into the seat's persistent home. It states which dprox versions it
  works with — 0.3.0 up to but not including 0.4.0 — and checks before every
  question, not only in `dprox-query check`.

- **`embedding.query_prefix`, required, no default, empty string a real
  choice.** Put in front of every question before it is embedded, and it must
  match what the collection was indexed with — nothing can check that it
  does, because a wrong prefix does not fail, it lowers every score. dprox has
  never applied one; that was symmetric with ingstr, which does not either, so
  retrieval degraded rather than broke. `nomic-embed-text` wants
  `"search_query: "` with the trailing space; `bge-m3` wants `""`.
  (Operator's model choice, arch 2026-11-19.)
- **Fixes from arch's review of the release** (hub #16893):
  - a plan fault no longer puts the plan's own text in a 502 body. The plan
    must be read to find out whether a CN is in it, so that path runs before
    the caller is known to be one: any certificate the CA signed could read
    out the names of other principals, their groups and the plan's path. The
    caller now gets a fixed sentence and the operator gets the detail.
  - a caller refused for an unconfigured org is no longer told which orgs
    dprox does serve.
  - **each collection is now searched with only its own org's groups**, and
    every hit is checked again against them. One filter of all the caller's
    groups went to every collection, so a passage misfiled into another org's
    collection came back labelled with the wrong org. The two checks are now
    independent, which is what the design claims for them.
  - `/healthz` goes degraded when the plan stops loading. `loaded` stayed true
    from the last success, so health said ok while every query got a 502.
  - the body limit **stops reading at the limit** instead of measuring a body
    already in memory. It runs before the route and the listener accepts a
    peer with no certificate, so anyone who can reach the port could send a
    chunked multi-gigabyte body.
  - `plan.cache_enabled` with `plan.reload_on_mtime_change` false is refused:
    it never re-reads the plan, so removing a caller's group would not take
    effect until restart — the one revocation the design promises is
    immediate. Staleness is now `(mtime_ns, inode, size)`, not mtime alone,
    because the estate renames a new file over the old one.
  - `build_classification_filter` refuses an empty group set rather than
    building `MatchAny(any=[])` and trusting Qdrant to match nothing.
  - one `passage_dropped` warning per collection per query, with a count,
    rather than one per passage.
  - the `query` audit event is always at INFO. Turning `log_query_text` on at
    the usual level used to delete the whole event.
  - `dprox-query` builds an `ssl.SSLContext` (httpx 0.28) and refuses a
    private key that anyone but its owner can read.

**Action — the estate, before rolling the image:**

1. Rewrite `config.yml`. Remove the top-level `org:`. Replace
   `qdrant.collection: <name>` with a collection for each org:

   ```yaml
   qdrant:
     collections:
       arc: arc_documents
       job: job_documents
   embedding:
     query_prefix: "search_query: "   # required; "" is a real choice
   ```

   `plan.cache_enabled: true` with `plan.reload_on_mtime_change: false` is
   now refused at startup, so check the deployed file does not have it.

   Both old keys fail at startup rather than being ignored, because the schema
   forbids unknown keys. `examples/config.example.yml` is the full shape.
2. Roll rbac-compiler v0.7.3 or later first, so the plan carries `groups[]`.
   dprox 0.3.0 refuses a plan without it, and refuses a plan where a caller
   holds a group `groups[]` does not list.
3. Place `dprox-query`'s configuration, certificate and key in each seat's
   persistent home. `dprox-query check` says whether all three are in place.

## v0.2.0 — 2026-11-13 — **built, never released**

**No image exists and none will.** It was tagged locally on 2026-11-13 and the
push waited; by the time the route for releasing was settled, v0.3.0 had
replaced it, and v0.3.0's configuration shape supersedes v0.2.0's. Publishing
it would add an image nobody can roll (arch, hub #16996). Everything below
shipped in v0.3.0.

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
