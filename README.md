# anthropic-proxy

Transparent reverse proxy for `api.anthropic.com`.

Forwards **all** requests (any method, any path) to the upstream Anthropic API
with full streaming (SSE) support. No buffering. No API keys stored on the proxy.

## Architecture

```
Claude Code (Beget) → anthropic-proxy (Render) → api.anthropic.com
```

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `ANTHROPIC_API_URL` | `https://api.anthropic.com` | Upstream target |
| `PORT` | set by Render | Listen port |

## Deploy on Render

1. Connect this repo to Render as a **Web Service**
2. **Plan:** Starter ($7/mo) — required for long streaming responses
3. **Build command:** `pip install -r requirements.txt`
4. **Start command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. **Health check path:** `/health`

## Health check

```shell
curl https://<your-proxy>.onrender.com/health
# → {"status":"ok","target":"https://api.anthropic.com","version":"v1"}
```

## Usage

Set `ANTHROPIC_BASE_URL=https://<your-proxy>.onrender.com` on the client server.
Claude Code will automatically route all Anthropic API calls through the proxy.
