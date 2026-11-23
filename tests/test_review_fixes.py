"""The five fixes from arch's review of 33e8463..0e4e078 (hub #16893).

Each test is the failure the review found, not the fix: if a later change
reintroduces the fault, these say which one came back.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog
from fastapi.testclient import TestClient

from dprox.config import Config
from dprox.mtls import require_mtls
from dprox.plan import PlanCache
from dprox.server import create_app


def _client(config, plan_cache, ollama, qdrant, *, cn="agent_alice") -> TestClient:
    app = create_app(config, plan_cache, ollama=ollama, qdrant=qdrant)
    app.dependency_overrides[require_mtls] = lambda: cn
    return TestClient(app)


# --- 1. the plan's own text must not reach the caller -------------------------


def test_a_plan_fault_tells_the_caller_nothing_about_the_plan(
    baseline_config_dict: dict, plan_yaml: Path, mock_ollama, mock_qdrant
) -> None:
    """Any certificate the CA signed reaches this path.

    The plan has to be read to find out whether a CN is in it, so this runs
    before the caller is known to be a caller. PlanError text names other
    principals, their groups and the plan's path.
    """
    config = Config.model_validate(baseline_config_dict)
    plan_cache = PlanCache(config.plan)
    plan_cache.initial_load()

    # A caller holding a group groups[] does not list. parse_plan_dict refuses
    # the plan and names both the caller and the group.
    plan_yaml.write_text(
        plan_yaml.read_text().replace(
            "- name: admin_alice\n  groups:\n  - g_engineering",
            "- name: admin_alice\n  groups:\n  - g_secret_project",
            1,
        ),
        encoding="utf-8",
    )

    with structlog.testing.capture_logs() as logs:
        with _client(config, plan_cache, mock_ollama, mock_qdrant, cn="stranger") as c:
            response = c.post("/v1/query", json={"query": "x"})

    assert response.status_code == 502
    body = response.json()
    assert body["message"] == "the access plan could not be read"
    assert "g_secret_project" not in body["message"]
    assert str(plan_yaml) not in body["message"]

    # And the operator still gets the whole thing.
    failed = [e for e in logs if e.get("event") == "query_failed"]
    assert failed, "the fault must still be recorded"
    assert "g_secret_project" in failed[-1]["detail"]


# --- 2. the orgs dprox serves must not reach a refused caller -----------------


def test_a_refused_org_is_not_told_which_orgs_exist(
    two_org_config_one_collection, two_org_plan_cache, mock_ollama
) -> None:
    config, qdrant = two_org_config_one_collection

    with _client(config, two_org_plan_cache, mock_ollama, qdrant, cn="agent_both") as c:
        response = c.post("/v1/query", json={"query": "x"})

    assert response.status_code == 502
    message = response.json()["message"]
    assert message == "search is not configured for your org"
    # What leaked before was the list of orgs dprox serves, introduced by
    # "configured orgs are". The shape of the estate is not a caller's to learn.
    assert "configured orgs" not in message
    assert "arc_documents" not in message


# --- 4. health must follow the plan actually being loaded ---------------------


def test_healthz_goes_degraded_when_the_plan_stops_loading(
    baseline_config_dict: dict, plan_yaml: Path, mock_ollama, mock_qdrant
) -> None:
    """It used to say ok while every query got a 502.

    `_loaded` stayed true from the last success, so the one thing health is
    for — telling you dprox cannot answer — was the one thing it did not say.
    """
    config = Config.model_validate(baseline_config_dict)
    plan_cache = PlanCache(config.plan)
    plan_cache.initial_load()

    with _client(config, plan_cache, mock_ollama, mock_qdrant) as c:
        assert c.get("/healthz").json()["status"] == "ok"

        plan_yaml.write_text("this: [is not: a plan", encoding="utf-8")

        # A query notices first, because it reads the plan.
        assert c.post("/v1/query", json={"query": "x"}).status_code == 502

        health = c.get("/healthz")
        assert health.status_code == 503
        body = health.json()
        assert body["status"] == "degraded"
        assert body["checks"]["plan"]["loaded"] is False
        assert body["checks"]["plan"]["error"], "degraded says why"


def test_a_failed_reload_drops_the_old_entries(
    baseline_config_dict: dict, plan_yaml: Path
) -> None:
    """Fail closed as well as visibly: no stale answer survives the failure."""
    config = Config.model_validate(baseline_config_dict)
    cache = PlanCache(config.plan)
    cache.initial_load()
    assert cache.lookup("agent_alice") is not None

    plan_yaml.write_text("this: [is not: a plan", encoding="utf-8")

    from dprox.plan import PlanError

    with pytest.raises(PlanError):
        cache.lookup("agent_alice")
    assert cache.loaded is False
    with pytest.raises(PlanError):
        cache.lookup("agent_alice")


# --- 5. the body limit must stop reading, not measure afterwards --------------


async def test_an_oversize_body_stops_being_read_at_the_limit() -> None:
    """The limit is what bounds an unauthenticated peer.

    `client_cert_mode: optional` means a peer with no certificate completes the
    handshake, and this runs before the route, so it runs before identity
    exists: anyone who can reach the port can send a body. The old code called
    `await request.body()` and measured afterwards, so a chunked multi-gigabyte
    body was in memory before the check (arch's review, hub #16893).

    Driven at the ASGI layer on purpose. Through a test client the transport
    collects the body before the app sees it, so how much dprox itself read
    would be invisible — which is the only thing this test is about.
    """
    from dprox.bodylimit import BodyLimitMiddleware

    pulled = 0

    async def receive() -> dict:
        nonlocal pulled
        pulled += 1
        return {"type": "http.request", "body": b"x" * 512, "more_body": True}

    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    async def never(scope, receive, send) -> None:  # pragma: no cover
        raise AssertionError("the application must not be reached")

    rejected: list[tuple[int, int]] = []
    middleware = BodyLimitMiddleware(
        never, limit=1024, on_reject=lambda _scope, size, limit: rejected.append((size, limit))
    )

    await middleware(
        {"type": "http", "path": "/v1/query", "headers": [], "client": ("10.0.0.1", 9000)},
        receive,
        send,
    )

    # 1024 / 512 = two chunks to reach the limit, one more to pass it.
    assert pulled == 3, f"pulled {pulled} chunks; it must stop at the limit"
    assert sent[0]["status"] == 413
    assert rejected == [(1536, 1024)]


async def test_a_disconnect_while_reading_is_not_a_413() -> None:
    """A client that goes away has not sent an oversize body."""
    from dprox.bodylimit import BodyLimitMiddleware

    async def receive() -> dict:
        return {"type": "http.disconnect"}

    sent: list[dict] = []

    async def send(message: dict) -> None:  # pragma: no cover
        sent.append(message)

    async def never(scope, receive, send) -> None:  # pragma: no cover
        raise AssertionError("the application must not be reached")

    middleware = BodyLimitMiddleware(never, limit=16, on_reject=lambda *_a: None)
    await middleware_call(middleware, receive, send)

    assert sent == []


async def middleware_call(middleware, receive, send) -> None:
    await middleware(
        {"type": "http", "path": "/v1/query", "headers": [], "client": None}, receive, send
    )


def test_a_declared_oversize_length_is_refused_before_any_body(
    baseline_config_dict: dict, plan_cache, mock_ollama, mock_qdrant
) -> None:
    baseline_config_dict["server"]["max_body_bytes"] = 1024
    config = Config.model_validate(baseline_config_dict)

    with _client(config, plan_cache, mock_ollama, mock_qdrant) as c:
        response = c.post("/v1/query", content=b"x" * 2048)

    assert response.status_code == 413


def test_a_body_under_the_limit_still_reaches_the_handler(
    baseline_config: Config, plan_cache, mock_ollama, mock_qdrant
) -> None:
    """The middleware replays what it read, or every request would break."""
    with _client(baseline_config, plan_cache, mock_ollama, mock_qdrant) as c:
        response = c.post("/v1/query", json={"query": "a real question"})

    assert response.status_code == 200
    assert response.json()["metadata"]["agent"] == "agent_alice"


def test_healthz_still_answers_with_no_body_at_all(
    baseline_config: Config, plan_cache, mock_ollama, mock_qdrant
) -> None:
    """A GET sends one empty body message; the middleware must not hang on it."""
    with _client(baseline_config, plan_cache, mock_ollama, mock_qdrant) as c:
        assert c.get("/healthz").status_code == 200
        assert c.get("/version").status_code == 200


# --- the prefix survives the round trip the estate will actually do ----------


ARC_PREFIX = (
    "Instruct: Given a web search query, retrieve relevant passages that "
    "answer the query\nQuery: "
)
"""The value chosen for ARC on 2026-11-23 (operator, via arch hub #16906):
an instruction line, a newline, then `Query: ` with a trailing space."""


@pytest.mark.parametrize(
    ("form", "yaml_text"),
    [
        (
            "double-quoted with \\n",
            'embedding:\n'
            '  query_prefix: "Instruct: Given a web search query, retrieve '
            'relevant passages that answer the query\\nQuery: "\n',
        ),
        (
            "literal block",
            "embedding:\n"
            "  query_prefix: |-\n"
            "    Instruct: Given a web search query, retrieve relevant "
            "passages that answer the query\n"
            "    Query: \n",
        ),
    ],
)
def test_the_arc_prefix_survives_yaml(form: str, yaml_text: str) -> None:
    """Both forms parse to the same string, trailing space and all.

    Worth a test because the value is two lines ending in a space, and a
    prefix that loses its last character does not fail — it lowers every
    score. The example config recommends the quoted form anyway: in a block
    scalar that space is at the end of a line, where stripping trailing
    whitespace on save deletes it silently.
    """
    import yaml

    assert yaml.safe_load(yaml_text)["embedding"]["query_prefix"] == ARC_PREFIX


def test_the_prefix_is_not_stripped_by_the_config_model(
    baseline_config_dict: dict,
) -> None:
    """`_Strict` strips whitespace on every other string; not this one."""
    baseline_config_dict["embedding"]["query_prefix"] = ARC_PREFIX
    baseline_config_dict["embedding"]["model"] = "qwen3-embedding:4b"
    baseline_config_dict["embedding"]["vector_dim"] = 2560

    config = Config.model_validate(baseline_config_dict)

    assert config.embedding.query_prefix == ARC_PREFIX
    assert config.embedding.query_prefix.endswith("Query: "), "the space is kept"
    assert config.embedding.vector_dim == 2560


async def test_what_dprox_sends_to_ollama_with_the_arc_prefix(
    baseline_config_dict: dict,
) -> None:
    """End to end on the one thing that matters: the body of the embed call."""
    import httpx

    from dprox.ollama import OllamaClient

    baseline_config_dict["embedding"]["query_prefix"] = ARC_PREFIX
    baseline_config_dict["embedding"]["vector_dim"] = 4
    config = Config.model_validate(baseline_config_dict)

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"embedding": [0.1] * 4})

    async with OllamaClient(
        config.embedding, transport=httpx.MockTransport(handler)
    ) as client:
        await client.embed("what is the wage policy")

    assert seen["body"]["prompt"] == ARC_PREFIX + "what is the wage policy"
    assert "\nQuery: what is the wage policy" in seen["body"]["prompt"]
