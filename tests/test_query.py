"""End-to-end tests for the POST /v1/query pipeline (spec §4.2 / §4.3).

Pipeline: auth → plan → body schema → embed → search → respond.
TLS handshake is short-circuited via `app.dependency_overrides[require_mtls]`
so we don't need a live cert in the test client.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

from dprox.config import Config
from dprox.mtls import AuthFailure, require_mtls
from dprox.ollama import OllamaClient
from dprox.plan import PlanCache, PlanError
from dprox.qdrant import QdrantClient
from dprox.server import create_app


def _client(
    config: Config,
    plan_cache: PlanCache,
    ollama: OllamaClient,
    qdrant: QdrantClient,
    *,
    cn: str | None = None,
) -> TestClient:
    app = create_app(config, plan_cache, ollama=ollama, qdrant=qdrant)
    if cn is not None:
        app.dependency_overrides[require_mtls] = lambda cn=cn: cn
    return TestClient(app)


# --- happy path ---------------------------------------------------------------


def test_query_happy_path_returns_results_and_metadata(
    baseline_config, plan_cache, mock_ollama, mock_qdrant
) -> None:
    with _client(baseline_config, plan_cache, mock_ollama, mock_qdrant, cn="agent_alice") as c:
        response = c.post("/v1/query", json={"query": "wage policy", "limit": 5})

    assert response.status_code == 200
    body = response.json()

    # Spec §4.2 response shape
    assert set(body) == {"results", "metadata"}
    assert isinstance(body["results"], list)

    md = body["metadata"]
    assert md["agent"] == "agent_alice"
    assert md["groups_applied"] == ["g_engineering"]
    assert md["result_count"] == len(body["results"])
    assert md["query_hash"] == hashlib.sha256(b"wage policy").hexdigest()[:16]


def test_query_uses_default_limit_when_omitted(
    baseline_config, plan_cache, mock_ollama, mock_qdrant_backend
) -> None:
    qdrant = QdrantClient(
        baseline_config.qdrant,
        baseline_config.embedding.vector_dim,
        backend=mock_qdrant_backend,
    )
    with _client(baseline_config, plan_cache, mock_ollama, qdrant, cn="agent_alice") as c:
        response = c.post("/v1/query", json={"query": "no-limit-given"})

    assert response.status_code == 200
    # baseline default_limit = 10
    assert mock_qdrant_backend.query_points.await_args.kwargs["limit"] == 10


def test_query_passes_caller_groups_to_qdrant_filter(
    baseline_config, plan_cache, mock_ollama, mock_qdrant_backend
) -> None:
    """Spec §3.4 critical invariant: filter derives only from the CN's group set."""
    qdrant = QdrantClient(
        baseline_config.qdrant,
        baseline_config.embedding.vector_dim,
        backend=mock_qdrant_backend,
    )
    with _client(baseline_config, plan_cache, mock_ollama, qdrant, cn="agent_oversight") as c:
        c.post("/v1/query", json={"query": "x"})

    q_filter = mock_qdrant_backend.query_points.await_args.kwargs["query_filter"]
    cond = q_filter.must[0]
    assert cond.key == "classification_group"
    # agent_oversight in the fixture has g_engineering + g_finance
    assert set(cond.match.any) == {"g_engineering", "g_finance"}


def test_query_result_shape_matches_spec(
    baseline_config, plan_cache, mock_ollama, mock_qdrant_backend
) -> None:
    """Each result has all spec §4.2 fields; absent ones are null, not missing."""
    mock_qdrant_backend.query_points.return_value = SimpleNamespace(
        points=[
            SimpleNamespace(
                id=1,
                score=0.9,
                payload={
                    "text": "complete chunk",
                    "classification_group": "g_engineering",
                    "source_path_rel": "docs/x.docx",
                    "file_type": "docx",
                    "chunk_index": 2,
                    "chunk_total": 5,
                    "modified_at": "2026-04-28T08:00:00Z",
                    "indexed_at": "2026-04-28T08:15:00Z",
                },
            ),
            SimpleNamespace(
                id=2,
                score=0.5,
                payload={
                    # Optional fields absent
                    "text": "minimal chunk",
                    "classification_group": "g_engineering",
                },
            ),
        ]
    )
    qdrant = QdrantClient(
        baseline_config.qdrant,
        baseline_config.embedding.vector_dim,
        backend=mock_qdrant_backend,
    )
    with _client(baseline_config, plan_cache, mock_ollama, qdrant, cn="agent_alice") as c:
        response = c.post("/v1/query", json={"query": "x"})

    body = response.json()
    expected_keys = {
        "text",
        "classification_group",
        "score",
        "source_path_rel",
        "file_type",
        "chunk_index",
        "chunk_total",
        "modified_at",
        "indexed_at",
    }
    for hit in body["results"]:
        assert set(hit) == expected_keys

    # Optional fields are null on the minimal hit
    minimal = body["results"][1]
    assert minimal["source_path_rel"] is None
    assert minimal["chunk_index"] is None


# --- auth + identity ----------------------------------------------------------


def test_query_with_admin_cn_resolves_admin_groups(
    baseline_config, plan_cache, mock_ollama, mock_qdrant_backend
) -> None:
    qdrant = QdrantClient(
        baseline_config.qdrant,
        baseline_config.embedding.vector_dim,
        backend=mock_qdrant_backend,
    )
    with _client(baseline_config, plan_cache, mock_ollama, qdrant, cn="admin_alice") as c:
        response = c.post("/v1/query", json={"query": "x"})

    assert response.status_code == 200
    md = response.json()["metadata"]
    assert md["agent"] == "admin_alice"
    assert set(md["groups_applied"]) == {"g_engineering", "g_finance", "g_admin"}


def test_query_with_unknown_cn_returns_403(
    baseline_config, plan_cache, mock_ollama, mock_qdrant
) -> None:
    with _client(baseline_config, plan_cache, mock_ollama, mock_qdrant, cn="agent_ghost") as c:
        response = c.post("/v1/query", json={"query": "x"})

    assert response.status_code == 403
    body = response.json()
    assert body["error"] == "unknown_agent"
    assert "agent_ghost" in body["message"]


def test_query_without_cert_returns_401(
    baseline_config, plan_cache, mock_ollama, mock_qdrant
) -> None:
    """No dependency_override + no peer cert → require_mtls raises AuthFailure → 401."""
    app = create_app(baseline_config, plan_cache, ollama=mock_ollama, qdrant=mock_qdrant)
    with TestClient(app) as c:
        response = c.post("/v1/query", json={"query": "x"})

    assert response.status_code == 401
    body = response.json()
    assert body == {"error": "auth_required", "message": "no client cert presented"}


def test_auth_takes_priority_over_body_validation(
    baseline_config, plan_cache, mock_ollama, mock_qdrant
) -> None:
    """Spec §4.2: malformed body from unauth caller returns 401, never 400."""
    app = create_app(baseline_config, plan_cache, ollama=mock_ollama, qdrant=mock_qdrant)
    with TestClient(app) as c:
        response = c.post("/v1/query", json={"groups": ["malicious"]})

    assert response.status_code == 401
    assert response.json()["error"] == "auth_required"


# --- body validation (400 bad_request) ----------------------------------------


def test_query_rejects_unexpected_field(
    baseline_config, plan_cache, mock_ollama, mock_qdrant
) -> None:
    """Identity-looking fields must be rejected (spec §3.1 — never trust caller groups)."""
    with _client(baseline_config, plan_cache, mock_ollama, mock_qdrant, cn="agent_alice") as c:
        response = c.post(
            "/v1/query", json={"query": "x", "groups": ["arc_g0_engineering_global"]}
        )

    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "bad_request"
    assert "unexpected field" in body["message"]
    assert "groups" in body["message"]


def test_query_rejects_unexpected_classification_group_field(
    baseline_config, plan_cache, mock_ollama, mock_qdrant
) -> None:
    with _client(baseline_config, plan_cache, mock_ollama, mock_qdrant, cn="agent_alice") as c:
        response = c.post(
            "/v1/query",
            json={"query": "x", "classification_group": "g_finance"},
        )

    assert response.status_code == 400
    assert "classification_group" in response.json()["message"]


def test_query_rejects_missing_query_field(
    baseline_config, plan_cache, mock_ollama, mock_qdrant
) -> None:
    with _client(baseline_config, plan_cache, mock_ollama, mock_qdrant, cn="agent_alice") as c:
        response = c.post("/v1/query", json={"limit": 5})

    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "bad_request"
    assert "missing required field" in body["message"]
    assert "query" in body["message"]


def test_query_rejects_empty_query_string(
    baseline_config, plan_cache, mock_ollama, mock_qdrant
) -> None:
    with _client(baseline_config, plan_cache, mock_ollama, mock_qdrant, cn="agent_alice") as c:
        response = c.post("/v1/query", json={"query": ""})

    assert response.status_code == 400
    assert response.json()["error"] == "bad_request"


def test_query_rejects_malformed_json(
    baseline_config, plan_cache, mock_ollama, mock_qdrant
) -> None:
    with _client(baseline_config, plan_cache, mock_ollama, mock_qdrant, cn="agent_alice") as c:
        response = c.post(
            "/v1/query",
            content=b"{this isn't json",
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "bad_request"


def test_query_rejects_limit_above_max(
    baseline_config, plan_cache, mock_ollama, mock_qdrant
) -> None:
    """baseline max_limit = 50; limit=51 should be rejected."""
    with _client(baseline_config, plan_cache, mock_ollama, mock_qdrant, cn="agent_alice") as c:
        response = c.post("/v1/query", json={"query": "x", "limit": 51})

    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "bad_request"
    assert "limit must be 1..50" in body["message"]


def test_query_rejects_zero_limit(
    baseline_config, plan_cache, mock_ollama, mock_qdrant
) -> None:
    with _client(baseline_config, plan_cache, mock_ollama, mock_qdrant, cn="agent_alice") as c:
        response = c.post("/v1/query", json={"query": "x", "limit": 0})

    assert response.status_code == 400
    assert "limit must be" in response.json()["message"]


def test_query_rejects_negative_limit(
    baseline_config, plan_cache, mock_ollama, mock_qdrant
) -> None:
    with _client(baseline_config, plan_cache, mock_ollama, mock_qdrant, cn="agent_alice") as c:
        response = c.post("/v1/query", json={"query": "x", "limit": -5})

    assert response.status_code == 400


# --- upstream errors (502 / 504) ----------------------------------------------


def test_query_when_plan_reload_fails_returns_502(
    baseline_config, plan_cache, mock_ollama, mock_qdrant
) -> None:
    def _raise(_cn: str):
        raise PlanError("simulated registry mount disappeared")

    plan_cache.lookup = _raise  # type: ignore[method-assign]

    with _client(baseline_config, plan_cache, mock_ollama, mock_qdrant, cn="agent_alice") as c:
        response = c.post("/v1/query", json={"query": "x"})

    assert response.status_code == 502
    body = response.json()
    assert body["error"] == "upstream_unavailable"
    assert "simulated" in body["message"]


def test_query_when_ollama_times_out_returns_504(
    baseline_config, plan_cache, mock_qdrant
) -> None:
    def fail(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("ollama too slow")

    ollama = OllamaClient(
        baseline_config.embedding, transport=httpx.MockTransport(fail)
    )
    with _client(baseline_config, plan_cache, ollama, mock_qdrant, cn="agent_alice") as c:
        response = c.post("/v1/query", json={"query": "x"})

    assert response.status_code == 504
    assert response.json()["error"] == "upstream_timeout"


def test_query_when_ollama_unreachable_returns_502(
    baseline_config, plan_cache, mock_qdrant
) -> None:
    def fail(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("ollama down")

    ollama = OllamaClient(
        baseline_config.embedding, transport=httpx.MockTransport(fail)
    )
    with _client(baseline_config, plan_cache, ollama, mock_qdrant, cn="agent_alice") as c:
        response = c.post("/v1/query", json={"query": "x"})

    assert response.status_code == 502
    assert response.json()["error"] == "upstream_unavailable"


def test_query_when_qdrant_times_out_returns_504(
    baseline_config, plan_cache, mock_ollama, mock_qdrant_backend
) -> None:

    mock_qdrant_backend.query_points.side_effect = TimeoutError()
    qdrant = QdrantClient(
        baseline_config.qdrant,
        baseline_config.embedding.vector_dim,
        backend=mock_qdrant_backend,
    )
    with _client(baseline_config, plan_cache, mock_ollama, qdrant, cn="agent_alice") as c:
        response = c.post("/v1/query", json={"query": "x"})

    assert response.status_code == 504
    assert response.json()["error"] == "upstream_timeout"


def test_query_when_qdrant_unreachable_returns_502(
    baseline_config, plan_cache, mock_ollama, mock_qdrant_backend
) -> None:
    mock_qdrant_backend.query_points.side_effect = ConnectionError("qdrant down")
    qdrant = QdrantClient(
        baseline_config.qdrant,
        baseline_config.embedding.vector_dim,
        backend=mock_qdrant_backend,
    )
    with _client(baseline_config, plan_cache, mock_ollama, qdrant, cn="agent_alice") as c:
        response = c.post("/v1/query", json={"query": "x"})

    assert response.status_code == 502
    assert response.json()["error"] == "upstream_unavailable"


# --- response error-shape regression -----------------------------------------


def test_auth_failure_handler_returns_flat_error_shape(
    baseline_config, plan_cache, mock_ollama, mock_qdrant
) -> None:
    """Spec §4.3: error body is {"error":..., "message":...}, never {"detail":...}."""
    app = create_app(baseline_config, plan_cache, ollama=mock_ollama, qdrant=mock_qdrant)

    def _fail():
        raise AuthFailure("cert_invalid", "test message", status=401)

    app.dependency_overrides[require_mtls] = _fail

    with TestClient(app) as c:
        response = c.post("/v1/query", json={"query": "x"})

    assert response.status_code == 401
    body = response.json()
    assert "detail" not in body
    assert body == {"error": "cert_invalid", "message": "test message"}


def test_validation_error_handler_returns_flat_error_shape(
    baseline_config, plan_cache, mock_ollama, mock_qdrant
) -> None:
    with _client(baseline_config, plan_cache, mock_ollama, mock_qdrant, cn="agent_alice") as c:
        response = c.post("/v1/query", json={"query": "x", "weird": 1})

    body = response.json()
    assert "detail" not in body
    assert set(body) == {"error", "message"}


# --- public routes still work ------------------------------------------------


def test_healthz_remains_public_with_full_pipeline(
    baseline_config, plan_cache, mock_ollama, mock_qdrant
) -> None:
    with _client(baseline_config, plan_cache, mock_ollama, mock_qdrant) as c:
        response = c.get("/healthz")
    assert response.status_code == 200


def test_version_remains_public_with_full_pipeline(
    baseline_config, plan_cache, mock_ollama, mock_qdrant
) -> None:
    with _client(baseline_config, plan_cache, mock_ollama, mock_qdrant) as c:
        response = c.get("/version")
    assert response.status_code == 200


# --- request size limit (server.max_body_bytes) -------------------------------
#
# Ruled by the operator 2026-11-13 via agenteco-needs-dprox-request-size-limit.
# The limit is enforced BEFORE parsing, so these assert on the raw body rather
# than on a QueryRequest field.


def _body_of(n: int) -> bytes:
    """A syntactically valid request body of exactly n bytes."""
    envelope = b'{"query":"' + b'"}'
    pad = n - len(envelope)
    assert pad > 0, "n too small to build a valid body"
    return b'{"query":"' + (b"a" * pad) + b'"}'


def test_body_one_byte_under_the_limit_is_accepted(
    baseline_config, plan_cache, mock_ollama, mock_qdrant
) -> None:
    limit = baseline_config.server.max_body_bytes
    body = _body_of(limit - 1)
    assert len(body) == limit - 1

    with _client(baseline_config, plan_cache, mock_ollama, mock_qdrant, cn="agent_alice") as c:
        response = c.post(
            "/v1/query", content=body, headers={"Content-Type": "application/json"}
        )

    assert response.status_code == 200


def test_body_one_byte_over_the_limit_is_refused(
    baseline_config, plan_cache, mock_ollama, mock_qdrant
) -> None:
    limit = baseline_config.server.max_body_bytes
    body = _body_of(limit + 1)
    assert len(body) == limit + 1

    with _client(baseline_config, plan_cache, mock_ollama, mock_qdrant, cn="agent_alice") as c:
        response = c.post(
            "/v1/query", content=body, headers={"Content-Type": "application/json"}
        )

    assert response.status_code == 413
    assert response.json()["error"] == "request_too_large"


def test_oversize_body_is_refused_without_parsing(
    baseline_config, plan_cache, mock_ollama, mock_qdrant
) -> None:
    """An oversize body that is not valid JSON still gets 413, not 400.

    Proves the limit runs before parsing: if the body reached QueryRequest
    first, malformed JSON would answer 400 bad_request instead.
    """
    body = b"x" * (baseline_config.server.max_body_bytes + 1)

    with _client(baseline_config, plan_cache, mock_ollama, mock_qdrant, cn="agent_alice") as c:
        response = c.post(
            "/v1/query", content=body, headers={"Content-Type": "application/json"}
        )

    assert response.status_code == 413
    assert response.json()["error"] == "request_too_large"
