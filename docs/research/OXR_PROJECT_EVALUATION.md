# OXR 与现有报销 OCR 流程：部署核验及合成样本测评

日期：2026-10-28（Asia/Shanghai）。本报告比较的是当前项目流程与 OXR 官方免费接口接入同一票据解析器后的结果。**没有完成 OXR 本地部署，也没有 GPU 本地性能数据。**

## 结论与建议

OXR 可以在自有 Linux/NVIDIA GPU 服务器部署；当前官方要求支持 BF16、兼容 CUDA 13.0 的驱动，显存 24GB 起步、建议至少 48GB。这台开发机为 macOS arm64，不满足该部署路径；Docker daemon 当前也未运行。本次没有下载约 3.16GB 的 OXR 模型。[官方部署说明](https://github.com/SoMarkAI/OXR#1-run-locally)

现阶段建议保留 **PDF 原生文字优先 → PP-OCRv6 small 回退 → 本地票据规则**，把 OXR 留作复杂扫描表格的候选补充。在本次 12 个合成文件上，两套流程各有 11 个文件的全部被测字段正确；OXR 补全了现有流程未恢复的行程单图片明细，但相同内容的数字 PDF 则是现有流程完整、OXR 经本次适配不完整。没有证据支持全量替换。

许可条件也是实际部署成本的一部分：OXR 使用附加限制的自定义许可，组织及关联方合并融资或年收入任一超过 200 万美元，商业使用需另取商业许可。内部使用不豁免该门槛，不能将“可下载权重”理解成“本公司一定免费使用”。[固定版本 LICENSE](https://github.com/SoMarkAI/OXR/blob/c1a1dcb312ee9f691404f127785510453c0d9350/LICENSE)

官方硬件、源码、模型版本、离线边界、许可证及榜单核验详见 [OXR_OFFICIAL_RESEARCH.md](OXR_OFFICIAL_RESEARCH.md)。该文档中的“未调用托管 API”指独立资料核验工作；本报告记录随后实际执行的合成材料 API 实验。

## 测的是什么

### 现有流程

- 实测工作区基于 commit `8f4ec94e1e8b22e8d04aacf1c1606c3301478cb9`，保留并发产生的其他工作区修改。没有改动产品、运行配置或数据库。
- PaddleOCR 3.7.0、PaddlePaddle 3.3.1、PP-OCRv6_small_det/rec；OpenCV 4.10.0.84、pypdfium2 5.13.0；CPU 4 线程。
- 两个本地模型目录均通过仓库固定 SHA-256 manifest 检查；文件总量约 31.48MB，包含权重及配置。磁盘体积不能作为内存或速度的直接比例。
- 使用真实 `OcrService`、`KillableProcessRunner(WarmOcrProcess())`，与应用入口保持相同的单个执行槽和模型复用方式；单份执行 120 秒上限。没有使用 fake engine。
- 费用文件调用 `recognize_file`，行程单调用 `recognize_itinerary_file`，默认分类关键词。没有读取生产数据库中的自定义分类词，也没有测上传、鉴权、草稿保存、排队竞争和 OA 提交。
- 原生 PDF 走现有原生文字、字段完整性及必要回退逻辑；图片与扫描 PDF 使用本地 Paddle。代码依据：[应用入口](../../backend/app/main.py)、[OCR 服务](../../backend/app/services/ocr_service.py)、[OCR worker](../../backend/app/ocr/workers.py)、[常驻子进程](../../backend/app/services/warm_ocr_process.py)。

### OXR 路径

- 实际请求 `https://free-oxr.somark.ai/api/v1/parse/sync`，依次发送相同的 12 个文件；没有调用付费版 SoMark API。
- 显式开启 `feature_config.keep_header_footer=true`，请求 JSON、Markdown，以及 HTML 表格；检查 HTTP 状态、JSON `code` 和 `data.error`。普通 API 默认不保留页眉页脚。[接口文档](https://docs.somark.ai/api-reference/endpoint/sync)、[固定源码路由](https://github.com/SoMarkAI/OXR/blob/c1a1dcb312ee9f691404f127785510453c0d9350/oxr/server/routes.py)
- 将 JSON 页内块内容转成普通文字，保留 HTML 表格行和列分隔，然后调用与现有流程相同的 `ReceiptParserRegistry` / `parse_itinerary_pages`。适配仅去除标记、保留表格分隔，不新增金额、日期或费用分类业务规则；没有完整利用 OXR 坐标做版面重建。
- 为满足当前接口类型，适配器输入行的置信度暂设 1.0；**这不是 OXR 测得的置信度，不能用于两模型置信度比较**。行程单页来源暂用现有类型允许的 `paddle` 占位，仅存在于实验内存；本次没有把 OXR 结果接入产品或持久业务记录。
- 官方免费接口没有向本次实验提供可核验的模型 revision、GPU 配置或服务负载；不能认定其权重与公开模型某一 revision 完全一致。

### 样本与指标

所有输入均由 [oxr_benchmark.py](oxr_benchmark.py) 生成，包含“合成测试/非真实票据”标记，不含真实姓名、证件、税号、订单、发票标识或公司材料。

12 个文件来自 7 类内容：铁路客票、出租车小票、住宿发票、美元住宿收据、旅客运输列式表格、两行行程表、故意缺金额的发票。包含 8 张 PNG、3 份带嵌入中文字体的数字 PDF、1 份纯图像扫描 PDF；出租车额外含缩小再放大、轻微模糊及旋转 2 度的版本。不同格式共享内容，**不是 12 张独立真实票据**。

每组共 41 项检查：票据类别、日期、金额；美元票据另查币种、原币金额和人民币金额保持空值；行程单查总额、起止日期、行程条数及 `complete`。缺失金额应保持空值。字段按相等比较，包括金额两位小数字符串及 `null`；全部被测项正确才记作该文件通过。

没有计入路线文字、销售方、号码、二维码交叉验证、逐段行程所有字段、用途分类、全部警告、全量字符错误率或票据验真。因此“文件通过”不等于所有可提交业务字段都完整正确。

现有流程对同组文件顺序重复 3 轮，观察到相同的字段结果；OXR 最终组每文件请求 1 次。重复识别同一文件不增加样本多样性，本实验不提供统计显著性、可靠 p95 或生产准确率结论。

## 实测结果

| 指标 | 现有本地流程 | OXR 官方免费接口 + 相同 Parser |
| --- | --- | --- |
| 返回可解析结果 | 36/36 次 | 12/12 次 |
| 每组全部被测字段正确的文件 | 11/12；三轮一致 | 11/12 |
| 每组字段正确数 | 39/41；三轮共 117/123 | 39/41 |
| 全部任务耗时中位数 | 0.986 秒，36 次 | 4.226 秒，12 次 |
| 数字 PDF 耗时中位数 | 0.102 秒，后两轮共 6 次 | 3.474 秒，3 次 |
| 图片/扫描 PDF 耗时中位数 | 1.002 秒，后两轮共 18 次 | 4.236 秒，9 次 |
| 首张图片，包含本地模型初始化 | 2.599 秒 | 5.588 秒；远端是否冷启动未知 |

**时间列只说明本次两条可用路径的用户等待时间。** OXR 包含外网传输、厂商排队和远端计算；本地数据是 macOS CPU 服务层测量。不能据此声称 OXR 模型本地推理慢 4 倍，也不能据此估算同 GPU 下的吞吐。两边计时均未包含本项目完整上传/UI 流程。

本地进程资源统计：主进程峰值 RSS 约 66.5MiB；已结束子进程的最大峰值 RSS 约 1.77GiB。该值是本次 macOS `getrusage` 记录的子进程峰值，**不是整个应用同时占用量**；没有 OXR 对应的本地 RAM/显存测量。

逐文件结果如下。本地耗时为 3 轮中位数，OXR 为最终组单次请求。

| 合成文件 | 被测项 | 本地正确数 / 秒 | OXR 正确数 / 秒 |
| --- | ---: | ---: | ---: |
| 铁路图片 `train.png` | 3 | 3 / 1.004 | 3 / 5.588 |
| 铁路数字 PDF `train.pdf` | 3 | 3 / 0.108 | 3 / 3.474 |
| 铁路扫描 PDF `train.scan.pdf` | 3 | 3 / 1.008 | 3 / 3.700 |
| 出租车图片 `taxi.png` | 3 | 3 / 0.943 | 3 / 4.236 |
| 模糊旋转出租车图片 `taxi.degraded.png` | 3 | 3 / 1.017 | 3 / 5.237 |
| 住宿图片 `hotel.png` | 3 | 3 / 1.022 | 3 / 5.704 |
| 住宿数字 PDF `hotel.pdf` | 3 | 3 / 0.106 | 3 / 3.127 |
| 美元收据 `foreign.png` | 5 | 5 / 0.949 | 5 / 4.215 |
| 旅客运输表格 `passenger.png` | 3 | 3 / 1.124 | 3 / 4.483 |
| 行程表图片 `itinerary.png` | 5 | 3 / 1.137 | 5 / 3.970 |
| 行程表数字 PDF `itinerary.pdf` | 5 | 5 / 0.039 | 3 / 4.542 |
| 缺金额发票 `missing-total.png` | 2 | 2 / 0.985 | 2 / 3.449 |

### 差异意味着什么

1. **普通字段没有观察到 OXR 优势。** 铁路、出租车、住宿、外币及缺金额样本，两条路径的被测结果一致；两边均未把 108.25 美元填成人民币金额，也没有给缺金额样本补出金额。此结论仅覆盖这些合成输入。
2. **扫描表格值得继续试。** 行程单图片中，现有流程有正确总额与起止日期，但行程条数为 0、`complete=false`；OXR 经相同表格解析器恢复 2 条、`complete=true`。这是一个可复现的候选收益点，不能扩展成所有扫描行程单都更准。
3. **数字 PDF 应继续保留快路径。** 相同表格的数字 PDF 在现有流程中恢复 2 条且完整；OXR 路径有正确总额与日期，但条数为 0，提示 `ITINERARY_ROWS_INCOMPLETE`。本实验比较的是“识别 + 最小适配 + 现有解析器”，该失败可能涉及版面识别或适配，不能直接归因于模型完全没识别出文字。OXR 返回的总额数字在规范化文本中确实存在。
4. **PDF 字体要先校验。** 预实验用未嵌入的 STSong CID 字体时，OXR 在三个数字 PDF 上均有字段缺失；本地文字提取与本机渲染正常。为避免字体替代差异混入最终结论，最终三个数字 PDF 均嵌入 Arial Unicode 字形，重新整组测量，并用本地 PDFium 渲染人工检查。修正后铁路、住宿 PDF 的字段完整；行程表 PDF 仍有前述问题。没有检查远端渲染图片，不能断言远端缺失哪个字体。

## 部署与集成取舍

| 维度 | 当前方案 | OXR 候选方案 |
| --- | --- | --- |
| 运行资源 | 现有 CPU 后端、固定 small 模型 | 独立 Linux/NVIDIA 服务，官方 24GB 起步、48GB 以上建议 |
| 模型准备 | 当前两目录约 31.48MB，已校验 | 官方模型目录元数据约 3.16GB，尚未下载校验；另需 CUDA/vLLM 等 |
| 数字 PDF | 原生文字优先，保留现有快路径 | 当前源码统一渲染再识别，默认 150 DPI |
| 业务解析 | 已有类别、金额、日期、外币、二维码及人工核对规则 | 文档块及结构输出仍需映射到现有业务规则 |
| 隐私与可用性 | 生产 OCR 路径本地运行 | 私有部署可把文档留在内网，仍须做断网验收；免费 API 不适合生产依赖 |
| 持久状态与安全 | 已有 Session、CSRF、归属、revision、单槽 FIFO、失败恢复 | OXR 的服务不能替代这些能力；应仅作为受控内部识别子服务 |
| 商业许可 | 项目已保存现有组件许可说明 | 必须核对 OXR 自定义门槛及商业许可条件 |

OXR 的公开榜单对手包括 **PaddleOCR-VL-1.6**，并非本项目使用的 PP-OCRv6 small；官方 GPU 速度实验使用 RTX PRO 6000 Blackwell 96GB。榜单成绩不等于本项目票据字段正确率，也不能直接换算当前 CPU 资源上的收益。[官方 benchmark](https://github.com/SoMarkAI/OXR/blob/c1a1dcb312ee9f691404f127785510453c0d9350/docs/benchmark.md)

下一步如具备许可与 GPU 条件，最有价值的实验是：在同一 Linux 主机部署独立 OXR 服务，保持当前 PDF 原生文字优先，用扩大后的合成/彻底脱敏扫描行程表做对照；分别测原始识别、表格坐标适配及业务解析，记录错误金额、漏行、人工修改率、冷/热延迟、RAM/显存、超时和取消回收。只有扫描件收益持续且资源成本可接受时，再设计显式候选回退；本次没有实现该集成。

## 复现与保存的证据

研究脚本不接收任意上传路径，远端模式只接受两个官方免费域名；上传前校验合成文件允许清单与哈希。没有保存 OCR 原文、远端任务号或返回的下载链接。结果、样本清单和合成文件保存在 Git 忽略的 `outputs/oxr-evaluation/`；报告中的汇总留在仓库。`samples.json` 保存最终文件哈希，`local.json` / `remote.json` 的每行携带相应哈希及结构化字段。

在仓库根目录运行：

```bash
# 生成器需要 reportlab、Pillow，使用 macOS 自带中文字体。
# 本次通过 Codex bundled Python 使用 reportlab 4.4.9；不改后端依赖。
python-with-reportlab docs/research/oxr_benchmark.py generate

# 先确认本地固定制品，随后调用真实服务层，复用单个 OCR 子进程。
make ocr-models-check
backend/.venv/bin/python docs/research/oxr_benchmark.py local --repeats 3

# 此命令会上传脚本生成且通过哈希校验的合成文件到官方免费服务。
backend/.venv/bin/python docs/research/oxr_benchmark.py remote --limit 12
backend/.venv/bin/python docs/research/oxr_benchmark.py summary
```

`python-with-reportlab` 是说明性占位，需要替换为安装了 reportlab/Pillow 的 Python 路径。免费接口结果文件可续跑；文件哈希变化会使对应旧结果失效。如要强制重复远端实测，可先把已有 `remote.json` 改名；不要重新上传真实业务材料。

实际执行并通过：`make ocr-models-check`、最终本地 36 次真实 OCR 服务调用、最终 OXR 免费接口 12 次调用、4 个 PDF 的渲染及视觉检查、`make backend-lint`、研究脚本按 `backend/pyproject.toml` 的 Ruff 检查及格式检查、Python 语法与适配器 smoke、样本哈希/结果计数/评分一致性断言，以及 `git diff --check` 和新增文件的空白检查。

没有执行：OXR 本地部署/权重下载/GPU 推理/禁外网验收、真实票据评估、并发负载和完整浏览器链路、真实钉钉/OA、生产部署。产品代码与配置没有因本研究变更，未运行全量前后端回归，不把实验当成生产上线验收。
