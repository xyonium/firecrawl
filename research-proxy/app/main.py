"""firecrawl research-upstream shim.

Implements the upstream protocol consumed by firecrawl's research proxy
(controllers/v2/research-proxy.ts) so a self-hosted stack gets working
research/developer endpoints:

  GET /v2/research/papers                  search   (30s budget upstream)
  GET /v2/research/papers/{id}             inspect  (5s) / read (?query=, 120s)
  GET /v2/research/papers/{id}/similar     similar  (10s)
  GET /v2/research/github                  legacy repo readme search (12s)
  GET /v2/code/search                      developer/code search (15s)

firecrawl passes our status + JSON through verbatim; errors should carry
{detail: ...} (it also reads error/title for logging).
"""

from __future__ import annotations

import logging
import os
from typing import Annotated

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from . import github, papers

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("research-proxy")

app = FastAPI(title="firecrawl-research-proxy", docs_url=None, redoc_url=None)


@app.exception_handler(github.GitHubUnavailable)
async def gh_unavailable(_req: Request, exc: github.GitHubUnavailable) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=503)


@app.exception_handler(httpx.HTTPStatusError)
async def upstream_http(_req: Request, exc: httpx.HTTPStatusError) -> JSONResponse:
    log.warning("upstream http error: %s", exc)
    return JSONResponse({"detail": f"backend http {exc.response.status_code}"}, status_code=502)


@app.exception_handler(httpx.HTTPError)
async def upstream_conn(_req: Request, exc: httpx.HTTPError) -> JSONResponse:
    log.warning("backend unreachable: %s", exc)
    return JSONResponse({"detail": f"backend unreachable: {type(exc).__name__}"}, status_code=502)


@app.get("/healthz")
async def healthz() -> dict:
    return {
        "ok": True,
        "githubTokens": len(github._tokens),
        "paperSources": papers.PAPER_SOURCES,
        "mcpo": papers.MCPO_BASE,
    }


@app.get("/v2/research/papers")
async def search_papers(
    query: Annotated[str, Query(min_length=1)],
    k: int = 10,
    authors: list[str] | None = Query(default=None),
    categories: list[str] | None = Query(default=None),
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = None,
) -> dict:
    return await papers.search_papers(
        query=query,
        k=max(1, min(k, 500)),
        authors=authors,
        categories=categories,
        date_from=from_,
        date_to=to,
    )


# /similar must be registered before /{paper_id} or FastAPI routes it as inspect.
# {paper_id:path}: doi ids contain '/', and uvicorn decodes %2F before routing,
# so a plain str param would 404 on every doi.
@app.get("/v2/research/papers/{paper_id:path}/similar")
async def similar(
    paper_id: str,
    intent: Annotated[str, Query(min_length=1)],
    mode: str = "similar",
    k: int = 10,
    rerank: bool = False,
    anchor: list[str] | None = Query(default=None),
) -> dict:
    if mode not in ("similar", "citers", "references"):
        mode = "similar"
    return await papers.similar_papers(
        paper_id=paper_id,
        intent=intent,
        mode=mode,
        k=max(1, min(k, 500)),
        rerank=rerank,
        anchors=anchor,
    )


@app.get("/v2/research/papers/{paper_id:path}")
async def inspect_or_read(
    paper_id: str,
    query: str | None = None,
    k: int = 4,
) -> JSONResponse:
    if query:
        return JSONResponse(await papers.read_paper(paper_id, query, max(1, min(k, 50))))
    meta = await papers.inspect_paper(paper_id)
    if meta is None:
        return JSONResponse({"detail": f"paper not found: {paper_id}"}, status_code=404)
    return JSONResponse({"success": True, "paper": meta})


@app.get("/v2/research/github")
async def github_legacy(
    query: Annotated[str, Query(min_length=1)],
    k: int = 5,
) -> dict:
    return await github.github_legacy_search(query, max(1, min(k, 100)))


@app.get("/v2/code/search")
async def code_search(
    query: Annotated[str, Query(min_length=1)],
    k: int = 10,
    language: str | None = None,
    repos: list[str] | None = Query(default=None),
    license_: str | None = Query(default=None, alias="license"),
    passages: int = 3,
    archived: bool | None = None,
    fork: bool | None = None,
    # accepted-but-ignored (firecrawl forwards them; GitHub code search has
    # no such qualifiers): types, sources, topic, min_stars, max_stars, skills
) -> dict:
    return await github.code_search(
        query=query,
        k=max(1, min(k, 100)),
        language=language,
        repos=repos,
        license_=license_,
        max_passages=max(1, min(passages, 5)),
        archived=archived,
        fork=fork,
    )
