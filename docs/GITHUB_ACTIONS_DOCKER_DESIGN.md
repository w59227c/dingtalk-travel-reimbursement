# GitHub Actions 镜像构建方案

状态：已实现于 `codex/github-actions-docker` 分支。日期：2026-09-10。

## 目标与边界

GitHub Actions 自动完成代码检查、OCR 模型下载与校验、前后端镜像构建、最终镜像冒烟验证和 GHCR 发布。工作流到镜像发布为止，不连接生产服务器、不启动生产容器，也不接触钉钉凭据或生产 `.env`。

前后端分别发布为 `linux/amd64` 镜像。模型二进制保存在官方模型源和最终容器镜像中，不进入 Git 仓库或 Git 历史。若将来要求模型也不能存放在 GitHub 基础设施，需要把同一构建流程的目标 registry 改为企业 ACR、Harbor 等仓库。

## 构建时下载模型

`backend/app/ocr/model_manifest.json` 是模型版本、官方 URL、归档 SHA-256、逐文件 SHA-256 和目录清单的唯一依据。`backend/scripts/download-ocr-models.py` 执行以下流程：

1. 从固定 HTTPS 地址下载模型归档，使用超时、有限重试和大小限制。
2. 校验归档 SHA-256，失败时立即终止。
3. 解包前拒绝绝对路径、`..`、链接、设备文件、重复路径和额外文件。
4. 复用运行时校验逻辑验证目录中的准确文件集合及每个文件 hash。
5. 全部通过后才把模型目录复制到最终镜像的 `/opt/expense/models`。

Dockerfile 把模型下载放在独立 stage 中。该层只依赖 manifest、下载器和校验模块，因此业务代码变化不会强制重新下载。缓存只是速度优化；每次使用已有模型目录时仍会验证内容。运行容器不会联网下载模型或自动选择新版模型。

## 工作流结构

| Job | 作用 | 凭据 |
| --- | --- | --- |
| `backend` | 后端测试、跨栈计算契约测试和 Ruff | 只读仓库 |
| `frontend` | 前端测试、Lint、生产构建和 Nginx 策略检查 | 只读仓库 |
| `pull-request-images` | 构建并验证最终镜像，不推送 | 只读仓库 |
| `images` | 对 `main` 或版本 tag 构建、验证并推送相同镜像 | 临时 `GITHUB_TOKEN`，仅增加 `packages: write` |

PR 使用 `pull_request` 事件，不使用可让不受信任代码接触高权限上下文的 `pull_request_target`。第三方 Actions 固定到完整 commit SHA。前后端使用独立 GitHub Actions cache scope。

`main` 发布不可变的 `sha-<完整 commit SHA>`，并把同一份已验证镜像更新为 `main` 标签，便于服务器用固定 Compose 配置拉取最新主线版本。受保护的 `v*` tag 额外发布版本标签。工作流记录两份远程镜像 digest、源码 commit 和模型 manifest hash 到 `release.json` artifact，便于后续人工或外部部署系统选用准确产物及精确回滚。

## `.env` 设计

镜像构建与运行配置分离：

- Docker 构建上下文显式排除 `.env`、`.env.*`、本地数据和模型压缩包。
- 构建参数和 Docker `ENV` 不传钉钉 Secret、`SESSION_SECRET` 或其他生产凭据。
- 前端使用同源 `/api`，无需为了生产域名重新构建；任何 `VITE_*` 值都应视为浏览器可见信息。
- 应用运行时继续从容器环境变量读取配置。部署方可以使用容器平台的 Secret、环境变量或权限受控的 `env_file`。
- 仓库中的 `.env.example` 与 `.env.production.example` 只描述键名、安全默认值和本地操作方式，不包含真实值。

因此 GitHub 仓库无需保存生产 Environment Secrets，也不需要把 `.env` 从 Actions 传到服务器。新增运行参数时，只需更新应用配置与示例文件；镜像通常仍可在各环境复用。

若未来模型下载源需要鉴权，应只用 BuildKit secret mount 向对应下载指令提供令牌，并检查日志和最终层；不能使用 `ARG` 或 `ENV` 传递秘密。

## 验收标准

1. 全新检出、没有 `.env` 和本地模型时能构建两份正式镜像。
2. 模型 hash 不符、归档越界、额外文件或下载失败会使构建失败。
3. 最终后端镜像在断网、非 root、只读根条件下完成真实 OCR 初始化及推理。
4. 后端完成迁移与 readiness，Web 镜像通过 Nginx 配置和 `/api/ready` 代理检查。
5. 镜像配置、历史层和前端产物不包含环境文件或测试凭据标记。
6. 只有通过上述验证的同一份镜像才推送；远程 digest 与本地已测试镜像一致。
7. 工作流不包含部署 job、SSH 操作、生产 Environment 或生产 Secrets 引用。

具体使用方法见 [配置说明](GITHUB_ACTIONS_SETUP.md)，官方资料见 [研究记录](research/github-actions-docker-sources.md)。
