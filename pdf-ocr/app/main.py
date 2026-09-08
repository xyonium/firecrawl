"""pdf-ocr — RunPod-MU-compatible OCR adapter backed by a self-hosted MinerU API.

firecrawl's pdf engine (engines/pdf/runpodMU.ts) only speaks the RunPod
serverless contract, and its base URL is hardcoded to api.runpod.ai. This
service implements that contract — POST /runpod/v2/{pod_id}/runsync taking
{input: {file_content: <base64 pdf>, ...}} and returning
{id, status: "COMPLETED", output: {markdown}} — and forwards the PDF to a
MinerU server (/file_parse). The firecrawl api container's startup sed patch
rewrites the hardcoded base URL to this service; RUNPOD_MU_API_KEY/POD_ID
only need to be non-empty (the bearer key is ignored here).

Any failure returns 502 on purpose: firecrawl treats MU errors as
"fall back to in-container pdf-parse", which is exactly what we want.
"""

import base64
import binascii
import logging
import os
import uuid

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

MINERU_BASE_URL = os.environ.get("MINERU_BASE_URL", "http://gpu.savorcare.com:8800").rstrip("/")
MINERU_BACKEND = os.environ.get("MINERU_BACKEND", "pipeline")
# Hard ceiling for one MinerU job; the per-request `timeout` (ms, from
# firecrawl's scrapeTimeout) can lower it further.
MINERU_TIMEOUT_S = float(os.environ.get("MINERU_TIMEOUT_S", "300"))
POLL_INTERVAL_S = 2.0

logger = logging.getLogger("pdf-ocr")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

app = FastAPI(title="firecrawl pdf-ocr adapter")

_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(MINERU_TIMEOUT_S))
    return _client


def _err(status: int, msg: str):
    return JSONResponse({"error": msg}, status_code=status)


def _extract_markdown(payload: dict) -> str | None:
    results = payload.get("results") or {}
    parts = [
        v.get("md_content", "")
        for v in results.values()
        if isinstance(v, dict) and v.get("md_content")
    ]
    md = "\n\n".join(parts).strip()
    return md or None


@app.get("/healthz")
async def healthz():
    mineru_ok = False
    try:
        r = await client().get(f"{MINERU_BASE_URL}/health", timeout=5.0)
        mineru_ok = r.status_code == 200
    except httpx.HTTPError:
        pass
    return {"ok": True, "mineru": MINERU_BASE_URL, "mineruReachable": mineru_ok}


@app.post("/runpod/v2/{pod_id}/runsync")
async def runsync(pod_id: str, request: Request):
    try:
        body = await request.json()
    except Exception:
        return _err(400, "invalid JSON body")
    inp = body.get("input") or {}

    b64 = inp.get("file_content")
    if not b64 or not isinstance(b64, str):
        return _err(400, "input.file_content (base64 pdf) is required")
    try:
        pdf_bytes = base64.b64decode(b64)
    except (binascii.Error, ValueError):
        return _err(400, "input.file_content is not valid base64")
    if not pdf_bytes.startswith(b"%PDF"):
        return _err(400, "decoded file_content is not a PDF")

    filename = inp.get("filename") or "document.pdf"
    job_id = inp.get("id") or str(uuid.uuid4())

    # firecrawl passes scrapeTimeout in ms; floor at 30s so tiny scrape
    # timeouts don't instantly kill OCR, cap at MINERU_TIMEOUT_S.
    timeout_ms = inp.get("timeout")
    budget_s = min(MINERU_TIMEOUT_S, max(30.0, (timeout_ms or 120000) / 1000.0))

    data: dict[str, str] = {"return_md": "true", "backend": MINERU_BACKEND}
    max_pages = inp.get("max_pages")
    if isinstance(max_pages, int) and max_pages > 0:
        # MinerU end_page_id is 0-based inclusive
        data["end_page_id"] = str(max_pages - 1)

    logger.info("runsync job=%s pod=%s file=%s bytes=%d budget=%.0fs",
                job_id, pod_id, filename, len(pdf_bytes), budget_s)

    try:
        r = await client().post(
            f"{MINERU_BASE_URL}/file_parse",
            files=[("files", (filename, pdf_bytes, "application/pdf"))],
            data=data,
            timeout=budget_s,
        )
    except httpx.HTTPError as e:
        return _err(502, f"mineru request failed: {type(e).__name__}")
    if r.status_code != 200:
        return _err(502, f"mineru returned {r.status_code}: {r.text[:200]}")

    payload = r.json()

    # MinerU >=3.x /file_parse normally completes inline; handle async-style
    # responses defensively by polling the task until terminal.
    status = payload.get("status")
    if status not in ("completed", None):
        task_id = payload.get("task_id")
        if not task_id:
            return _err(502, f"mineru async response without task_id: {str(payload)[:200]}")
        import asyncio
        import time

        deadline = time.monotonic() + budget_s
        while time.monotonic() < deadline:
            await asyncio.sleep(POLL_INTERVAL_S)
            try:
                sr = await client().get(f"{MINERU_BASE_URL}/tasks/{task_id}", timeout=15.0)
                payload = sr.json()
            except httpx.HTTPError:
                continue
            status = payload.get("status")
            if status in ("completed", "failed"):
                break
        if status != "completed":
            return _err(502, f"mineru task {task_id} ended in status {status!r}")

    if payload.get("error"):
        return _err(502, f"mineru error: {str(payload['error'])[:200]}")

    md = _extract_markdown(payload)
    if md is None:
        # results may live behind result_url on some server versions
        result_url = payload.get("result_url")
        if result_url:
            try:
                rr = await client().get(result_url, timeout=30.0)
                md = _extract_markdown(rr.json())
            except (httpx.HTTPError, ValueError):
                pass
    if md is None:
        return _err(502, "mineru returned no markdown (empty results)")

    return {"id": job_id, "status": "COMPLETED", "output": {"markdown": md}}


@app.get("/runpod/v2/{pod_id}/status/{job_id}")
async def runpod_status(pod_id: str, job_id: str):
    # runsync never returns IN_QUEUE/IN_PROGRESS, so firecrawl never polls —
    # this exists only so an unexpected poll terminates the loop instead of
    # hanging. A non-success status makes the api fall back to pdf-parse.
    return {"id": job_id, "status": "FAILED", "error": "unknown job (adapter is synchronous)"}
