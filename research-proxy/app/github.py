"""GitHub backends: code search (authenticated, token pool) and the legacy
/v2/research/github repo-readme endpoint.

Rate limits (verified): code search = 10 req/min per token (separate bucket),
general search = 30 req/min, core = 5000 req/hr — all per-token, not per-IP.
The pool round-robins tokens and cools one down on 403/429 + Retry-After.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import httpx

GH_API = "https://api.github.com"
CODE_SEARCH_MIN_INTERVAL = 6.2  # seconds between code-search calls per token

_tokens = [t.strip() for t in os.environ.get("GITHUB_TOKENS", "").split(",") if t.strip()]

_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=5.0))
    return _client


class TokenPool:
    def __init__(self, tokens: list[str]) -> None:
        self._state = [
            {"token": t, "cooldown_until": 0.0, "next_at": 0.0} for t in tokens
        ]
        self._lock = asyncio.Lock()
        self._idx = 0

    @property
    def empty(self) -> bool:
        return not self._state

    async def acquire(self) -> dict | None:
        """Next available token; applies the per-token code-search spacing."""
        async with self._lock:
            now = time.monotonic()
            for _ in range(len(self._state)):
                st = self._state[self._idx % len(self._state)]
                self._idx += 1
                if st["cooldown_until"] > now:
                    continue
                wait = st["next_at"] - now
                if wait > 0:
                    await asyncio.sleep(wait)
                st["next_at"] = time.monotonic() + CODE_SEARCH_MIN_INTERVAL
                return st
            return None

    def penalize(self, st: dict, retry_after: float | None) -> None:
        st["cooldown_until"] = time.monotonic() + max(retry_after or 60.0, 30.0)


_pool = TokenPool(_tokens)


async def _gh_get(path: str, params: dict[str, str], *, space: bool) -> httpx.Response:
    """GET with token rotation: up to len(tokens)+1 attempts across the pool."""
    last: httpx.Response | None = None
    for _ in range(max(len(_tokens), 1) + 1):
        if _pool.empty:
            r = await client().get(f"{GH_API}{path}", params=params, headers=_headers(None))
            if r.status_code in (401, 403):
                raise GitHubUnavailable("no GITHUB_TOKENS configured and unauthenticated limit hit")
            return r
        st = await _pool.acquire() if space else _best_token()
        if st is None:
            raise GitHubUnavailable("all GitHub tokens cooling down; retry shortly")
        r = await client().get(f"{GH_API}{path}", params=params, headers=_headers(st["token"]))
        if r.status_code in (403, 429):
            retry_after = None
            try:
                retry_after = float(r.headers.get("retry-after", ""))
            except ValueError:
                pass
            _pool.penalize(st, retry_after)
            last = r
            continue
        return r
    raise GitHubUnavailable(f"GitHub rate limited on all tokens (last={last.status_code if last else '?'})")


def _best_token() -> dict:
    now = time.monotonic()
    available = [s for s in _pool._state if s["cooldown_until"] <= now]
    if not available:
        raise GitHubUnavailable("all GitHub tokens cooling down; retry shortly")
    _pool._idx = (_pool._idx + 1) % len(available)
    return available[_pool._idx % len(available)]


def _headers(token: str | None, text_match: bool = False) -> dict[str, str]:
    accept = "application/vnd.github.text-match+json" if text_match else "application/vnd.github+json"
    h = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


class GitHubUnavailable(Exception):
    pass


# --- code search ------------------------------------------------------------


async def code_search(
    query: str,
    k: int,
    language: str | None,
    repos: list[str] | None,
    license_: str | None,
    max_passages: int,
    archived: bool | None,
    fork: bool | None,
) -> dict[str, Any]:
    if _pool.empty:
        raise GitHubUnavailable("GITHUB_TOKENS not configured: GitHub code search requires auth")
    q = query
    if language:
        q += f" language:{language}"
    for repo in (repos or [])[:5]:
        q += f" repo:{repo}"
    if license_:
        q += f" license:{license_}"
    if archived is True:
        q += " is:archived"
    if fork is True:
        q += " is:fork"
    r: httpx.Response | None = None
    for _ in range(min(len(_tokens), 3)):
        st = await _pool.acquire()
        if st is None:
            raise GitHubUnavailable("all GitHub tokens cooling down; retry shortly")
        r = await client().get(
            f"{GH_API}/search/code",
            params={"q": q, "per_page": str(min(k, 30))},
            headers=_headers(st["token"], text_match=True),
        )
        if r.status_code in (403, 429):
            try:
                retry_after = float(r.headers.get("retry-after", ""))
            except ValueError:
                retry_after = None
            _pool.penalize(st, retry_after)
            continue
        break
    assert r is not None
    if r.status_code in (403, 429):
        raise GitHubUnavailable(f"GitHub code search rate limited ({r.status_code})")
    if r.status_code == 422:
        return {"success": True, "results": [], "note": f"query rejected by GitHub: {r.json().get('message', '')[:200]}"}
    r.raise_for_status()
    items = r.json().get("items", [])[:k]
    results = []
    for it in items:
        repo = it.get("repository") or {}
        matches = it.get("text_matches") or []
        passages = [
            {"text": m.get("fragment", ""), "citation_url": it.get("html_url", "")}
            for m in matches[:max_passages]
            if m.get("fragment")
        ]
        if not passages:
            passages = [{"text": f"{repo.get('full_name', '')}/{it.get('path', '')}", "citation_url": it.get("html_url", "")}]
        results.append(
            {
                "id": it.get("html_url", ""),
                "url": it.get("html_url", ""),
                "title": f"{repo.get('full_name', '')}:{it.get('path', '')}",
                "passages": passages,
                "license": (repo.get("license") or {}).get("spdx_id"),
                "repo": repo.get("full_name"),
                "language": it.get("language") or None,
            }
        )
    # code-search repo payloads carry no license — enrich from /repos/{full}
    # (core quota, 5000/hr per token; capped at 10 lookups)
    need = {res["repo"] for res in results if res["repo"] and not res["license"]}
    if need and not _pool.empty:
        async def lic(full: str) -> tuple[str, str | None]:
            try:
                rr = await client().get(
                    f"{GH_API}/repos/{full}", headers=_headers(_best_token()["token"]), timeout=6.0
                )
                if rr.status_code == 200:
                    spdx = (rr.json().get("license") or {}).get("spdx_id")
                    return full, None if spdx in (None, "NOASSERTION") else spdx
            except (httpx.HTTPError, GitHubUnavailable):
                pass
            return full, None

        for full, spdx in await asyncio.gather(*(lic(f) for f in list(need)[:10])):
            if spdx:
                for res in results:
                    if res["repo"] == full:
                        res["license"] = spdx
    for res in results:
        if res["license"] in (None, "NOASSERTION"):
            res["license"] = None
    return {"success": True, "results": results}


# --- legacy /v2/research/github (repo readme search) ------------------------


async def github_legacy_search(query: str, k: int) -> dict[str, Any]:
    r = await _gh_get(
        "/search/repositories",
        {"q": query, "per_page": str(min(k, 10))},
        space=False,
    )
    if r.status_code != 200:
        raise GitHubUnavailable(f"GitHub repo search failed ({r.status_code})")
    repos = r.json().get("items", [])[:k]

    async def readme(full_name: str) -> str | None:
        try:
            # core endpoint (5000/hr per token) — round-robin, no 6s spacing
            token = None if _pool.empty else _best_token()["token"]
            rr = await client().get(
                f"{GH_API}/repos/{full_name}/readme",
                headers={"Accept": "application/vnd.github.raw", **({"Authorization": f"Bearer {token}"} if token else {})},
                timeout=8.0,
            )
            if rr.status_code == 200:
                return rr.text[:6000]
        except (httpx.HTTPError, GitHubUnavailable, TypeError):
            pass
        return None

    readmes = await asyncio.gather(*(readme(rp.get("full_name", "")) for rp in repos))
    results = []
    for rp, md in zip(repos, readmes):
        item: dict[str, Any] = {
            "resultType": "repo_readme",
            "repo": rp.get("full_name"),
            "readmeUrl": rp.get("html_url"),
            "snippet": (rp.get("description") or "")[:400],
        }
        if md:
            item["contentMd"] = md
        results.append(item)
    return {"success": True, "results": results}
