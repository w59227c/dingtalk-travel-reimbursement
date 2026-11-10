# 运行、运维与源码部署

本文记录就绪检查、数据备份、运行边界及源码构建部署。使用 GHCR 镜像的部署、更新和回滚见 [服务器部署](deployment/README.md)。

## 存活、就绪与部署边界

- `GET /api/health` 只表示 FastAPI 进程存活，不访问依赖。
- `GET /api/ready` 检查 SQLite 连接、当前 Alembic revision 与关键表字段、Excel 模板完整契约、临时目录可写性，以及
  报销持久暂存目录的受控权限、写入/删除能力和最小可用空间，并检查
  已启用 OCR 的模型精确文件集合、固定 SHA 清单和锁定包版本；返回内容只有组件状态，不包含路径、Secret 或异常细节。模型内容校验按文件 stat 指纹缓存，文件变化才重新计算 SHA；就绪检查不会初始化或加载 OCR 模型。
- 模板严格校验结果按文件 stat 指纹缓存在最多 8 个条目的进程内缓存中；模板文件变化会重新
  执行完整 ZIP 与工作簿契约校验，常规 10 秒 readiness 轮询不会重复解析不变的 `.xlsx`。
- OCR 默认启用并纳入就绪检查；模型、固定 SHA 或锁定的 OCR 运行依赖不完整时返回 503，
  不会把缺少识别能力的实例标记为就绪。仅诊断或聚焦测试可显式设置 `OCR_ENABLED=false`。
- 生产启动会在独立受限子进程中完成一次真实模型初始化和空白图片推理；自检失败时 FastAPI 不接受流量。
- Compose 后端 healthcheck 使用 readiness，Web 容器等到后端可接单才启动。Dockerfile 仍以
  固定非 root 用户运行，后端没有宿主端口，只能从 Compose 网络中的 Nginx 访问。

应用入口可以直接使用 HTTP，也可以放在 HTTP 或 HTTPS 反向代理之后。生产配置示例默认
使用 `APP_BIND_ADDRESS=0.0.0.0`，便于员工设备直接访问；如果只允许本机反向代理访问，可改为 `127.0.0.1`。
Nginx 始终以同源方式提供 Vue 和 `/api`。`SESSION_COOKIE_SECURE=false` 兼容 HTTP；
只有所有浏览器入口都启用 HTTPS 时才应设为 `true`。

真实 `DINGTALK_CLIENT_SECRET` 和 `SESSION_SECRET` 由部署平台的 Secret 环境注入或受权限保护的
宿主机环境文件提供，不提交仓库、不进入镜像，也不要把会展开环境值的 Compose 配置输出保存或
发送。`.env.example` 提供键名和安全默认值，私密凭据留空。

应用 Nginx 关闭版本标识和含查询参数的访问日志，设置 CSP、`nosniff`、no-referrer 与受限
Permissions Policy；`/api`、认证和下载均为 `no-store`，仅 Vite 哈希资源长期缓存。它不依据
`remote_addr` 或调用方提供的 `X-Forwarded-For` 限流：认证、上传、OCR 和 Excel 的 IP/账号限流
由受控的入口层执行，入口层必须丢弃并重建外部转发头。正式材料与票据诊断上传请求均关闭 Nginx request
buffering，并使用同一请求总字节限制；无法避免的 client/proxy temp 路径位于 Web 容器受限 tmpfs，不写镜像层或宿主目录。
Web 容器以非 root、只读根文件系统、全部 capability drop、no-new-privileges、内存和 PID 上限运行。

正式材料和管理员票据诊断共享上传接收总时限：`UPLOAD_RECEIVE_TIMEOUT_SECONDS` 默认 90 秒，可配置为大于 0、至多 300 秒。该时限从后端开始解析 multipart 请求体计算，持续小量发送也不会重置；超时返回 `408 / UPLOAD_RECEIVE_TIMEOUT`，清理本次解析缓存并释放上传准入，已成功上传的其他文件不受影响。该时限不包含后续 OCR 或向钉钉上传附件的时间。
运行 `make nginx-policy-check` 可做仓库内静态策略检查。若本机安装了 Nginx，仍应在实际部署
环境对最终配置执行 `nginx -t`；本检查不冒充完整的 Nginx 解析验证。

Compose 要求显式提供 `APP_ENV`，没有该值会在配置展开阶段失败。开发环境复制
`.env.example` 后得到明确的 `APP_ENV=development`；生产必须显式设置 `APP_ENV=production`，
随后后端启动校验还会拒绝 Mock、占位凭据或短 Secret，不能把 Compose
开发默认误当成生产配置。

Compose 将上传缓存保留在 `/tmp/expense` 的 tmpfs，同时把报销原件、票据汇总 PDF 和待提交 Excel
保存在独立的 `reimbursement_staging` named volume（容器内固定为 `/app/staging`）。两者不能
相同或互相嵌套；不要把持久暂存路径改回 tmpfs。默认逻辑容量上限为 4 GiB，实际总量预留由
报销状态服务在数据库事务中执行。

### 最短生产部署路径

GitHub Actions 自动构建和发布见
[镜像构建与发布配置](GITHUB_ACTIONS_SETUP.md)。该流程在构建时下载并校验 OCR 模型，
镜像内置模型、Excel 模板和 Nginx 配置；流水线不读取生产 `.env`，也不执行服务器部署。
以下保留本地构建和宿主模型挂载的部署方式。

目标服务器使用 Linux x86_64/amd64。Apple Silicon Mac 上的 Docker Compose 会通过
`TARGET_PLATFORM=linux/amd64` 将前后端统一构建为目标架构；本机运行时使用模拟架构，适合
构建和联调，不建议承担正式 OCR：

```bash
cp .env.production.example .env
chmod 600 .env
# 填写钉钉应用凭据和 AgentId、管理员 userId，并用 `openssl rand -hex 32` 生成 SESSION_SECRET
make deploy
```

`make deploy` 会先检查 Compose 展开、Nginx 安全策略和本地 OCR 模型，再构建并等待容器
健康检查通过；可用 `make logs` 查看日志。只想预检而不启动时执行 `make deploy-check`。随后验证
`http://127.0.0.1:8080/api/ready`。如需其他设备直接使用 HTTP，还需在 `.env` 中设置合适的
`APP_BIND_ADDRESS`；如需反向代理，可选用 HTTP 或 HTTPS 入口。发布前可执行
`make verify`，一次完成后端测试/Lint、前端测试/类型检查/Lint/构建和部署静态检查。

代码和环境示例中 `DINGTALK_OA_WORKER_ENABLED` 均默认为 `false`，上述命令不会自动消费已排队的 OA 提交。worker 关闭时，前端根据 `oaSubmissionEnabled=false` 禁用新提交；后端对新提交返回 `503 / OA_SUBMISSION_DISABLED`，不生成快照、不锁定草稿、不创建任务，草稿仍可编辑。已有任务的重复提交和只读查询仍可恢复原记录。开启前必须确认没有会被意外消费的非终态任务。只有数据库迁移、模板 Schema/映射、权限、应用归属、任务清单和 readiness 预检全部通过，且已准备进行单次真实验收或正式接单时，才显式开启。`/api/ready` 只检查本地依赖和钉钉基础配置，不表示 worker 已开启，也不代替真实权限、模板目录或远端调用预检。

## 运维与隐私

- 后端结构化日志记录 request ID、方法、URL path、状态、耗时和安全错误类型；钉钉
  OpenAPI 最终失败时另记录固定操作类型、HTTP 状态和经过字符白名单过滤的上游错误码，
  并通过固定 `failureKind` 区分请求超时和网络通信失败，以区分审批列表、审批详情、创建和附件操作。不记录钉钉响应正文、动态资源标识、查询
  参数、Cookie、票据 OCR 原文、证件号、票号或完整路线。Nginx access log 默认关闭；HTTP error log 输出到 `/dev/null`，因为其原生格式会附带无法脱敏的请求及上游 URL。代理错误仍向客户端返回真实 HTTP 状态，使用 HTTP/readiness 检查和容器状态发现代理故障；启动配置错误仍由 Nginx 向 stderr 报告。
- 管理员诊断通过 `POST /api/admin/oa/travel-approvals/diagnose` 传入目标，要求已认证管理员和 CSRF；员工普通查询仍只使用 Session 本人的 userId。报告不持久化且响应禁止缓存，每个后端进程同时最多执行一次、最长 60 秒；短时目标回查最多读取 60 个实例，不扩大普通员工的访问权限。
- `POST /api/admin/receipts/diagnose` 要求当前有效管理员 Session 和 CSRF；仅处理本次上传的单个 JPG、PNG 或单页 PDF，沿用正式文件大小/内容校验、临时空间配额及同一个有界 FIFO OCR 执行槽。不提供手动参考年份，使用识别流程的默认日期解析。返回禁止缓存，识别原文及分类文字各最多展示 100000 字符并标记截断；普通 OCR API 不返回这些原文。诊断不访问其他员工已有文件、不持久化报告，处理完成、失败及取消退出均清理本次临时文件。
- 票据诊断允许管理员查看自己本次上传材料的识别文字，是原文展示的受限入口；不写入日志或 SQLite。后台日志仍只记录固定操作、状态、安全错误码和请求编号，不记录员工数据、审批标识、票据内容或原始 OCR。
- 未处理异常的日志保留 `INTERNAL_ERROR` 和安全异常类型，钉钉读取超时/网络异常保留固定 `failureKind=timeout/transport`；不记录异常正文、请求对象或 URL，OA 结果不确定时继续遵循回查和人工恢复要求。
- Uvicorn 的服务端异常日志同样通过安全 JSON 格式化，只保留异常类型；关闭其访问日志，不输出原始 traceback。SQLAlchemy 引擎另启用参数隐藏，避免数据库错误附带员工数据或业务绑定值。
- 服务启动和运行期间会清理遗留上传缓存；退出会立即清理当前 Session 尚未落入报销 staging 的缓存，不会越过报销权限和状态机删除持久文件。过期服务端 Session 在启动及请求期按 5 分钟门限清理。
- SQLite 持久卷保存配置、Session、自动保存内容、附件元数据、模板目录、不可变提交快照、任务状态和远端操作检查点。原件、汇总 PDF 和 Excel 保存在独立 `reimbursement_staging` 持久卷。两个持久卷必须做一致性备份和恢复；只恢复一份会造成数据库哈希与文件不匹配，系统应停止提交而不是继续使用错误文件。短期 `sessions` 不作为业务恢复来源。
- 完整恢复演练应在隔离环境恢复同一时点的 SQLite 和 `reimbursement_staging`，执行当前 migration，运行 `/api/ready`，并核对草稿文件 SHA-256、提交快照和远端检查点。只恢复项目和设置的初始化演练不等于业务数据恢复。

## 本地 OCR 制品上架步骤

1. 只从 `backend/app/ocr/model_manifest.json` 记录的官方来源取得
   PP-OCRv6 small 的两套 ONNX 制品。该机器可读文件是来源、归档 SHA、模型文件
   SHA 和目录清单 SHA 的唯一记录。模型二进制不提交仓库。
2. macOS/arm64 可按[本地开发说明](DEVELOPMENT.md#macos-arm64-本地-ocr-联调)做 CPU 开发 smoke；正式环境仍在 Linux/amd64、支持
   AVX 的目标机构建默认包含 OCR 的镜像，源码 Compose 将两个已核验目录从
   `OCR_MODEL_HOST_DIR`（默认 `./backend/models/onnx`）只读挂载。升级旧源码部署时一并更新此路径。
3. Compose 默认启用 OCR 并使用容器内标准模型目录，无需另外设置启用开关；不配置任何云 OCR 地址或 Token。运行时
   不下载模型，也不会在失败时回退付费服务。
4. 先运行 `make ocr-models-check` 和 `make ocr-runtime-check`，再确认 `/api/ready` 为 200；最后
   用公司提供的脱敏独立票据样本执行真实 smoke test。模型目录/依赖不完整时部署保持未就绪，
   修复制品后再上线。

## 正式验收仍依赖的外部输入

- 真实钉钉租户的 CorpId、应用凭据、权限、应用访问地址、多部门用户和管理员账号联调。
- 公司当前正式空白模板，并在公司 Microsoft Excel 中核对打开、打印和版式；现有脱敏模板
  只证明开发闭环。
- 经固定 SHA 校验的 ONNX 模型制品以及公司脱敏 JPG/PNG/独立单页 PDF 样本，用于 Linux 目标机准确率、资源
  上限和断网 smoke test。
- 原生 Linux/amd64 生产机上的断网冷启动、持久卷恢复和实际 HTTP/HTTPS 入口验收。
- 公司确认现行补助制度、12:00 整点归属、预算代码选项、网约车材料和境外人民币金额确认方式，以及管理员钉钉 userId。

服务器出口域名、端口和严格防火墙部署建议见 [出口放行清单](deployment/NETWORK_EGRESS.md)。
