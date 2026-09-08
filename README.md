# portainer-stack

这个分支是 Portainer 部署 firecrawl 的专用分支，与 main 完全独立（orphan branch），只包含部署所需文件：

| 文件 | 用途 | 谁来改 |
|---|---|---|
| `docker-compose.yaml` | **预合并生成文件**：上游 compose + Portainer override 合并后的最终结果，且 firecrawl 系镜像已按 digest 钉版，Portainer 直接部署它（`${VAR}` 变量保留，仍由 Portainer 的 stack env 注入） | **只由机器人生成**，手改会被覆盖 |
| `docker-compose.upstream.yaml` | 上游 [firecrawl/firecrawl](https://github.com/firecrawl/firecrawl) 的原样拷贝 | **只由机器人改** |
| `docker-compose.portainer.yaml` | 我们的全部自定义（镜像源、restart、traefik、reverse-proxy 网络、api 启动 patch、数据卷） | **要调整部署只改这个文件** |
| `research-proxy/` | research 上游 shim 源码（FastAPI）：桥接 mcpo 的 paper-search-mcp/reach-mcp + GitHub API，让 cloud-only 的 research papers / similar / read / code search 端点自托管可用。compose 里 `build: ./research-proxy` | 手改，**更新须 bump compose 里的 image tag**（CE 重部署不带 `--build`） |

### research-proxy 速览

- api 服务设 `RESEARCH_PROXY_URL=http://research-proxy:3100` 后才会挂载
  `/v2/search/research/*` 与 `/v2/search/developer` 路由；MCP 的 research 工具组与
  search 的 `developer` category 全部走它
- 需在 Portainer stack env 配：`GITHUB_TOKENS`（逗号分隔多 token 轮询，code search
  10 req/min/token）、`SEMANTIC_SCHOLAR_API_KEY`（可选但强烈建议，否则 S2 易 429）
- 经 external 网络 `open-webui-nogpu_default` 访问 mcpo（`http://mcp:8000`），
  该 stack 必须先在线
- 细节见 `research-proxy/README.md`

为什么是预合并单文件：Portainer 2.39 只有**创建** stack 时才能配 additional paths，
已有 stack 改不了（2.45 的 "Edit git settings" 才行）。预合并后 Portainer 只需要一个 compose 文件，任何版本都行。

## Portainer 配置

Compose path 保持默认的 `docker-compose.yaml` 不变，**只需把分支指过来**：

- 方式一（API，不重建 stack）：在 Portainer 界面生成 access token（右上角头像 → My account → Access tokens），然后：
  ```bash
  curl -X POST "https://<portainer地址>/api/stacks/<stack-id>/git?endpointId=1" \
    -H "X-API-Key: <token>" -H "Content-Type: application/json" \
    -d '{"RepositoryReferenceName": "refs/heads/portainer-stack"}'
  ```
  回到 stack 页面点 **Pull and redeploy**。（stack-id 在 stack 页面的 URL 里）
- 方式二（重建 stack）：删掉旧 stack 后重新创建，创建表单里分支填 `refs/heads/portainer-stack`。
  先把 env 变量复制出来再删；compose 里 `name: firecrawl` 已固定项目名，数据卷（firecrawl_redis 等）会自动挂回，**数据不丢**；若用了 webhook 自动更新，新 stack 的 webhook URL 会变，记得换。
- 方式三（升级到 Portainer 2.45+）：stack 页面出现 **Edit git settings**，可直接改分支，不用重建。

切换时合并配置与切换前完全一致（已逐字节验证），对容器是无扰动重建。
建议开启 **Automatic updates**（polling 或 webhook），机器人推送后 stack 自动更新。

## 同步机制

`main` 分支上的 `.github/workflows/portainer-stack-sync.yml` **每月 5 日 03:42 UTC**（北京时间 11:42）运行
（GitHub 定时任务只跑默认分支，所以工作流放在 main；想临时同步可在 Actions 页面手动 Run workflow）：

1. 拉取上游最新 compose → `docker-compose.upstream.yaml`
2. 与本分支的 override 合并（`docker compose config --no-interpolate`）
3. **镜像 digest 钉版**：从 ghcr.io 解析 `firecrawl` / `playwright-service` / `nuq-postgres`
   三个镜像 `latest` 的当前 digest，写进生成文件（`...@sha256:...`）。
   因为 CE 版 Portainer 没有 re-pull image,tag 不变的镜像永远不会自动更新；
   钉 digest 后镜像更新变成 compose 文件的 diff,redeploy 时自然会拉新镜像。
   redis / rabbitmq / foundationdb 是稳定第三方镜像，刻意保持浮动 tag 不钉。
4. 校验合并结果（自解析 + 关键配置哨兵检查），通过且有变化才提交推送

上游若做了与 override 不兼容的改动（比如删除/重命名某个 service），Action 会**失败并通知**，
stack 保持旧版本不受影响——不会半夜悄悄挂掉。修好后在 Actions 页面手动 Re-run 即可。

回滚：分支上 revert 对应的 sync commit 再让 Portainer redeploy 即可（digest 历史都在 git 里）。

注意：若仓库长期无活动，GitHub 可能自动暂停定时任务（会提前发邮件提醒），重新 enable 即可。
