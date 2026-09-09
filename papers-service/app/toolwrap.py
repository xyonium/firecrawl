"""tool.py（paper-search 仓库的 OWUI 工具）的进程内封装：加载、valves 注入、检索分发。

镜像构建时 Dockerfile 把 xyonium/paper-search 的 tool.py ADD 到 /srv/vendor/tool.py
（PAPER_SEARCH_REF build-arg 决定 ref）；本地跑测试用 PAPER_SEARCH_TOOL_PATH 指到
工作区副本。

设计约束：
- research-proxy（shim）按 mcpo/paper-search-mcp 的形状消费我们：item 的 paper_id
  必须是**裸 id**（shim 会自行加 "{source}:" 前缀），所以这里要把 tool.py 结果里
  的前缀剥掉。
- key-gated 源（ieee/zhihuiya/firecrawl/tavily/google_scholar）没配 key 时返回 []
  而不是报错——"未配置"不是故障，shim 当空结果正常合并。
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import re

import anyio
import requests

_TOOL_PATH = os.environ.get("PAPER_SEARCH_TOOL_PATH", "/srv/vendor/tool.py")

_spec = importlib.util.spec_from_file_location("paper_search_tool", _TOOL_PATH)
tool_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool_mod)

_m = re.search(r"version:\s*([0-9.]+)", tool_mod.__doc__ or "")
TOOL_VERSION = _m.group(1) if _m else "unknown"

UA = {"User-Agent": "papers-service/1.0 (+https://github.com/xyonium/firecrawl)"}

# tool.py paper_id 的已知前缀 → 剥掉还原裸 id（doi/URL key 等无前缀的不受影响）
_STRIP_PREFIXES = {
    "arxiv", "semantic", "hal", "zenodo", "ieee", "openaire", "openalex",
    "crossref", "core", "iacr", "pubmed", "pmid", "pmc", "scholar",
    "biorxiv", "medrxiv", "europepmc",
}

_tools = None


def build_tools():
    """从环境变量注入 valves（等价 OWUI admin valves 的效果）。"""
    t = tool_mod.Tools()
    v = t.valves
    v.semantic_api_key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "")
    v.core_api_key = os.environ.get("CORE_API_KEY", "")
    v.ncbi_api_key = os.environ.get("NCBI_API_KEY", "")
    v.ieee_apikey = os.environ.get("IEEE_APIKEY", "")
    v.zenodo_access_token = os.environ.get("ZENODO_ACCESS_TOKEN", "")
    v.zhihuiya_apikey = os.environ.get("ZHIHUIYA_APIKEY", "")
    v.firecrawl_base_url = os.environ.get("FIRECRAWL_BASE_URL", "")
    v.tavily_base_url = os.environ.get("TAVILY_BASE_URL", "")
    v.apify_rotator_base_url = os.environ.get("APIFY_ROTATOR_BASE_URL", "")
    v.mcpo_api_key = os.environ.get("MCPO_API_KEY", "")
    return t


def tools():
    """惰性单例；测试可直接赋值 toolwrap._tools 或 monkeypatch 其方法。"""
    global _tools
    if _tools is None:
        _tools = build_tools()
    return _tools


def _strip_prefix(paper_id: str) -> str:
    pid = (paper_id or "").strip()
    pfx, _, rest = pid.partition(":")
    if rest and pfx.lower() in _STRIP_PREFIXES:
        return rest
    return pid


# --- google_scholar 三级链（与 tool.py search_papers 里的 _gscholar 同序） --------


async def scholar_chain(t, query: str, limit: int) -> list:
    attempts = []
    if t._firecrawl_base():
        attempts.append(lambda: t._google_scholar_firecrawl_search(query, limit))
    if t._tavily_base():
        attempts.append(lambda: t._google_scholar_tavily_search(query, limit))
    if t._apify_rotator_base():
        attempts.append(lambda: t._google_scholar_actor_search(query, limit))
    last_exc = None
    for i, fn in enumerate(attempts):
        try:
            papers = await fn()
            if papers or i == len(attempts) - 1:
                return papers
        except Exception as e:
            last_exc = e
    if last_exc is not None:
        raise last_exc
    return []


# --- key-gated 包装：未配置返回 [] -------------------------------------------------


async def _ieee(t, q, n):
    if not t.valves.ieee_apikey:
        return []
    return await t._ieee_search(q, n, t.valves.ieee_apikey)


async def _zhihuiya(t, q, n):
    if not t.valves.zhihuiya_apikey:
        return []
    return await t._zhihuiya_search(q, n, t.valves.zhihuiya_apikey)


async def _firecrawl(t, q, n):
    if not t._firecrawl_base():
        return []
    return await t._firecrawl_search_papers(q, n)


async def _tavily(t, q, n):
    if not t._tavily_base():
        return []
    return await t._tavily_search_papers(q, n)


# --- doaj 直连（DOAJ 公开 search API，免 key）--------------------------------------


async def _doaj(t, q, n):
    def _fetch():
        r = requests.get(
            f"https://doaj.org/api/search/articles/{requests.utils.quote(q, safe='')}",
            params={"page": 1, "pageSize": min(max(1, int(n)), 100)},
            timeout=20,
            headers=UA,
        )
        r.raise_for_status()
        out = []
        for it in (r.json().get("results") or [])[:n]:
            b = it.get("bibjson") or {}
            title = (b.get("title") or "").strip()
            if not title:
                continue
            doi = next(
                (i.get("id", "") for i in (b.get("identifier") or []) if i.get("type") == "doi"),
                "",
            )
            link = next(
                (l.get("url", "") for l in (b.get("link") or []) if l.get("type") == "fulltext"),
                "",
            )
            out.append({
                "title": title,
                "authors": "; ".join(
                    a.get("name", "") for a in (b.get("author") or []) if a.get("name")
                ),
                "published_date": str(b.get("year") or ""),
                "abstract": b.get("abstract") or "",
                "paper_id": doi or str(it.get("id") or ""),
                "doi": doi,
                "source": "doaj",
                "pdf_url": link,
                "citations": 0,
                "url": link or (f"https://doi.org/{doi}" if doi else ""),
            })
        return out

    return await anyio.to_thread.run_sync(_fetch)


# --- crossref DOI 查询（inspect 的 doi 兜底）---------------------------------------


async def crossref_by_doi(doi: str) -> dict | None:
    def _fetch():
        r = requests.get(
            f"https://api.crossref.org/works/{requests.utils.quote(doi, safe='')}",
            timeout=10,
            headers=UA,
        )
        if r.status_code != 200:
            return None
        m = r.json().get("message") or {}
        title = ((m.get("title") or [""])[0] or "").strip()
        if not title:
            return None
        authors = "; ".join(
            " ".join(x for x in (a.get("given", ""), a.get("family", "")) if x).strip()
            for a in (m.get("author") or [])
        )
        date = ""
        for k in ("published-print", "published-online", "published", "issued"):
            parts = ((m.get(k) or {}).get("date-parts") or [[]])[0]
            if parts and parts[0]:
                date = str(parts[0]) + "".join(f"-{str(p).zfill(2)}" for p in parts[1:])
                break
        abstract = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.get("abstract") or "")).strip()
        doi_v = m.get("DOI") or doi
        return {
            "title": title,
            "authors": authors,
            "published_date": date,
            "abstract": abstract,
            "paper_id": doi_v,
            "doi": doi_v,
            "source": "crossref",
            "pdf_url": "",
            "citations": m.get("is-referenced-by-count") or 0,
            "url": m.get("URL") or f"https://doi.org/{doi_v}",
        }

    return await anyio.to_thread.run_sync(_fetch)


# --- 检索分发 ----------------------------------------------------------------------


SEARCH_DISPATCH = {
    "arxiv": lambda t, q, n: t._arxiv_search(q, n),
    "semantic": lambda t, q, n: t._semantic_search(q, n),
    "pubmed": lambda t, q, n: t._pubmed_search(q, n),
    "pmc": lambda t, q, n: t._pmc_search(q, n),
    "openalex": lambda t, q, n: t._openalex_search(q, n),
    "crossref": lambda t, q, n: t._crossref_search(q, n),
    "europepmc": lambda t, q, n: t._europepmc_search(q, n),
    "core": lambda t, q, n: t._core_search(q, n),
    "hal": lambda t, q, n: t._hal_search(q, n),
    "dblp": lambda t, q, n: t._dblp_search(q, n),
    "zenodo": lambda t, q, n: t._zenodo_search(q, n),
    "openaire": lambda t, q, n: t._openaire_search(q, n),
    "iacr": lambda t, q, n: t._iacr_search(q, n),
    "ieee": _ieee,
    "zhihuiya": _zhihuiya,
    "google_scholar": scholar_chain,
    "firecrawl": _firecrawl,
    "tavily": _tavily,
    "doaj": _doaj,
}


async def search(source: str, query: str, limit: int) -> list:
    """调 tool.py 对应直连适配器，剥 paper_id 前缀后返回 shim 消费的形状。

    shim 给单源 18s 预算，这里 16s 截断（留 2s 余量），超时抛 asyncio.TimeoutError。
    """
    fn = SEARCH_DISPATCH[source]  # 路由层已校验存在
    papers = await asyncio.wait_for(fn(tools(), query, limit), timeout=16)
    out = []
    for p in papers or []:
        p = dict(p)
        p["paper_id"] = _strip_prefix(p.get("paper_id"))
        p.setdefault("source", source)
        out.append(p)
    return out
