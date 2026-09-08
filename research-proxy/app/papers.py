"""Paper backends: paper-search-mcp via mcpo, Semantic Scholar graph, arXiv API.

Implements the firecrawl research-upstream paper contract:
  search  -> GET /v2/research/papers            (30s budget)
  inspect -> GET /v2/research/papers/{id}       (5s budget)
  read    -> GET /v2/research/papers/{id}?query (120s budget)
  similar -> GET /v2/research/papers/{id}/similar (10s budget)
"""

from __future__ import annotations

import asyncio
import os
import re
import time
import xml.etree.ElementTree as ET
from typing import Any

import httpx

from .util import chunk_text, keyword_score, parse_paper_id, rank_passages, s2_lookup_id

MCPO_BASE = os.environ.get("MCPO_BASE_URL", "http://mcpo:8000").rstrip("/")
PAPER_SOURCES = [
    s.strip()
    for s in os.environ.get(
        "PAPER_SOURCES",
        "arxiv,semantic,pubmed,openalex,crossref,europepmc,dblp,hal,pmc",
    ).split(",")
    if s.strip()
]
S2_BASE = "https://api.semanticscholar.org/graph/v1"
S2_RECO_BASE = "https://api.semanticscholar.org/recommendations/v1"
S2_KEY = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "")
S2_FIELDS = "title,abstract,authors,year,externalIds,publicationDate,venue,openAccessPdf,url"

# paper-search-mcp read tools keyed by source name.
READ_TOOL = {
    s: f"read_{s}_paper"
    for s in (
        "arxiv", "pubmed", "biorxiv", "medrxiv", "iacr", "semantic", "crossref",
        "dblp", "openaire", "citeseerx", "doaj", "base", "zenodo", "hal",
        "ssrn", "openalex", "ieee",
    )
}

_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0))
    return _client


# --- mapping ----------------------------------------------------------------


def _paperhit_from_mcp(item: dict[str, Any]) -> dict[str, Any]:
    """paper-search-mcp item -> PaperHit (MCP research.ts consumer shape)."""
    source = str(item.get("source") or "").strip() or "unknown"
    raw_id = str(item.get("paper_id") or "").strip()
    doi = str(item.get("doi") or "").strip()
    if doi:
        paper_id = f"doi:{doi}"
    elif source == "arxiv" and raw_id:
        paper_id = f"arxiv:{raw_id}"
    elif raw_id:
        paper_id = f"{source}:{raw_id}"
    else:
        paper_id = f"{source}:{(item.get('title') or '')[:48]}"
    ids: dict[str, list[str]] = {}
    if raw_id:
        ids[source] = [raw_id]
    if doi:
        ids["doi"] = [doi]
    cats = [
        c.strip()
        for c in re.split(r"[,\s]+", str(item.get("categories") or ""))
        if c.strip()
    ]
    published = str(item.get("published_date") or "")[:10] or None
    updated = str(item.get("updated_date") or "")[:10] or None
    return {
        "paperId": paper_id,
        "primaryId": paper_id,
        "ids": ids or None,
        "title": item.get("title") or "",
        "abstract": item.get("abstract") or "",
        "authors": item.get("authors") or "",
        "categories": cats or None,
        "createdDate": published,
        "updateDate": updated,
        "url": item.get("url") or item.get("pdf_url") or None,
        "source": source,
    }


def _paperhit_from_s2(p: dict[str, Any]) -> dict[str, Any]:
    ext = p.get("externalIds") or {}
    if ext.get("ArXiv"):
        paper_id = f"arxiv:{ext['ArXiv']}"
    elif ext.get("DOI"):
        paper_id = f"doi:{ext['DOI']}"
    elif ext.get("PubMed"):
        paper_id = f"pmid:{ext['PubMed']}"
    elif ext.get("PubMedCentral"):
        paper_id = f"pmcid:PMC{ext['PubMedCentral']}"
    else:
        paper_id = f"s2:{p.get('paperId', '')}"
    ids = {
        k.lower(): [str(v)]
        for k, v in ext.items()
        if v and k.lower() in ("arxiv", "doi", "pubmed", "pubmedcentral", "corpusid")
    }
    authors = [{"name": a.get("name", "")} for a in p.get("authors") or [] if a.get("name")]
    return {
        "paperId": paper_id,
        "primaryId": paper_id,
        "ids": ids or None,
        "title": p.get("title") or "",
        "abstract": p.get("abstract") or "",
        "authors": authors or None,
        "categories": [p["venue"]] if p.get("venue") else None,
        "createdDate": p.get("publicationDate") or (str(p["year"]) if p.get("year") else None),
        "updateDate": None,
        "url": (p.get("openAccessPdf") or {}).get("url") or p.get("url"),
        "source": "semantic",
    }


# --- search -----------------------------------------------------------------


async def _search_one(source: str, query: str, per_source: int) -> tuple[str, list[dict], str | None]:
    """One source with its own deadline — a hanging source never sinks the rest."""
    try:
        r = await client().post(
            f"{MCPO_BASE}/papers/search_{source}",
            json={"query": query, "max_results": per_source},
            timeout=18.0,
        )
        if r.status_code != 200:
            return source, [], f"http {r.status_code}"
        data = r.json()
        items = data if isinstance(data, list) else data.get("papers") or data.get("result") or []
        if isinstance(items, dict):  # some sources wrap in {"papers": [...]}
            items = items.get("papers", [])
        for it in items:
            it.setdefault("source", source)
        return source, items, None
    except httpx.HTTPError as e:
        return source, [], type(e).__name__


async def search_papers(
    query: str,
    k: int,
    authors: list[str] | None,
    categories: list[str] | None,
    date_from: str | None,
    date_to: str | None,
) -> dict[str, Any]:
    per_source = max(2, min(10, -(-k // max(len(PAPER_SOURCES), 1))))
    fanned = await asyncio.gather(
        *(_search_one(s, query, per_source) for s in PAPER_SOURCES)
    )
    raw: list[dict] = []
    errors: dict[str, str] = {}
    for source, items, err in fanned:
        if err:
            errors[source] = err
        raw.extend(items)

    hits = [_paperhit_from_mcp(p) for p in raw]

    def keep(h: dict[str, Any]) -> bool:
        if date_from and (not h["createdDate"] or h["createdDate"] < date_from):
            return False
        if date_to and (not h["createdDate"] or h["createdDate"] > date_to):
            return False
        if authors:
            blob = h["authors"] if isinstance(h["authors"], str) else " ".join(
                a.get("name", "") for a in h["authors"]
            )
            if not any(a.lower() in blob.lower() for a in authors):
                return False
        if categories:
            blob_c = " ".join(h["categories"] or []).lower()
            if not any(c.lower() in blob_c for c in categories):
                return False
        return True

    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for h in hits:
        if not keep(h):
            continue
        key = (h["ids"] or {}).get("doi", [""])[0] or h["title"].lower().strip()
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    out = out[:k]
    total = len(out)
    for i, h in enumerate(out):
        h["score"] = round(1 - i / max(total, 1), 4)
    body: dict[str, Any] = {"success": True, "results": out, "total": total}
    if errors:
        body["meta"] = {"sourceErrors": errors}
    return body


# --- inspect ----------------------------------------------------------------


_s2_lock = asyncio.Lock()
_s2_next_at = 0.0


async def _s2_get(
    path: str, timeout: float = 4.0, base: str = S2_BASE, retry_429: bool = False
) -> dict[str, Any] | None:
    """S2 is strict about bursts (429s even keyed) — space calls ~1.1s apart.
    retry_429 only where the upstream budget allows (similar/read, not inspect)."""
    global _s2_next_at
    headers = {"x-api-key": S2_KEY} if S2_KEY else {}
    for attempt in range(2 if retry_429 else 1):
        async with _s2_lock:
            wait = _s2_next_at - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            _s2_next_at = time.monotonic() + 1.1
        try:
            r = await client().get(f"{base}{path}", headers=headers, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429 and attempt == 0:
                await asyncio.sleep(2.5)
                continue
        except httpx.HTTPError:
            pass
        return None
    return None


async def _arxiv_meta(arxiv_id: str) -> dict[str, Any] | None:
    base = re.sub(r"v\d+$", "", arxiv_id)
    try:
        r = await client().get(
            f"https://export.arxiv.org/api/query?id_list={base}", timeout=4.0
        )
        if r.status_code != 200:
            return None
        root = ET.fromstring(r.text)
        ns = {"a": "http://www.w3.org/2005/Atom"}
        entry = root.find("a:entry", ns)
        if entry is None or entry.find("a:title", ns) is None:
            return None
        authors = [a.findtext("a:name", "", ns) for a in entry.findall("a:author", ns)]
        cats = [c.get("term", "") for c in entry.findall("a:category", ns)]
        published = (entry.findtext("a:published", "", ns) or "")[:10]
        return {
            "paperId": f"arxiv:{base}",
            "primaryId": f"arxiv:{base}",
            "ids": {"arxiv": [base]},
            "title": re.sub(r"\s+", " ", entry.findtext("a:title", "", ns)).strip(),
            "abstract": re.sub(r"\s+", " ", entry.findtext("a:summary", "", ns)).strip(),
            "authors": [{"name": a} for a in authors if a],
            "categories": [c for c in cats if c] or None,
            "createdDate": published or None,
            "updateDate": (entry.findtext("a:updated", "", ns) or "")[:10] or None,
            "url": f"https://arxiv.org/abs/{base}",
            "source": "arxiv",
        }
    except (httpx.HTTPError, ET.ParseError):
        return None


async def _crossref_by_doi(doi: str) -> dict[str, Any] | None:
    """doi inspect fallback via paper-search-mcp's crossref lookup."""
    try:
        r = await client().post(
            f"{MCPO_BASE}/papers/get_crossref_paper_by_doi", json={"doi": doi}, timeout=4.0
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if isinstance(data, str):
            return None
        item = data.get("result") or data
        if isinstance(item, str) or not (item.get("title") or item.get("paper_id")):
            return None
        item.setdefault("source", "crossref")
        return _paperhit_from_mcp(item)
    except httpx.HTTPError:
        return None


async def inspect_paper(paper_id: str) -> dict[str, Any] | None:
    """5s budget: S2 graph first, arXiv export / crossref as fallbacks."""
    ns, value = parse_paper_id(paper_id)
    tasks = []
    s2id = s2_lookup_id(paper_id)
    if s2id:
        tasks.append(_s2_get(f"/paper/{s2id}?fields={S2_FIELDS}"))
    arxiv_id = value if ns == "arxiv" else (value if not ns and re.match(r"^\d{4}\.", value) else None)
    if not arxiv_id and ns == "doi":
        # arXiv datacite DOIs (10.48550/arxiv.XXXX.XXXXX) map straight back
        m = re.match(r"^10\.48550/arxiv\.(.+)$", value, re.IGNORECASE)
        if m:
            arxiv_id = m.group(1)
    if arxiv_id:
        tasks.append(_arxiv_meta(arxiv_id))
    if ns == "doi":
        tasks.append(_crossref_by_doi(value))
    if not tasks:
        return None
    results = await asyncio.gather(*tasks)
    for r in results:
        if not r:
            continue
        if "paperId" in r and "externalIds" in r:  # S2 payload
            return _paperhit_from_s2(r)
        return r  # already a PaperHit (arxiv / crossref fallback)
    return None


# --- read -------------------------------------------------------------------


async def _read_via_mcpo(ns: str, raw_id: str) -> str | None:
    tool = READ_TOOL.get(ns)
    if not tool or not raw_id:
        return None
    try:
        r = await client().post(
            f"{MCPO_BASE}/papers/{tool}", json={"paper_id": raw_id}, timeout=90.0
        )
        if r.status_code != 200:
            return None
        data = r.json()
        # mcpo unwraps single-string tool results to a bare JSON string
        result = data if isinstance(data, str) else data.get("result")
        if isinstance(result, str) and len(result) > 200 and "error" not in result[:80].lower():
            return result
    except httpx.HTTPError:
        pass
    return None


async def _read_via_reach(url: str) -> str | None:
    """jina-reader style fetch through reach-mcp's /read_url."""
    if not url:
        return None
    try:
        r = await client().post(
            f"{MCPO_BASE}/reach/read_url", json={"url": url}, timeout=60.0
        )
        if r.status_code != 200:
            return None
        data = r.json()
        text = data.get("text") or data.get("content") or ""
        return text if len(text) > 400 else None
    except httpx.HTTPError:
        return None


def _fulltext_candidates(ns: str, raw_id: str, meta: dict[str, Any] | None) -> list[str]:
    """URLs most likely to yield actual full text, best first.

    Never trust meta["url"] blindly: S2 sets it to the paper *page* on
    semanticscholar.org (abstract + recommendations), not the full text.
    """
    ids = (meta or {}).get("ids") or {}

    def first(key: str) -> str | None:
        v = ids.get(key)
        return v[0] if isinstance(v, list) and v else None

    arxiv_id = raw_id if ns == "arxiv" else first("arxiv")
    doi = raw_id if ns == "doi" else first("doi")
    if not arxiv_id and doi:
        m = re.match(r"^10\.48550/arxiv\.(.+)$", doi, re.IGNORECASE)
        if m:
            arxiv_id = m.group(1)
    urls: list[str] = []
    if arxiv_id:
        base = re.sub(r"v\d+$", "", arxiv_id)
        urls += [f"https://arxiv.org/html/{base}", f"https://arxiv.org/pdf/{base}"]
    meta_url = (meta or {}).get("url") or ""
    is_s2_page = "semanticscholar.org" in meta_url
    if meta_url and not is_s2_page:
        urls.append(meta_url)  # typically an open-access publisher PDF
    if doi:
        urls.append(f"https://doi.org/{doi}")
    # an S2 paper *page* has no full text (and its recommendations section
    # actively pollutes passage ranking) — never use it as a fallback
    return urls


async def read_paper(paper_id: str, query: str, k: int) -> dict[str, Any]:
    ns, raw_id = parse_paper_id(paper_id)
    meta_task = asyncio.create_task(inspect_paper(paper_id))

    text = await _read_via_mcpo(ns, raw_id)
    meta = await meta_task
    if text is None:
        for url in _fulltext_candidates(ns, raw_id, meta)[:3]:
            text = await _read_via_reach(url)
            if text:
                break

    if not text:
        return {
            "success": True,
            "paper": meta,
            "paperId": paper_id,
            "query": query,
            "passages": [],
            "note": "full text unavailable for this paper",
        }
    chunks = chunk_text(text)
    passages = rank_passages(chunks, query, k)
    return {
        "success": True,
        "paper": meta,
        "paperId": paper_id,
        "query": query,
        "passages": passages,
        "poolSize": len(chunks),
    }


# --- similar ----------------------------------------------------------------


async def similar_papers(
    paper_id: str,
    intent: str,
    mode: str,
    k: int,
    rerank: bool,
    anchors: list[str] | None,
) -> dict[str, Any]:
    seeds = [paper_id] + [a for a in (anchors or []) if a]
    limit = min(max(k, 20), 100)
    pool: dict[str, dict[str, Any]] = {}
    notes: list[str] = []

    async def expand(seed: str) -> None:
        s2id = s2_lookup_id(seed)
        if not s2id:
            notes.append(f"{seed}: no Semantic Scholar mapping")
            return
        rows: list[Any] = []
        if mode == "citers":
            data = await _s2_get(
                f"/paper/{s2id}/citations?fields={S2_FIELDS}&limit={limit}", timeout=8.0, retry_429=True
            )
            if data is None:
                notes.append(f"{seed}: citations pool unavailable (S2 rate limit)")
            rows = [d.get("citingPaper") for d in (data or {}).get("data", [])]
        elif mode == "references":
            data = await _s2_get(
                f"/paper/{s2id}/references?fields={S2_FIELDS}&limit={limit}", timeout=8.0, retry_429=True
            )
            if data is None:
                notes.append(f"{seed}: references pool unavailable (S2 rate limit)")
            rows = [d.get("citedPaper") for d in (data or {}).get("data", [])]
        else:
            # recommendations service has thin coverage — union it with the
            # citation-graph neighbourhood so well-connected papers always pool.
            half = max(limit // 2, 5)
            reco, citing, cited = await asyncio.gather(
                _s2_get(f"/papers/forpaper/{s2id}?fields={S2_FIELDS}&limit={half}",
                        timeout=8.0, base=S2_RECO_BASE, retry_429=True),
                _s2_get(f"/paper/{s2id}/citations?fields={S2_FIELDS}&limit={half}", timeout=8.0, retry_429=True),
                _s2_get(f"/paper/{s2id}/references?fields={S2_FIELDS}&limit={half}", timeout=8.0, retry_429=True),
            )
            missing = [name for name, d in (("recommendations", reco), ("citations", citing), ("references", cited)) if d is None]
            if missing:
                notes.append(f"{seed}: {', '.join(missing)} pool unavailable (S2 rate limit)")
            rows = list((reco or {}).get("recommendedPapers", []))
            rows += [d.get("citingPaper") for d in (citing or {}).get("data", [])]
            rows += [d.get("citedPaper") for d in (cited or {}).get("data", [])]
        for row in rows:
            if not row or not row.get("title"):
                continue
            hit = _paperhit_from_s2(row)
            pool.setdefault(hit["paperId"], hit)

    await asyncio.gather(*(expand(s) for s in seeds))

    results = list(pool.values())
    for h in results:
        h["score"] = round(keyword_score(f"{h['title']} {h['abstract']}", intent), 4)
    # intent always ranks (the MCP contract says "`intent` ranks candidates");
    # the optional rerank flag is accepted for protocol compatibility.
    if intent.strip():
        results.sort(key=lambda h: h["score"], reverse=True)
    total_pool = len(results)
    truncated = total_pool > k
    body: dict[str, Any] = {
        "success": True,
        "results": results[:k],
        "poolSize": total_pool,
        "truncated": truncated,
    }
    if notes:
        body["note"] = "; ".join(notes)
    return body
