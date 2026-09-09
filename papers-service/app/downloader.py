"""download_with_fallback 的 OA 仓储链实现（v2：照搬 paper-search-mcp server.py 的
_try_repository_fallback 设计，源码参考 uv-cache 里的 paper_search_mcp 0.1.4）。

链序（与 paper-search-mcp 一致）：
  1. source-native 直下（调用方决定，本模块不负责）
  2. OA 仓储: openaire → core → europepmc → pmc，按 DOI/标题搜出 pdf_url 再下载
  3. Unpaywall resolve_best_pdf_url（best_oa_location → oa_locations）
  4. Sci-Hub（可选，embed/iframe 解析；未配 SCIHUB_ENABLED=false 或未配 URL 跳过）

差异（有意为之）：
  - 不落盘：返回 PDF 字节，调用方（OWUI tool）直接上传 Knowledge
  - 下载后过标题身份闸（tool.py v2.9.6 同款逻辑），不匹配拒收
  - core/europepmc/pmc 检索复用 toolwrap 里 tool.py 的直连适配器（同款解析）
"""

from __future__ import annotations

import logging
import re

import anyio
import requests

from . import toolwrap
from .toolwrap import UA, tools

log = logging.getLogger("papers-service.downloader")

# 仓储链（照搬 paper-search-mcp 顺序：openaire 第 1 位）
_REPOSITORIES = ("openaire", "core", "europepmc", "pmc")

_PDF_MARKERS = (b"%PDF",)

# 下载点（CDN/出版商）对自标 UA 常见 403/429，统一用浏览器 UA；
# 公开学术 API 检索仍走 UA（toolwrap.UA）
_DL_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept": "application/pdf,application/octet-stream,text/html;q=0.8,*/*;q=0.5",
}


def _looks_like_pdf(data: bytes, url: str) -> bool:
    if data.startswith(_PDF_MARKERS):
        return True
    return url.lower().endswith(".pdf") and len(data) > 1000


def _fetch_pdf_bytes(url: str, timeout: int = 90) -> bytes | None:
    try:
        r = requests.get(url, timeout=timeout, headers=_DL_HEADERS, allow_redirects=True)
        if r.status_code >= 400 or not r.content:
            return None
        if _looks_like_pdf(r.content, r.url or url):
            return r.content
        return None
    except Exception as e:
        log.info("pdf fetch failed %s: %s", url[:100], e)
        return None


def pdf_bytes_to_text(data: bytes) -> str | None:
    """PDF 字节 → 全文文本（提取失败返回 None，调用方自行降级）。"""
    try:
        text = toolwrap.tool_mod.Tools._pdf_to_text(data)
        return (text or "").strip() or None
    except Exception as e:
        log.info("pdf text extraction failed: %s", str(e)[:120])
        return None


def _gate(data: bytes, title: str, via: str) -> bool:
    """身份闸：全文页 token 与标题覆盖率 ≥60% 才认。提取失败（扫描版）不拦。"""
    if not (title or "").strip():
        return True
    text = pdf_bytes_to_text(data)
    if not text:
        return True
    ok, ratio = title_in_pdf(text, title)
    if ok:
        return True
    log.info("gate rejected %s (ratio %.2f)", via[:80], ratio)
    return False


async def _repository_search_pdf_url(repo: str, query: str) -> str:
    """单仓储单查询：返回第一个带 pdf_url 的结果（复用 tool.py 直连适配器的解析）。"""
    t = tools()
    if repo == "openaire":
        papers = await t._openaire_search(query, 3)
    elif repo == "core":
        papers = await t._core_search(query, 3)
    elif repo == "europepmc":
        papers = await t._europepmc_search(query, 3)
    elif repo == "pmc":
        papers = await t._pmc_search(query, 3)
    else:
        return ""
    for p in papers or []:
        pdf_url = str(p.get("pdf_url") or "").strip()
        if pdf_url.startswith("http"):
            return pdf_url
    return ""


async def _repository_fallback(doi: str, title: str) -> tuple[bytes, str] | None:
    """照搬 _try_repository_fallback：每仓储 × 每查询候选（doi 先、标题后），
    搜到 pdf_url 就下载+过身份闸，通过即返回（bytes, via）；不匹配继续试下一级。"""
    query_candidates = [q for q in ((doi or "").strip(), (title or "").strip()) if q]
    if not query_candidates:
        return None
    for repo in _REPOSITORIES:
        for q in query_candidates:
            try:
                pdf_url = await _repository_search_pdf_url(repo, q)
            except Exception as e:
                log.info("repo %s query failed: %s", repo, str(e)[:120])
                continue
            if not pdf_url:
                continue
            data = await anyio.to_thread.run_sync(_fetch_pdf_bytes, pdf_url, 90)
            if data and _gate(data, title, f"repository:{repo}({pdf_url[:80]})"):
                return data, f"repository:{repo}"
    return None


# --- Unpaywall（照搬 resolve_best_pdf_url）------------------------------------------

async def _unpaywall_pdf_url(doi: str) -> str:
    email = tools().valves.__dict__.get("unpaywall_email") or "paper-search@openwebui.local"

    def _f():
        r = requests.get(
            f"https://api.unpaywall.org/v2/{doi.strip()}",
            params={"email": email},
            headers={"Accept": "application/json"},
            timeout=20,
        )
        if r.status_code != 200:
            return ""
        data = r.json()
        best = data.get("best_oa_location") or {}
        url = best.get("url_for_pdf") or best.get("url") or ""
        if url:
            return url
        for loc in data.get("oa_locations") or []:
            if isinstance(loc, dict):
                c = loc.get("url_for_pdf") or loc.get("url") or ""
                if c:
                    return c
        return ""

    return await anyio.to_thread.run_sync(_f)


# --- Sci-Hub（照搬 SciHubFetcher._get_direct_url 的 embed/iframe/链接解析）-----------

_SCIHUB_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def _resolve_relative(src: str, base: str) -> str:
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("/"):
        return base.rstrip("/") + src
    return src


def _scihub_direct_url(base_url: str, identifier: str) -> str:
    try:
        r = requests.get(f"{base_url.rstrip('/')}/{identifier}",
                         headers=_SCIHUB_HEADERS, timeout=25, verify=False)
        if r.status_code != 200 or "article not found" in r.text.lower():
            return ""
        html = r.text
        # embed[type=application/pdf]（现代 sci-hub 最常见）→ iframe → a[href*=pdf]
        m = re.search(
            r"<embed[^>]+type=[\"']application/pdf[\"'][^>]+src=[\"']([^\"']+)[\"']", html, re.I
        ) or re.search(
            r"<embed[^>]+src=[\"']([^\"']+)[\"'][^>]+type=[\"']application/pdf[\"']", html, re.I
        )
        if m:
            return _resolve_relative(m.group(1), base_url)
        m = re.search(r"<iframe[^>]+src=[\"']([^\"']+)[\"']", html, re.I)
        if m and (".pdf" in m.group(1).lower() or "/pdf" in m.group(1).lower()):
            return _resolve_relative(m.group(1), base_url)
        for m in re.finditer(r"<a[^>]+href=[\"']([^\"']+)[\"']", html, re.I):
            href = m.group(1)
            if ".pdf" in href.lower():
                return _resolve_relative(href, base_url)
        return ""
    except Exception as e:
        log.info("scihub resolve failed: %s", str(e)[:120])
        return ""


async def _scihub_pdf(base_url: str, doi: str, title: str, paper_id: str) -> tuple[bytes, str] | None:
    identifier = (doi or "").strip() or (title or "").strip() or (paper_id or "").strip()
    if not identifier:
        return None
    direct = await anyio.to_thread.run_sync(_scihub_direct_url, base_url, identifier)
    if not direct:
        return None
    data = await anyio.to_thread.run_sync(_fetch_pdf_bytes, direct, 60)
    if data and _gate(data, title, f"scihub({direct[:80]})"):
        return data, "scihub"
    return None


# --- 标题身份闸（与 tool.py v2.9.6 _title_in_pdf 同逻辑）-----------------------------

_TITLE_STOP = frozenset(
    ("a an the of in on for with and or to is are was were be by at as from "
     "its their his her our your via using based study research analysis").split()
)


def _title_tokens(s: str) -> set:
    out = set()
    for w in re.findall(r"[0-9a-z]+|[一-鿿]", (s or "").lower()):
        if len(w) > 1 or w.isdigit() or re.match(r"[一-鿿]", w):
            if w not in _TITLE_STOP:
                out.add(w)
    return out


def title_in_pdf(pdf_text: str, title: str) -> tuple[bool, float]:
    want = _title_tokens(title)
    if not want:
        return True, 1.0
    got = _title_tokens((pdf_text or "")[:6000])
    if not got:
        return True, 0.0  # 提取不出 token（扫描版）→ 不拦
    hit = len(want & got) / len(want)
    return hit >= 0.6, hit


# --- 主入口 --------------------------------------------------------------------------

async def download_with_fallback(
    source: str, paper_id: str, doi: str = "", title: str = "",
    use_scihub: bool = True, scihub_base_url: str = "",
) -> tuple[bytes | None, str, list[str]]:
    """返回 (pdf_bytes | None, via, attempt_errors)。每个下载点过身份闸，不匹配
    自动落下一级（paper-search-mcp 原版没有闸，下错文档是我们踩过的坑）。"""
    errors: list[str] = []

    # 1. source-native：直链型源 → PDF 字节
    native_url = ""
    src = (source or "").strip().lower()
    pid = toolwrap._strip_prefix(paper_id)
    if src == "arxiv" and re.match(r"^\d{4}\.\d{4,5}$", pid):
        native_url = f"https://arxiv.org/pdf/{pid}"
    elif src == "iacr" and re.match(r"^\d{4}/\d+$", pid):
        native_url = f"https://eprint.iacr.org/{pid}.pdf"
    elif src == "biorxiv" and re.match(r"^10\.1101/", pid):
        native_url = f"https://www.biorxiv.org/content/{pid}.full.pdf"
    if native_url:
        data = await anyio.to_thread.run_sync(_fetch_pdf_bytes, native_url, 90)
        if data and _gate(data, title, f"native:{src}"):
            return data, f"native:{src}", errors
        if not data:
            errors.append(f"native({src}): download failed")

    # 2. OA 仓储链（openaire → core → europepmc → pmc，闸内嵌）
    try:
        repo = await _repository_fallback(doi, title)
        if repo:
            return repo[0], repo[1], errors
    except Exception as e:
        errors.append(f"repositories: {e}")

    # 3. Unpaywall（闸内嵌）
    if doi.strip():
        try:
            url = await _unpaywall_pdf_url(doi)
        except Exception as e:
            url = ""
            errors.append(f"unpaywall: {e}")
        if url:
            data = await anyio.to_thread.run_sync(_fetch_pdf_bytes, url, 90)
            if data and _gate(data, title, f"unpaywall({url[:80]})"):
                return data, "unpaywall", errors
            if not data:
                errors.append("unpaywall: resolved OA URL but download failed")
            else:
                errors.append(f"unpaywall: downloaded content failed title gate ({url[:60]})")
        else:
            errors.append("unpaywall: no OA URL found")
    else:
        errors.append("unpaywall: DOI not provided")

    # 4. Sci-Hub（可选，闸内嵌）
    base = (scihub_base_url or "").strip()
    if use_scihub and base:
        try:
            sh = await _scihub_pdf(base, doi, title, paper_id)
            if sh:
                return sh[0], sh[1], errors
        except Exception as e:
            errors.append(f"scihub: {e}")
        errors.append("scihub: no PDF resolved or gate rejected")
    return None, "", errors
