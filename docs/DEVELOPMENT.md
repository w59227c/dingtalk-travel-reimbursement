# 本地开发与联调

本文面向从源码开发和本地联调。命令均在仓库根目录执行；生产服务器使用发布镜像时见 [服务器部署](deployment/README.md)。

## 项目结构

- `backend/app/`：FastAPI 接口、领域规则、OCR 和 OA 提交服务。
- `backend/migrations/`：SQLite/Alembic 数据库迁移；生产启动时自动升级。
- `backend/tests/`：后端单元、集成、迁移和安全回归测试。
- `frontend/src/`：Vue 页面、组件、状态管理和 API 客户端。
- `nginx/`：生产静态页面与同源 `/api` 反向代理配置，由 Compose 实际挂载。
- `docs/`：产品设计、实现计划和已完成调研。
- `scripts/`：开发启动、Nginx 策略检查和 OCR 制品校验工具。

本机 `.env`、SQLite 数据、上传材料、OCR 模型和依赖目录均已忽略。`make clean` 只删除
Python/前端缓存和可重新生成的构建产物，不会删除这些运行数据，也不会删除虚拟环境或已安装依赖。
临时导出的企业模板或诊断数据必须使用 `*.private.json` 或 `production-oa-*.json` 文件名，
这两类文件会在全仓库范围内被 Git 忽略。

## 依赖与版本

- 前端提交 npm v3 `package-lock.json`，本地安装与容器构建统一使用 `npm ci`，不允许在构建阶段改写锁文件；当前锁已通过全新 `npm ci`、测试、类型检查、Lint 和构建验证。
- 前端使用 TypeScript 官方的 7/6 并行过渡方案：`@typescript/native` 提供 TypeScript 7 原生 `tsc`，`typescript` 别名指向 TypeScript 6 API，供尚未兼容原生编译器 API 的 `vue-tsc` 和 `typescript-eslint` 使用。`npm run typecheck` 会同时运行两条检查链，不能删除其中任一依赖后只验证另一条。
- 容器构建使用 Node 24 LTS，后端运行时使用 Python 3.13.15；uv 固定为 0.12.9，入口使用 Nginx 1.30.4 stable。运行时和依赖锁按 Python 3.13 验证。
- 本地 Python 由 `backend/.python-version` 固定为 3.13.15 并交给 uv 管理，不要求通过 Homebrew 安装 Python；macOS 自带 Python 不参与后端运行。
- 后端业务调用钉钉继续使用 `httpx` 0.28.1；测试环境另外锁定 `httpx2` 2.12.0，专供新版 Starlette `TestClient` 使用，不混用两套客户端处理业务请求。
- 当前 npm 11 标准生成结果并未为全部依赖条目写入 `resolved`/`integrity`。这里不手工拼接 URL 或哈希，也不宣称具备完整的锁文件校验和覆盖；若上线环境把完整哈希作为供应链硬性要求，应在干净 npm 环境中重新生成并单独复核后再发布。
- 后端已使用 uv 生成并提交 `uv.lock`；本地安装、检查与容器构建统一使用 `--frozen`，确保声明与锁文件不一致时立即失败。

## 本地普通浏览器联调

先安装已锁定依赖：

```bash
make backend-install
make frontend-install
```

然后在仓库根目录运行：

```bash
make dev
```

需要把日志分开时，仍可在两个终端分别执行 `make dev-backend` 和 `make dev-frontend`。

后端脚本先对 `backend/data/dev.db` 执行 Alembic migration，再监听
`http://127.0.0.1:8000`；前端监听 `http://127.0.0.1:5173` 并将 `/api`
同源代理到后端。打开前端地址后点击“使用固定测试身份”。脚本会强制使用合成的
`local-dev-user / 本地开发用户 / 本地开发部门`，该身份同时是本地管理员；请求不能覆盖它。
`make backend-install` 已包含锁定的 OCR 依赖。`make dev-backend` 默认校验本地模型并启用
PP-OCRv6 small 模型与 ONNX Runtime CPU；依赖或模型缺失时直接拒绝启动，不会静默关闭 OCR。

### macOS arm64 本地 OCR 联调

当前 Apple Silicon Mac 可以使用官方 macOS arm64 CPU wheel 做开发测试；不启用 GPU 或 HPI，
也不改变“生产 OCR 必须运行在 Linux x86_64/amd64”的部署边界。本地开发默认启用真实本地 OCR：

1. 按 [模型目录说明](../backend/models/README.md) 从官方地址取得两个推理模型，记录原始归档
   SHA-256，并解压到指定的两个目录。模型文件不提交仓库。
2. 安装锁定的 macOS arm64 OCR 依赖，先核对模型固定 SHA 清单，再用 ONNX Runtime 识别合成出租车票据和扫描行程单：

   ```bash
   make backend-install
   make ocr-models-check
   make ocr-runtime-check
   ```

3. 启动默认的本地 OCR 后端和前端：

   ```bash
   make dev
   ```

4. 访问 `http://127.0.0.1:8000/api/ready`；`ocr` 应为 `configured`。随后只用脱敏、独立单票据
   JPG/PNG/单页 PDF 做 smoke test，核对日期、金额、类型和路线，而不是只看是否返回文字。

`dev-backend` 固定使用合成管理员身份、本地 CPU、显式模型目录以及
`PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=1`，不会回退到云服务或在运行时下载模型。它会先拒绝
缺失、被修改或与固定 SHA 清单不一致的模型目录。

不要复制这组开发值到生产配置。`APP_ENV=production` 时，Mock、空或占位
钉钉配置、短 Session Secret、Fake OCR 都会使配置校验直接失败。

## 钉钉免登配置

后端使用固定的组织应用配置完成免登，浏览器只会读取 `DINGTALK_CLIENT_ID` 和
`DINGTALK_CORP_ID`；`DINGTALK_CLIENT_SECRET`、`DINGTALK_AGENT_ID` 与 `SESSION_SECRET`
只存在后端环境变量中。
钉钉应用需要具备免登码换用户、读取用户详情和读取部门详情的权限，并将实际部署的
HTTP 或 HTTPS 地址按钉钉开放平台要求配置到应用中。首次部署使用 `ADMIN_USER_IDS` 配置至少一个初始管理员
钉钉 userId，多个值用英文逗号分隔。该列表始终生效且不能从页面移除；初始管理员登录后
可在系统设置中新增或撤销其他管理员，避免因误操作失去管理入口。配置文件中的全部管理员和页面新增管理员具有同等管理功能；所有管理员接口按当前配置逐请求鉴权，写操作要求 CSRF。普通员工不显示设置入口，直接访问桌面或移动设置路由时返回对应报销页；设置页进入前重新核对登录角色，前端获知撤权后隐藏管理组件。普通员工仅能读取用于报销计算的标准配置，不能读取管理员列表或修改设置。

本地普通浏览器调试可以显式设置 `APP_ENV=development`、
`AUTH_MOCK_ENABLED=true` 和固定的 `AUTH_MOCK_USER_ID`、
`AUTH_MOCK_USER_NAME`、`AUTH_MOCK_DEPARTMENTS`。Mock 接口不接受请求传入的身份或管理员
标记，且生产配置发现 Mock 开启会直接拒绝启动。`.env.example` 默认关闭 Mock。HTTP 入口使用
`SESSION_COOKIE_SECURE=false`；只有确保所有用户入口都是 HTTPS 时才设为 `true`。生产环境仍需配置至少 32 字符的随机
`SESSION_SECRET`。

当前免登 API：

- `GET /api/config/public`：返回页面标题、CorpId、Client ID、开发 Mock 开关、上传/费用条数限制及布尔值 `oaSubmissionEnabled`；后者直接对应 OA worker 配置，不返回应用 Secret 或 AgentId。
- `POST /api/auth/dingtalk`：接收一次性 `authCode` 并建立服务端 Session。
- `GET /api/me`：读取当前身份并返回仅保存在前端内存中的 CSRF token；同一登录 Session 内保持稳定。
- `POST /api/me/department/from-travel-approval`：核验出差审批并按当前有效部门规则确定本次报销部门；历史发起部门无法匹配且存在多个当前部门时，返回过滤、去重后的部门候选供员工确认。
- `POST /api/auth/logout`：校验 CSRF 后注销服务端 Session。

预算选项、设置读取和全部管理接口都要求 Session；写接口还要求
`X-CSRF-Token`。开发环境用 Vite `/api` 同源代理，生产环境由 Nginx 同源反向代理，不启用
携带凭据的通配 CORS。

### 从钉钉测试真实 dev 免登

普通 `make dev-backend` 始终使用合成身份，不会调用钉钉。需要联调真实免登时，可以创建
本机私密配置并填写真实组织应用信息：

```bash
cp .env.dingtalk-dev.example .env.dingtalk-dev
```

`.env.dingtalk-dev` 已被 Git 忽略，但不是必需文件：文件不存在时，脚本直接读取当前终端
已有的环境变量；显式设置 `DINGTALK_DEV_ENV_FILE` 后，指定文件不存在则拒绝启动。后端始终
要求真实的 `DINGTALK_CLIENT_ID`、`DINGTALK_CLIENT_SECRET`、`DINGTALK_CORP_ID` 和
`DINGTALK_AGENT_ID`。
开发配置中的 `SESSION_SECRET` 可留空，脚本会为本次进程生成随机值；重启后已有开发 Session
失效是预期行为。生产环境仍必须显式提供至少 32 字符的持久随机值。

一个命令同时启动后端和前端：

```bash
make dev-dingtalk
```

使用正式公司配置在本机联调（无需 Docker）：

```bash
make dev-dingtalk-prod
```

读取根目录现有 `.env`，以开发运行模式启动真实免登及 OCR：前端 5173，后端 8000。
启动前先停止占用这些端口的开发服务或 Docker 服务。
数据库使用 `backend/data/dev-dingtalk-prod.db`，附件使用
`backend/data/reimbursement-staging-prod`，与测试公司及 Docker 数据隔离。
OA 开关沿用 `.env`，开启后提交会创建正式公司的审批。
新数据库仍需管理员确认模板目录；此命令不会自动导入待确认 JSON 或绕过关联验收。
将来 Docker 部署仍使用其自身数据卷，不会自动迁移本地联调的配置和记录。

需要把日志分开时，仍可在两个终端分别执行 `make dev-dingtalk-backend` 和
`make dev-dingtalk-frontend`。

新模式固定关闭 `AUTH_MOCK_ENABLED`，使用独立的 `backend/data/dev-dingtalk.db` 和
`/tmp/dingtalk-travel-reimbursement-dingtalk-dev`。前端仍通过 Vite 将同源 `/api` 代理到本机后端。
`dev-dingtalk-frontend` 会显式设置 `VITE_DINGTALK_REMOTE_DEBUG=true`，按需动态加载锁定的
`dingtalk-h5-remote-debug@0.1.3`；只有钉钉调试平台生成的调试链接才会继续加载远程调试 SDK。
真实钉钉联调后端不启用 Uvicorn 热重载，以免 macOS 的重载子进程与图片/OCR 隔离子进程冲突；
修改后端代码后手动重启 `make dev-dingtalk` 即可。
普通 `dev-frontend`、测试和生产构建默认不初始化该工具。
PC 钉钉本机调试时，不配置 `DINGTALK_DEV_PUBLIC_HOST`，保持
`DINGTALK_DEV_COOKIE_SECURE=false`，在官方四端调试工具中填写
`http://127.0.0.1:5173`。如果改用外部 HTTPS 入口，则把
`DINGTALK_DEV_PUBLIC_HOST` 设置为不含协议、端口和路径的可信域名，将
`DINGTALK_DEV_COOKIE_SECURE=true`，并把该入口反向代理或安全隧道转发到前端端口；HTTPS
入口还必须支持 WebSocket 才能使用 Vite 热更新。若入口运行在本机，保持
`DINGTALK_DEV_BIND_ADDRESS=127.0.0.1`；只有可信代理从局域网连接时才改为 `0.0.0.0`。
最终必须从钉钉调试工具、工作台或钉钉内置浏览器打开配置地址，验证
`dd.requestAuthCode -> /api/auth/dingtalk -> /api/me`，而不是直接点击开发 Mock。
