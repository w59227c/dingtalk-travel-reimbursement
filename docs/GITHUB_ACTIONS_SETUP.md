# GitHub Actions 镜像发布与自动部署

代码提供两个工作流：`docker-publish.yml` 构建并发布前后端镜像；`deploy.yml` 通过 SSH 部署到单台 Linux/amd64 服务器。模型从官方地址在构建时下载并校验，服务器无需保存模型、源码或 Nginx 配置。

## 1. 镜像发布

- PR 执行后端/前端测试、Lint、构建及真实 Docker smoke，不使用生产凭据。
- `main` 通过门禁后发布 `sha-<完整 commit SHA>` 镜像。
- 受保护的 `v*` tag 还发布同名版本标签；不受保护的版本 tag 会明确失败。
- 每次成功发布生成 `release-<commit>-<attempt>` artifact，内含 `release.json`，记录两份镜像的完整 digest、源码来源、commit 和模型清单 hash。部署固定 digest，不使用 `latest`。

当前镜像名：

```text
ghcr.io/w59227c/dingtalk-expense-backend
ghcr.io/w59227c/dingtalk-expense-web
```

工作流按仓库 owner 自动生成小写名称。使用临时 `GITHUB_TOKEN` 推送，只在发布 job 授予 `packages: write`。首次包通常为 private；如已有同名包，需要允许本仓库的 Actions 访问。构建 job 不需要 `.env` 或钉钉 Secret。

在 GitHub 仓库 Settings → Rules → Rulesets 为 `v*` 创建并启用 tag 规则，限制正式版本的创建/更新者；分支检查及环境访问也应只允许可信维护者。规则需真的生效，使 `github.ref_protected` 为 true。

## 2. 首次登记生产环境

创建 GitHub Environment `production`，限制部署来源为正式的 `v*` tag。自动部署不要求每次人工审批；若组织已启用 required reviewers 则沿用其策略。

配置以下 **Environment Secrets**：

| 名称 | 内容 |
| --- | --- |
| `DINGTALK_CLIENT_SECRET` | 钉钉应用凭据 |
| `SESSION_SECRET` | 用 `openssl rand -hex 32` 首次生成一次并保存，后续发布保持不变 |
| `DEPLOY_SSH_KEY` | 部署账户 SSH 私钥，与服务器上的公钥配对 |

配置以下 **Environment Variables**：

| 名称 | 内容/默认值 |
| --- | --- |
| `DINGTALK_CLIENT_ID`、`DINGTALK_CORP_ID`、`DINGTALK_AGENT_ID` | 钉钉平台提供的 ID；Agent ID 为正整数 |
| `ADMIN_USER_IDS` | 初始管理员 userId，多个以逗号分隔 |
| `DEPLOY_HOST`、`DEPLOY_USER` | 服务器主机名/IPv4 和部署账号 |
| `DEPLOY_KNOWN_HOSTS` | 经可信渠道核对的完整 SSH known_hosts 行；非 22 端口需匹配 `[host]:port` |
| `DEPLOY_PORT` | 默认 `22` |
| `DEPLOY_DIR` | 默认 `/opt/dingtalk-expense`，必须为此应用专用目录 |
| `APP_BIND_ADDRESS` | 默认 `127.0.0.1`；员工直连时按入口方案设为 `0.0.0.0` |
| `APP_PORT` | 默认 `8080` |
| `SESSION_COOKIE_SECURE` | 默认 `false`；所有浏览器入口均为 HTTPS 时设为 `true` |
| `DINGTALK_OA_WORKER_ENABLED` | 默认 `false`；完成首次真实验收后再明确改为 `true` |
| `DINGTALK_APPROVAL_DETAIL_URL_TEMPLATE` | 可留空，需配置时沿用现有模板规则 |
| `BACKEND_MEMORY_LIMIT` | 默认 `3g` |
| `REIMBURSEMENT_STAGING_MAX_BYTES` | 默认 `4294967296` |

最后在 **Repository Variables** 设置 `DEPLOY_ENABLED=true` 才启用自动部署；不要只把它设置在 Environment 中，因为调用部署工作流前就要判断这个开关。只想自动制作镜像时，无需配置任何生产值或开启此开关。

其他运行限制仍在 `compose.production.yml` 中有默认值；自动部署需要覆盖新增参数时，将其明确加入 `deploy.yml` 的环境映射，不能假设所有 GitHub Variables 自动进入容器。

## 3. 准备服务器

服务器需原生 Linux/amd64、支持 AVX 的 CPU、Docker、支持 `up --wait` 的 Docker Compose、Bash、Python 3.10+ 和 `flock`。部署账号需有专用目录写权限及 Docker 权限；Docker 使用本机默认 context。为镜像、持久数据和升级备份预留磁盘空间。

私有 GHCR 镜像需在服务器上以部署账号一次登录，使用最小 `read:packages` classic PAT，通过 `docker login ghcr.io --username <账号> --password-stdin` 输入；有组织 SSO 时还需完成对应授权。凭据保存在该部署账号的 Docker 配置中，后续拉取自动复用。公开镜像无需登录。不要把工作流的短期 `GITHUB_TOKEN` 保存为服务器长期凭据。

默认项目名沿用 `dingtalk-travel-reimbursement`，继续使用：

```text
dingtalk-travel-reimbursement_sqlite_data
dingtalk-travel-reimbursement_reimbursement_staging
```

升级时检查现存卷标签、旧容器挂载及 SQLite 路径，拒绝明显不一致或仅存在一个数据卷的状态。这条路径只支持 `/app/data/app.db` SQLite 单副本部署。旧部署若使用自定义项目名、外部数据库、宿主目录存储或自定义 Excel 模板，应先按其现状迁移/适配，不能直接用默认新卷代替原数据。

## 4. 日常发布与配置管理

推送受保护版本 tag 后，流水线自动完成：

```text
测试 → 模型下载校验 → 构建最终镜像 → 断网 OCR + Web/API 检查
→ 推送同一镜像 → 生成双镜像发布清单 → SSH 下发版本与运行配置
→ 拉取并核对镜像来源/版本 → 停止写入 → 备份数据库与持久材料
→ 自动迁移、启动和健康检查 → 更新 current 指针
```

服务器目录结构：

```text
/opt/dingtalk-expense/
├── current -> releases/<版本目录>
├── releases/<版本目录>/
│   ├── compose.production.yml
│   ├── .env.production         # 自动生成，0600，不打印到日志
│   ├── release.json
│   ├── deploy-production.sh
│   └── render-production-env.py
└── backups/<版本目录>/         # 私有目录，数据库和材料的一致性备份
```

`.env.production` 根据明确的参数列表生成，拒绝缺少必填值、换行和不支持的生产配置。处理 `$`、双引号、单引号、反斜杠；生成文件供 Compose 使用，**不要 `source` 成 shell 脚本**。应用通过容器环境变量读取配置，镜像内没有生产 `.env`。

更新 GitHub Secrets/Variables 后，在 Actions → Deploy production 选择原受保护版本 tag，填入该版本 `release.json` 的两份 digest 即可重部署配置。后端和前端必须来自同一源码版本；服务器会检查镜像 OCI source/revision 和模型清单，拒绝版本混配。不要每次重新生成 `SESSION_SECRET`。

GitHub 运行状态会报告发布/部署失败。上线有短暂停机窗口；失败时保留版本、备份和容器状态，停止可能继续写入的新容器，不自动回滚数据库。`current` 仅成功后更新，但其存在不代表失败后服务仍在运行。旧镜像可能无法读取新 schema；恢复应选择配套旧镜像、SQLite 与持久材料，按维护窗口恢复，不能只切换镜像。备份含敏感业务数据，按组织保留周期清理，脚本不会自行删除历史备份或数据卷。

## 5. 本地验证

以下构建不需要 `.env` 或宿主模型目录：

```bash
docker build --platform linux/amd64 -t expense-backend:check backend
docker build --platform linux/amd64 -f frontend/Dockerfile -t expense-web:check .
bash scripts/smoke-images.sh expense-backend:check expense-web:check
```

smoke 使用独立临时容器及合成凭据，验证真实 OCR 初始化/推理、迁移、readiness 和 Web/API 后自动清理，不访问钉钉或现存业务卷。Apple Silicon 可以模拟执行，正式服务器仍应是原生 amd64。

仅检查下载器可使用一个尚不存在的输出目录：

```bash
python3 backend/scripts/download-ocr-models.py --output /tmp/expense-ocr-models
```

再次执行会重新校验已有目录；不会接受内容变化的缓存。默认开发 Compose 仍支持宿主模型、模板和 Nginx 挂载及 `make deploy` 的原有本机构建方式；正式镜像部署使用独立 `compose.production.yml`。

设计依据见 [方案](GITHUB_ACTIONS_DOCKER_DESIGN.md) 和 [官方资料核对](research/github-actions-docker-sources.md)。
