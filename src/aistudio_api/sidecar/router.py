"""Minimal ASGI path router in front of aistudio-api and uncut-bridge."""

from __future__ import annotations

import json
import os

import httpx

BRIDGE_URL = os.environ.get("ROUTER_BRIDGE_URL", "http://127.0.0.1:8765").rstrip("/")
API_URL = os.environ.get("ROUTER_API_URL", "http://127.0.0.1:8080").rstrip("/")
READ_TIMEOUT = max(300.0, float(os.environ.get("ROUTER_READ_TIMEOUT", "620.0")))

_HOP_HEADERS = frozenset(
    {
        b"connection",
        b"keep-alive",
        b"proxy-authenticate",
        b"proxy-authorization",
        b"te",
        b"trailers",
        b"transfer-encoding",
        b"upgrade",
    }
)
_STRIP_REQ_HEADERS = _HOP_HEADERS | frozenset({b"content-length", b"host"})

_state: dict[str, httpx.AsyncClient] = {}


def _make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        http2=False,
        follow_redirects=False,
        timeout=httpx.Timeout(connect=10.0, read=READ_TIMEOUT, write=60.0, pool=10.0),
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
    )


async def _send_json(send, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


async def _read_body(receive) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    while True:
        msg = await receive()
        mtype = msg.get("type")
        if mtype == "http.request":
            part = msg.get("body", b"")
            if part:
                chunks.append(part)
            if not msg.get("more_body", False):
                return b"".join(chunks), False
        elif mtype == "http.disconnect":
            return b"".join(chunks), True


async def app(scope, receive, send) -> None:
    stype = scope["type"]
    if stype == "lifespan":
        while True:
            msg = await receive()
            if msg["type"] == "lifespan.startup":
                _state["client"] = _make_client()
                await send({"type": "lifespan.startup.complete"})
            elif msg["type"] == "lifespan.shutdown":
                client = _state.pop("client", None)
                if client is not None:
                    await client.aclose()
                await send({"type": "lifespan.shutdown.complete"})
                return
        return

    if stype != "http":
        return

    client = _state.get("client")
    if client is None:
        client = _make_client()
        _state["client"] = client

    body, disconnected = await _read_body(receive)
    if disconnected:
        return

    path = scope.get("path") or "/"
    raw_path = scope.get("raw_path")
    if isinstance(raw_path, (bytes, bytearray)) and raw_path:
        req_path = bytes(raw_path).decode("latin-1").split("?", 1)[0]
    else:
        req_path = path

    base = BRIDGE_URL if (path == "/v1" or path.startswith("/v1/")) else API_URL
    qs = (scope.get("query_string") or b"").decode("latin-1")
    target_url = f"{base}{req_path}?{qs}" if qs else f"{base}{req_path}"

    method = scope["method"]
    fwd_headers = [
        (k, v)
        for k, v in (scope.get("headers") or ())
        if k.lower() not in _STRIP_REQ_HEADERS
    ]
    content = body if (body or method in ("POST", "PUT", "PATCH")) else None

    started = False
    try:
        async with client.stream(
            method, target_url, headers=fwd_headers, content=content
        ) as resp:
            resp_headers = [
                (k, v)
                for k, v in resp.headers.raw
                if k.lower() not in _HOP_HEADERS
            ]
            await send(
                {
                    "type": "http.response.start",
                    "status": resp.status_code,
                    "headers": resp_headers,
                }
            )
            started = True
            async for chunk in resp.aiter_raw():
                if chunk:
                    await send(
                        {
                            "type": "http.response.body",
                            "body": chunk,
                            "more_body": True,
                        }
                    )
            await send({"type": "http.response.body", "body": b"", "more_body": False})
    except httpx.ConnectError:
        if not started:
            await _send_json(
                send,
                503,
                {"error": {"message": "upstream unavailable", "type": "service_unavailable"}},
            )
        else:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
    except httpx.TimeoutException:
        if not started:
            await _send_json(
                send,
                504,
                {"error": {"message": "upstream timed out", "type": "timeout"}},
            )
        else:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
    except (httpx.RemoteProtocolError, httpx.ReadError, OSError):
        if not started:
            await _send_json(
                send,
                502,
                {"error": {"message": "upstream error", "type": "bad_gateway"}},
            )
        else:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
