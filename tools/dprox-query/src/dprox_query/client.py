"""Talking to dprox over mTLS: the version gate, then the question.

Nothing here decides what the caller may see. dprox does that, from the
certificate presented in the handshake. This tool cannot ask for a group, an
org or another agent's results; there is no parameter for it, which is the
point of the server refusing unknown request fields.
"""

from __future__ import annotations

import ssl
from dataclasses import dataclass
from typing import Any

import httpx

from dprox_query.config import QueryConfig
from dprox_query.version import UnreadableVersion, describe_support, supports


class QueryError(Exception):
    """Anything that stopped the question being answered. Exit code 1."""


class IncompatibleServer(QueryError):
    """The dprox answering is outside the range this tool reads. Exit code 3."""


@dataclass(frozen=True)
class Answer:
    results: list[dict[str, Any]]
    metadata: dict[str, Any]


def build_ssl_context(config: QueryConfig) -> ssl.SSLContext:
    """The TLS context: trust the estate's CA, present the caller's cert.

    Built here rather than passed to httpx as `verify=<path>` and
    `cert=(...)`, which httpx 0.28 deprecates. Hostname checking and
    certificate verification are on and there is no flag to turn either off:
    the tool checks the server exactly as the server checks the tool.
    """
    context = ssl.create_default_context(cafile=str(config.ca_cert_path))
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_cert_chain(
        certfile=str(config.client_cert_path),
        keyfile=str(config.client_key_path),
    )
    return context


def build_client(config: QueryConfig) -> httpx.Client:
    """An httpx client that presents the caller's certificate."""
    return httpx.Client(
        base_url=config.endpoint,
        verify=build_ssl_context(config),
        timeout=config.timeout_seconds,
    )


def server_version(client: httpx.Client) -> str:
    """Read GET /version. Raises QueryError if it cannot be read."""
    try:
        response = client.get("/version")
    except httpx.HTTPError as exc:
        raise QueryError(f"could not reach dprox: {exc}") from exc
    if response.status_code != 200:
        raise QueryError(f"dprox /version returned {response.status_code}")
    try:
        version = response.json()["version"]
    except (ValueError, KeyError, TypeError) as exc:
        raise QueryError("dprox /version did not return a version") from exc
    return str(version)


def check_compatible(version: str) -> None:
    """Refuse a dprox outside the stated range, and say what to do about it.

    This runs before every question, not only in ``check``. A tool baked into
    an image months ago is exactly the copy nobody is going to run a check on,
    and the failure it would otherwise produce — results with fields missing,
    or an org silently unmentioned — is one the reader cannot see.
    """
    try:
        ok = supports(version)
    except UnreadableVersion as exc:
        raise IncompatibleServer(
            f"dprox reports a version this tool cannot read: {exc}"
        ) from exc
    if not ok:
        raise IncompatibleServer(
            f"this dprox is {version}; dprox-query works with "
            f"{describe_support()}. Install the dprox-query released with "
            f"that dprox — in an image, that is a new image."
        )


def ask(client: httpx.Client, question: str, limit: int | None = None) -> Answer:
    """Send one question and return the passages dprox allowed."""
    payload: dict[str, Any] = {"query": question}
    if limit is not None:
        payload["limit"] = limit

    try:
        response = client.post("/v1/query", json=payload)
    except httpx.HTTPError as exc:
        raise QueryError(f"could not reach dprox: {exc}") from exc

    if response.status_code != 200:
        raise QueryError(_error_text(response))

    try:
        body = response.json()
        return Answer(results=body["results"], metadata=body["metadata"])
    except (ValueError, KeyError, TypeError) as exc:
        raise QueryError("dprox returned a body this tool does not recognise") from exc


def healthz(client: httpx.Client) -> tuple[int, dict[str, Any]]:
    """Read GET /healthz. Returns (status code, body)."""
    try:
        response = client.get("/healthz")
    except httpx.HTTPError as exc:
        raise QueryError(f"could not reach dprox: {exc}") from exc
    try:
        return response.status_code, response.json()
    except ValueError as exc:
        raise QueryError("dprox /healthz did not return JSON") from exc


def _error_text(response: httpx.Response) -> str:
    """dprox's error body is {"error": code, "message": text}. Say both."""
    try:
        body = response.json()
        code = body.get("error", "error")
        message = body.get("message", "")
    except ValueError:
        return f"dprox returned {response.status_code}"
    return f"dprox returned {response.status_code} {code}: {message}".rstrip(": ")
