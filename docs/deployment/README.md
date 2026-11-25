# 服务器部署

适用于 Linux x86_64/amd64、支持 AVX 且已安装 Docker Compose v2 的服务器。镜像公开可拉取，服务器无需登录 GHCR；OCR 模型、Excel 模板和 Web 配置已包含在镜像中。

受限网络部署前，请按[生产服务器出口放行清单](NETWORK_EGRESS.md)分别开放应用运行与 Docker 拉取镜像所需的域名和端口。

## 首次部署

把本目录的 `compose.yaml` 和项目根目录的 `.env` 上传到服务器同一目录，例如：

```text
/opt/dingtalk-travel-reimbursement/
├── compose.yaml
└── .env
```

本目录的 `compose.yaml` 即为 16C / 32 GiB 内存 / 100 GiB 磁盘准备的资源配置；将项目
根目录的 `.env.production.16c32g.example` 复制为同目录 `.env`。该配置为后端限制 12 核、16 GiB，
OCR 单槽使用 8 个 CPU 线程并限制为 8 GiB，Web 限制 1 核、256 MiB，持久材料逻辑上限为
30 GiB；剩余资源保留给宿主系统、Docker、页缓存、备份和突发任务。当前程序只允许一个 OCR
执行槽，不要把 `OCR_CONCURRENCY` 改成大于 1。低资源测试服务器可参考 `compose.dev.yaml`。

`.env` 不需要添加 `IMAGE_TAG`，缺省会使用 `main`；也可以显式设置：

```dotenv
IMAGE_TAG=main
```

然后执行：

```bash
cd /opt/dingtalk-travel-reimbursement
chmod 600 .env
docker compose config --quiet
docker compose pull
docker compose up -d --wait --wait-timeout 300
docker compose ps
```

访问地址由 `.env` 中的 `APP_BIND_ADDRESS` 和 `APP_PORT` 决定。16C/32 GiB 示例默认监听 `127.0.0.1:8080`，供本机反向代理访问；需要其他设备直接访问时，按实际网络配置绑定地址和端口。以下检查示例使用默认端口，修改后需同步替换：

```bash
curl -fsS http://127.0.0.1:8080/api/ready
docker compose logs --tail=100 backend web
```

## 更新

确认 GitHub Actions 构建成功后，先停止接单与后台处理（停止 `web`、`backend`），备份同一时点的 `sqlite_data` 和 `reimbursement_staging`，并保存当前私密配置及镜像 SHA/digest。备份完成后在服务器执行下面的命令；容器启动会自动迁移数据库。

```bash
cd /opt/dingtalk-travel-reimbursement
docker compose pull
docker compose up -d --wait --wait-timeout 300
```

镜像名和 `.env` 不需要随版本修改，`main` 会指向最新一次通过验证的主线镜像。

如果早期 0029 升级因 I/O 等异常中断，重启时报 `duplicate column name: kind`，先保留并一致性备份现状，不要手工修改 `alembic_version` 或删除列／表。包含恢复补丁的新镜像会核验已添加列的完整定义，再安全续跑；不一致的结构仍拒绝启动。新版 0029 失败时整体回滚，持续的存储 I/O 故障仍需先排查存储，不能靠反复重启绕过。

镜像入口在迁移前准备 `TEMP_DIR/.spool` 私有目录，并把它传给 SQLite 作为临时目录。只读根文件系统下，`/tmp/expense` 的 tmpfs 挂载会遮住镜像内预建的 `.spool`；有数据的材料表重建可能需要磁盘临时文件，因此不能等应用启动时再创建。旧镜像因 `SQLITE_IOERR_GETTEMPPATH`（6410，表面报 `disk I/O error`）失败时，使用本目录 `env_file` 配置可暂在 `.env` 设置 `TMPDIR=/tmp/expense` 后重新创建容器；只用于 Compose 插值、没有传入容器的变量不会生效。新镜像自动准备目录，无需这项临时设置。

OCR 镜像内置经过校验的 PP-OCRv6 small ONNX 模型，默认使用 ONNX Runtime CPU。旧 `.env` 的 `OCR_ENGINE=paddle_static` 由新版映射到默认引擎，镜像内模型路径保持兼容。`OCR_CPU_THREADS` 控制每个模型的算子线程数，使用目标服务器的批量样本决定线程配置。

## 低内存测试机

默认 `OCR_MEMORY_PROFILE=standard` 适用于正常资源配置。约 2 核、1.6 GiB 的测试主机可使用本目录的 `compose.low-memory.yaml` 作为覆盖配置：后端限制为 1 GiB / 1.5 核、OCR 算子 2 线程、BLAS 1 线程，并启用 `OCR_MEMORY_PROFILE=low`。该模式减少检测工作图及模型临时缓存内存，保留原始材料与相同模型，密集小字可能漏检，须核对业务样本；这不是对任意允许上传文件的内存保证。

必须先确认目标镜像已支持 `OCR_MEMORY_PROFILE`，再按上方更新流程备份并启用覆盖。仅把旧镜像限制到 1 GiB 不会获得该优化。将该文件放到服务器 `compose.yaml` 同目录并命名为 `compose.override.yaml`，Docker Compose 会自动合并，无需改 `.env` 或日常命令：

```bash
# 若已有 compose.override.yaml，先保留并合并原配置，不要直接覆盖。
cp compose.low-memory.yaml compose.override.yaml
docker compose config --quiet
docker compose up -d --wait --wait-timeout 300
```

移除该覆盖文件并重建容器即可恢复基础配置。宿主仍须为操作系统、Web、文件校验进程和页缓存保留内存；以批量业务材料实测宿主 MemAvailable、PSI 和 cgroup OOM 计数，不仅看容器是否健康。正式服务器继续使用标准模式，低内存配置仅供测试机按需启用。

## 回滚

先停止接单及 worker，记录当前数据库 revision，并核对目标后端镜像的 `alembic heads`、数据库约束和快照格式兼容性。**不能仅改镜像标签就跨数据库版本回滚**：旧镜像无法识别新 revision，启动时的 `alembic upgrade head` 会失败；部分降级还会因业务记录或审计证据而被明确拒绝。

数据库 revision 相同也须核对应用格式。版本 11 快照可能包含 `detailOrder`、代报的 `oaInitiator`、报销身份字段映射，以及出差模板的 `schemaContractVersion=2`（发布标识变化不改变字段契约），模板目录也可保存报销人和报销部门映射。保留当前数据的镜像回滚须选用支持已写入字段和映射的后端。确需回到更早的镜像时，按下方步骤恢复升级前的一致性备份。

当前差旅和普通费用的新提交均使用版本 13 快照，在 `documentNaming` 中冻结上海提交日期和当日序号，并据此固定正式附件名；版本 11 差旅和版本 12 普通费用属于历史兼容格式。当前迁移 head 为 `20261125_0030`，已有版本 13 编号快照时拒绝降级。保留现有数据回滚的目标镜像须同时支持版本 13、统一附件编号和对应附件清单约束，还须支持普通费用的独立模板、费用科目、运单及福利费子类，以及两种报销类型各自的跨部门当前工作区、可更正的部门属性和已有恢复记录；不能只以版本 11 或版本 12 可读作为兼容依据。

只有确认目标镜像支持当前数据库和已保存快照时，才把 `.env` 的 `IMAGE_TAG` 改为 Actions 已成功发布的目标 `sha-<完整 commit>`，再执行 `docker compose pull` 和 `docker compose up -d --wait --wait-timeout 300`。恢复跟随主线时改回 `IMAGE_TAG=main`。

存在数据库版本或快照不兼容时，优先发布修复版本。确需恢复旧版本，应在停机状态下同时恢复升级前同一时点的 SQLite、staging 与匹配镜像，保留当前数据副本供审计；恢复后先保持 `DINGTALK_OA_WORKER_ENABLED=false`，核对备份之后已经发生的钉钉提交、附件与检查点，再决定恢复接单。不要强改 `alembic_version`、强行丢弃证据或只恢复一个持久卷。

回滚至旧 Paddle 镜像时，从 `.env` 移除 `OCR_ENGINE=onnxruntime` 或改为 `paddle_static`，让旧镜像按原配置启动。

## 数据

`sqlite_data` 保存数据库，`reimbursement_staging` 保存报销附件和生成文件。升级或重建容器不会删除它们；不要执行 `docker compose down -v`。这两个 volume 应在同一时间点一起备份。

首次验收前可保持 `DINGTALK_OA_WORKER_ENABLED=false`；需要正式处理 OA 提交时再改为 `true` 并重启服务。
