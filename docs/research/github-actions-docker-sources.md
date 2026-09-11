# GitHub Actions 镜像与运行配置：官方资料核对

> 调研基准日：2026-09-10。本文记录镜像构建与发布所依据的官方能力。

## 核对结论

| 主题 | 官方能力 | 本项目建议 |
| --- | --- | --- |
| GHCR 发布 | 工作流可用 `GITHUB_TOKEN` 发布关联仓库的镜像；示例配置 `contents: read`、`packages: write`，通过 `docker/login-action` 登录 `ghcr.io`。[GitHub 发布教程](https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images) | 构建阶段无需另配长期推送 PAT。PR 只验证，可信分支或版本标签才推送。 |
| 包权限与拉取 | 首次发布默认为 private；工作流使用 `GITHUB_TOKEN` 发布会自动关联仓库；既有同名包未关联仓库可能拒绝推送。公共镜像支持匿名拉取，私有镜像外部拉取可使用具备 `read:packages` 的 classic PAT。[GHCR 文档](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry) | 首次配置确认包可见性及仓库关联。工作流记录镜像 digest，便于后续系统精确选用产物。 |
| 构建缓存 | 支持 `cache-from: type=gha` 与 `cache-to: type=gha,mode=max`；也支持独立 registry cache。`inline` 只支持 `min`。普通 cache mount 不会默认持久化到 Actions cache。[Docker 缓存指南](https://docs.docker.com/build/ci/github-actions/cache/) | 前后端分开 cache scope；将模型下载放进只依赖 manifest 和下载脚本的层，避免业务代码变化重新下载。大型模型缓存可改用 registry cache。 |
| 多架构 | Buildx 可以构建、推送多平台镜像；默认 runner 的镜像存储不能直接加载多平台结果，需要额外配置。[Docker 多平台指南](https://docs.docker.com/build/ci/github-actions/multi-platform/) | 能力可用不代表依赖可用。按项目生产约束只发布 `linux/amd64`，本次不增加 arm64 或 QEMU。 |
| 构建秘密 | Docker 不建议通过 `ARG`、`ENV` 传秘密；BuildKit secret mount 仅在相应构建指令执行期间提供秘密。[Docker Build secrets](https://docs.docker.com/build/building/secrets/) | 业务 `.env` 不参与构建。若未来模型源需要鉴权，仅为下载指令提供 secret mount，避免复制凭据到输出或日志。 |
| 容器环境 | Compose 的 `environment` 或 `env_file` 可以设置容器变量；项目 `.env` 用于 Compose 插值，不能假设它自动进入容器。[Docker 环境变量指南](https://docs.docker.com/compose/how-tos/environment-variables/set-environment-variables/) | 镜像构建不读取 `.env`。部署方在运行容器时注入配置，使同一镜像可以用于不同环境。 |

## 模型在构建时下载

PaddleOCR 官方提供推理模型下载，并支持通过 `text_detection_model_dir`、`text_recognition_model_dir` 指定本地模型目录。因此“构建时下载，运行时从镜像内本地目录加载”符合其接口。[PaddleOCR 使用教程](https://www.paddleocr.ai/latest/en/version3.x/pipeline_usage/OCR.html)

本项目的 [`model_manifest.json`](../../backend/app/ocr/model_manifest.json) 已记录 `PP-OCRv6_small_det`、`PP-OCRv6_small_rec` 的官方 BOS 地址、压缩包 SHA-256 和文件 SHA-256。这些是项目已锁定的数据；本次只核对官方文档和本地 manifest，**未重新下载大文件验证远程内容**。

设计建议：独立 Docker 构建阶段读取 manifest → 下载到临时目录 → 校验压缩包 hash → 安全解包（拒绝路径穿越和危险链接）→ 校验必需文件 hash → 仅复制模型目录进最终镜像。失败即终止构建，不回退到浮动最新版或首次启动下载。Git 保留 manifest 和说明，二进制留在模型源及镜像层；缓存命中应仍以锁定 manifest 为依据。

## 自动化边界

本项目的 Actions 流程止于构建、验证和 GHCR 发布，不读取业务 Secrets，也不连接运行环境。`.env` 及容器启动参数由实际部署方管理。构建层缓存只用于提速，模型真实性仍由固定 manifest 和 SHA-256 校验保证。
