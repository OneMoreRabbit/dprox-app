from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from qdrant_client.http.exceptions import (
    ResponseHandlingException,
    UnexpectedResponse,
)
from qdrant_client.models import FieldCondition, Filter, MatchAny

from dprox.config import QdrantConfig
from dprox.qdrant import (
    CLASSIFICATION_GROUP_KEY,
    EmptyGroupSet,
    MalformedPassage,
    QdrantClient,
    QdrantHit,
    QdrantTimeout,
    QdrantUnavailable,
    build_classification_filter,
)


def by_org(groups: frozenset[str], org: str = "testorg") -> dict:
    """The caller's groups, split by org — what search() now takes.

    One argument rather than an org set and a group set, so an org and the
    groups searched inside it cannot come from different places.
    """
    return {org: groups}


def _one(status: dict, org: str = "testorg") -> dict:
    """One org's entry in a check_health result."""
    return status["collections"][org]


def _config(**overrides) -> QdrantConfig:
    base: dict = {
        "url": "http://test-qdrant:6333",
        "api_key_env": "TEST_QDRANT_API_KEY",
        "collections": {"testorg": "documents"},
        "default_limit": 10,
        "max_limit": 50,
        "timeout_seconds": 5,
    }
    base.update(overrides)
    return QdrantConfig(**base)


def _client(backend, *, config=None, dim: int = 4) -> QdrantClient:
    return QdrantClient(config or _config(), expected_vector_dim=dim, backend=backend)


def _point(payload: dict, score: float = 0.85, point_id: int = 1):
    return SimpleNamespace(id=point_id, score=score, payload=payload)


def _response(points: list) -> SimpleNamespace:
    """Wrap a list of points in a QueryResponse-shaped object (.points)."""
    return SimpleNamespace(points=points)


def _collection_info(size: int):
    return SimpleNamespace(
        config=SimpleNamespace(
            params=SimpleNamespace(vectors=SimpleNamespace(size=size))
        )
    )


# --- build_classification_filter (security boundary) -------------------------
# Spec §10.3: 100% test coverage on this code path is non-negotiable.


def test_filter_uses_classification_group_key() -> None:
    f = build_classification_filter(frozenset({"g1", "g2"}))
    assert isinstance(f, Filter)
    assert len(f.must) == 1
    cond = f.must[0]
    assert isinstance(cond, FieldCondition)
    assert cond.key == CLASSIFICATION_GROUP_KEY
    assert cond.key == "classification_group"


def test_filter_must_clause_uses_match_any() -> None:
    f = build_classification_filter(frozenset({"g1", "g2"}))
    cond = f.must[0]
    assert isinstance(cond.match, MatchAny)
    assert set(cond.match.any) == {"g1", "g2"}


def test_filter_groups_are_sorted_for_deterministic_audit() -> None:
    f = build_classification_filter(frozenset({"zeta", "alpha", "mu"}))
    assert f.must[0].match.any == ["alpha", "mu", "zeta"]


def test_no_groups_refuses_to_build_a_filter_at_all() -> None:
    """It used to build `MatchAny(any=[])` and trust Qdrant to match nothing.

    That is Qdrant's reading of an empty list, not a guarantee, and the cost
    of being wrong about it once is every passage in the collection. Nothing
    reaches here in practice — a caller with no groups has no orgs, so the
    search returns before a collection is opened — so raising costs nothing
    and removes the assumption (arch's review, hub #16893).
    """
    with pytest.raises(EmptyGroupSet):
        build_classification_filter(frozenset())


async def test_a_caller_with_no_groups_opens_no_collection() -> None:
    """The guard above is never reached, and this is why."""
    backend = AsyncMock()
    client = _client(backend)

    found = await client.search([0.1] * 4, {}, limit=10)

    assert found.hits == []
    assert found.collections == []
    backend.query_points.assert_not_called()


def test_filter_with_single_group() -> None:
    f = build_classification_filter(frozenset({"g_only"}))
    assert f.must[0].match.any == ["g_only"]


def test_filter_uses_must_not_should() -> None:
    """Spec §3.4 critical invariant: the filter is non-negotiable. 'must' clauses
    are required matches in Qdrant; 'should' is optional. Wrong clause → filter
    is bypassable. This test guards against that regression."""
    f = build_classification_filter(frozenset({"g1"}))
    assert f.must is not None and len(f.must) == 1
    assert f.should is None
    assert f.must_not is None


# --- QdrantClient.search (calls AsyncQdrantClient.query_points under the hood) ---


async def test_search_passes_built_filter_to_backend() -> None:
    backend = AsyncMock()
    backend.query_points.return_value = _response([])
    client = _client(backend)

    await client.search([0.1] * 4, by_org(frozenset({"g1", "g2"})), limit=5)

    backend.query_points.assert_awaited_once()
    kwargs = backend.query_points.await_args.kwargs
    assert kwargs["collection_name"] == "documents"
    # qdrant-client universal query API: vector is passed as `query=`,
    # not `query_vector=` (the old search()/1.13- kwarg).
    assert kwargs["query"] == [0.1] * 4
    assert kwargs["limit"] == 5
    assert kwargs["with_payload"] is True

    q_filter = kwargs["query_filter"]
    assert isinstance(q_filter, Filter)
    assert q_filter.must[0].key == "classification_group"
    assert set(q_filter.must[0].match.any) == {"g1", "g2"}


async def test_search_does_not_call_legacy_search_method() -> None:
    """Regression guard: dprox must use query_points (qdrant-client >=1.10),
    NOT search() (removed circa 1.13). See dprox-v0.1.0-qdrant-client-bug.md."""
    backend = AsyncMock()
    backend.query_points.return_value = _response([])
    client = _client(backend)

    await client.search([0.1] * 4, by_org(frozenset({"g"})), limit=5)

    backend.query_points.assert_awaited_once()
    backend.search.assert_not_called()


async def test_search_projects_payload_to_qdrant_hit() -> None:
    backend = AsyncMock()
    backend.query_points.return_value = _response(
        [
            _point(
                {
                    "text": "wage policy",
                    "classification_group": "arc_g0",
                    "source_path_rel": "drive/policy.docx",
                    "file_type": "docx",
                    "chunk_index": 3,
                    "chunk_total": 12,
                    "modified_at": "2026-04-28T08:00:00Z",
                    "indexed_at": "2026-04-28T08:15:00Z",
                },
                score=0.781,
                point_id=42,
            )
        ]
    )
    client = _client(backend)

    found = await client.search([0.1] * 4, by_org(frozenset({"arc_g0"})), limit=10)
    hits, elapsed_ms = found.hits, found.elapsed_ms

    assert len(hits) == 1
    h = hits[0]
    assert isinstance(h, QdrantHit)
    assert h.text == "wage policy"
    assert h.classification_group == "arc_g0"
    assert h.score == pytest.approx(0.781)
    assert h.source_path_rel == "drive/policy.docx"
    assert h.chunk_index == 3
    assert elapsed_ms >= 0


async def test_search_handles_minimal_payload() -> None:
    """Optional fields can be missing; required fields cannot."""
    backend = AsyncMock()
    backend.query_points.return_value = _response(
        [_point({"text": "minimal", "classification_group": "g_min"})]
    )
    client = _client(backend)

    found = await client.search([0.1] * 4, by_org(frozenset({"g_min"})), limit=10)
    hits = found.hits

    assert hits[0].source_path_rel is None
    assert hits[0].file_type is None
    assert hits[0].chunk_index is None


# A passage dprox cannot read is DROPPED and COUNTED, not raised. Dropping is
# fail-closed — nothing is shown whose group could not be read — and one bad
# point out of a hundred thousand must not make a caller's search fail on some
# questions and work on others (arch's suggestion, hub #16875; dprox's call).


async def test_a_point_missing_text_is_dropped_and_counted() -> None:
    backend = AsyncMock()
    backend.query_points.return_value = _response(
        [
            _point({"classification_group": "g_eng"}),  # no 'text'
            _point({"text": "fine", "classification_group": "g_eng"}, point_id=2),
        ]
    )
    client = _client(backend)

    found = await client.search([0.1] * 4, by_org(frozenset({"g_eng"})), limit=10)

    assert [h.text for h in found.hits] == ["fine"], "the good passage still comes back"
    assert len(found.dropped) == 1
    assert found.dropped[0][0] == "documents", "the collection is named"
    assert "missing 'text'" in found.dropped[0][1]


async def test_a_point_missing_classification_group_is_dropped_and_counted() -> None:
    """Fail-closed: a passage whose group cannot be read is never shown."""
    backend = AsyncMock()
    backend.query_points.return_value = _response([_point({"text": "orphan chunk"})])
    client = _client(backend)

    found = await client.search([0.1] * 4, by_org(frozenset({"g"})), limit=10)

    assert found.hits == []
    assert len(found.dropped) == 1
    assert "missing 'classification_group'" in found.dropped[0][1]


async def test_a_point_with_empty_text_is_dropped_and_counted() -> None:
    backend = AsyncMock()
    backend.query_points.return_value = _response(
        [_point({"text": "", "classification_group": "g"})]
    )
    client = _client(backend)

    found = await client.search([0.1] * 4, by_org(frozenset({"g"})), limit=10)

    assert found.hits == []
    assert len(found.dropped) == 1


async def test_a_healthy_collection_drops_nothing() -> None:
    """The count earns its place by being zero when nothing is wrong."""
    backend = AsyncMock()
    backend.query_points.return_value = _response(
        [_point({"text": "t", "classification_group": "g"})]
    )
    client = _client(backend)

    found = await client.search([0.1] * 4, by_org(frozenset({"g"})), limit=10)

    assert found.dropped == []


async def test_a_malformed_passage_is_still_a_502_if_it_escapes() -> None:
    """MalformedPassage subclasses QdrantUnavailable, so nothing becomes a 500."""
    assert issubclass(MalformedPassage, QdrantUnavailable)


async def test_search_rejects_response_without_points_attribute() -> None:
    """Defence in depth: a future client returning a non-QueryResponse
    object surfaces as QdrantUnavailable rather than AttributeError."""
    backend = AsyncMock()
    backend.query_points.return_value = SimpleNamespace()  # no .points
    client = _client(backend)

    with pytest.raises(QdrantUnavailable, match="missing 'points'"):
        await client.search([0.1] * 4, by_org(frozenset({"g"})), limit=10)


async def test_search_timeout_raises_qdrant_timeout() -> None:
    backend = AsyncMock()
    backend.query_points.side_effect = TimeoutError()
    client = _client(backend)

    with pytest.raises(QdrantTimeout, match="timed out"):
        await client.search([0.1] * 4, by_org(frozenset({"g"})), limit=10)


async def test_search_unexpected_response_raises_unavailable() -> None:
    backend = AsyncMock()
    backend.query_points.side_effect = UnexpectedResponse(
        status_code=503, reason_phrase="busy", content=b"", headers={}
    )
    client = _client(backend)

    with pytest.raises(QdrantUnavailable, match="status 503"):
        await client.search([0.1] * 4, by_org(frozenset({"g"})), limit=10)


async def test_search_response_handling_error_raises_unavailable() -> None:
    backend = AsyncMock()
    backend.query_points.side_effect = ResponseHandlingException("malformed")
    client = _client(backend)

    with pytest.raises(QdrantUnavailable, match="response error"):
        await client.search([0.1] * 4, by_org(frozenset({"g"})), limit=10)


async def test_search_unknown_exception_wrapped_as_unavailable() -> None:
    backend = AsyncMock()
    backend.query_points.side_effect = ConnectionError("refused")
    client = _client(backend)

    with pytest.raises(QdrantUnavailable, match="ConnectionError"):
        await client.search([0.1] * 4, by_org(frozenset({"g"})), limit=10)


async def test_search_empty_results_returns_empty_list() -> None:
    backend = AsyncMock()
    backend.query_points.return_value = _response([])
    client = _client(backend)

    found = await client.search([0.1] * 4, by_org(frozenset({"g"})), limit=10)
    hits = found.hits
    assert hits == []


# --- QdrantClient.check_health -----------------------------------------------


async def test_check_health_collection_exists_with_matching_dim() -> None:
    backend = AsyncMock()
    backend.get_collection.return_value = _collection_info(size=4)
    client = _client(backend, dim=4)

    status = await client.check_health()

    assert status["reachable"] is True
    assert _one(status)["collection_exists"] is True
    assert _one(status)["vector_dim"] == 4
    assert _one(status)["vector_dim_matches"] is True
    assert status["error"] is None


async def test_check_health_dim_mismatch_marks_failed() -> None:
    backend = AsyncMock()
    backend.get_collection.return_value = _collection_info(size=768)
    client = _client(backend, dim=384)

    status = await client.check_health()

    assert _one(status)["collection_exists"] is True
    assert _one(status)["vector_dim"] == 768
    assert _one(status)["vector_dim_matches"] is False


async def test_check_health_collection_not_found() -> None:
    backend = AsyncMock()
    backend.get_collection.side_effect = UnexpectedResponse(
        status_code=404, reason_phrase="Not Found", content=b"", headers={}
    )
    client = _client(backend)

    status = await client.check_health()

    assert status["reachable"] is True
    assert _one(status)["collection_exists"] is False
    assert "not found" in (status["error"] or "")


async def test_check_health_other_status_marks_unreachable() -> None:
    backend = AsyncMock()
    backend.get_collection.side_effect = UnexpectedResponse(
        status_code=500, reason_phrase="ISE", content=b"", headers={}
    )
    client = _client(backend)

    status = await client.check_health()
    assert status["reachable"] is False
    assert "500" in (status["error"] or "")


async def test_check_health_timeout() -> None:
    backend = AsyncMock()
    backend.get_collection.side_effect = TimeoutError()
    client = _client(backend)

    status = await client.check_health()
    assert status["reachable"] is False
    assert _one(status)["error"] == "timeout"


async def test_check_health_connection_error() -> None:
    backend = AsyncMock()
    backend.get_collection.side_effect = ConnectionError("refused")
    client = _client(backend)

    status = await client.check_health()
    assert status["reachable"] is False
    assert "refused" in (status["error"] or "")


async def test_check_health_handles_named_vectors_config() -> None:
    """Some collections use named-vector config (a dict), not VectorParams.
    dprox's v0.1 collection is unnamed; the helper still tolerates the
    other shape."""
    backend = AsyncMock()
    backend.get_collection.return_value = SimpleNamespace(
        config=SimpleNamespace(
            params=SimpleNamespace(
                vectors={"my_vec": SimpleNamespace(size=4)}
            )
        )
    )
    client = _client(backend, dim=4)

    status = await client.check_health()
    assert _one(status)["vector_dim"] == 4
    assert _one(status)["vector_dim_matches"] is True


# --- API key resolution ------------------------------------------------------


def test_api_key_resolved_from_env(monkeypatch) -> None:
    """The QdrantClient pulls the API key from the env var named in config."""
    monkeypatch.setenv("TEST_QDRANT_API_KEY", "secret-token")
    # We don't construct AsyncQdrantClient for real (would try to connect on
    # first call) — but we can verify the env var is read by inspecting the
    # behaviour indirectly. Smoke: construction succeeds.
    client = QdrantClient(_config(), expected_vector_dim=4)
    # Smoke — the real backend is built lazily
    assert client.url == "http://test-qdrant:6333"
    assert client.collections == {"testorg": "documents"}


def test_missing_api_key_env_does_not_raise() -> None:
    """Missing env var → API key is None; Qdrant rejects later if it requires auth."""
    client = QdrantClient(_config(api_key_env="DEFINITELY_NOT_SET_XYZ"), expected_vector_dim=4)
    assert client.collections == {"testorg": "documents"}


# --- aclose -------------------------------------------------------------------


async def test_aclose_calls_backend_close() -> None:
    backend = AsyncMock()
    backend.close = AsyncMock(return_value=None)
    client = _client(backend)
    await client.aclose()
    backend.close.assert_awaited_once()


async def test_aclose_tolerates_backend_without_close_method() -> None:
    backend = SimpleNamespace()  # no close() attribute
    client = _client(backend)
    await client.aclose()  # should not raise


# --- the collection's distance metric -----------------------------------------
#
# ingstr's qdrant-collection-contract fixes cosine and says plainly that ingstr
# CANNOT check it: "a collection built with the wrong distance is written
# happily and searched wrongly with no error anywhere". dprox reads the
# collection info already, so dprox checks it.


def _info(dim: int, distance: str | None):
    """CollectionInfo-shaped double; distance=None models an older Qdrant."""
    vectors = SimpleNamespace(size=dim)
    if distance is not None:
        vectors.distance = SimpleNamespace(name=distance)
    return SimpleNamespace(config=SimpleNamespace(params=SimpleNamespace(vectors=vectors)))


@pytest.mark.asyncio
async def test_health_reports_cosine_as_matching(baseline_config, mock_qdrant_backend) -> None:
    dim = baseline_config.embedding.vector_dim
    mock_qdrant_backend.get_collection.return_value = _info(dim, "Cosine")
    client = QdrantClient(baseline_config.qdrant, dim, backend=mock_qdrant_backend)

    status = await client.check_health()

    assert _one(status)["distance"] == "cosine"
    assert _one(status)["distance_matches"] is True


@pytest.mark.asyncio
async def test_health_reports_a_wrong_distance_as_not_matching(
    baseline_config, mock_qdrant_backend
) -> None:
    """The case the contract says nobody catches: right size, wrong metric.

    The collection is readable, the vector size is correct and every query
    succeeds — it simply ranks wrongly. Only this check sees it.
    """
    dim = baseline_config.embedding.vector_dim
    mock_qdrant_backend.get_collection.return_value = _info(dim, "Dot")
    client = QdrantClient(baseline_config.qdrant, dim, backend=mock_qdrant_backend)

    status = await client.check_health()

    assert _one(status)["vector_dim_matches"] is True, "size is right; only the metric is wrong"
    assert _one(status)["distance"] == "dot"
    assert _one(status)["distance_matches"] is False


@pytest.mark.asyncio
async def test_health_treats_an_unreadable_distance_as_not_matching(
    baseline_config, mock_qdrant_backend
) -> None:
    """If the metric cannot be read, dprox does not assume it is cosine."""
    dim = baseline_config.embedding.vector_dim
    mock_qdrant_backend.get_collection.return_value = _info(dim, None)
    client = QdrantClient(baseline_config.qdrant, dim, backend=mock_qdrant_backend)

    status = await client.check_health()

    assert _one(status)["distance"] is None
    assert _one(status)["distance_matches"] is False
