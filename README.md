# portainer-stack

这个分支是 Portainer 部署 firecrawl 的专用分支，与 main 完全独立（orphan branch），只包含部署所需文件：

| 文件 | 用途 | 谁来改 |
|---|---|---|
| `docker-compose.yaml` | 上游 [firecrawl/firecrawl](https://github.com/firecrawl/firecrawl) 的原样拷贝 | **只由机器人改**，手动改了也会被同步覆盖 |
| `docker-compose.portainer.yaml` | 我们的全部自定义（镜像源、restart、traefik、reverse-proxy 网络、api 启动 patch、数据卷） | **要调整部署只改这个文件** |

## Portainer 配置

Stacks → firecrawl → Editor：

- **Repository reference**: `refs/heads/portainer-stack`
- **Compose path**: `docker-compose.yaml`
- **Additional paths**: 添加 `docker-compose.portainer.yaml`（顺序在主文件之后，后者覆盖前者）
- 建议开启 **Automatic updates**（GitOps webhook 或 polling），这样机器人推送后 stack 自动更新

合并后的最终配置可以在本地验证：

```bash
docker compose -f docker-compose.yaml -f docker-compose.portainer.yaml config
```

## 同步机制

`main` 分支上的 `.github/workflows/portainer-stack-sync.yml` 每天 03:17 UTC 运行
（GitHub 定时任务只跑默认分支，所以工作流放在 main）：

1. 拉取上游最新 `docker-compose.yaml`
2. 与本分支的 override 做合并校验（`docker compose config` + 关键配置哨兵检查）
3. 校验通过且有变化才提交推送

上游若做了与 override 不兼容的改动（比如删除/重命名某个 service），Action 会**失败并通知**，
stack 保持旧版本不受影响——不会半夜悄悄挂掉。修好后在 Actions 页面手动 Re-run 即可。

注意：若仓库长期无活动，GitHub 可能自动暂停定时任务（会提前发邮件提醒），重新 enable 即可。
