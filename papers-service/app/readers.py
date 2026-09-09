"""read_{source}_paper 的轻量独立实现：直连公开全文端点，不依赖 mcpo/paper-search-mcp。

返回 None（或抛异常，路由层兜底）→ 404 → research-proxy 落 reach/read_url
（jina 风格抓取）兜底，所以这里只做"确定能做对"的源：
  arxiv/iacr/hal   PDF 直链 → pymupdf 提取
  semantic         S2 openAccessPdf → PDF；无 OA 时退化为 著录+abstract
  pubmed           efetch abstract（纯文本）
  pmc/europepmc    Europe PMC fullTextXML（仅 PMC id）→ 去标签
  crossref/openalex 元数据级 markdown（著录 + abstract）
"""

from __future__ import annotations

import logging
import re

import anyio
import requests

from . import toolwrap
from .toolwrap import UA, tools

log = logging.getLogger("papers-service.readers")


def _pdf_text(data: bytes) -> str:
    return toolwrap.tool_mod.Tools._pdf_to_text(data)


def _strip_known_prefix(pid: str) -> str:
    pfx, _, rest = (pid or "").strip().partition(":")
    if rest and pfx.lower() in (
        "arxiv", "pmid", "pmc", "hal", "iacr", "semantic", "doi",
        "openalex", "crossref", "europepmc",
    ):
        return rest
    return (pid or "").strip()


def _fetch_pdf(url: str, timeout: int = 60) -> str | None:
    try:
        r = requests.get(url, timeout=timeout, headers=UA)
        r.raise_for_status()
        return _pdf_text(r.content) or None
    except Exception as e:
        log.info("pdf fetch failed %s: %s", url, e)
        return None


async def read_arxiv(pid: str) -> str | None:
    pid = re.sub(r"v\d+$", "", _strip_known_prefix(pid))
    if not re.match(r"^\d{4}\.\d{4,5}$", pid) and not re.match(r"^[a-z-]+/\d{7}$", pid):
        return None
    return await anyio.to_thread.run_sync(_fetch_pdf, f"https://arxiv.org/pdf/{pid}")


async def read_iacr(pid: str) -> str | None:
    pid = _strip_known_prefix(pid)
    if not re.match(r"^\d{4}/\d+$", pid):
        return None
    return await anyio.to_thread.run_sync(_fetch_pdf, f"https://eprint.iacr.org/{pid}.pdf")


async def read_semantic(pid: str) -> str | None:
    pid = _strip_known_prefix(pid)
    key = tools().valves.semantic_api_key

    def _f():
        headers = dict(UA)
        if key:
            headers["x-api-key"] = key
        # S2 /paper/{id} 接受：裸 40-hex paperId、CorpusId:、ARXIV:、DOI:、PMID: 等
        if re.match(r"^10\.\d{4,9}/", pid):
            sid = f"DOI:{pid}"
        elif re.match(r"^\d{4}\.\d{4,5}$", pid):
            sid = f"ARXIV:{pid}"
        elif pid.isdigit():
            sid = f"CorpusId:{pid}"
        else:
            sid = pid
        r = requests.get(
            f"https://api.semanticscholar.org/graph/v1/paper/{sid}",
            params={"fields": "title,abstract,authors,year,venue,openAccessPdf,externalIds"},
            headers=headers,
            timeout=15,
        )
        if r.status_code != 200:
            return None
        m = r.json()
        title = (m.get("title") or "").strip()
        if not title:
            return None
        authors = "; ".join(a.get("name", "") for a in (m.get("authors") or []) if a.get("name"))
        header = (
            f"# {title}\n\n作者: {authors}\n年份: {m.get('year') or ''} "
            f"{m.get('venue') or ''}\n"
        )
        oa = (m.get("openAccessPdf") or {}).get("url") or ""
        if oa:
            text = _fetch_pdf(oa)
            if text:
                return header + "\n" + text
        if m.get("abstract"):
            return header + "\n" + m["abstract"]
        return None

    return await anyio.to_thread.run_sync(_f)


async def read_pubmed(pid: str) -> str | None:
    pid = _strip_known_prefix(pid)
    if not pid.isdigit():
        return None
    api_key = tools().valves.ncbi_api_key

    def _f():
        params = {"db": "pubmed", "id": pid, "rettype": "abstract", "retmode": "text"}
        if api_key:
            params["api_key"] = api_key
        r = requests.get(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
            params=params,
            timeout=30,
            headers=UA,
        )
        if r.status_code != 200:
            return None
        return r.text.strip() or None

    return await anyio.to_thread.run_sync(_f)


def _xml_to_text(xml: str) -> str:
    # 粗提取 JATS fullTextXML：去掉引用标记/图/公式，其余去标签留正文
    xml = re.sub(r"(?s)<(xref|graphic|inline-formula|disp-formula)[^>]*>.*?</\1>", " ", xml)
    xml = re.sub(r"(?s)<(xref|graphic|inline-formula|disp-formula)[^>]*/>", " ", xml)
    text = re.sub(r"<[^>]+>", " ", xml)
    return re.sub(r"\s+", " ", text).strip()


async def _europepmc_fulltext(pmcid: str) -> str | None:
    def _f():
        r = requests.get(
            f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML",
            timeout=45,
            headers=UA,
        )
        if r.status_code != 200 or "<article" not in r.text[:3000]:
            return None
        return _xml_to_text(r.text) or None

    return await anyio.to_thread.run_sync(_f)


async def read_pmc(pid: str) -> str | None:
    pid = _strip_known_prefix(pid)
    if not pid.upper().startswith("PMC"):
        return None
    return await _europepmc_fulltext(pid)


async def read_europepmc(pid: str) -> str | None:
    pid = _strip_known_prefix(pid)
    if not pid.upper().startswith("PMC"):
        return None  # pmid 形态读不了全文 → 404 落 reach
    return await _europepmc_fulltext(pid)


async def read_hal(pid: str) -> str | None:
    pid = _strip_known_prefix(pid)

    def _f():
        r = requests.get(
            "https://api.archives-ouvertes.fr/search/",
            params={
                "q": f"halId_s:{pid}",
                "fl": "halId_s,title_s,abstract_s,authFullName_s,producedDate_s,uri_s,fileMain_s",
                "wt": "json",
            },
            timeout=20,
            headers=UA,
        )
        if r.status_code != 200:
            return None
        docs = (r.json().get("response") or {}).get("docs") or []
        if not docs:
            return None
        d = docs[0]
        header = (
            f"# {d.get('title_s', '')}\n\n"
            f"作者: {'; '.join(d.get('authFullName_s') or [])}\n"
            f"日期: {d.get('producedDate_s', '')}\n链接: {d.get('uri_s', '')}\n"
        )
        fm = d.get("fileMain_s") or ""
        if fm:
            text = _fetch_pdf(fm)
            if text:
                return header + "\n" + text
        ab = d.get("abstract_s") or ""
        return (header + "\n" + ab) if ab else None

    return await anyio.to_thread.run_sync(_f)


async def read_crossref(pid: str) -> str | None:
    pid = _strip_known_prefix(pid)
    item = await toolwrap.crossref_by_doi(pid)
    if not item:
        return None
    text = (
        f"# {item['title']}\n\n作者: {item['authors']}\n日期: {item['published_date']}\n"
        f"DOI: {item['doi']}\n被引: {item['citations']}\n链接: {item['url']}\n\n{item['abstract']}"
    ).strip()
    return text if len(text) > 100 else None


async def read_openalex(pid: str) -> str | None:
    pid = _strip_known_prefix(pid)
    if pid.startswith("10."):
        wid = requests.utils.quote(f"https://doi.org/{pid}", safe="")
    else:
        wid = pid  # W... 或裸 openalex id

    def _f():
        r = requests.get(f"https://api.openalex.org/works/{wid}", timeout=15, headers=UA)
        if r.status_code != 200:
            return None
        m = r.json()
        title = (m.get("title") or "").strip()
        if not title:
            return None
        authors = "; ".join(
            (a.get("author") or {}).get("display_name", "")
            for a in (m.get("authorships") or [])
        )
        abstract = toolwrap.tool_mod.Tools._openalex_abstract(
            m.get("abstract_inverted_index")
        )
        loc = m.get("primary_location") or {}
        url = loc.get("landing_page_url") or m.get("id") or ""
        text = (
            f"# {title}\n\n作者: {authors}\n日期: {m.get('publication_date') or ''}\n"
            f"链接: {url}\n\n{abstract}"
        ).strip()
        return text if len(text) > 100 else None

    return await anyio.to_thread.run_sync(_f)


READERS = {
    "arxiv": read_arxiv,
    "semantic": read_semantic,
    "pubmed": read_pubmed,
    "pmc": read_pmc,
    "europepmc": read_europepmc,
    "hal": read_hal,
    "iacr": read_iacr,
    "crossref": read_crossref,
    "openalex": read_openalex,
}
