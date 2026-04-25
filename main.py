"""
anthropic-proxy — Transparent reverse proxy for api.anthropic.com.

Forwards ALL requests (any method, any path) to the upstream Anthropic API.
Streaming (SSE / chunked) is fully supported — no response buffering.
API keys are NOT stored here; the client passes them via headers.

Architecture:
    Claude Code (Beget) → anthropic-proxy (Render) → api.anthropic.com

Environment variables:
    ANTHROPIC_API_URL  — upstream target (default: https://api.anthropic.com)
    PORT               — listen port (set automatically by Render)
"""
import asyncio
import logging
import os

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("anthropic_proxy")

TARGET = os.getenv("ANTHROPIC_API_URL", "https://api.anthropic.com").rstrip("/")

app = FastAPI(title="anthropic-proxy", docs_url=None, redoc_url=None)

# ── Health checks ──────────────────────────────────────────────────────────────

@app.get("/")
@app.get("/health")
async def health():
    return {"status": "ok", "target": TARGET, "version": "v1"}


# ── Hop-by-hop headers that MUST NOT be forwarded ─────────────────────────────
# content-encoding is also stripped from upstream responses because httpx
# automatically decompresses the body — forwarding the header would cause
# clients (e.g. ccd-cli / Bun) to try to decompress already-raw bytes → ZlibError.

_HOP_BY_HOP = frozenset([
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
    "content-encoding",  # httpx decompresses; never re-announce compression
])


# ── Transparent relay ─────────────────────────────────────────────────────────

@app.api_route(
    "/{full_path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
)
async def relay(request: Request, full_path: str):
    upstream_url = f"{TARGET}/{full_path}"

    # Forward all headers except hop-by-hop.
    # Crucially: x-api-key, anthropic-version, anthropic-beta, authorization,
    # content-type, accept, user-agent are all passed through.
    # accept-encoding is overridden to "identity" so Anthropic returns raw
    # (uncompressed) bytes — httpx would decompress but still forward the
    # Content-Encoding header, causing ZlibError in Bun/ccd-cli.
    forward_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP and k.lower() != "accept-encoding"
    }
    forward_headers["accept-encoding"] = "identity"  # no compression from upstream

    body = await request.body()

    # Log only safe metadata — never log auth headers or body content.
    logger.info(
        "→ %s /%s  body_len=%d",
        request.method,
        full_path,
        len(body),
    )

    # Detect whether the client wants a streaming response.
    # Claude Code sets "stream": true in the JSON body and/or
    # "accept: text/event-stream" header.
    wants_stream = (
        b'"stream":true' in body
        or b'"stream": true' in body
        or "text/event-stream" in request.headers.get("accept", "")
    )

    # Shared httpx client settings: 600 s timeout for long Opus 4 responses.
    timeout = httpx.Timeout(connect=15.0, read=600.0, write=60.0, pool=15.0)

    if wants_stream:
        # ── Streaming path: pipe bytes as they arrive, no buffering ──────────
        async def stream_generator():
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    async with client.stream(
                        method=request.method,
                        url=upstream_url,
                        headers=forward_headers,
                        content=body,
                        params=request.query_params,
                        follow_redirects=True,
                    ) as upstream_resp:
                        logger.info(
                            "← %d /%s (streaming)",
                            upstream_resp.status_code,
                            full_path,
                        )
                        async for chunk in upstream_resp.aiter_bytes(chunk_size=None):
                            yield chunk
            except asyncio.CancelledError:
                logger.info("Client disconnected during stream /%s", full_path)
            except Exception as exc:
                logger.error("Stream error /%s: %s", full_path, exc)
                yield b""

        # We need the response headers from the upstream before we can build
        # StreamingResponse, so we open the stream and peek at headers first.
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream(
                    method=request.method,
                    url=upstream_url,
                    headers=forward_headers,
                    content=body,
                    params=request.query_params,
                    follow_redirects=True,
                ) as upstream_resp:
                    resp_headers = {
                        k: v for k, v in upstream_resp.headers.items()
                        if k.lower() not in _HOP_BY_HOP
                    }
                    logger.info(
                        "← %d /%s (streaming)",
                        upstream_resp.status_code,
                        full_path,
                    )

                    async def _gen():
                        async for chunk in upstream_resp.aiter_bytes(chunk_size=None):
                            yield chunk

                    return StreamingResponse(
                        _gen(),
                        status_code=upstream_resp.status_code,
                        headers=resp_headers,
                        media_type=upstream_resp.headers.get(
                            "content-type", "text/event-stream"
                        ),
                    )
        except httpx.ConnectTimeout:
            logger.error("ConnectTimeout → %s", upstream_url)
            return Response(
                content=b'{"error":"proxy_timeout","message":"upstream connect timeout"}',
                status_code=504,
                media_type="application/json",
            )
        except Exception as exc:
            logger.error("Proxy error → %s: %s", upstream_url, exc)
            return Response(
                content=f'{{"error":"proxy_error","message":"{exc}"}}'.encode(),
                status_code=502,
                media_type="application/json",
            )

    else:
        # ── Non-streaming path: buffer the full response ───────────────────
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                upstream_resp = await client.request(
                    method=request.method,
                    url=upstream_url,
                    headers=forward_headers,
                    content=body,
                    params=request.query_params,
                    follow_redirects=True,
                )
        except httpx.ConnectTimeout:
            logger.error("ConnectTimeout → %s", upstream_url)
            return Response(
                content=b'{"error":"proxy_timeout","message":"upstream connect timeout"}',
                status_code=504,
                media_type="application/json",
            )
        except Exception as exc:
            logger.error("Proxy error → %s: %s", upstream_url, exc)
            return Response(
                content=f'{{"error":"proxy_error","message":"{exc}"}}'.encode(),
                status_code=502,
                media_type="application/json",
            )

        resp_headers = {
            k: v for k, v in upstream_resp.headers.items()
            if k.lower() not in _HOP_BY_HOP
        }

        logger.info("← %d /%s", upstream_resp.status_code, full_path)

        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
        )
