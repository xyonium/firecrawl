# research-proxy — firecrawl research 上游 shim

实现 firecrawl 的 research-upstream 协议（`controllers/v2/research-proxy.ts`），让自托管
stack 获得 cloud-only 的 research / developer 端点。firecrawl 对上游响应**零校验直通**，
本服务就是按上游契约 + MCP 消费端（firecrawl-mcp `research.ts`）字段形状写的。

## 端点 → 后端

| 端点 | 上游预算 | 后端 |
|---|---|---|
| `GET /v2/research/papers` | 30s | papers-service `/papers/search_{src}` **逐源并发扇出**（18s/源，慢源隔离），合并去重 |
| `GET /v2/research/papers/{id}` | 5s | S2 graph → arXiv export → papers-service crossref（doi）fallback 并发竞速 |
| `GET /v2/research/papers/{id}?query=` | 120s | papers-service `read_{source}_paper` → reach `/read_url`（mcpo）fallback；BM25-lite 取 top-k passages |
| `GET /v2/research/papers/{id}/similar` | 10s | S2 recommendations ∪ citations ∪ references；`intent` 关键词 rerank |
| `GET /v2/research/github` | 12s | GitHub repo 搜索 + README 抓取（上游已弃用，2026-11 移除） |
| `GET /v2/code/search` | 15s | GitHub `/search/code`（text-match 片段）+ `/repos` license 富化 |

注意：`{paper_id}` 用 `:path` 转换器——doi 含 `/`，uvicorn 在路由前解码 `%2F`。

`/papers/*` 上游默认是 **papers-service**（`../papers-service/`，paper-search-mcp 的
替代：直连公开学术 API，复用 paper-search 仓库 tool.py 的适配器）；`/reach/read_url`
仍走 mcpo 的 reach-mcp。两个 base 独立可配，缺省都回落 `MCPO_BASE_URL`（旧行为）。

## GitHub token 池

code search 必须登录（10 req/min/token，独立桶，按 token 不按 IP）。
`GITHUB_TOKENS` 逗号分隔多 token 轮询；403/429 时按 `Retry-After` 冷却该 token 并换下一个。
每 token 自动保持 6.2s code-search 间隔。repo 搜索/README/license 走 core 桶（5000/hr），不做间隔。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `PAPERS_BASE_URL` | `$MCPO_BASE_URL` | /papers/* 上游；生产配 `http://papers-service:3200` |
| `REACH_BASE_URL` | `$MCPO_BASE_URL` | /reach/read_url（reach-mcp 仍在 mcpo 上） |
| `MCPO_BASE_URL` | `http://mcpo:8000` | 上面两个的缺省回落；单独保留无其他用途 |
| `PAPER_SOURCES` | `arxiv,semantic,pubmed,openalex,crossref,europepmc,dblp,hal,pmc` | 扇出源列表 |
| `SEMANTIC_SCHOLAR_API_KEY` | 空 | S2 graph/similar 用；空也能跑但更易 429 |
| `GITHUB_TOKENS` | 空 | 逗号分隔；**空则 code search 返回 503** |
| `LOG_LEVEL` | `INFO` | |

密钥走 Portainer stack env，**不进 git**。

## 部署

镜像由本分支的 `.github/workflows/research-proxy-image.yml` 构建：
推 `research-proxy/**` 改动 → GH Actions buildx 推 `ghcr.io/xyonium/firecrawl-research-proxy`
→ 把 digest 钉回两个 compose 文件 → Portainer 看到 compose diff 拉新镜像。
compose 里引用走 mirror：`jcr.savorcare.com/ghcr/xyonium/firecrawl-research-proxy`。

本地手动构建：先 `docker pull jcr.savorcare.com/docker/library/python:3.12-slim`
再 `docker tag` 成 `python:3.12-slim`（Dockerfile FROM 直连 docker.io，供 GH runner 用）。

## 后续方向

- ~~paper-search-mcp 桥接~~ → **已替换为 papers-service**（`../papers-service/`，
  2026-09）；mcpo 上只剩 reach-mcp 供 `/reach/read_url`
- min_stars/max_stars/topic 参数当前忽略（GitHub code search 无此 qualifier，需 repo 逐个富化）
