# GitHub Actions 镜像构建与发布

`.github/workflows/docker-publish.yml` 负责测试、构建、验证并发布前后端镜像。工作流不会连接服务器，也不会读取或生成生产 `.env`。

## 触发规则

| 事件 | 执行动作 |
| --- | --- |
| Pull request | 后端与前端测试、Lint、生产构建、Nginx 检查、两份 Docker 镜像构建及冒烟验证；不推送镜像 |
| `main` push | 完成相同门禁后，向 GHCR 推送 `sha-<完整 commit SHA>` 镜像 |
| 受保护的 `v*` tag | 除 SHA 标签外，再推送同名版本标签；未受保护的版本标签会失败 |

镜像名按仓库 owner 自动生成并转为小写：

```text
ghcr.io/<owner>/dingtalk-expense-backend
ghcr.io/<owner>/dingtalk-expense-web
```

推送 job 使用 GitHub 自动提供的临时 `GITHUB_TOKEN`，权限限制为 `contents: read` 和 `packages: write`，无需配置长期 GHCR PAT。首次发布后应在 GitHub Packages 中确认镜像与仓库的关联及所需可见性。

每次成功推送还会上传 `release.json` artifact，其中记录源码 commit、模型清单 hash，以及经过测试并实际推送的两份镜像 digest。它只用于确认构建产物，不会触发部署。

## OCR 模型

模型二进制不提交到 Git。后端 Dockerfile 在独立构建阶段读取 `backend/app/ocr/model_manifest.json`，从清单中的官方地址下载 `PP-OCRv6_small_det` 和 `PP-OCRv6_small_rec`，校验归档与逐文件 SHA-256，再复制到最终镜像。

下载失败、hash 不一致、压缩包包含危险路径或文件集合不匹配都会终止构建。运行时只读取镜像中的固定模型目录，不联网下载，也不会回退到浮动版本。BuildKit 缓存以 manifest 和下载脚本为输入，普通业务代码变化通常可以复用模型层。

## `.env` 处理

构建镜像不需要 `.env`。根目录、后端和前端构建上下文均排除 `.env`、`.env.*` 及本地模型压缩包；工作流还放入测试标记并检查最终镜像配置、历史层和前端静态文件，防止环境文件或凭据进入镜像。

钉钉凭据、`SESSION_SECRET`、端口和其他运行参数属于部署环境，应在运行容器时通过环境变量或受保护的 `env_file` 提供。仓库仅保留 `.env.example` 和 `.env.production.example` 作为键名与默认值参考。由于 Actions 不执行部署，不需要配置 GitHub Environment、SSH key、生产 Secrets 或 `DEPLOY_ENABLED`。

## 镜像验证

工作流先加载最终的 `linux/amd64` 镜像，再执行 `scripts/smoke-images.sh`。检查包括：

- 镜像以非 root 用户运行，且不包含 `.env` 或测试凭据标记；
- 后端在断网、只读根文件系统下完成真实 PaddleOCR 初始化和空白图推理；
- 后端完成迁移并通过 production readiness；
- Web 镜像通过 Nginx 配置检查，并能代理 `/api/ready`；
- 推送后的 registry digest 对应刚刚通过测试的本地镜像。

所有检查通过后才登录 GHCR 并推送镜像。

## 本地复现

本地不需要 `.env` 或宿主模型目录即可复现镜像构建和验证：

```bash
make images-build
make images-check
```

也可以单独检查模型下载器，输出目录必须尚不存在：

```bash
python3 backend/scripts/download-ocr-models.py --output /tmp/expense-ocr-models
```

Apple Silicon 上会通过 Docker 模拟 `linux/amd64`，适合构建检查；正式运行仍要求原生 Linux/amd64 和支持 AVX 的 CPU。实际服务器如何拉取镜像、保存 `.env`、挂载持久数据及升级回滚，由部署环境单独管理。

设计依据见 [方案](GITHUB_ACTIONS_DOCKER_DESIGN.md) 和 [官方资料核对](research/github-actions-docker-sources.md)。
