"""Conversation passages: the attribution reaches the caller.

`qdrant-collection-contract` 0.4 (2026-11-19) lets a collection hold turns of
an agent's conversation. It is additive — dprox does not break on one — but
the contract chunks by turn and never across two *so that a result is
attributable*, and that only holds if the seven fields reach the caller.
Before this, dprox read a conversation passage without error and dropped
every one of them.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dprox.qdrant import CONVERSATION_FILE_TYPE, QdrantClient, QdrantUnavailable
from dprox.server import _hit_to_dict

TURN = {
    "text": "we agreed to rebuild the seat",
    "classification_group": "arc_g1_eng",
    "source_path_rel": "agents/claude/sessions/48ed.jsonl",
    "source_path": "/mnt/data/agents/claude/sessions/48ed.jsonl",
    "file_type": CONVERSATION_FILE_TYPE,
    "chunk_index": 0,
    "chunk_total": 1,
    "indexed_at": "2026-11-19T09:00:00Z",
    "agent": "agent_oversight",
    "runtime": "claude",
    "conversation_id": "48ed22d9",
    "turn_id": "t-14",
    "role": "person",
    "turn_time": "2026-11-18T11:00:00Z",
    "sidechain": False,
}

DOCUMENT = {
    "text": "the wage policy is this",
    "classification_group": "arc_g1_eng",
    "source_path_rel": "hr/wages.docx",
    "file_type": "docx",
    "chunk_index": 1,
    "chunk_total": 4,
    "modified_at": "2026-04-28T08:00:00Z",
    "indexed_at": "2026-04-28T08:15:00Z",
    "content_hash": "abc123",
}


def _point(payload: dict):
    return SimpleNamespace(id="p1", score=0.77, payload=payload)


def test_a_conversation_turn_keeps_its_attribution() -> None:
    hit = QdrantClient._point_to_hit(_point(TURN), "arc")

    assert hit.file_type == CONVERSATION_FILE_TYPE
    assert hit.conversation is not None
    assert hit.conversation.agent == "agent_oversight"
    assert hit.conversation.runtime == "claude"
    assert hit.conversation.conversation_id == "48ed22d9"
    assert hit.conversation.turn_id == "t-14"
    assert hit.conversation.role == "person"
    assert hit.conversation.turn_time == "2026-11-18T11:00:00Z"
    assert hit.conversation.sidechain is False


def test_the_response_carries_every_conversation_field() -> None:
    """Nothing the contract states about a turn is dropped at the boundary."""
    out = _hit_to_dict(QdrantClient._point_to_hit(_point(TURN), "arc"))

    assert set(out["conversation"]) == {
        "agent",
        "runtime",
        "conversation_id",
        "turn_id",
        "role",
        "turn_time",
        "sidechain",
    }
    assert out["conversation"]["role"] == "person"


def test_a_document_passage_has_a_null_conversation() -> None:
    """One shape, not two: the key is always there and null for a document."""
    out = _hit_to_dict(QdrantClient._point_to_hit(_point(DOCUMENT), "arc"))

    assert out["conversation"] is None
    assert out["file_type"] == "docx"


def test_a_conversation_has_no_modified_at_and_that_is_not_an_error() -> None:
    """The contract says a conversation grows while it is read, so it has none."""
    hit = QdrantClient._point_to_hit(_point(TURN), "arc")
    assert hit.modified_at is None


@pytest.mark.parametrize("key", ["agent", "conversation_id", "turn_id", "role"])
def test_an_unattributable_turn_is_refused(key: str) -> None:
    """A turn attributed to nobody is worse than no turn, so it raises.

    The same treatment as a passage with no `text`: returning it would look
    thinner rather than wrong, and nobody would see it.
    """
    payload = dict(TURN)
    del payload[key]

    with pytest.raises(QdrantUnavailable, match=key):
        QdrantClient._point_to_hit(_point(payload), "arc")


@pytest.mark.parametrize(("key", "value"), [("runtime", "unknown"), ("turn_time", "")])
def test_the_values_the_contract_uses_for_unknown_are_accepted(
    key: str, value: str
) -> None:
    """`unknown` and `""` are real values the writer uses, not absences."""
    payload = dict(TURN, **{key: value})
    hit = QdrantClient._point_to_hit(_point(payload), "arc")

    assert getattr(hit.conversation, key) == value


def test_a_sidechain_turn_is_marked() -> None:
    hit = QdrantClient._point_to_hit(_point(dict(TURN, sidechain=True)), "arc")
    assert hit.conversation.sidechain is True


# --- a dropped passage, end to end --------------------------------------------


def test_one_bad_passage_does_not_fail_the_search(
    baseline_config, plan_cache, mock_ollama, mock_qdrant_backend
) -> None:
    """One unreadable point out of a hundred thousand must not break a caller.

    Arch suggested the choice (hub #16875); this is the behaviour chosen, and
    it applies to a missing `text` and an unattributable turn alike.
    """
    import structlog
    from fastapi.testclient import TestClient

    from dprox.mtls import require_mtls
    from dprox.qdrant import QdrantClient
    from dprox.server import create_app

    bad_turn = dict(TURN)
    del bad_turn["role"]
    mock_qdrant_backend.query_points.return_value = SimpleNamespace(
        points=[_point(bad_turn), _point(DOCUMENT)]
    )
    qdrant = QdrantClient(
        baseline_config.qdrant,
        baseline_config.embedding.vector_dim,
        backend=mock_qdrant_backend,
    )
    app = create_app(baseline_config, plan_cache, ollama=mock_ollama, qdrant=qdrant)
    app.dependency_overrides[require_mtls] = lambda: "agent_alice"

    with structlog.testing.capture_logs() as logs:
        with TestClient(app) as c:
            response = c.post("/v1/query", json={"query": "x"})

    assert response.status_code == 200, "the search still answers"
    body = response.json()
    assert len(body["results"]) == 1
    assert body["results"][0]["file_type"] == "docx"

    # The caller is told the answer was shortened, so it does not read complete.
    assert body["metadata"]["dropped_count"] == 1

    # And the fault is named in the audit log, once, at warning level.
    dropped = [e for e in logs if e.get("event") == "passage_dropped"]
    assert len(dropped) == 1
    assert dropped[0]["log_level"] == "warning"
    assert "role" in dropped[0]["reason"]
    assert "documents" in dropped[0]["reason"]

    query_events = [e for e in logs if e.get("event") == "query"]
    assert query_events[-1]["dropped_count"] == 1


def test_a_healthy_query_reports_nothing_dropped(
    baseline_config, plan_cache, mock_ollama, mock_qdrant
) -> None:
    """Zero when nothing is wrong is what makes a non-zero worth reading."""
    import structlog
    from fastapi.testclient import TestClient

    from dprox.mtls import require_mtls
    from dprox.server import create_app

    app = create_app(baseline_config, plan_cache, ollama=mock_ollama, qdrant=mock_qdrant)
    app.dependency_overrides[require_mtls] = lambda: "agent_alice"

    with structlog.testing.capture_logs() as logs:
        with TestClient(app) as c:
            body = c.post("/v1/query", json={"query": "x"}).json()

    assert body["metadata"]["dropped_count"] == 0
    assert [e for e in logs if e.get("event") == "passage_dropped"] == []
