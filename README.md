# kb-api

FastAPI service over `rag.chunks` in Postgres container `mandrock-kb-postgres`
(db `mandrock_kb`, host `127.0.0.1:5433`). Runs as systemd service `kb-api`
on `127.0.0.1:3492` (see `/etc/systemd/system/kb-api.service`).

## REST (`/v1`)

- `GET /v1/healthz`
- `GET /v1/search?q=...&ns=prod&visibility=public&category=...&limit=10`
- `GET /v1/chunk/{chunk_id}`
- `GET /v1/article/{slug}?ns=prod`
- `GET /v1/stats`

All routes except `/v1/healthz` require `Authorization: Bearer $KB_API_TOKEN`.

Public URL (via nginx, `tools.mandrock.me` / `mandrock-tools.duckdns.org`
redirects there): `https://tools.mandrock.me/kb/v1/...`

## MCP (`/mcp`)

Same FastAPI process mounts a streamable-HTTP MCP server (via `fastmcp`) at
`/mcp`, exposing 3 high-level tools backed by the same internal functions the
REST routes use — no raw SQL is exposed to MCP callers:

- `kb_search(query, namespace='prod', visibility='public', category=None, limit=10)`
  — full-text search; defaults to public/prod content only.
- `kb_get_article(slug, namespace='prod')` — full article (all chunks, in order).
- `kb_stats()` — chunk counts by namespace/visibility/category + latest indexed_at.

Auth: same `KB_API_TOKEN` as Bearer token, enforced by app-level middleware
(no nginx `auth_basic` on this path — combining it with Bearer app-auth causes
a 401 loop for MCP clients).

Public MCP endpoint: `https://tools.mandrock.me/kb/mcp/` (trailing slash;
`https://mandrock-tools.duckdns.org/kb/mcp` also works, it 301-redirects to
the `tools.mandrock.me` host first). nginx has `proxy_buffering off` and
`proxy_read_timeout 3600s` on this location — required for streamable-http/SSE.

### Connector example

```
URL: https://tools.mandrock.me/kb/mcp/
Transport: streamable-http
Headers:
  Authorization: Bearer <KB_API_TOKEN>
```

Manual handshake:

```sh
TOKEN=$(grep KB_API_TOKEN .env | cut -d= -f2)
curl -s -D - -X POST https://tools.mandrock.me/kb/mcp/ \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}'
```

Grab the `mcp-session-id` response header, send
`{"jsonrpc":"2.0","method":"notifications/initialized"}` with that header,
then `tools/list` / `tools/call` the same way.

## Config

`.env` (gitignored): `KB_API_TOKEN`, `KB_DB_HOST`, `KB_DB_PORT`, `KB_DB_NAME`,
`KB_DB_USER`, `KB_DB_PASSWORD`.

## Deploy

```sh
systemctl restart kb-api
systemctl status kb-api
```

nginx location for `/kb/` (incl. `/kb/mcp/`) lives in
`/etc/nginx/snippets/mandrock-tools-apps.conf`, included from
`/etc/nginx/sites-available/mandrock-me.conf` (symlinked into
`sites-enabled`) for the `tools.mandrock.me` server block.
