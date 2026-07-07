# AGENTS.md — Atlas entry hook

This repo is the **dprox** component of the AgentEco platform, governed by
**Architecture-Above-Code** (AAC). The architecture for this repo does not live here —
it lives in the project vault:

- **Component home:** `G:\VSProjects\Atlas-AgentEco\components\dprox\`
- **Method spec:** `G:\VSProjects\Atlas\AAC-method.md` (pinned in the vault's `registry/io-graph.yml`)

## Before working (every session)

1. Read the vault's `architecture/constitution.md` — global principles.
2. Resolve dprox's edges in `registry/io-graph.yml` (or the compiled reading list at
   `registry/.compiled/dprox/io-manifest.yml`).
3. Read pinned upstream contracts — dprox consumes ingstr's `qdrant-collection-contract`
   from `components/ingstr/docs/provides/`. Note any latest > pinned as drift.
4. Read consumers' feedback — agent-compile's asks in `components/agent-compile/docs/needs/`.
5. Skim `architecture/proposals/` for in-flight ADRs affecting dprox.

## After working

- Publish contracts to `components/dprox/docs/provides/`, asks to `docs/needs/`,
  versioned per AAC §4 (MINOR/MAJOR = new file, prior version to `archive/`).
- Shared-architecture changes go through an ADR in `architecture/proposals/` — never edit
  the constitution directly.
- Bump `updated:` in `components/dprox/component.md`.

Design docs, user manuals, and contracts live in the vault, one home each. This repo holds
code only.
