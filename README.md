# dprox

RBAC-enforcing query proxy. Read-path service for ARC Power's RAG system.

> **One dprox answers every org.** Stateless, long-running, mTLS-fronted query
> proxy in front of one Qdrant server holding a collection for each org.

The architecture lives in the project's Atlas vault, not here (constitution §3).
The canonical references, by name:

| For | Read |
|---|---|
| What dprox is and how it decides | `dprox-design-spec-v0_3` |
| How it is deployed | `dprox-platform-brief-v0_2` |
| What the estate must place | `dprox-estate-requirements-v0_1` |
| Where agents are told to find it | `dprox-endpoints-file-v0_3` |
| The index it reads | `qdrant-collection-contract` 0.4 (ingstr's) |
| Who may see what | `compiled-rbac-plan` 0.12 (rbac-compile's) |

## Status

**v0.3.0 is the current release** — `ghcr.io/onemorerabbit/dprox:v0.3.0`,
digest `sha256:e1b0f91de2b7e2299e40f40484f12e80c48a312002536428b91aad38bc17dcb4`,
tagged on `main` `831d4bd` (2026-11-23). What is deployed where is the estate's
to state, not this file's: ask estate-manage, or read `component.md` in the
vault, which carries a dated line.

Two checks, independent, and the reason the service is safe to share across
orgs: the caller's orgs come from the plan's `groups[]` and choose which
collections are opened; inside each collection the filter carries only that
org's groups. If one is wrong the other still holds.

> **Image namespace.** Estate ruling 2026-09-13: published images use the
> organisation namespace `ghcr.io/onemorerabbit/`, never a personal account,
> and the release workflow derives it from `github.repository_owner` rather
> than naming it (constitution §11).
>
> **Only two images have ever existed.** `ghcr.io/jobcpf/dprox:v0.1.1`, from
> before the move, and `ghcr.io/onemorerabbit/dprox:v0.3.0`. There is **no
> v0.1.2 image** — its build failed pushing to the old namespace after the
> repository had moved — and **no v0.2.0 image**: v0.2.0 was tagged locally,
> overtaken by v0.3.0, and never released. `release-notes.md` records both.

## Upgrading to 0.3.0 — BREAKING config change

**Edit the config first, roll the image second. They are two acts, not one,**
and there is no order in which both the old and the new config are briefly
fine: a 0.1.x config fails at startup against 0.3.0 and a 0.3.0 config fails
against 0.1.x. Expect dprox down between the two. (Config is templated by one
playbook and the image pinned in another, so they cannot be one act —
estate-manage, `dprox-config-migration-response-v0_1`.)

**Roll rbac-compiler v0.7.3 or later first.** 0.3.0 refuses a plan with no
`groups[]` section and names the contract version it needs.

Parsing is strict (`extra="forbid"`), so every removed key below **stops the
start and names itself** rather than being ignored.

### Keys that must go

| Removed in | Key | Was |
|---|---|---|
| 0.2.0 | `server.request_timeout_seconds` | parsed, never applied — no request envelope existed |
| 0.2.0 | `server.max_request_body_bytes` | parsed, never enforced — **no body-size limit existed** |
| 0.2.0 | `mtls.cn_to_agent_strategy` | parsed, never read; only one value was ever legal |
| 0.3.0 | `org` (top level) | one dprox has no single org |
| 0.3.0 | `qdrant.collection` | replaced by `qdrant.collections` |

All three 0.2.0 removals were declared, validated and read by nothing. They
were deleted rather than implemented because nobody had chosen their values —
implementing `65536` would have turned an example comment into a real limit on
the trust boundary (constitution §11).

### Keys that must appear

| Added in | Key | Note |
|---|---|---|
| 0.2.0 | `server.max_body_bytes` | required, no default. A real limit, enforced **while the body is read** and refused with HTTP 413 `request_too_large`. A new name on purpose: reusing `max_request_body_bytes` would have changed that line's behaviour with no diff to notice |
| 0.3.0 | `qdrant.collections` | a collection for each org, at least one. Two orgs may not share one |
| 0.3.0 | `embedding.query_prefix` | required, no default, `""` an explicit choice. **Write it double-quoted**, not as a YAML block scalar — see the note beside the key in `examples/config.example.yml` |

### One combination is refused

`plan.cache_enabled: true` with `plan.reload_on_mtime_change: false`. Together
they mean the plan in memory is whatever was there at startup, so removing a
caller's group would not take effect until restart — and that is the one
revocation the design promises is immediate. A setting that makes a documented
guarantee silently false is a phantom control, so dprox does not start.

### What the failure looks like

```
config validation failed for /etc/dprox/config.yml:
server.max_request_body_bytes
  Extra inputs are not permitted [type=extra_forbidden, ...]
```

Delete the named key and restart. `examples/config.example.yml` is the whole
file in its current shape; `dprox-estate-requirements-v0_1` in the vault is the
estate's list with a check for each item.

## What the service does

| Part | What it does |
|---|---|
| `cli.py` | `dprox serve`, `dprox health`, `dprox version` |
| `config.py` | the schema, `extra="forbid"` everywhere, exit 3 on a bad config |
| `plan.py` | `compiled_plan.yml` parsing, and the cache that re-reads it when the file changes. Splits a caller's groups by org, refusing a plan it cannot resolve |
| `mtls.py` | the listener takes a client certificate or none; the routes enforce. `/healthz` and `/version` are open, `/v1/query` is not |
| `ollama.py` | embedding, with `query_prefix` applied here and nowhere else |
| `qdrant.py` | the two checks and the search. `build_classification_filter` is the security boundary and is tested to 100% |
| `bodylimit.py` | refuses an oversize body **while reading it**, before the route and before any identity exists |
| `server.py` | the app, the endpoints, the error taxonomy and the audit events |
| `audit.py` | one JSON event per query: `orgs_applied`, `collections_searched`, `result_count`, `dropped_count`, `query_hash` and the latencies. The query text only when `logging.log_query_text` is on |
| `tools/dprox-query/` | the search command, **its own distribution** — the seat and OpenClaw images install it without the server's dependencies |

### Release history

See [`release-notes.md`](release-notes.md) — the one home for release history,
including versions built but not released. Two entries describe versions with
no image: `v0.1.2` (its build failed on the namespace) and `v0.2.0` (tagged
locally, overtaken by v0.3.0, never released). Both say so in their own entry,
so neither can be mistaken for something pullable.

## Quick start (venv, Windows PowerShell)

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# Generate a throwaway CA + server + agent certs (one-shot).
python scripts\dev_certs.py
# certs/{ca,server,agent_alice,agent_bob,agent_oversight}.{crt,key} written.

# Run the server with mTLS on :8443.
dprox serve --config examples\config.dev.yml

# In another shell:
# /healthz and /version are public — no client cert needed (--insecure
# only because the dev CA isn't in your system trust store).
curl --cacert certs\ca.crt https://127.0.0.1:8443/healthz
curl --cacert certs\ca.crt https://127.0.0.1:8443/version

# /v1/query requires a verified agent client cert.
curl --cacert certs\ca.crt `
     --cert certs\agent_alice.crt --key certs\agent_alice.key `
     -X POST https://127.0.0.1:8443/v1/query `
     -H 'Content-Type: application/json' `
     -d '{\"query\": \"test\", \"limit\": 5}'

# A request without a cert hits the same port, returns 401 auth_required.
curl --cacert certs\ca.crt `
     -X POST https://127.0.0.1:8443/v1/query `
     -H 'Content-Type: application/json' `
     -d '{\"query\": \"test\"}'

# Tests
pytest
```

## Smoke testing on Windows

`scripts/smoke_mtls.py` runs an end-to-end test against a running dprox using
the dev certs. **It will fail on machines with antivirus / endpoint security
that performs TLS interception** (AVG, Avast, ESET, Symantec etc) — these
products substitute their own cert in place of dprox's, which the test
client cannot validate. This is environmental, not a dprox bug.

If you see `unable to get local issuer certificate` from the smoke client,
check what cert the server is actually presenting:

```powershell
.\.venv\Scripts\python.exe -c "import socket, ssl; ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE; s = ctx.wrap_socket(socket.create_connection(('localhost', 8443)), server_hostname='localhost'); from cryptography import x509; c = x509.load_der_x509_certificate(s.getpeercert(binary_form=True)); print('Issuer:', c.issuer.rfc4514_string())"
```

If the issuer isn't `CN=dprox-dev-ca`, an interceptor is in the way.
Either add a localhost exclusion in the AV product, or run the smoke test
on a clean container / VM. The unit tests (`pytest`) don't go over the
wire and pass regardless.

## Configuration

`dprox` reads `config.yml` resolved in this order:

1. `--config <path>` CLI flag, if given.
2. `DPROX_CONFIG` env var.
3. `/etc/dprox/config.yml` (the in-container default — populated by the
   platform's Ansible templating).

The schema is enforced with Pydantic v2 (`extra="forbid"` everywhere), so a
typo in any field name causes startup to fail with exit code 3 rather than a
silent ignore. See [`examples/config.example.yml`](examples/config.example.yml)
for the production shape and [`examples/config.dev.yml`](examples/config.dev.yml)
for a localhost-targeted variant.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 2 | `dprox health` upstream check failed (config-load failure included) |
| 3 | `dprox serve` could not load or validate config |
| 4 | Runtime error during `dprox serve` |

## Layout

```
src/dprox/
  cli.py          CLI entry point — serve / health / version
  server.py       FastAPI app factory (create_app(config, plan_cache, ...))
  config.py       schema + loader + ConfigError
  plan.py         compiled_plan.yml parsing, PlanCache, groups split by org
  mtls.py         peer-certificate validation and the require_mtls dependency
  ollama.py       embedding; query_prefix is applied here
  qdrant.py       the two checks, the search, the collection health
  bodylimit.py    pure-ASGI body limit, enforced while reading
  audit.py        the audit logger and its reason vocabulary
  version.py      version + image identity (single source of truth)
tools/dprox-query/
  src/dprox_query/  the search command — its own distribution and own wheel
  tests/            its tests, run by the same `pytest`
tests/              the server's tests, shared fixtures in conftest.py
examples/           config.example.yml (production shape), config.dev.yml,
                    compiled_plan.example.yml
```

## Versioning + release

`__version__` lives in [`src/dprox/version.py`](src/dprox/version.py).
[`pyproject.toml`](pyproject.toml) and
[`tools/dprox-query/pyproject.toml`](tools/dprox-query/pyproject.toml) carry
the same string: the tool is released from this repo by the same tag, and a
reader comparing a tool against a server should not have to map one number onto
another. The release job gates on all three. Production deployments pin
explicitly — never `:latest`.

**The flow, as used for v0.3.0 on 2026-11-23.** A component merges its own
code repo and its own release PR (operator, 2026-11-11), and the seat does the
pushing — the operator does not run pushes in seats (operator, via arch
2026-11-23).

```bash
# 1. On dev: bump the version in all three files. No automation — discipline.
$EDITOR src/dprox/version.py pyproject.toml tools/dprox-query/pyproject.toml
git commit -am "release: X.Y.Z" && git push origin dev   # let CI go green

# 2. A release branch and a PR into main. Merge it yourself once checks pass.
git push origin dev:refs/heads/release/vX.Y.Z
gh pr create --base main --head release/vX.Y.Z --title "Release vX.Y.Z ..."
gh pr checks <n>            # all green first
gh pr merge <n> --merge --subject "Release: merge dev -> main for vX.Y.Z"

# 3. Tag the merge commit. ANNOTATED (constitution §12), which `gh release
#    create` does not make — so the tag object and the ref go through the API.
gh api --method POST /repos/<owner>/<repo>/git/tags \
       -f tag=vX.Y.Z -f message="Release vX.Y.Z ..." \
       -f object=<merge commit sha> -f type=commit
gh api --method POST /repos/<owner>/<repo>/git/refs \
       -f ref=refs/tags/vX.Y.Z -f sha=<tag object sha>

# 4. Watch the build, then READ THE DIGEST BACK FROM GHCR, not from the log.
gh run watch <run id> --exit-status
gh api /orgs/<org>/packages/container/dprox/versions \
       --jq '.[] | {tags: .metadata.container.tags, digest: .name}'
```

Branch from `dev` rather than from a locally prepared `main`, so `main`'s
history is one merge of `dev` and no local release commits survive. Release
tags are `vMAJOR.MINOR.PATCH` (constitution §12) — never a bare `X.Y.Z`.

> **Why not `git push origin main --follow-tags`.** It was the flow until
> v0.3.0 and it is refused in a seat session, correctly: one command publishes
> an image and moves the branch the estate installs from. The PR route does
> the same work with a reviewable step in the middle, and it is what the other
> components use. The earlier `REVISIT(branch-protection-pass)` note asked for
> exactly this change; it has happened, so the note is gone.

The `release.yml` workflow runs the test gate, verifies the tag matches both
`dprox.__version__` and `dprox_query.__version__`, builds the image with
Buildx, and pushes both `ghcr.io/<org>/dprox:vX.Y.Z` and
`ghcr.io/<org>/dprox:X.Y.Z`. The namespace is derived from
`github.repository_owner`, lowercased, not hardcoded (constitution §11).

> **The `dprox-query` wheel is not yet attached to a release.** The change that
> builds it and attaches it sits on branch `atlas/dprox/release-wheel`: this
> seat's GitHub App has no `workflows` permission, so it cannot push a change
> to `.github/workflows/`. Until someone pushes that branch or grants the
> permission, a tag publishes the image only, and agent-skeleton and
> agent-image have nothing to fetch.

## Cert mount permissions (production note)

The platform's Ansible bind-mounts `~/docker/dprox/<org>/certs/` into the
container at `/etc/dprox/certs/`. The container runs as **UID 10042**
(see [`Dockerfile`](Dockerfile)). For dprox to read `server.key` (mode
0400, owner-only), the host file must be owned by UID 10042 — either
chown the certs at distribution time, or run the container with
`--user $(id -u <ansi-user>)` to override. Either is fine; document
the choice in the per-org compose template.

## License

TBD — pending alignment with the sibling Ingstr project.
