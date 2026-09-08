# pdf-ocr — firecrawl PDF OCR 适配器

firecrawl 的 pdf 引擎（`engines/pdf/runpodMU.ts`）只会说 RunPod serverless 契约，
且 base URL **硬编码** `https://api.runpod.ai/v2/`。本服务实现该契约并把 PDF 转发到
自托管 MinerU（`/file_parse`），让扫描件/图片型 PDF 走自己的 GPU 而不是付费 RunPod。

## 接线方式

1. 本服务暴露 `POST /runpod/v2/{pod_id}/runsync`，入队即同步执行，永远返回
   `COMPLETED`（firecrawl 不会进入 status 轮询分支）
2. api 容器启动命令里有一条 sed，把 dist 里硬编码的 `https://api.runpod.ai/v2/`
   改写成 `http://pdf-ocr:3200/runpod/v2/`（与 llmExtract patch 同款模式）
3. stack env 里 `RUNPOD_MU_API_KEY`/`RUNPOD_MU_POD_ID` 只要非空即可（Bearer 被忽略）
4. 失败一律返回 502 —— firecrawl 会把 MU 错误降级为容器内 pdf-parse，行为安全

firecrawl 侧触发条件：pdf 引擎判定需要 OCR（扫描件/图片型，或 Rust 提取不合格）
且文件 < 19MB（`MAX_FILE_SIZE`，dist 内常量）。文本型 PDF 不经过本服务。

## MinerU 契约

`POST $MINERU_BASE_URL/file_parse`（multipart：`files`=PDF binary；`return_md=true`、
`backend`、`end_page_id`），响应 `results.{name}.md_content`。
MinerU 3.x 通常同步完成；若返回 pending 状态则按 `task_id` 轮询 `/tasks/{id}` 至终结。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `MINERU_BASE_URL` | `http://gpu.savorcare.com:8800` | MinerU API 地址 |
| `MINERU_BACKEND` | `pipeline` | MinerU backend（pipeline / vlm-engine / hybrid-engine …） |
| `MINERU_TIMEOUT_S` | `300` | 单个 MinerU 任务的硬上限（秒）；请求里的 scrapeTimeout 可进一步收紧 |
| `LOG_LEVEL` | `INFO` | |

## 部署

镜像由本分支的 `.github/workflows/pdf-ocr-image.yml` 构建：推 `pdf-ocr/**` 改动 →
GH Actions buildx 推 `ghcr.io/xyonium/firecrawl-pdf-ocr` → digest 钉回两个 compose
→ Portainer 看到 compose diff 拉新镜像。compose 引用走 mirror：
`jcr.savorcare.com/ghcr/xyonium/firecrawl-pdf-ocr`。

本地手动构建同 research-proxy：先经 mirror pull base image 再 tag。
