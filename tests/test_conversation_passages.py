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
