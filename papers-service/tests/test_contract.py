"""papers-service 端点契约测试：对齐 research-proxy（shim）对 mcpo/paper-search-mcp
的消费形状。全部 mock 网络/tool 方法，不打真实外部服务。

需要在本地能找到 tool.py（默认 /home/eli/paper-search/tool.py，或用
PAPER_SEARCH_TOOL_PATH 指定）；找不到则整文件 skip。
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TOOL = os.environ.get("PAPER_SEARCH_TOOL_PATH", "/home/eli/paper-search/tool.py")
if not os.path.exists(_TOOL):
    pytest.skip("tool.py not present (set PAPER_SEARCH_TOOL_PATH)", allow_module_level=True)
os.environ["PAPER_SEARCH_TOOL_PATH"] = _TOOL

from fastapi.testclient import TestClient  # noqa: E402

from app import main, readers, toolwrap  # noqa: E402

client = TestClient(main.app)

_SAMPLE = {
    "title": "Graph Neural Networks: A Review",
    "authors": "Zhang; Li",
    "published_date": "2026-01-02",
    "abstract": "We review GNNs.",
    "paper_id": "arxiv:2401.00001",
    "doi": "10.1234/gnn",
    "source": "arxiv",
    "pdf_url": "https://arxiv.org/pdf/2401.00001",
    "citations": 42,
    "url": "https://arxiv.org/abs/2401.00001",
}


def _patch_search(monkeypatch, method, result=None, exc=None, calls=None):
    async def fake(query, limit, *__a, **__kw):
        if calls is not None:
            calls.append(method)
        if exc is not None:
            raise exc
        return [dict(result)]

    monkeypatch.setattr(toolwrap.tools(), method, fake)


# --- search -------------------------------------------------------------------


def test_search_returns_papers_and_strips_prefix(monkeypatch):
    _patch_search(monkeypatch, "_arxiv_search", result=_SAMPLE)
    r = client.post("/papers/search_arxiv", json={"query": "gnn", "max_results": 3})
    assert r.status_code == 200
    papers = r.json()["papers"]
    assert len(papers) == 1
    p = papers[0]
    # 前缀剥掉（shim 会自行加 source 前缀）
    assert p["paper_id"] == "2401.00001"
    # shim 的 _paperhit_from_mcp 消费的键都在
    for k in ("title", "authors", "published_date", "abstract", "paper_id",
              "doi", "source", "pdf_url", "url"):
        assert k in p


def test_search_unknown_source_404():
    r = client.post("/papers/search_acm", json={"query": "x", "max_results": 3})
    assert r.status_code == 404
    assert "unknown search source" in r.json()["detail"]


def test_search_adapter_error_maps_502(monkeypatch):
    _patch_search(monkeypatch, "_arxiv_search", exc=RuntimeError("boom"))
    r = client.post("/papers/search_arxiv", json={"query": "x", "max_results": 3})
    assert r.status_code == 502
    assert "boom" in r.json()["detail"]


def test_search_query_required():
    r = client.post("/papers/search_arxiv", json={"max_results": 3})
    assert r.status_code == 400


def test_search_ieee_without_key_returns_empty():
    toolwrap.tools().valves.ieee_apikey = ""
    r = client.post("/papers/search_ieee", json={"query": "x", "max_results": 3})
    assert r.status_code == 200
    assert r.json()["papers"] == []


# --- google_scholar 链 ----------------------------------------------------------


def test_scholar_firecrawl_first_no_fallback(monkeypatch):
    calls = []
    _patch_search(monkeypatch, "_google_scholar_firecrawl_search",
                  result={**_SAMPLE, "paper_id": "scholar:123", "source": "google_scholar"},
                  calls=calls)
    _patch_search(monkeypatch, "_google_scholar_tavily_search",
                  result=_SAMPLE, calls=calls)
    _patch_search(monkeypatch, "_google_scholar_actor_search",
                  result=_SAMPLE, calls=calls)
    t = toolwrap.tools()
    monkeypatch.setattr(t, "_firecrawl_base", lambda *a: "http://fc")
    monkeypatch.setattr(t, "_tavily_base", lambda *a: "http://tav")
    monkeypatch.setattr(t, "_apify_rotator_base", lambda: "http://rot")
    r = client.post("/papers/search_google_scholar",
                    json={"query": "gnn", "max_results": 3})
    assert r.status_code == 200
    assert calls == ["_google_scholar_firecrawl_search"]
    assert r.json()["papers"][0]["paper_id"] == "123"


def test_scholar_falls_back_to_tavily_then_actor(monkeypatch):
    calls = []
    _patch_search(monkeypatch, "_google_scholar_firecrawl_search",
                  exc=RuntimeError("captcha"), calls=calls)
    _patch_search(monkeypatch, "_google_scholar_tavily_search", result=None,
                  calls=calls)  # 空结果 → 继续落
    _patch_search(monkeypatch, "_google_scholar_actor_search",
                  result={**_SAMPLE, "paper_id": "scholar:9"}, calls=calls)
    t = toolwrap.tools()
    monkeypatch.setattr(t, "_firecrawl_base", lambda *a: "http://fc")
    monkeypatch.setattr(t, "_tavily_base", lambda *a: "http://tav")
    monkeypatch.setattr(t, "_apify_rotator_base", lambda: "http://rot")

    async def _empty(*a, **k):
        calls.append("_google_scholar_tavily_search")
        return []

    monkeypatch.setattr(t, "_google_scholar_tavily_search", _empty)
    r = client.post("/papers/search_google_scholar",
                    json={"query": "gnn", "max_results": 3})
    assert r.status_code == 200
    assert calls == ["_google_scholar_firecrawl_search",
                     "_google_scholar_tavily_search",
                     "_google_scholar_actor_search"]
    assert r.json()["papers"][0]["paper_id"] == "9"


def test_scholar_unconfigured_returns_empty(monkeypatch):
    t = toolwrap.tools()
    monkeypatch.setattr(t, "_firecrawl_base", lambda *a: "")
    monkeypatch.setattr(t, "_tavily_base", lambda *a: "")
    monkeypatch.setattr(t, "_apify_rotator_base", lambda: "")
    r = client.post("/papers/search_google_scholar",
                    json={"query": "gnn", "max_results": 3})
    assert r.status_code == 200
    assert r.json()["papers"] == []


# --- crossref by doi ------------------------------------------------------------


def _fake_resp(status=200, payload=None):
    class R:
        status_code = status

        def json(self):
            return payload

    return R()


def test_crossref_by_doi_shape(monkeypatch):
    payload = {"message": {
        "title": ["Some Paper"],
        "author": [{"given": "Ann", "family": "Lee"}],
        "published-print": {"date-parts": [[2025, 3, 1]]},
        "DOI": "10.1/abc",
        "URL": "https://doi.org/10.1/abc",
        "abstract": "<jats:p>Hello abstract.</jats:p>",
        "is-referenced-by-count": 7,
    }}
    monkeypatch.setattr(toolwrap.requests, "get",
                        lambda *a, **k: _fake_resp(200, payload))
    r = client.post("/papers/get_crossref_paper_by_doi", json={"doi": "10.1/abc"})
    assert r.status_code == 200
    item = r.json()  # 裸 dict（shim: data.get("result") or data）
    assert item["title"] == "Some Paper"
    assert item["doi"] == "10.1/abc"
    assert item["published_date"] == "2025-03-01"
    assert item["abstract"] == "Hello abstract."  # JATS 标签已剥


def test_crossref_by_doi_404(monkeypatch):
    monkeypatch.setattr(toolwrap.requests, "get", lambda *a, **k: _fake_resp(404))
    r = client.post("/papers/get_crossref_paper_by_doi", json={"doi": "10.1/none"})
    assert r.status_code == 404


# --- read -----------------------------------------------------------------------


def _make_pdf(text: str) -> bytes:
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    return doc.tobytes()


def test_read_arxiv_pdf(monkeypatch):
    body = "papers-service full text " * 20  # >200 字符，满足 shim 的采纳阈值
    monkeypatch.setattr(
        readers.requests, "get",
        lambda *a, **k: type("R", (), {
            "status_code": 200,
            "content": _make_pdf(body),
            "raise_for_status": lambda self: None,
        })(),
    )
    r = client.post("/papers/read_arxiv_paper", json={"paper_id": "2401.00001"})
    assert r.status_code == 200
    assert body.split()[0] in r.json()["result"]


def test_read_arxiv_bad_id_404(monkeypatch):
    r = client.post("/papers/read_arxiv_paper", json={"paper_id": "not-an-id"})
    assert r.status_code == 404


def test_read_unsupported_source_404():
    r = client.post("/papers/read_base_paper", json={"paper_id": "x"})
    assert r.status_code == 404
    assert "not supported" in r.json()["detail"]


def test_read_fetch_failure_404(monkeypatch):
    def boom(*a, **k):
        raise readers.requests.ConnectionError("down")

    monkeypatch.setattr(readers.requests, "get", boom)
    r = client.post("/papers/read_arxiv_paper", json={"paper_id": "2401.00001"})
    assert r.status_code == 404


def test_unknown_tool_404():
    r = client.post("/papers/download_arxiv", json={"paper_id": "x"})
    assert r.status_code == 404


# --- misc -----------------------------------------------------------------------


def test_healthz():
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "arxiv" in body["searchSources"]
    assert "google_scholar" in body["searchSources"]
    assert "doaj" in body["searchSources"]
    assert "arxiv" in body["readSources"]
    assert re.match(r"^\d+\.\d+\.\d+$", body["toolVersion"]), body["toolVersion"]
