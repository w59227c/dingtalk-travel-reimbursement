# 智能差旅费报销

公司内部的钉钉 H5 报销工具：关联已通过的出差审批，核对补助和费用，上传票据与证明材料，生成报销单并提交 OA。系统负责成功发起 OA 并回读确认，后续审批和财务处理在钉钉完成。

## 主要能力

- 钉钉免登，按企业、员工和部门隔离报销数据；公司与预算从关联审批带入。
- 一个当前报销工作区，自动保存与恢复；报销记录只展示最近 10 笔成功提交 OA 的记录。
- 统一上传发票、行程单、住宿明细和付款凭证；支持本地 OCR、手工修正、多文件关联及材料校验。
- 按出差审批计算补助，支持分段行程、非补助时段和市内项目的实际出勤日。
- 生成公司模板 Excel；有材料时另生成票据汇总 PDF。提交使用不可变快照，异常通过重试、回查或人工核对处理。
- 成功明细只读，Excel 可重新生成；票据在钉钉审批中查看，服务器本地副本在成功核验后清理。

前端使用 Vue 3 + TypeScript，后端使用 FastAPI + SQLAlchemy/Alembic + SQLite，生产入口为 Nginx。OCR 默认使用 PP-OCRv6 small ONNX 模型与 ONNX Runtime CPU，不需要 GPU，也不调用付费 OCR 或大模型接口。

## 本地启动

准备 Node.js、uv，以及模型下载所需的网络；依赖版本以仓库锁文件和运行时配置为准。在仓库根目录执行：

```bash
make backend-install frontend-install
python3 backend/scripts/download-ocr-models.py --output backend/models/onnx
make dev
```

打开 <http://127.0.0.1:5173>，点击“使用固定测试身份”；后端为 <http://127.0.0.1:8000>。此模式使用合成管理员与独立开发数据，模型缺失时拒绝启动。

真实钉钉免登、本机正式公司联调和 macOS OCR 检查见[本地开发与联调](docs/DEVELOPMENT.md)。

## 服务器部署

CPU 生产环境使用 Linux x86_64/amd64（支持 AVX）与 Docker Compose v2。已发布镜像包含模型、Excel 模板和 Web 配置，服务器无需安装 Python 或 Node.js。

按[服务器部署说明](docs/deployment/README.md)准备 `compose.yaml` 和私密 `.env`。该说明包含 16 核／32 GiB／100 GiB 配置、首次启动、更新、回滚和备份。GitHub Actions 负责测试及镜像发布，不自动部署服务器。

- 填写真实钉钉应用配置、至少一名初始管理员及随机 `SESSION_SECRET`；凭据只保存在后端，不提交 `.env`。
- 生产环境禁止 Mock/Fake OCR。OA worker 默认关闭，完成迁移、模板映射、权限和真实验收后再显式启用。
- SQLite 与 `reimbursement_staging` 必须在同一时间点一起备份；容器更新不应删除持久卷。
- `/api/health` 表示进程存活，`/api/ready` 检查本地就绪状态；就绪不等于已完成钉钉权限与 OA 验收。

源码构建部署、模型挂载和运维细节见[运行与运维](docs/OPERATIONS.md)。

## 常用检查

```bash
make backend-test backend-lint
make frontend-test frontend-typecheck frontend-lint frontend-build
make compose-config-development nginx-policy-check
make ocr-models-check ocr-runtime-check
```

OCR 检查需要已下载模型及锁定的 OCR 依赖；测试只能使用合成或脱敏材料。当前数据库迁移说明和变更要求见 [AGENTS.md](AGENTS.md)，实际迁移 head 以 Alembic 输出为准。

## 文档

| 需要了解 | 文档 |
| --- | --- |
| 业务规则、补助、Excel 与材料限制 | [产品与业务契约](docs/PRODUCT_CONTRACT.md) |
| 自动保存、状态机、OA 提交、恢复与清理 | [OA 集成设计](docs/DINGTALK_OA_INTEGRATION_DESIGN.md) |
| 依赖、本地启动、钉钉联调 | [本地开发与联调](docs/DEVELOPMENT.md) |
| 服务器镜像部署、升级与回滚 | [部署指南](docs/deployment/README.md) |
| 就绪检查、备份与源码部署 | [运行与运维](docs/OPERATIONS.md) |
| CI 与镜像发布配置 | [GitHub Actions](docs/GITHUB_ACTIONS_SETUP.md) |
| 协作要求与验证命令 | [AGENTS.md](AGENTS.md) |
| 其他设计、调研和历史计划 | [完整文档索引](docs/README.md) |

业务变更同步更新产品契约与 OA 设计；根 README 保持为项目入口，不再记录逐次界面调整和历史阶段细节。
