"""Refuse an oversize request body while reading it, never after.

Pure ASGI rather than a FastAPI middleware, because this one owns ``receive``:
it has to stop pulling bytes the moment the limit is passed, and then hand the
bytes it did read to the application. A FastAPI ``@app.middleware("http")``
cannot do the first half — ``await request.body()`` reads to the end, so the
check happened after the whole body was already in memory, which is the
opposite of what its own docstring claimed (arch's review, hub #16893).

Two things make this worth owning. The listener runs with
``client_cert_mode: optional``, so a peer with no certificate completes the
handshake and reaches here; and this runs before the route, so it runs before
identity exists. Anyone who can reach the port can send a body. The limit is
what bounds that, and it is the reason the limit has no default.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


class BodyLimitMiddleware:
    """Stop reading at ``max_body_bytes`` and answer 413.

    Memory is bounded by the limit plus one chunk: the body is accumulated
    only while it is still under the limit, and the moment it is not, reading
    stops and the rest is never pulled.
    """

    def __init__(self, app, limit: int, on_reject) -> None:
        self.app = app
        self.limit = limit
        self.on_reject = on_reject

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit = self.limit

        # A declared Content-Length is a fast reject, and a claim: it can be
        # absent (chunked) or wrong, so it is never trusted on its own.
        declared = _declared_length(scope)
        if declared is not None and declared > limit:
            await self._refuse(scope, send, declared, limit)
            return

        chunks: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > limit:
                await self._refuse(scope, send, total, limit)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break

        body = b"".join(chunks)
        replayed = False

        async def replay() -> dict[str, Any]:
            nonlocal replayed
            if replayed:
                return await receive()
            replayed = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay, send)

    async def _refuse(self, scope: Scope, send: Send, size: int, limit: int) -> None:
        self.on_reject(scope, size, limit)
        payload = json.dumps(
            {
                "error": "request_too_large",
                "message": f"request body is {size} bytes; the limit is {limit}",
            }
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})


def _declared_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None
