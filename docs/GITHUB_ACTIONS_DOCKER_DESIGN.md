# GitHub Actions 镜像构建与自动部署方案

状态：方案已实现于 `codex/github-actions-docker` 分支，具体操作见
[配置说明](GITHUB_ACTIONS_SETUP.md)。本文保留设计依据；尚未向远程发布或部署。日期：2026-09-10。

## 1. 推荐决策

采用「官方模型下载 → 校验 → 内置后端镜像 → GitHub Actions 发布 GHCR → 生产部署时注入配置」。前后端分别发布镜像，正式平台固定 `linux/amd64`。

构建不读取生产 `.env`，同一镜像可以用于不同环境。首次登记钉钉凭据、服务器连接和部署参数后，后续版本自动构建、检查、发布；若接入部署服务器，再自动拉取和启动。

这里避免的是模型二进制进入 Git 仓库和 Git 历史。模型内置镜像后仍会作为镜像层存储于 GHCR；若要求模型完全不存储在 GitHub 基础设施，应换用企业镜像仓库，例如现有 ACR/Harbor，并保留同一构建流程。

## 2. 项目现状与必须调整的点

| 现状 | 设计处理 |
| --- | --- |
| `backend/app/ocr/model_manifest.json` 已有两份模型的官方 URL、归档 SHA-256、逐文件 SHA-256 和目录清单 SHA-256 | 继续作为唯一版本依据，不在工作流重复维护 URL/hash |
| `model_artifacts.py` 已验证目录精确文件集合和 SHA-256 | 下载脚本复用校验逻辑，构建和运行期都保留校验 |
| 当前 Dockerfile 不内置模型，Compose 强制挂载宿主模型目录 | 生产 Compose 移除模型挂载，否则会遮盖镜像中的模型 |
| 当前 Web 镜像依赖宿主机挂载 Nginx 配置 | 把 `nginx/default.conf` 和 `expense-proxy.conf` 烘焙进 Web 镜像 |
| Excel 模板已在后端 `COPY app` 范围内，但 Compose 又有模板挂载 | 生产默认用镜像模板；企业自定义模板作为显式覆盖项 |
| `make deploy` 会本机构建并校验宿主机模型 | 保留开发路径，新增生产镜像部署入口，不复用此目标 |
| Paddle 依赖和生产校验限制 Linux x86_64 | 第一版只发布 amd64，目标 CPU 需支持 AVX |
| 根 `.env*` 已被 Git 忽略，两个 `.dockerignore` 尚未显式排除环境文件 | 补齐构建上下文排除规则，不依赖 `.gitignore` 代替 Docker 排除规则 |

本次检查只读取环境示例，没有读取本地真实 `.env` 内容。`backend/models.zip` 当前为未跟踪文件，现有 `backend/models/*` 规则不覆盖它；落地时应补充忽略，保留本地文件。

## 3. 构建时下载模型

新增 `backend/scripts/download-ocr-models.py`，以 manifest 和目标目录为输入，流程如下：

1. 从 manifest 指定的 HTTPS 官方地址下载两份推理模型，设置超时、有限重试和大小限制；下载到临时目录。
2. 先校验归档 SHA-256；不一致立即失败，禁止自动接受新的 hash 或临时降级关闭 OCR。
3. 解包前检查成员：拒绝绝对路径、`..` 路径、符号/硬链接、设备文件、重复路径和额外文件，并限制解压总量。把归档外层目录规范化成 manifest 的 `directoryName`。
4. 复用现有校验逻辑验证每个文件及目录清单；全部通过才发布到输出目录。
5. 下载或校验失败时构建失败；缓存损坏也必须通过同样的验证。

后端 Dockerfile 增加独立 `models` stage：只复制 manifest、校验模块和下载脚本，执行下载。runtime stage 用 `COPY --from=models` 放到 `/opt/expense/models`，由 root 拥有、非 root 应用可读不可写。把两个默认模型目录设为镜像环境变量，沿用现有禁止运行时下载的行为。

这个 stage 放在业务代码变化影响之外：仅修改业务代码时可复用模型层；修改 manifest 或下载逻辑时才重新下载。官方源不可用时有限重试后失败；确有稳定性需求再配置企业对象存储镜像源，但必须匹配同一个固定 hash。首次构建和无缓存构建都必须能成功。

两份模型采用仓库现有 `PP-OCRv6_small_det` 和 `PP-OCRv6_small_rec`，不让 Paddle 自动选择最新版。不另建模型 Git 仓库、不使用 Git LFS、不上传 `models.zip` 到 Release。模型许可证/NOTICE 应随制品保留或附带发布材料，正式实施时核对当前制品的分发要求。

## 4. GitHub Actions 工作流

建议两个工作流：`docker-publish.yml` 管构建发布，`deploy.yml` 管生产部署。

| 事件 | 动作 |
| --- | --- |
| Pull request | 后端测试/Lint、前端测试/类型检查/Lint/构建、Nginx 策略检查及 Docker 构建验证；不读取生产 Secrets、不推送镜像 |
| 默认分支 push | 通过检查后发布开发镜像，以完整 commit SHA 标识；不自动替换生产 |
| 受保护的 `v*` 版本 tag | 对 tag 对应 commit 执行完整门禁，发布版本镜像和双镜像 digest 清单，成功后触发生产部署 |
| `workflow_dispatch` | 重跑指定的受信任版本部署；不允许任意 PR 代码访问生产环境 |

镜像名建议 `ghcr.io/<owner>/dingtalk-expense-backend` 和 `ghcr.io/<owner>/dingtalk-expense-web`，owner/name 规范化为小写。人读版本标签用于查找，实际部署固定 `@sha256:...`，发布清单记录前后端 digest、源码 commit、模型 manifest hash。

使用 Buildx/BuildKit 和官方 Docker Actions；实施时将第三方 Actions 固定到审核过的完整 commit SHA。两个镜像使用独立 cache scope（如 `backend-amd64`、`web-amd64`），缓存只优化速度，不能代替校验。基础镜像现有版本在实施时核验可拉取，并进一步固定 digest。

发布 job 只授予 `contents: read`、`packages: write`，用临时 `GITHUB_TOKEN` 登录 GHCR，不另存长期推送 PAT。PR 检查 job 仅有读取权限；避免通过 `pull_request_target` 执行不受信任代码。环境 Secrets 仅部署 job 可用。

正式发布的镜像必须先通过最终镜像验证再推广为版本：Buildx 构建并加载最终镜像，运行 smoke 后推送同一镜像，或先推送候选 digest、验证后推广该 digest；不能检查一次、再无约束重建另一个镜像用于上线。前后端均成功才产出可部署发布清单。

来源：[GitHub 发布 Docker 镜像](https://docs.github.com/en/actions/use-cases-and-examples/publishing-packages/publishing-docker-images)、[Docker Actions 缓存](https://docs.docker.com/build/ci/github-actions/cache/)。

## 5. `.env` 自动化

### 构建与运行的边界

构建 job 无需生产配置，不把 `.env` 复制进镜像、不用 build args 传递钉钉凭据。前端现在请求同源 `/api`，无需按生产域名重新构建；任何 `VITE_*` 值都应视为浏览器可见的公开值。

应用已使用 Pydantic Settings，容器可以直接接收环境变量，不需要内部 `.env` 文件。生产 Compose 建议显式枚举所需环境变量，非敏感默认值保持在 Compose 中，必填变量使用 `${NAME:?required}` 提前拒绝缺项。

### 一次登记，后续复用

| 位置 | 保存内容 |
| --- | --- |
| 仓库 | 示例键名、安全默认值、部署 Compose、部署脚本；不保存真实凭据 |
| GitHub Environment `production` Secrets | `DINGTALK_CLIENT_SECRET`、稳定的 `SESSION_SECRET`、SSH 私钥；私有仓库拉取凭据按需配置 |
| 同一 Environment Variables | 钉钉 Client/Corp/Agent ID、管理员 ID、端口、绑定地址、Cookie HTTPS 开关、部署主机/用户/目录及可信 host key 等；组织认为敏感的值也放 Secrets |
| 发布清单 | 当前版本对应的前后端完整 digest 引用，不含凭据 |
| 服务器受保护运行配置 | 部署时自动生成并原子替换，用于重启、维护和后续升级 |

`SESSION_SECRET` 首次生成一次至少 32 字节随机值并保存在 Secrets；不能每次构建/部署重新生成，否则已有登录会失效。钉钉平台发放的 Secret、应用授权和域名配置仍需管理员首次完成；后续发布无需重复填写。

部署 job 通过环境映射把各项 Secrets/Variables 交给受控脚本，脚本生成结构化配置并经 SSH 加密传输。禁止直接把 `${{ secrets.X }}` 拼接成 shell 源代码，禁止 `set -x` 或输出完整 Compose 配置。

服务器脚本以 `umask 077` 写临时文件，校验后原子替换 `/opt/dingtalk-expense/.env.production`（0600）。部署 Compose 通过 `--env-file` 读取并映射到容器。实现时必须处理 `$`、引号、空格、反斜杠等 dotenv 转义；对不支持的换行或非法值明确拒绝，不能用无转义 heredoc/简单 echo 拼接凭据。用包含特殊字符的假凭据验证容器最终读到的值一致。

非交互部署应清理可能覆盖 `--env-file` 的同名继承环境变量；远端维护统一走部署脚本。只执行 `docker compose ... config --quiet`，不把展开后的配置上传 artifact 或写日志。临时传输文件、SSH key 和 registry 临时登录配置用结束清理机制删除。[Compose 插值、转义及优先级](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/)

若未来需要私有模型下载令牌，单独通过 BuildKit secret mount 传给下载 stage，不用 `ARG`/`ENV`；目前 manifest 的公开官方源不需要令牌。[Docker 构建密钥](https://docs.docker.com/build/building/secrets/)

不建议第一版引入 Compose secrets 文件：现有设置读取环境变量，要切换文件挂载还需实现 `_FILE`/`secrets_dir` 并验证配置优先级。受保护的宿主文件加环境注入更贴合当前代码；Docker 管理员仍可读取容器环境，不能把它当作对宿主管理员保密。

## 6. 自动部署链路（以单台 Linux 服务器为默认假设）

1. 发布成功后，受保护版本的部署 job 进入 `production` Environment。仅这个 job 读取部署配置，禁止任意 ref；生产 concurrency 串行执行，正在部署的任务不被新任务强制取消，服务器侧也加锁。
2. 用受限 SSH 部署账号连接服务器并验证预置 host key，不关闭主机验证。服务器需预装 Docker/Compose，网络需能拉取目标镜像仓库。当前并未检查实际服务器连通性。
3. 把同一发布版本的部署文件、发布清单和受保护配置传到版本目录。生产用独立 `compose.production.yml`，无 `build`、无源码或模型挂载，沿用非 root、只读根、tmpfs、资源限制和健康检查。
4. 固定 Compose project name，并保持现有数据卷归属。升级前识别现有 `sqlite_data` 和 `reimbursement_staging`，防止因项目名变化启动成空库；绝不运行 `down -v`。
5. 验证配置及磁盘空间，拉取两个 digest。私有 GHCR 由服务器持有最小 `read:packages` 拉取凭据；不要把 Actions 的临时推送 token 当作服务器长期凭据。镜像若公开则无需该凭据。
6. 停止旧后端写入，备份 SQLite 与持久暂存数据的一致状态，再启动新版本（启动脚本已自动执行 `alembic upgrade head`）。这条单机 SQLite 路径允许短暂维护窗口，不承诺零停机。
7. 执行 `docker compose ... up -d --no-build --wait --wait-timeout 180`（超时按首次 OCR 实测调整），再经 Web 入口检查 `/api/ready`，成功后才标记版本生效。
8. 失败时保留受限诊断信息和旧版本记录。只有迁移明确向后兼容才自动回切旧镜像；其他情况停止继续发布并通知，按匹配版本恢复数据库和持久材料。不能把镜像回退等同于数据库回退。

现有 `DINGTALK_OA_WORKER_ENABLED=false` 的首次上线要求保留。完成钉钉权限/模板/任务检查和一次真实验收后，在 Environment 中明确启用，后续部署沿用该值；流水线不能因为 readiness 成功自行启用正式 OA 提交。

## 7. 落地文件与验收

拟新增：

- `.github/workflows/docker-publish.yml`、`.github/workflows/deploy.yml`。
- `backend/scripts/download-ocr-models.py` 及针对 hash 不符、路径穿越、额外文件的测试。
- `compose.production.yml`、`scripts/deploy-production.sh` 和配置序列化/校验脚本。

拟修改：

- 后端 Dockerfile 增加模型 stage；前端 Dockerfile 内置 Nginx 配置。Web 构建需要访问根 `nginx/`，建议改用根构建上下文、显式 COPY `frontend/` 与 `nginx/`，同步本地 Compose 并添加根 `.dockerignore`。
- `.dockerignore` 排除 `.env`、`.env.*`、`.git`、模型压缩包/本地目录、依赖目录和运行数据；`.gitignore` 补上 `backend/models.zip`。
- 生产环境示例移除必填宿主模型目录，README/模型说明/Makefile 增加镜像部署路径，明确与原开发路径的区别。

验收标准：

1. 全新检出、没有 `.env` 和本地模型时也能构建两份正式镜像。
2. 模型损坏、归档越界或官方源下载失败时构建明确失败；缓存命中不改变校验结果。
3. 最终后端镜像在 `--network none`、非 root 和只读根条件下，使用临时数据卷完成真实 Paddle 初始化及空白图推理；测试凭据全部临时生成，OA worker 关闭，不连接钉钉。
4. 原生 Linux/amd64 CI 上检查迁移与 readiness；完整前后端集成检查 Web 页面及 `/api/ready`，同时执行实际 Nginx 配置检查。不能只检查 manifest 而忽略真实推理。
5. 镜像配置、文件层、缓存、前端产物和 CI artifact 不含测试 Secret 标记或任何 `.env`；dotenv 特殊字符往返一致。
6. 第二次部署保留数据库、持久材料及 SESSION_SECRET；失败不标记为成功，备份恢复路径有可验证演练。
7. 实际生产凭据、目标机 AVX/资源、钉钉权限和业务验收单独验证；CI 的合成配置不能代替这些检查。

本地已完成官方模型下载/校验、两份 Linux/amd64 镜像构建以及断网真实 OCR、迁移、readiness 和 Web/API 检查。GitHub 远程运行与真实服务器部署仍需首次登记环境后验证。官方资料核对见 [研究记录](research/github-actions-docker-sources.md)。
