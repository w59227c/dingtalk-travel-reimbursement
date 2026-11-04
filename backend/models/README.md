# 本地 OCR 模型挂载点

正式发布镜像在构建阶段自动下载并校验模型，无需宿主模型目录，见
[GitHub Actions 镜像构建说明](../../docs/GITHUB_ACTIONS_SETUP.md)。下面的目录布局仍用于本机
开发和 `docker-compose.yml` 的显式模型挂载。

此目录不提交模型二进制。macOS arm64 本地测试和 Linux 生产部署都需要先在受控流程中取得并
校验 `PP-OCRv6_small_det`、`PP-OCRv6_small_rec` ONNX 制品的来源和 SHA-256。

只使用官方“推理模型”，不要放训练模型。来源 URL、官方归档 SHA-256、解压后每个文件的
SHA-256 和目录清单 SHA-256 都只维护在机器可读的
[`backend/app/ocr/model_manifest.json`](../app/ocr/model_manifest.json) 中；README 不复制第二份
数值，避免两个来源漂移。

从仓库根目录运行下载器。它会验证归档和逐文件 SHA-256，并在两份模型均验证通过后发布目录。

```bash
python3 backend/scripts/download-ocr-models.py --output backend/models/onnx
```

下载后目录布局如下。

```text
backend/models/onnx/
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
