"""Qdrant client: RBAC-filtered vector search + health check.

Mirrors `dprox-design-spec-v0.2.md` §3.2 (filter is non-negotiable),
§3.4 (critical invariant), §4.2 (result shape), §4.4 (/healthz upstream
check), §9.1 (qdrant_ms in audit log).

`build_classification_filter` is the single point in the codebase that
constructs the Qdrant filter for /v1/query. Per spec §10.3, this code
path requires 100% test coverage — it is the security boundary.

Since v0.3.0 one dprox answers every org, and the boundary is two checks
rather than one (search-one-service design, accepted 2026-11-13):
``targets_for`` picks the collections of the caller's orgs, and
``build_classification_filter`` keeps only passages of the caller's groups
inside each. They are independent, so one being wrong does not open the
other.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from qdrant_client import AsyncQdrantClient
from qdrant_client.http.exceptions import (
    ResponseHandlingException,
    UnexpectedResponse,
)
from qdrant_client.models import FieldCondition, Filter, MatchAny

from dprox.config import QdrantConfig

CLASSIFICATION_GROUP_KEY = "classification_group"


class QdrantError(Exception):
    """Base for Qdrant-related failures."""


class QdrantUnavailable(QdrantError):
    """Maps to HTTP 502 upstream_unavailable."""


class QdrantTimeout(QdrantError):
    """Maps to HTTP 504 upstream_timeout."""


class QdrantOrgNotConfigured(QdrantError):
    """A caller's org has no collection in this dprox's config.

    Maps to HTTP 502. It is a deployment fault, not a caller fault: the plan
    grants the caller groups in an org this dprox cannot reach. The only
    alternatives — search the orgs that are configured, or return nothing —
    both answer the question wrongly without saying so.
    """


CONVERSATION_FILE_TYPE = "conversation"
"""``file_type`` on a passage that is a turn of an agent's conversation —
the one field that tells the two kinds of passage apart
(qdrant-collection-contract 0.4)."""


class MalformedPassage(QdrantUnavailable):
    """One passage dprox cannot read: no text, no group, or unattributable.

    A subclass of QdrantUnavailable so that one escaping anywhere outside the
    search loop is still a 502 rather than a 500. Inside the loop it is caught
    per passage: the passage is dropped and counted, because dropping is
    fail-closed — nothing is shown whose group could not be read — and one bad
    point in a hundred thousand must not make a caller's search fail on some
    questions and work on others.
    """


@dataclass(frozen=True)
class ConversationTurn:
    """Who said this, in which conversation (contract 0.4).

    The contract chunks a conversation by turn and never across two "so a
    result is attributable". That only holds if the attribution reaches the
    caller, so these are projected rather than dropped.

    ``agent``, ``conversation_id``, ``turn_id`` and ``role`` are required on a
    conversation passage for the same reason ``text`` is: a turn attributed to
    nobody is worse than no turn. ``runtime`` and ``turn_time`` are not —
    the contract states ``unknown`` and ``""`` as real values the writer uses
    when the source did not say.
    """

    agent: str
    conversation_id: str
    turn_id: str
    role: str
    runtime: str | None = None
    turn_time: str | None = None
    sidechain: bool | None = None


@dataclass(frozen=True)
class QdrantHit:
    """One Qdrant search result projected to dprox response shape (spec §4.2).

    ``org`` is the org whose collection the hit came from. It is dprox's to
    state, not the payload's: with one dprox answering several orgs a merged
    result list is otherwise unreadable, because ``source_path_rel`` is
    relative to an org's own root and two orgs can hold the same path.
    """

    text: str
    classification_group: str
    score: float
    org: str
    source_path_rel: str | None = None
    file_type: str | None = None
    chunk_index: int | None = None
    chunk_total: int | None = None
    modified_at: str | None = None
    indexed_at: str | None = None
    conversation: ConversationTurn | None = None
    """Set when ``file_type`` is ``conversation``, null otherwise."""


@dataclass(frozen=True)
class SearchResult:
    """What one search produced, including what it could not read.

    ``dropped`` carries one line for each passage thrown away, so the fault is
    readable in the audit log and the count reaches the caller. A short answer
    that reads like a complete one is the thing being avoided.
    """

    hits: list[QdrantHit]
    elapsed_ms: int
    collections: list[str]
    dropped: list[tuple[str, str]]
    """(collection, why) for each passage thrown away. Paired with its
    collection so the warning can be one line per collection rather than one
    per passage: a collection rebuilt wrongly would otherwise write `limit`
    identical warnings for every query (arch's review, hub #16893)."""


REQUIRED_DISTANCE = "cosine"
"""The metric ingstr's collection contract fixes, and the only one dprox may
search with. A collection built with another is written happily by ingstr and
searched wrongly by dprox, with no error anywhere — the contract says ingstr
cannot check this, so dprox does."""


class EmptyGroupSet(QdrantError):
    """build_classification_filter was given no groups.

    Reaching it is a bug upstream — a caller with no groups has no orgs, so
    the search returns before any collection is opened. It raises rather than
    building a filter, because "a filter that matches nothing" would mean
    trusting Qdrant's reading of ``MatchAny(any=[])``, and the cost of being
    wrong about that once is every passage in the collection.
    """


def build_classification_filter(groups: frozenset[str]) -> Filter:
    """Build the RBAC filter for a Qdrant search.

    THE SECURITY BOUNDARY. Caller must pass groups derived only from the
    verified mTLS peer cert's CN, resolved through compiled_plan.yml.
    There must be no code path where a caller-supplied parameter
    contributes to this filter (spec §3.4 critical invariant).

    Sorting the group list makes audit log diffs deterministic.
    """
    if not groups:
        raise EmptyGroupSet(
            "refusing to build a classification filter with no groups"
        )
    return Filter(
        must=[
            FieldCondition(
                key=CLASSIFICATION_GROUP_KEY,
                match=MatchAny(any=sorted(groups)),
            )
        ]
    )


class QdrantClient:
    """Async Qdrant client with health probe + RBAC-filtered search.

    Wraps qdrant-client's AsyncQdrantClient. Caller is responsible for
    closing — use `async with` or call `aclose()`. In production,
    FastAPI lifespan manages this.
    """

    def __init__(
        self,
        config: QdrantConfig,
        expected_vector_dim: int,
        *,
        backend: Any | None = None,
    ) -> None:
        """
        Args:
            config: QdrantConfig (url, api_key_env, collection, timeouts).
            expected_vector_dim: dim from EmbeddingConfig — health check
                verifies the collection's vector size matches.
            backend: optional pre-built async client (test injection).
                When None, a real AsyncQdrantClient is constructed using
                the API key from env var named by `config.api_key_env`.
        """
        self._config = config
        self._expected_dim = expected_vector_dim
        if backend is not None:
            self._client: Any = backend
        else:
            api_key = os.environ.get(config.api_key_env) or None
            self._client = AsyncQdrantClient(
                url=config.url,
                api_key=api_key,
                timeout=config.timeout_seconds,
            )

    @property
    def url(self) -> str:
        return self._config.url

    @property
    def collections(self) -> dict[str, str]:
        """org name -> collection name, as the estate declared it."""
        return dict(self._config.collections)

    def targets_for(self, orgs: Iterable[str]) -> list[tuple[str, str]]:
        """CHECK ONE OF TWO: which collections this caller may be searched in.

        Returns (org, collection) pairs, sorted by org so an audit line and a
        merged result list are deterministic. The group filter is the second
        check and applies inside each collection; this one decides which
        collections are opened at all.

        An org the caller holds that this dprox has no collection for is
        refused, never skipped. Skipping it would return a short answer that
        reads exactly like a complete one.
        """
        missing = sorted(o for o in orgs if o not in self._config.collections)
        if missing:
            # The message names only the caller's own orgs. Listing the orgs
            # dprox serves would tell a refused caller the shape of the estate
            # (arch's review, hub #16893); the operator reads that from the
            # audit log and from `dprox health`, not from a 502 body.
            raise QdrantOrgNotConfigured(
                f"no collection is configured for org(s): {', '.join(missing)}"
            )
        return sorted((o, self._config.collections[o]) for o in orgs)

    async def aclose(self) -> None:
        close = getattr(self._client, "close", None)
        if close is None:
            return
        result = close()
        if asyncio.iscoroutine(result):
            await result

    async def __aenter__(self) -> QdrantClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def search(
        self,
        query_vector: list[float],
        groups_by_org: Mapping[str, frozenset[str]],
        limit: int,
    ) -> SearchResult:
        """Run the RBAC-filtered search.

        ``groups_by_org`` is the caller's groups split by the org each belongs
        to, from the plan. One argument rather than two because an org and the
        groups searched inside it must come from the same place: passing them
        separately made it possible — and it happened — to send every group to
        every collection.

        **The two checks, and why they are independent.** The keys choose the
        collections. Inside each collection the filter carries **only that
        org's groups**, not all of the caller's. So a passage written into the
        wrong org's collection is not returned even when the caller holds its
        group, where before it came back labelled with the wrong org. Then
        every hit is checked again against the caller's whole group set, which
        catches a Qdrant filter that did not do what it was asked. Three
        chances to be right, each able to survive the others being wrong
        (arch's review, hub #16893).

        Each collection is asked for ``limit`` results and the merged list is
        cut back to ``limit``, which gives the same answer as one collection
        would: a passage outside a collection's own top ``limit`` cannot be in
        the top ``limit`` of the union. Scores are comparable across
        collections only because every collection is cosine with the same
        model, which ``check_health`` is what proves.

        One collection failing fails the whole search. A partial answer is
        indistinguishable from a complete one at the caller.

        A single malformed passage does NOT fail the search: it is dropped and
        counted, and the count is returned so the caller is told the answer was
        shortened. Dropping is fail-closed — a passage whose group cannot be
        read is never shown — and one bad point out of a hundred thousand
        should not make a caller's search break on some questions and work on
        others.

        Raises QdrantTimeout on timeout, QdrantOrgNotConfigured when an org
        has no collection, QdrantUnavailable on every other failure.
        """
        targets = self.targets_for(groups_by_org)
        collections = [c for _, c in targets]
        if not targets:
            # No orgs means no groups. Nothing is searched, nothing is built,
            # and the audit line says so.
            return SearchResult(hits=[], elapsed_ms=0, collections=[], dropped=[])

        start = time.monotonic()
        results = await asyncio.gather(
            *(
                self._search_one(
                    collection,
                    query_vector,
                    build_classification_filter(groups_by_org[org]),
                    limit,
                )
                for org, collection in targets
            ),
            return_exceptions=True,
        )
        elapsed_ms = int((time.monotonic() - start) * 1000)

        hits: list[QdrantHit] = []
        dropped: list[tuple[str, str]] = []
        for (org, collection), result in zip(targets, results, strict=True):
            if isinstance(result, BaseException):
                raise result
            for point in result:
                try:
                    hit = self._point_to_hit(point, org)
                except MalformedPassage as exc:
                    dropped.append((collection, str(exc)))
                    continue
                # The second check again, on what came back, and against THIS
                # org's groups rather than all the caller's. Arch asked for the
                # caller's whole set; per collection is strictly stronger and
                # catches one more fault: a passage of org B sitting in org A's
                # collection, whose group the caller does happen to hold. The
                # weaker check would pass it and dprox would return it saying
                # org A. Reaching here at all means Qdrant returned a passage
                # its filter did not ask for.
                if hit.classification_group not in groups_by_org[org]:
                    dropped.append(
                        (
                            collection,
                            f"point with group {hit.classification_group!r}, "
                            f"which is not a group of org {org!r} that was "
                            f"asked for — the filter and the index disagree",
                        )
                    )
                    continue
                hits.append(hit)

        # Highest score first; org, path and passage number break ties so two
        # runs of the same query return the same order.
        hits.sort(
            key=lambda h: (
                -h.score,
                h.org,
                h.source_path_rel or "",
                h.chunk_index if h.chunk_index is not None else 0,
            )
        )
        return SearchResult(
            hits=hits[:limit],
            elapsed_ms=elapsed_ms,
            collections=collections,
            dropped=dropped,
        )

    async def _search_one(
        self,
        collection: str,
        query_vector: list[float],
        q_filter: Filter,
        limit: int,
    ) -> list[Any]:
        """Query one collection and return its raw points."""
        try:
            # qdrant-client >=1.10 exposes the universal query API. Server
            # 1.13.x supports it; AsyncQdrantClient.search() was removed by
            # 1.18. Response is QueryResponse with .points: list[ScoredPoint]
            # (the ScoredPoint shape — id/score/payload — is unchanged from
            # the old search() return type, so _point_to_hit is the same).
            response = await self._client.query_points(
                collection_name=collection,
                query=query_vector,
                query_filter=q_filter,
                limit=limit,
                with_payload=True,
            )
        except TimeoutError as exc:
            raise QdrantTimeout(
                f"qdrant search of {collection!r} timed out after "
                f"{self._config.timeout_seconds}s"
            ) from exc
        except UnexpectedResponse as exc:
            raise QdrantUnavailable(
                f"qdrant returned status {exc.status_code} for {collection!r}"
            ) from exc
        except ResponseHandlingException as exc:
            raise QdrantUnavailable(
                f"qdrant response error for {collection!r}: {exc}"
            ) from exc
        except QdrantError:
            raise
        except Exception as exc:
            raise QdrantUnavailable(
                f"qdrant unreachable for {collection!r}: "
                f"{exc.__class__.__name__}: {exc}"
            ) from exc

        points = getattr(response, "points", None)
        if points is None:
            raise QdrantUnavailable(
                f"qdrant query_points response for {collection!r} "
                "missing 'points' field"
            )
        return list(points)

    @staticmethod
    def _point_to_hit(point: Any, org: str) -> QdrantHit:
        payload = getattr(point, "payload", None) or {}
        text = payload.get("text")
        group = payload.get("classification_group")
        if not isinstance(text, str) or not text:
            raise MalformedPassage(
                f"point id={getattr(point, 'id', '?')!r} missing 'text' payload"
            )
        if not isinstance(group, str) or not group:
            point_id = getattr(point, "id", "?")
            raise MalformedPassage(
                f"point id={point_id!r} missing 'classification_group' payload"
            )
        file_type = payload.get("file_type")
        return QdrantHit(
            text=text,
            classification_group=group,
            score=float(getattr(point, "score", 0.0)),
            org=org,
            source_path_rel=payload.get("source_path_rel"),
            file_type=file_type,
            chunk_index=payload.get("chunk_index"),
            chunk_total=payload.get("chunk_total"),
            modified_at=payload.get("modified_at"),
            indexed_at=payload.get("indexed_at"),
            conversation=(
                QdrantClient._point_to_turn(point, payload)
                if file_type == CONVERSATION_FILE_TYPE
                else None
            ),
        )

    @staticmethod
    def _point_to_turn(point: Any, payload: dict) -> ConversationTurn:
        """Project a conversation passage's attribution (contract 0.4).

        The four required fields are treated as ``text`` is: absent means the
        passage cannot be attributed, and handing the caller a turn with a null
        speaker would be thinner-looking rather than visibly wrong.
        """
        missing = [
            key
            for key in ("agent", "conversation_id", "turn_id", "role")
            if not isinstance(payload.get(key), str) or not payload[key]
        ]
        if missing:
            point_id = getattr(point, "id", "?")
            raise MalformedPassage(
                f"point id={point_id!r} is a conversation passage missing "
                f"{', '.join(missing)} — contract 0.4 requires it"
            )
        sidechain = payload.get("sidechain")
        return ConversationTurn(
            agent=payload["agent"],
            conversation_id=payload["conversation_id"],
            turn_id=payload["turn_id"],
            role=payload["role"],
            runtime=payload.get("runtime"),
            turn_time=payload.get("turn_time"),
            sidechain=sidechain if isinstance(sidechain, bool) else None,
        )

    async def check_health(self) -> dict[str, Any]:
        """Return structured health status for EVERY configured collection.

        Does not raise. Shape:
            {
                "url": str,
                "orgs": [str, ...],
                "collections": {
                    "<org>": {
                        "collection": str, "reachable": bool,
                        "collection_exists": bool,
                        "vector_dim": int | None, "vector_dim_matches": bool,
                        "distance": str | None, "distance_matches": bool,
                        "error": str | None,
                    }, ...
                },
                "reachable": bool, "collections_exist": bool,
                "vector_dims_match": bool, "distances_match": bool,
                "error": str | None,
            }

        Every collection is checked, not the first: one org's collection
        rebuilt with the wrong metric or the wrong model is exactly the fault
        that is otherwise invisible, and checking one would clear all of them.
        The aggregates are what ``/healthz`` gates on; the per-org entries say
        which one is wrong.
        """
        per_org: dict[str, dict[str, Any]] = {}
        for org in sorted(self._config.collections):
            per_org[org] = await self._check_one(self._config.collections[org])

        errors = [
            f"{org}: {s['error']}" for org, s in per_org.items() if s["error"]
        ]
        return {
            "url": self._config.url,
            "orgs": sorted(per_org),
            "collections": per_org,
            "reachable": all(s["reachable"] for s in per_org.values()),
            "collections_exist": all(
                s["collection_exists"] for s in per_org.values()
            ),
            "vector_dims_match": all(
                s["vector_dim_matches"] for s in per_org.values()
            ),
            "distances_match": all(
                s["distance_matches"] for s in per_org.values()
            ),
            "error": "; ".join(errors) if errors else None,
        }

    async def _check_one(self, collection: str) -> dict[str, Any]:
        """Health of one collection. Does not raise."""
        try:
            info = await self._client.get_collection(collection)
        except UnexpectedResponse as exc:
            if exc.status_code == 404:
                return self._status(
                    collection,
                    reachable=True,
                    collection_exists=False,
                    error="collection not found",
                )
            return self._status(
                collection, reachable=False, error=f"status {exc.status_code}"
            )
        except TimeoutError:
            return self._status(collection, reachable=False, error="timeout")
        except Exception as exc:
            return self._status(
                collection,
                reachable=False,
                error=f"{exc.__class__.__name__}: {exc}",
            )

        vector_dim = self._extract_vector_dim(info)
        distance = self._extract_distance(info)
        return self._status(
            collection,
            reachable=True,
            collection_exists=True,
            vector_dim=vector_dim,
            vector_dim_matches=vector_dim == self._expected_dim,
            distance=distance,
            distance_matches=distance == REQUIRED_DISTANCE,
        )

    @staticmethod
    def _extract_vector_dim(info: Any) -> int | None:
        """Pull the vector size out of CollectionInfo, tolerating named-vector configs."""
        try:
            vectors = info.config.params.vectors
        except AttributeError:
            return None
        size = getattr(vectors, "size", None)
        if isinstance(size, int):
            return size
        if isinstance(vectors, dict) and vectors:
            first = next(iter(vectors.values()))
            inner_size = getattr(first, "size", None)
            return inner_size if isinstance(inner_size, int) else None
        return None

    @staticmethod
    def _extract_distance(info: Any) -> str | None:
        """Pull the distance metric out of CollectionInfo, as a lowercase name.

        Same shapes as the vector size: a plain config, or named vectors.
        Qdrant reports an enum whose str() is like ``Distance.COSINE``; the
        trailing name is what is compared.
        """
        try:
            vectors = info.config.params.vectors
        except AttributeError:
            return None
        distance = getattr(vectors, "distance", None)
        if distance is None and isinstance(vectors, dict) and vectors:
            distance = getattr(next(iter(vectors.values())), "distance", None)
        if distance is None:
            return None
        name = getattr(distance, "name", None) or str(distance).rsplit(".", 1)[-1]
        return str(name).lower()

    def _status(
        self,
        collection: str,
        *,
        reachable: bool,
        collection_exists: bool = False,
        vector_dim: int | None = None,
        vector_dim_matches: bool = False,
        distance: str | None = None,
        distance_matches: bool = False,
        error: str | None = None,
    ) -> dict[str, Any]:
        return {
            "collection": collection,
            "reachable": reachable,
            "collection_exists": collection_exists,
            "vector_dim": vector_dim,
            "vector_dim_matches": vector_dim_matches,
            "distance": distance,
            "distance_matches": distance_matches,
            "error": error,
        }
