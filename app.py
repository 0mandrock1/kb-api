import os
from contextlib import asynccontextmanager
from typing import Optional

import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
from fastmcp import FastMCP

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

KB_API_TOKEN = os.environ["KB_API_TOKEN"]
DB_DSN = (
    f"host={os.environ.get('KB_DB_HOST', '127.0.0.1')} "
    f"port={os.environ.get('KB_DB_PORT', '5433')} "
    f"dbname={os.environ.get('KB_DB_NAME', 'mandrock_kb')} "
    f"user={os.environ.get('KB_DB_USER', 'mandrock')} "
    f"password={os.environ.get('KB_DB_PASSWORD', '')}"
)

pool: dict = {}

mcp = FastMCP("kb-api")
mcp_app = mcp.http_app(path="/")


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool["conn"] = await psycopg.AsyncConnection.connect(DB_DSN, row_factory=dict_row)
    async with mcp_app.lifespan(app):
        yield
    await pool["conn"].close()


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def mcp_bearer_auth(request: Request, call_next):
    if request.url.path.startswith("/mcp"):
        try:
            check_auth(request.headers.get("authorization"))
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    return await call_next(request)


app.mount("/mcp", mcp_app)


def check_auth(authorization: Optional[str]):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization[len("Bearer "):]
    if token != KB_API_TOKEN:
        raise HTTPException(status_code=401, detail="invalid token")


def parse_csv(value: Optional[str], default: str) -> list[str]:
    if value is None:
        return [default]
    parts = [p.strip() for p in value.split(",") if p.strip()]
    return parts or [default]


@app.get("/v1/healthz")
async def healthz():
    return {"status": "ok"}


async def _search(
    q: str,
    namespaces: list[str],
    visibilities: list[str],
    category: Optional[str],
    limit: int,
) -> dict:
    conn = pool["conn"]
    sql = """
        SELECT id, slug, title, heading_path, leaf_heading, category, body,
               (ts_rank_cd(fts, websearch_to_tsquery('simple', %(q)s))
                + COALESCE(similarity(title, %(q)s), 0)) AS score
        FROM rag.chunks
        WHERE is_boilerplate = false
          AND namespace = ANY(%(namespaces)s)
          AND visibility = ANY(%(visibilities)s)
          AND (%(category)s::text IS NULL OR category = %(category)s)
          AND (
                fts @@ websearch_to_tsquery('simple', %(q)s)
                OR similarity(title, %(q)s) > 0.1
              )
        ORDER BY score DESC
        LIMIT %(limit)s
    """
    async with conn.cursor() as cur:
        await cur.execute(
            sql,
            {
                "q": q,
                "namespaces": namespaces,
                "visibilities": visibilities,
                "category": category,
                "limit": limit,
            },
        )
        rows = await cur.fetchall()
    return {"results": rows, "count": len(rows)}


async def _get_chunk(chunk_id: int) -> dict:
    conn = pool["conn"]
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT id, article_id, slug, category, title, heading_path, chunk_idx,
                   body, char_len, indexed_at, leaf_heading, is_boilerplate,
                   namespace, visibility
            FROM rag.chunks WHERE id = %s
            """,
            (chunk_id,),
        )
        chunk = await cur.fetchone()
        if not chunk:
            raise HTTPException(status_code=404, detail="chunk not found")

        await cur.execute(
            """
            SELECT id, article_id, slug, category, title, heading_path, chunk_idx,
                   body, char_len, indexed_at, leaf_heading, is_boilerplate,
                   namespace, visibility
            FROM rag.chunks
            WHERE slug = %s AND chunk_idx IN (%s, %s)
            ORDER BY chunk_idx
            """,
            (chunk["slug"], chunk["chunk_idx"] - 1, chunk["chunk_idx"] + 1),
        )
        neighbors = await cur.fetchall()

    return {"chunk": chunk, "neighbors": neighbors}


async def _get_article(slug: str, namespaces: list[str]) -> dict:
    conn = pool["conn"]
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT id, article_id, slug, category, title, heading_path, chunk_idx,
                   body, char_len, indexed_at, leaf_heading, is_boilerplate,
                   namespace, visibility
            FROM rag.chunks
            WHERE slug = %s AND namespace = ANY(%s)
            ORDER BY chunk_idx
            """,
            (slug, namespaces),
        )
        rows = await cur.fetchall()
    if not rows:
        raise HTTPException(status_code=404, detail="article not found")
    return {"slug": slug, "chunks": rows}


async def _stats() -> dict:
    conn = pool["conn"]
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT namespace, visibility, category, count(*) AS n
            FROM rag.chunks
            GROUP BY namespace, visibility, category
            ORDER BY namespace, visibility, category
            """
        )
        breakdown = await cur.fetchall()
        await cur.execute("SELECT max(indexed_at) AS max_indexed_at FROM rag.chunks")
        max_indexed = await cur.fetchone()
    return {"breakdown": breakdown, "max_indexed_at": max_indexed["max_indexed_at"]}


@app.get("/v1/search")
async def search(
    q: str = Query(..., min_length=1),
    ns: Optional[str] = Query(None),
    visibility: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=100),
    authorization: Optional[str] = Header(None),
):
    check_auth(authorization)
    return await _search(q, parse_csv(ns, "prod"), parse_csv(visibility, "public"), category, limit)


@app.get("/v1/chunk/{chunk_id}")
async def get_chunk(chunk_id: int, authorization: Optional[str] = Header(None)):
    check_auth(authorization)
    return await _get_chunk(chunk_id)


@app.get("/v1/article/{slug}")
async def get_article(
    slug: str,
    ns: Optional[str] = Query(None),
    authorization: Optional[str] = Header(None),
):
    check_auth(authorization)
    return await _get_article(slug, parse_csv(ns, "prod"))


@app.get("/v1/stats")
async def stats(authorization: Optional[str] = Header(None)):
    check_auth(authorization)
    return await _stats()


@mcp.tool(
    description=(
        "Search the knowledge base full-text index. By default returns only "
        "PUBLIC content from the PROD namespace (namespace='prod', "
        "visibility='public') — pass explicit namespace/visibility to widen "
        "the scope. Returns matching chunks ranked by relevance."
    )
)
async def kb_search(
    query: str,
    namespace: str = "prod",
    visibility: str = "public",
    category: Optional[str] = None,
    limit: int = 10,
) -> dict:
    return await _search(query, [namespace], [visibility], category, limit)


@mcp.tool(
    description=(
        "Fetch a full article (all its chunks, in order) by slug. By default "
        "looks it up in the PROD namespace (namespace='prod'); this tool does "
        "not filter by visibility, so it can return non-public chunks if the "
        "slug belongs to one — check the 'visibility' field on returned chunks."
    )
)
async def kb_get_article(slug: str, namespace: str = "prod") -> dict:
    return await _get_article(slug, [namespace])


@mcp.tool(
    description=(
        "Return knowledge base statistics: a breakdown of chunk counts by "
        "namespace/visibility/category, and the most recent indexed_at "
        "timestamp. Covers all namespaces and visibilities, not just "
        "public prod content."
    )
)
async def kb_stats() -> dict:
    return await _stats()
