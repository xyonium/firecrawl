# papers-service

**paper-search-mcp 的替代实现**——research-proxy（`../research-proxy/`）的 `/papers/*` 上游。
自托管 firecrawl 的 research 端点原先由 mcpo 承载的 paper-search-mcp 提供论文数据；
本服务用更可靠的方式实现同一组端点形状，paper-search-mcp 就此退役。

## 端点（与 mcpo/paper-search-mcp 形状一致，research-proxy 零改动可切）

| 端点 | 请求体 | 返回 |
|---|---|---|
| `POST /papers/search_{source}` | `{"query", "max_results"}` | `{"papers": [...]}` |
| `POST /papers/get_crossref_paper_by_doi` | `{"doi"}` | paper dict（裸） |
| `POST /papers/read_{source}_paper` | `{"paper_id"}` | `{"result": "全文文本"}` |
| `GET /healthz` | — | 源清单 + tool.py 版本 |

item 形状（research-proxy `_paperhit_from_mcp` 消费的就是这些键）：
`title / authors("; "分隔字符串) / published_date / abstract / paper_id(裸 id, 无前缀) / doi / source / pdf_url / url / citations`。

## 检索：复用 paper-search 仓库 tool.py 的直连适配器

镜像构建时 Dockerfile 把 [xyonium/paper-search](https://github.com/xyonium/paper-search)
的 `tool.py` ADD 进 `/srv/vendor/tool.py`（`PAPER_SEARCH_REF` build-arg，工作流自动
解析成 main 的 commit sha）。该文件是 OWUI 工具的同一套实现——搜索适配器一处维护，
OWUI 侧和本服务行为天然一致。

支持的 `search_{source}`（未知源 → 404，research-proxy 记错误后继续）：

- **直连免 key**：arxiv, semantic, pubmed, pmc, openalex, crossref, europepmc,
  core, hal, dblp, zenodo, openaire, iacr, doaj
- **key-gated**（没配 key 返回 `[]` 不报错）：ieee（IEEE_APIKEY）、
  zhihuiya（ZHIHUIYA_APIKEY）
- **google_scholar 三级链**（任一级都没配返回 `[]`）：
  firecrawl（FIRECRAWL_BASE_URL）→ tavily extract advanced（TAVILY_BASE_URL）
  → Apify actor（APIFY_ROTATOR_BASE_URL）
- **firecrawl / tavily**：通用 web 搜索源（需对应 BASE_URL）

## read：本服务自带的轻量直连实现

`read_{source}_paper` 覆盖：arxiv / semantic / pubmed / pmc / europepmc / hal /
crossref / openalex / iacr（PDF→pymupdf、efetch、fullTextXML、元数据级 markdown 等，
见 `app/readers.py`）。

**读不了的源或论文一律 404** → research-proxy 自动落 `reach/read_url`
（jina 风格抓取，reach-mcp 仍在 mcpo 上，**不**随 paper-search-mcp 退役）。
因此 biorxiv/medrxiv/zenodo/openaire/dblp 等源的行为与退役前一致——本来
paper-search-mcp 对这些源也只有元数据级 read，实际全文都是 reach 出的。

## 环境变量

| 变量 | 用途 |
|---|---|
| `SEMANTIC_SCHOLAR_API_KEY` | S2 检索/read 提额（强烈建议；匿名共享池常 429） |
| `NCBI_API_KEY` | pubmed/pmc 提额 |
| `CORE_API_KEY` | core 检索（不配走匿名） |
| `IEEE_APIKEY` / `ZHIHUIYA_APIKEY` / `ZENODO_ACCESS_TOKEN` | 对应 key-gated 源 |
| `FIRECRAWL_BASE_URL` | scholar 链 tier1 + dblp Anubis 兜底，如 `http://mcpo:8000/firecrawl` |
| `TAVILY_BASE_URL` | scholar 链 tier2 + tavily 源，如 `http://api-key-rotator:8788/tavily` |
| `APIFY_ROTATOR_BASE_URL` | scholar 链 tier3，如 `http://api-key-rotator:8788` |
| `MCPO_API_KEY` | mcpo 的 `--api-key`（firecrawl 走 mcpo 时需要） |
| `PAPER_SEARCH_TOOL_PATH` | 本地开发时覆盖 tool.py 路径（默认 `/srv/vendor/tool.py`） |

## 构建与部署

镜像由 `../.github/workflows/papers-service-image.yml` 在 push 到本分支
（paths: `papers-service/**`）时构建推 `ghcr.io/xyonium/firecrawl-papers-service`
并把 digest 钉回两个 compose 文件。compose 服务块见 `../docker-compose.portainer.yaml`
（`papers-service`，端口 3200，内部服务不过 traefik）。

**升级 tool.py**：tool.py 在另一个仓库，其更新不触发本工作流——
在 Actions 页对 `Build papers-service image` 手动 `workflow_dispatch` 即可
（会把当时的 paper-search main 打进新镜像并钉版）。

## 本地开发/测试

```bash
pip install -r requirements.txt
PAPER_SEARCH_TOOL_PATH=/home/eli/paper-search/tool.py \
    python3 -m pytest tests/ -q                      # 契约测试（mock 网络）
PAPER_SEARCH_TOOL_PATH=/home/eli/paper-search/tool.py \
    uvicorn app.main:app --port 3200                 # 起服务
curl -s localhost:3200/healthz
curl -s -X POST localhost:3200/papers/search_arxiv \
    -H 'content-type: application/json' \
    -d '{"query":"graph neural network","max_results":2}'
```
