# 本地 OCR 模型挂载点

正式发布镜像现在在构建阶段自动下载并校验模型，使用 `compose.production.yml` 时无需宿主
模型目录，见 [GitHub Actions 部署说明](../../docs/GITHUB_ACTIONS_SETUP.md)。下面的目录布局
仍用于本机开发和原有 `docker-compose.yml` 的显式模型挂载。

此目录不提交模型二进制。macOS arm64 本地测试和 Linux 生产部署都需要先在受控流程中取得并
校验 `PP-OCRv6_small_det`、`PP-OCRv6_small_rec` 制品的来源和 SHA-256。

只使用官方“推理模型”，不要放训练模型。来源 URL、官方归档 SHA-256、解压后每个文件的
SHA-256 和目录清单 SHA-256 都只维护在机器可读的
[`backend/app/ocr/model_manifest.json`](../app/ocr/model_manifest.json) 中；README 不复制第二份
数值，避免两个来源漂移。

下载应在临时 staging 目录中完成。先按 manifest 核对两个原始归档的 SHA-256 并检查归档成员，
再分别解压为：

```text
backend/models/
├── PP-OCRv6_small_det/
└── PP-OCRv6_small_rec/
```

两个目录必须只包含 manifest 列出的文件并逐文件匹配 SHA-256。运行以下只读检查：

```bash
make ocr-models-check
```

生产环境把这两个目录只读挂载到 `/opt/expense/models`。应用显式使用本地路径并禁用 PaddleX
模型源联网检查，运行时不下载模型。OCR 默认启用；未提供经过验证的制品时，
`/api/ready` 必须返回未就绪，不能将缺少 OCR 的服务标记为可上线。
