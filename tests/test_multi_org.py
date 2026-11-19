"""One dprox for every org: the two checks, and what they refuse (v0.3.0).

The accepted design says it in one sentence: *dprox searches only the
collections of the caller's orgs, and inside each keeps only passages of the
caller's groups. If one check is wrong, the other still holds.*
(`search-one-service-design-v0_1`, accepted 2026-11-13.)

These tests exercise the first check and the places where it must refuse
rather than answer. The second check is covered in `test_qdrant.py` against
`build_classification_filter`, which is unchanged.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import structlog
from fastapi.testclient import TestClient
from pydantic import ValidationError

from dprox.config import Config, QdrantConfig
from dprox.mtls import require_mtls
from dprox.plan import PlanCache
from dprox.qdrant import QdrantClient, QdrantOrgNotConfigured, QdrantUnavailable
from dprox.server import create_app

# --- fixtures: a plan and a config that span two orgs -------------------------


@pytest.fixture
def two_org_plan(tmp_path: Path) -> Path:
    """A plan where one caller reads one org and another reads both.

    `agent_both`'s two groups belong to different orgs, which is the case the
    design says could not be configured before: an agent had one dprox address,
    so it had one org.
    """
    target = tmp_path / "two_org_plan.yml"
    target.write_text(
        textwrap.dedent(
            """
            meta:
              compiled_at: '2026-01-01T00:00:00Z'
              compiler_version: 0.7.3
              schema_version: '0.11'
            required_groups:
            - arc_g1_eng
            - job_g1_eng
            groups:
            - name: arc_g1_eng
              org: arc
              grade: g1
              vertical: engineering
              scope: all
            - name: job_g1_eng
              org: job
              grade: g1
              vertical: engineering
              scope: all
            agent_users:
            - name: agent_arc
              groups:
              - arc_g1_eng
            - name: agent_both
              groups:
              - arc_g1_eng
              - job_g1_eng
            admin_users: []
            """
        ).lstrip(),
        encoding="utf-8",
    )
    return target


@pytest.fixture
def two_org_config(baseline_config_dict: dict, two_org_plan: Path) -> Config:
    data = dict(baseline_config_dict)
    data["plan"] = dict(data["plan"], compiled_plan_path=str(two_org_plan))
    data["qdrant"] = dict(
        data["qdrant"], collections={"arc": "arc_documents", "job": "job_documents"}
    )
    return Config.model_validate(data)


@pytest.fixture
def two_org_plan_cache(two_org_config: Config) -> PlanCache:
    cache = PlanCache(two_org_config.plan)
    cache.initial_load()
    return cache


def _point(group: str, score: float, path: str):
    return SimpleNamespace(
        id=path,
        score=score,
        payload={
            "text": f"passage from {path}",
            "classification_group": group,
            "source_path_rel": path,
        },
    )


def _backend(points_by_collection: dict[str, list]) -> AsyncMock:
    """A Qdrant backend that answers each collection with its own points."""
    backend = AsyncMock()

    async def query_points(*, collection_name: str, **_kwargs):
        if collection_name not in points_by_collection:
            raise AssertionError(f"searched an unexpected collection: {collection_name}")
        return SimpleNamespace(points=points_by_collection[collection_name])

    backend.query_points.side_effect = query_points
    backend.get_collection.return_value = SimpleNamespace(
        config=SimpleNamespace(
            params=SimpleNamespace(
                vectors=SimpleNamespace(size=768, distance=SimpleNamespace(name="Cosine"))
            )
        )
    )
    backend.close = AsyncMock(return_value=None)
    return backend


def _qdrant(config: Config, backend) -> QdrantClient:
    return QdrantClient(config.qdrant, config.embedding.vector_dim, backend=backend)


def _client(config, plan_cache, ollama, qdrant, *, cn: str) -> TestClient:
    app = create_app(config, plan_cache, ollama=ollama, qdrant=qdrant)
    app.dependency_overrides[require_mtls] = lambda cn=cn: cn
    return TestClient(app)


# --- check one: which collections are opened ----------------------------------


async def test_a_caller_in_one_org_searches_only_that_collection(
    two_org_config: Config,
) -> None:
    backend = _backend({"arc_documents": [], "job_documents": []})
    client = _qdrant(two_org_config, backend)

    found = await client.search(
        [0.1] * 768, frozenset({"arc_g1_eng"}), frozenset({"arc"}), limit=10
    )

    assert found.collections == ["arc_documents"]
    searched = [c.kwargs["collection_name"] for c in backend.query_points.call_args_list]
    assert searched == ["arc_documents"], "the other org's collection was never opened"


async def test_a_caller_in_two_orgs_searches_both_collections(
    two_org_config: Config,
) -> None:
    backend = _backend(
        {
            "arc_documents": [_point("arc_g1_eng", 0.9, "a.docx")],
            "job_documents": [_point("job_g1_eng", 0.8, "b.docx")],
        }
    )
    client = _qdrant(two_org_config, backend)

    found = await client.search(
        [0.1] * 768,
        frozenset({"arc_g1_eng", "job_g1_eng"}),
        frozenset({"arc", "job"}),
        limit=10,
    )

    assert found.collections == ["arc_documents", "job_documents"]
    assert [h.org for h in found.hits] == ["arc", "job"]


async def test_the_group_filter_is_applied_in_every_collection(
    two_org_config: Config,
) -> None:
    """The second check is not weakened by there being more than one collection."""
    backend = _backend({"arc_documents": [], "job_documents": []})
    client = _qdrant(two_org_config, backend)

    groups = frozenset({"arc_g1_eng", "job_g1_eng"})
    await client.search([0.1] * 768, groups, frozenset({"arc", "job"}), limit=10)

    filters = [c.kwargs["query_filter"] for c in backend.query_points.call_args_list]
    assert len(filters) == 2
    for f in filters:
        condition = f.must[0]
        assert condition.key == "classification_group"
        assert condition.match.any == sorted(groups)


async def test_no_orgs_searches_nothing(two_org_config: Config) -> None:
    """A caller with no groups has no orgs. Nothing is opened; it fails closed."""
    backend = _backend({"arc_documents": [], "job_documents": []})
    client = _qdrant(two_org_config, backend)

    found = await client.search([0.1] * 768, frozenset(), frozenset(), limit=10)

    assert found.hits == []
    assert found.collections == []
    backend.query_points.assert_not_called()


# --- merging ------------------------------------------------------------------


async def test_results_from_two_collections_are_merged_by_score(
    two_org_config: Config,
) -> None:
    backend = _backend(
        {
            "arc_documents": [
                _point("arc_g1_eng", 0.91, "arc/high.docx"),
                _point("arc_g1_eng", 0.40, "arc/low.docx"),
            ],
            "job_documents": [
                _point("job_g1_eng", 0.77, "job/mid.docx"),
                _point("job_g1_eng", 0.55, "job/lower.docx"),
            ],
        }
    )
    client = _qdrant(two_org_config, backend)

    found = await client.search(
        [0.1] * 768,
        frozenset({"arc_g1_eng", "job_g1_eng"}),
        frozenset({"arc", "job"}),
        limit=10,
    )

    assert [h.score for h in found.hits] == [0.91, 0.77, 0.55, 0.40]
    assert [h.org for h in found.hits] == ["arc", "job", "job", "arc"]


async def test_the_merged_list_is_cut_back_to_the_limit(
    two_org_config: Config,
) -> None:
    """Two collections asked for `limit` each must still return `limit` in all."""
    backend = _backend(
        {
            "arc_documents": [_point("arc_g1_eng", 0.9 - i / 100, f"a{i}") for i in range(3)],
            "job_documents": [_point("job_g1_eng", 0.8 - i / 100, f"b{i}") for i in range(3)],
        }
    )
    client = _qdrant(two_org_config, backend)

    found = await client.search(
        [0.1] * 768,
        frozenset({"arc_g1_eng", "job_g1_eng"}),
        frozenset({"arc", "job"}),
        limit=3,
    )

    assert len(found.hits) == 3
    assert [h.org for h in found.hits] == ["arc", "arc", "arc"], "the three best were arc's"


# --- refusals -----------------------------------------------------------------


async def test_an_org_with_no_collection_is_refused_not_skipped(
    two_org_config: Config,
) -> None:
    """The caller's plan grants an org this dprox cannot reach.

    Searching the orgs it does have would return a short answer that reads
    exactly like a complete one, so the whole search is refused.
    """
    backend = _backend({"arc_documents": [], "job_documents": []})
    client = _qdrant(two_org_config, backend)

    with pytest.raises(QdrantOrgNotConfigured) as exc:
        await client.search(
            [0.1] * 768,
            frozenset({"arc_g1_eng", "idea_g1_eng"}),
            frozenset({"arc", "idea"}),
            limit=10,
        )

    assert "idea" in str(exc.value)
    backend.query_points.assert_not_called()


async def test_one_collection_failing_fails_the_whole_search(
    two_org_config: Config,
) -> None:
    """No partial answers: a caller cannot tell a short list from a full one."""
    backend = _backend({"arc_documents": [], "job_documents": []})

    async def query_points(*, collection_name: str, **_kwargs):
        if collection_name == "job_documents":
            raise ConnectionError("refused")
        return SimpleNamespace(points=[_point("arc_g1_eng", 0.9, "a.docx")])

    backend.query_points.side_effect = query_points
    client = _qdrant(two_org_config, backend)

    with pytest.raises(QdrantUnavailable) as exc:
        await client.search(
            [0.1] * 768,
            frozenset({"arc_g1_eng", "job_g1_eng"}),
            frozenset({"arc", "job"}),
            limit=10,
        )

    assert "job_documents" in str(exc.value), "the failing collection is named"


# --- health -------------------------------------------------------------------


async def test_health_checks_every_collection(two_org_config: Config) -> None:
    backend = _backend({"arc_documents": [], "job_documents": []})
    client = _qdrant(two_org_config, backend)

    status = await client.check_health()

    assert status["orgs"] == ["arc", "job"]
    assert set(status["collections"]) == {"arc", "job"}
    assert status["collections_exist"] is True
    assert status["distances_match"] is True
    assert backend.get_collection.await_count == 2


async def test_one_bad_collection_makes_the_whole_check_fail(
    two_org_config: Config,
) -> None:
    """Checking only the first collection would clear a second one that is wrong."""
    backend = _backend({"arc_documents": [], "job_documents": []})

    def get_collection(name: str):
        distance = "Cosine" if name == "arc_documents" else "Dot"
        return SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(
                    vectors=SimpleNamespace(
                        size=768, distance=SimpleNamespace(name=distance)
                    )
                )
            )
        )

    backend.get_collection.side_effect = get_collection
    client = _qdrant(two_org_config, backend)

    status = await client.check_health()

    assert status["distances_match"] is False
    assert status["collections"]["arc"]["distance_matches"] is True
    assert status["collections"]["job"]["distance_matches"] is False


# --- end to end, through the endpoint -----------------------------------------


def test_query_from_a_two_org_caller_reports_both_orgs(
    two_org_config: Config, two_org_plan_cache: PlanCache, mock_ollama
) -> None:
    backend = _backend(
        {
            "arc_documents": [_point("arc_g1_eng", 0.9, "a.docx")],
            "job_documents": [_point("job_g1_eng", 0.8, "b.docx")],
        }
    )
    qdrant = _qdrant(two_org_config, backend)

    with _client(
        two_org_config, two_org_plan_cache, mock_ollama, qdrant, cn="agent_both"
    ) as c:
        response = c.post("/v1/query", json={"query": "x"})

    assert response.status_code == 200
    body = response.json()
    assert body["metadata"]["orgs_applied"] == ["arc", "job"]
    assert [h["org"] for h in body["results"]] == ["arc", "job"]


def test_query_from_a_one_org_caller_reports_one_org(
    two_org_config: Config, two_org_plan_cache: PlanCache, mock_ollama
) -> None:
    backend = _backend(
        {
            "arc_documents": [_point("arc_g1_eng", 0.9, "a.docx")],
            "job_documents": [_point("job_g1_eng", 0.8, "b.docx")],
        }
    )
    qdrant = _qdrant(two_org_config, backend)

    with _client(
        two_org_config, two_org_plan_cache, mock_ollama, qdrant, cn="agent_arc"
    ) as c:
        response = c.post("/v1/query", json={"query": "x"})

    assert response.status_code == 200
    body = response.json()
    assert body["metadata"]["orgs_applied"] == ["arc"]
    assert [h["org"] for h in body["results"]] == ["arc"]
    searched = [c.kwargs["collection_name"] for c in backend.query_points.call_args_list]
    assert searched == ["arc_documents"]


def test_the_audit_log_records_the_collections_searched(
    two_org_config: Config, two_org_plan_cache: PlanCache, mock_ollama
) -> None:
    """The design asks for this by name, so a wrong org check is readable after."""
    backend = _backend({"arc_documents": [], "job_documents": []})
    qdrant = _qdrant(two_org_config, backend)

    with structlog.testing.capture_logs() as logs:
        with _client(
            two_org_config, two_org_plan_cache, mock_ollama, qdrant, cn="agent_both"
        ) as c:
            c.post("/v1/query", json={"query": "x"})

    events = [e for e in logs if e.get("event") == "query"]
    assert events, "no query event was logged"
    assert events[-1]["collections_searched"] == ["arc_documents", "job_documents"]
    assert events[-1]["orgs_applied"] == ["arc", "job"]


def test_a_refused_org_is_audited_with_the_collections_it_did_not_search(
    two_org_config: Config, two_org_plan_cache: PlanCache, mock_ollama, tmp_path: Path
) -> None:
    """A 502 for an unconfigured org names the caller and its orgs in the log."""
    config = Config.model_validate(
        {
            **two_org_config.model_dump(mode="json"),
            "qdrant": {
                **two_org_config.qdrant.model_dump(mode="json"),
                "collections": {"arc": "arc_documents"},
            },
        }
    )
    backend = _backend({"arc_documents": []})
    qdrant = _qdrant(config, backend)

    with structlog.testing.capture_logs() as logs:
        with _client(
            config, two_org_plan_cache, mock_ollama, qdrant, cn="agent_both"
        ) as c:
            response = c.post("/v1/query", json={"query": "x"})

    assert response.status_code == 502
    failures = [e for e in logs if e.get("event") == "query_failed"]
    assert failures, "no query_failed event was logged"
    assert failures[-1]["orgs_applied"] == ["arc", "job"]
    assert failures[-1]["collections_searched"] is None
    backend.query_points.assert_not_called()


def test_healthz_names_the_orgs_this_dprox_serves(
    two_org_config: Config, two_org_plan_cache: PlanCache, mock_ollama
) -> None:
    backend = _backend({"arc_documents": [], "job_documents": []})
    qdrant = _qdrant(two_org_config, backend)

    with _client(
        two_org_config, two_org_plan_cache, mock_ollama, qdrant, cn="agent_both"
    ) as c:
        body = c.get("/healthz").json()

    assert body["orgs"] == ["arc", "job"]
    assert set(body["checks"]["qdrant"]["collections"]) == {"arc", "job"}


# --- config -------------------------------------------------------------------


def _qdrant_config(**overrides) -> QdrantConfig:
    base: dict = {
        "url": "http://q:6333",
        "api_key_env": "K",
        "collections": {"arc": "arc_documents"},
        "default_limit": 10,
        "max_limit": 50,
        "timeout_seconds": 5,
    }
    base.update(overrides)
    return QdrantConfig(**base)


def test_config_refuses_two_orgs_sharing_one_collection() -> None:
    """Sharing a collection would make the org check a no-op without saying so."""
    with pytest.raises(ValidationError, match="one collection holds one org"):
        _qdrant_config(collections={"arc": "documents", "job": "documents"})


def test_config_refuses_no_collections_at_all() -> None:
    with pytest.raises(ValidationError):
        _qdrant_config(collections={})


def test_config_refuses_an_empty_collection_name() -> None:
    with pytest.raises(ValidationError, match="is empty"):
        _qdrant_config(collections={"arc": "  "})


def test_config_refuses_a_leftover_org_key(baseline_config_dict: dict) -> None:
    """A 0.2 config carries `org:`. It must fail at startup, not be ignored."""
    data = dict(baseline_config_dict, org="arc")
    with pytest.raises(ValidationError, match="org"):
        Config.model_validate(data)


def test_config_refuses_the_0_2_single_collection_key(baseline_config_dict: dict) -> None:
    """Likewise `qdrant.collection:`, which 0.3 replaces with `collections:`."""
    qdrant = dict(baseline_config_dict["qdrant"])
    del qdrant["collections"]
    qdrant["collection"] = "documents"
    data = dict(baseline_config_dict, qdrant=qdrant)
    with pytest.raises(ValidationError):
        Config.model_validate(data)


# --- the shipped examples -----------------------------------------------------


def test_the_two_example_files_agree_with_each_other() -> None:
    """config.example.yml and compiled_plan.example.yml are a pair.

    Someone setting dprox up copies both. If the plan grants an org the config
    has no collection for, every query from that caller is a 502 — and the
    examples would be what taught them to do it.
    """
    import yaml

    from dprox.config import load_config
    from dprox.plan import parse_plan_dict

    config = load_config("examples/config.example.yml")
    plan = parse_plan_dict(
        yaml.safe_load(Path("examples/compiled_plan.example.yml").read_text())
    )

    configured = set(config.qdrant.collections)
    granted = {org for entry in plan.values() for org in entry.orgs}
    assert granted <= configured, (
        f"the example plan grants org(s) {sorted(granted - configured)}, "
        f"which the example config has no collection for"
    )
