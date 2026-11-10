# OXR 官方资料核验

日期：2026-10-28（Asia/Shanghai）
范围：部署、许可证、公开评测和接口适配；仅阅读公开资料与源码，没有下载权重、启动 OXR 或上传材料。项目内的实际对比测评另见同目录报告。

## 结论

OXR 有完整的私有部署代码和可下载权重，可以在具备 NVIDIA GPU 的 Linux 服务器上部署。当前官方路径要求 BF16、兼容 CUDA 13.0 的驱动和 Python 3.12，显存 24 GB 起步、建议至少 48 GB。这里的“起步”限低并发，并非本项目服务等级保证。[官方部署说明](https://github.com/SoMarkAI/OXR#1-run-locally)

它更适合作为复杂扫描文档、表格、阅读顺序的候选识别服务。是否优于当前票据流程，必须测金额、日期、类型、路线、销售方等业务字段；通用文档榜单不能直接回答。现阶段没有本项目 OXR 的本地推理实测结论。

## 固定版本与下载规模

| 项目 | 本次核对值 | 依据 |
| --- | --- | --- |
| 代码 commit | `c1a1dcb312ee9f691404f127785510453c0d9350`，提交时间 2026-10-25 | [固定源码](https://github.com/SoMarkAI/OXR/tree/c1a1dcb312ee9f691404f127785510453c0d9350)，临时只读 clone 后执行 `git rev-parse HEAD` / `git log -1` |
| HF 模型 revision | `c708407f9d2b4df658f2543d9132b62bb7221df4` | [模型文件版本](https://huggingface.co/SoMarkAI/OXR-1.0/tree/c708407f9d2b4df658f2543d9132b62bb7221df4)；公开 API 元数据 |
| 完整模型仓库文件合计 | 3,163,649,841 bytes，约 3.16 GB / 2.95 GiB | [官方文件元数据 API](https://huggingface.co/api/models/SoMarkAI/OXR-1.0?blobs=true)，本次只读取大小并求和 |
| 主权重 | `model.safetensors`：3,068,857,520 bytes | 同上；SHA-256：`795bb2f1faac579f58829913f2c606202531a96c1203e19ef7a70b103fb7ea51` |
| 布局权重 | `layout_analysis/last.pth`：78,791,307 bytes | 同上；SHA-256：`dff78ccf18439955362ca44a69eb28fb0b40222a7d278860589682de0176dc17` |

以上不含 CUDA、PyTorch、vLLM、容器层、构建缓存和推理缓存；权重磁盘大小不能当作实际显存需求。此处记录的是元数据声明的哈希，尚未下载权重逐字节校验。

## 本地运行方式与离线边界

官方采用三个进程：文档 API（8000）、布局分析 D-FINE（8081）、识别及阅读顺序 OXR/vLLM（8080）。All-in-one 镜像负责协调进程，不会把它们简化为当前后端进程内的普通 CPU 库。[配置](https://github.com/SoMarkAI/OXR/blob/c1a1dcb312ee9f691404f127785510453c0d9350/config.example.yaml)、[启动器](https://github.com/SoMarkAI/OXR/blob/c1a1dcb312ee9f691404f127785510453c0d9350/docker/allinone_entrypoint.py)

镜像固定 vLLM 0.25.1；启动器还要求 PyTorch `2.11.0+cu130` 和 Transformers `5.13.1`。OXR 的 vLLM 插件随模型目录分发，启动时从该目录安装，使用 `trust-remote-code`、BF16 和 Triton attention。应固定并审查模型仓库内的 Python 代码，而不只核对 safetensors。[Dockerfile](https://github.com/SoMarkAI/OXR/blob/c1a1dcb312ee9f691404f127785510453c0d9350/docker/Dockerfile.allinone)、[vLLM 启动脚本](https://github.com/SoMarkAI/OXR/blob/c1a1dcb312ee9f691404f127785510453c0d9350/services/oxr/start_vllm.sh)

布局子服务允许 `cpu`，但这不代表 OXR 主模型及完整管线已有官方 macOS/MPS 或纯 CPU 部署路径。[布局设备配置源码](https://github.com/SoMarkAI/OXR/blob/c1a1dcb312ee9f691404f127785510453c0d9350/services/layout_analyze/dfine_la/config.py)

所有模型和依赖提前准备、三个地址保持在自有主机/内网时，源码结构支持票据留在本地。All-in-one 启动器用 `--no-deps --no-build-isolation` 安装本地插件，未要求调用厂商 OCR API；但**本次没有做断外网启动或离线推理验证**，不能把架构判断写成已通过离线验收。[本地模型加载与服务地址](https://github.com/SoMarkAI/OXR/blob/c1a1dcb312ee9f691404f127785510453c0d9350/docker/allinone_entrypoint.py)、[模型客户端](https://github.com/SoMarkAI/OXR/blob/c1a1dcb312ee9f691404f127785510453c0d9350/oxr/model/client.py)

如后续准备 GPU 环境，应独立运行候选服务，端口只绑定回环或受控内网；先做权重/依赖校验与禁外网 smoke，再运行相同合成样本。不要直接向本项目现有 CPU 后端镜像追加这些 GPU 依赖。

## 与票据处理有关的接口事实

- `POST /v1/parse/sync` 接受 multipart：`file`、`api_key`、重复的 `output_formats`，输出 `data.result.outputs.json.pages` 与 Markdown；JSON 块有类型、矩形坐标、内容和格式，并没有本项目的金额、日期、费用类别业务契约。[路由](https://github.com/SoMarkAI/OXR/blob/c1a1dcb312ee9f691404f127785510453c0d9350/oxr/server/routes.py)、[输出序列化](https://github.com/SoMarkAI/OXR/blob/c1a1dcb312ee9f691404f127785510453c0d9350/oxr/pipeline/postprocess/json_postprocess.py)
- 普通 API 的 `feature_config.keep_header_footer` 默认 `false`，页眉页脚会在识别前过滤；Demo 则默认保留。票据测试应显式传 `{"keep_header_footer":true}`，否则票号、日期或合计若落入页边分类，可能根本不被识别。这是源码推断的风险，尚未用票据复现。[处理管线](https://github.com/SoMarkAI/OXR/blob/c1a1dcb312ee9f691404f127785510453c0d9350/oxr/pipeline/orchestrator.py)、[Demo API](https://github.com/SoMarkAI/OXR/blob/c1a1dcb312ee9f691404f127785510453c0d9350/oxr/server/demo_api.py)
- PDF 当前统一渲染为默认 150 DPI 图片，再做布局及识别，没有本项目的原生文字直接解析快路径。由此推断：以 OXR 全量取代 PDF 文字提取会引入重新识字、延迟和计算成本，不能仅因模型能力更强就删除快路径。[PDF 工具](https://github.com/SoMarkAI/OXR/blob/c1a1dcb312ee9f691404f127785510453c0d9350/oxr/utils/pdf.py)、[解码流程](https://github.com/SoMarkAI/OXR/blob/c1a1dcb312ee9f691404f127785510453c0d9350/oxr/pipeline/orchestrator.py)
- 错误可能仍返回 HTTP 200，须检查 JSON `code`、`data`、`error`；`api_key` 参数在当前这些路由中未作认证校验。异步任务为进程内 `MemoryTaskStore`，重启后丢失，不能替代本项目持久状态、归属、CSRF、FIFO 队列、revision 和恢复契约。[路由错误与参数](https://github.com/SoMarkAI/OXR/blob/c1a1dcb312ee9f691404f127785510453c0d9350/oxr/server/routes.py)、[内存任务存储](https://github.com/SoMarkAI/OXR/blob/c1a1dcb312ee9f691404f127785510453c0d9350/oxr/server/queue/memory_store.py)

测试请求示例（仅用于未来受控本地服务，本次未执行）：

```bash
curl --fail-with-body http://127.0.0.1:8000/v1/parse/sync \
  -F 'file=@synthetic-receipt.png' \
  -F 'api_key=dummy' \
  -F 'output_formats=markdown' \
  -F 'output_formats=json' \
  -F 'feature_config={"keep_header_footer":true}'
```

## 公开跑分怎样解读

以下均为**OXR 作者发布的对比快照**，准确率快照日期 2026-10-05；本次没有独立重跑。OmniDocBench 是综合分，不等于“97.18% 的票据金额正确”。[官方 benchmark](https://github.com/SoMarkAI/OXR/blob/c1a1dcb312ee9f691404f127785510453c0d9350/docs/benchmark.md)

| 指标 | OXR-1.0 官方结果 | 可比性限制 |
| --- | --- | --- |
| olmOCR-bench overall | 87.84 | 1,403 页、8,413 条规则，非本项目票据字段集 |
| OmniDocBench overall | 97.18 | 1,651 页综合文档指标；表中 PaddleOCR-VL-1.6 为 96.34，**不是本项目 PP-OCRv6 small** |
| DocParsingBench overall | 87.76 | 三轮平均，每轮尝试 1,400 页、固定排除 58 页后评分 1,342 页 |
| 端到端吞吐 | 0.990373 页/秒；100 页 100.972 秒 | 单 RTX PRO 6000 Blackwell 96GB、24 CPU quota、96 GiB 内存；同 100 页重复两轮取平均，不是单张请求 p95 |
| 串行解码 | 557.77 native tokens/秒 | 排除 prefill、首 token 和阅读顺序阶段；不同 tokenizer 的 token 数不能直接等价比较 |

准确率表的其他模型成绩来自论文、模型卡及各榜单，并非所有模型都在作者同一环境重测；速度实验则声明使用统一 GPU 和固定 100 页。报告中未见 PP-OCRv6 small / pypdf / 当前规则解析器的同场结果，不能从该表推导替换收益。

## 许可证会影响是否值得落地

OXR 是带附加限制的自定义许可证，不是无条件 Apache-2.0。组织及关联方合并口径下，累计融资或最近完整财年的年度收入，任一超过 **200 万美元**，商业使用前须另获商业许可。组织内部使用仅豁免竞争性托管 API 禁令，未豁免收入/融资门槛；向第三方提供服务另有署名要求，竞争性托管 OCR/解析 API 另需书面授权。是否适用及价格需按实际组织情况向权利方确认，本次未联系厂商。[固定版本 LICENSE](https://github.com/SoMarkAI/OXR/blob/c1a1dcb312ee9f691404f127785510453c0d9350/LICENSE)

## 对本项目的建议

1. 保留原生 PDF 文字优先和当前本地 OCR 默认路径；公开资料不足以支持直接替换。
2. 有合适 GPU 及许可条件时，把 OXR 作为独立候选服务，用完全相同的合成/彻底脱敏样本，分别比较原始识别质量与接入现有 Parser 后的业务字段完整率。
3. 重点覆盖扫描行程表、销售方与购买方双列、金额/税额干扰、页边日期、旋转与低清照片；记录漏识、虚构、错误金额、人工修改率、超时、冷启动、热态 p50/p95、CPU/RAM/显存。
4. 如仅复杂扫描件显著改善，可评估按失败样本显式回退；不能静默跳过未完成文件，也不能自动用生成文本覆盖人工修订。该项只是后续设计方向，本次未实现。

本次实际执行：公开源码 clone 与固定 commit 检查、Hugging Face 公共文件元数据查询、接口/过滤/任务存储/启动器源码检查。未执行：OXR 权重下载、本地部署、GPU 推理、断网验收、外部托管 API 调用、真实票据或真实钉钉/OA 验证。
