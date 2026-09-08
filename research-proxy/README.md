# research-proxy — firecrawl research 上游 shim

实现 firecrawl 的 research-upstream 协议（`controllers/v2/research-proxy.ts`），让自托管
stack 获得 cloud-only 的 research / developer 端点。firecrawl 对上游响应**零校验直通**，
本服务就是按上游契约 + MCP 消费端（firecrawl-mcp `research.ts`）字段形状写的。

## 端点 → 后端

| 端点 | 上游预算 | 后端 |
|---|---|---|
| `GET /v2/research/papers` | 30s | mcpo `/papers/search_{src}` **逐源并发扇出**（18s/源，慢源隔离），合并去重 |
| `GET /v2/research/papers/{id}` | 5s | S2 graph → arXiv export → crossref（doi）fallback 并发竞速 |
| `GET /v2/research/papers/{id}?query=` | 120s | mcpo `read_{source}_paper` → reach `/read_url` fallback；BM25-lite 取 top-k passages |
| `GET /v2/research/papers/{id}/similar` | 10s | S2 recommendations ∪ citations ∪ references；`intent` 关键词 rerank |
| `GET /v2/research/github` | 12s | GitHub repo 搜索 + README 抓取（上游已弃用，2026-11 移除） |
| `GET /v2/code/search` | 15s | GitHub `/search/code`（text-match 片段）+ `/repos` license 富化 |

注意：`{paper_id}` 用 `:path` 转换器——doi 含 `/`，uvicorn 在路由前解码 `%2F`。

## GitHub token 池

code search 必须登录（10 req/min/token，独立桶，按 token 不按 IP）。
`GITHUB_TOKENS` 逗号分隔多 token 轮询；403/429 时按 `Retry-After` 冷却该 token 并换下一个。
每 token 自动保持 6.2s code-search 间隔。repo 搜索/README/license 走 core 桶（5000/hr），不做间隔。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `MCPO_BASE_URL` | `http://mcp:8000` | mcpo 网关（paper-search-mcp + reach-mcp） |
| `PAPER_SOURCES` | `arxiv,semantic,pubmed,openalex,crossref,europepmc,dblp,hal,pmc` | 扇出源列表 |
| `SEMANTIC_SCHOLAR_API_KEY` | 空 | S2 graph/similar 用；空也能跑但更易 429 |
| `GITHUB_TOKENS` | 空 | 逗号分隔；**空则 code search 返回 503** |
| `LOG_LEVEL` | `INFO` | |

密钥走 Portainer stack env，**不进 git**。

## 部署

compose 里 `build: ./research-proxy` + 版本化 image tag。**更新代码必须 bump tag**
（如 `v1.0.0` → `v1.0.1`）：Portainer CE 重部署不带 `--build`，但新 tag 本地不存在必然触发构建。

## 后续方向

- 用户计划把 `~/paper-search`（16+ 源 OWUI tool）独立成 PyPI 包；届时本 shim 可直接
  import 它替代 mcpo 桥接（`papers.py` 的 `_search_one`/`READ_TOOL` 是唯一耦合点）
- min_stars/max_stars/topic 参数当前忽略（GitHub code search 无此 qualifier，需 repo 逐个富化）
