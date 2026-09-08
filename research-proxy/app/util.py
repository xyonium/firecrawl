"""Shared helpers: paper id parsing, text chunking, BM25-lite scoring."""

from __future__ import annotations

import math
import re
from collections import Counter

# --- paper id parsing -------------------------------------------------------

# Canonical ids we emit and accept: arxiv:2301.12345[vN], doi:10.xxxx/...,
# pmid:12345, pmcid:PMC12345, s2:<corpusId>, plus source-prefixed ids from
# paper-search-mcp such as pubmed:12345 / crossref:<doi> / hal:hal-xxx.
_ID_RE = re.compile(r"^([a-z0-9]+):(.+)$", re.IGNORECASE)

# S2 graph API accepts these namespace prefixes for paper lookup.
_S2_PREFIX = {
    "arxiv": "ARXIV",
    "doi": "DOI",
    "pmid": "PMID",
    "pmcid": "PMCID",
    "pubmed": "PMID",
    "s2": "CorpusId",
    "semantic": "CorpusId",
}


def parse_paper_id(raw: str) -> tuple[str, str]:
    """Split 'arxiv:1234.5' -> ('arxiv', '1234.5'). Unknown -> ('', raw)."""
    m = _ID_RE.match(raw.strip())
    if not m:
        return "", raw.strip()
    return m.group(1).lower(), m.group(2).strip()


def s2_lookup_id(raw: str) -> str | None:
    """Map a canonical paperId to a Semantic Scholar lookup id, or None."""
    ns, value = parse_paper_id(raw)
    if ns == "doi" and value.lower().startswith("10."):
        return f"DOI:{value}"
    prefix = _S2_PREFIX.get(ns)
    if prefix:
        return f"{prefix}:{value}"
    # bare arxiv-style id without a namespace
    if not ns and re.match(r"^\d{4}\.\d{4,5}(v\d+)?$", value):
        return f"ARXIV:{value}"
    return None


# --- text chunking + scoring ------------------------------------------------

_TERM_RE = re.compile(r"[a-z0-9]{3,}")


def terms(text: str) -> list[str]:
    return _TERM_RE.findall(text.lower())


def chunk_text(text: str, target: int = 1400, overlap_para: bool = True) -> list[str]:
    """Pack blank-line-separated paragraphs into ~target-char chunks."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    buf = ""
    for p in paras:
        if buf and len(buf) + len(p) > target:
            chunks.append(buf)
            buf = p if overlap_para else ""
        buf = f"{buf}\n\n{p}" if buf else p
        # a single huge paragraph gets hard-split on sentence-ish boundaries
        while len(buf) > target * 2:
            cut = buf.rfind(". ", 0, target)
            if cut < target // 2:
                cut = target
            chunks.append(buf[: cut + 1])
            buf = buf[cut + 1 :]
    if buf:
        chunks.append(buf)
    return chunks


def rank_passages(chunks: list[str], query: str, k: int) -> list[dict]:
    """BM25-lite: tf * log(1 + N/df), length-normalised. Returns top-k dicts."""
    q_terms = terms(query)
    if not q_terms or not chunks:
        return [{"text": c, "score": 0.0} for c in chunks[:k]]
    n = len(chunks)
    df: Counter[str] = Counter()
    tfs: list[Counter[str]] = []
    for c in chunks:
        tf = Counter(terms(c))
        tfs.append(tf)
        for t in tf:
            df[t] += 1
    scored = []
    for c, tf in zip(chunks, tfs):
        score = sum(
            tf.get(t, 0) * math.log(1 + n / (1 + df[t])) for t in set(q_terms)
        )
        score /= math.sqrt(max(len(c), 1) / 1000)
        scored.append({"text": c, "score": round(score, 3)})
    scored.sort(key=lambda x: x["score"], reverse=True)
    top = [s for s in scored[:k] if s["score"] > 0]
    return top or [{"text": c, "score": 0.0} for c in chunks[:k]]


def keyword_score(text: str, query: str) -> float:
    """Cheap relevance score for intent-based reranking."""
    qt = set(terms(query))
    if not qt:
        return 0.0
    tt = Counter(terms(text))
    return sum(tt.get(t, 0) for t in qt) / len(qt)
