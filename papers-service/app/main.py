"""papers-service：paper-search-mcp 的替代实现，research-proxy 的 /papers/* 上游。

端点形状与 mcpo 暴露的 paper-search-mcp 完全一致（research-proxy 零改动可切）：
  POST /papers/search_{source}            {"query", "max_results"} -> {"papers": [...]}
  POST /papers/get_crossref_paper_by_doi  {"doi"}                 -> paper dict（裸）
  POST /papers/read_{source}_paper        {"paper_id"}            -> {"result": "全文文本"}

检索复用 paper-search 仓库 tool.py 的直连适配器（镜像构建时 ADD 进 /srv/vendor/tool.py，
见 app/toolwrap.py）；read 用本服务自带的轻量实现（app/readers.py），读不了的源/论文
一律 404 → research-proxy 自动落 reach/read_url（reach-mcp 仍在 mcpo 上，不退役）。

错误约定（对齐 shim 的消费方式）：
  未知源/未知工具      404（shim 记 "http 404" 进 sourceErrors 后继续）
  检索适配器抛异常     502 + detail（shim 记错误字符串）
  单源超时（16s）      504
"""

from __future__ import annotations

import asyncio
import logging
import os
import re

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import readers, toolwrap

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("papers-service")

app = FastAPI(title="firecrawl-papers-service", docs_url=None, redoc_url=None)

SEARCH_RE = re.compile(r"^search_([a-z0-9_]+)$")
READ_RE = re.compile(r"^read_([a-z0-9_]+)_paper$")


@app.get("/healthz")
async def healthz() -> dict:
    return {
        "ok": True,
        "toolVersion": toolwrap.TOOL_VERSION,
        "searchSources": sorted(toolwrap.SEARCH_DISPATCH),
        "readSources": sorted(readers.READERS),
    }


@app.post("/papers/{tool}")
async def papers_tool(tool: str, request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"detail": "JSON object body required"}, status_code=400)

    if tool == "get_crossref_paper_by_doi":
        doi = str(body.get("doi") or "").strip()
        if not doi:
            return JSONResponse({"detail": "doi required"}, status_code=400)
        item = await toolwrap.crossref_by_doi(doi)
        if item is None:
            return JSONResponse({"detail": f"doi not found: {doi}"}, status_code=404)
        return item  # shim: data.get("result") or data —— 裸 dict 即可

    m = SEARCH_RE.match(tool)
    if m:
        source = m.group(1)
        if source not in toolwrap.SEARCH_DISPATCH:
            return JSONResponse({"detail": f"unknown search source: {source}"}, status_code=404)
        query = str(body.get("query") or "").strip()
        if not query:
            return JSONResponse({"detail": "query required"}, status_code=400)
        try:
            limit = max(1, min(int(body.get("max_results") or 10), 50))
        except (TypeError, ValueError):
            limit = 10
        try:
            papers = await toolwrap.search(source, query, limit)
        except asyncio.TimeoutError:
            return JSONResponse({"detail": f"{source} search timed out"}, status_code=504)
        except Exception as e:
            log.warning("search %s failed: %s", source, e)
            return JSONResponse(
                {"detail": f"{source}: {type(e).__name__}: {e}"}, status_code=502
            )
        return {"papers": papers}

    m = READ_RE.match(tool)
    if m:
        source = m.group(1)
        fn = readers.READERS.get(source)
        if fn is None:
            return JSONResponse(
                {"detail": f"read not supported for {source}"}, status_code=404
            )
        pid = str(body.get("paper_id") or "").strip()
        if not pid:
            return JSONResponse({"detail": "paper_id required"}, status_code=400)
        try:
            text = await asyncio.wait_for(fn(pid), timeout=80)
        except asyncio.TimeoutError:
            log.warning("read %s/%s timed out", source, pid)
            text = None
        except Exception as e:
            log.warning("read %s/%s failed: %s", source, pid, e)
            text = None
        if not text:
            return JSONResponse(
                {"detail": f"no full text for {source}:{pid}"}, status_code=404
            )
        return {"result": text}

    return JSONResponse({"detail": f"unknown tool: {tool}"}, status_code=404)
