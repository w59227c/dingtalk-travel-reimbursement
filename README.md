# 智能差旅费报销申请

本仓库实现一个公司内部钉钉 H5 工具：员工免登后选择 OA 提供的所属公司和预算代码，按需申请出差补助，上传票据和行程单，在一个费用列表中预览、修改并关联材料，再选择本人已通过的出差审批。页面自动保存填写内容。员工最终确认后，服务器按锁定快照生成 `票据汇总.pdf` 和报销单 `.xlsx`，只上传这两份文件到审批附件空间，一次性创建钉钉 OA。

完整的自动保存、票据材料、出差关联和 OA 提交设计见 [`docs/DINGTALK_OA_INTEGRATION_DESIGN.md`](docs/DINGTALK_OA_INTEGRATION_DESIGN.md)。本文是当前范围与业务契约；基础功能分阶段记录保存在 [`docs/V1_IMPLEMENTATION_PLAN.md`](docs/V1_IMPLEMENTATION_PLAN.md)。系统交付边界是成功发起 OA 并回读确认，后续审批和财务工作继续由钉钉及财务人员处理。

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

## 当前交付范围

- Vue 3 + TypeScript H5、FastAPI 单体后端、SQLite 配置库。
- 钉钉免登；姓名和部门只能来自后端确认的钉钉 Session。
- 公司、预算代码从已确认的钉钉表单选项读取；预算代码完整标签直接填 Excel 项目。
- 出差补助为可选项；不连续审批分别生成补助项，日期重叠的审批自动合并为一个补助时段，避免重复计算，员工只确认各时段出发/返回的上午或下午，由后端重算。境内市外项目按 30 个自然日边界判定短期/长期；公司内部出差不超过 30 天按设置标准计算，超过 30 天不补助；境外审批不生成补助。
- 票据支持 JPG、JPEG、PNG 和独立单页 PDF；行程单等证明材料还支持最多 30 页 PDF。
- PDF 文本优先、本地 PaddleOCR 回退；同时尽力本地解码旧版发票二维码作为金额和开票日期的交叉校验证据，不调用付费 OCR 或大模型 API。
- 自动解析铁路电子客票、旅客运输电子发票、纸质出租车小票，并尽力提取国外票据的原币总额和日期；通用票据保守兜底。
- 仅出租车/网约车费用显示行程单关联入口；网约车缺少行程单会阻止提交，同一份多行程文件可被多笔费用分别选用。材料持久区分行程单、付款凭证和普通附件。
- 单张票据的员工确认人民币金额严格超过 500 元时需要关联付款凭证；500 元整不要求。目前铁路豁免仅限明确的 `high_speed`，未把全部火车票作为豁免，最终铁路范围待业务确认。
- 国外票据保存原币信息，人民币报销金额由员工另行填写确认；币种不明确时仍需确认。
- OCR 结果可编辑、删除，也可完全手工新增费用明细。
- 管理员可在每日补助设置之前按费用类别维护关键词；所有关键词规则一致，均可直接新增、修改、移动类别或删除。
- 页面默认标题为“智能差旅费报销申请”；配置文件中的初始管理员可在系统设置中修改标题，并维护其他管理员 userId。
- 基于脱敏公司模板生成 Excel，预览前自动保存最新输入；正式提交还合并生成票据汇总 PDF，两份文件暂存到受控持久卷，上传并从 OA 回读确认后清理本地副本。
- 未完成输入自动保存并恢复；结构化 OCR 候选、原始附件元数据和提交状态持久保存；票据字节保存在受控 staging 卷，预览通过鉴权接口读取。
- 查询并复核当前员工已通过的出差审批，将选中实例写入 OA 的 `RelateField`。多选审批必须公司、预算和来源出差类别一致，日期允许不连续；每张审批保留独立关联记录，重叠日期在补助计算和 Excel 明细中自动合并。
- 后台持久任务完成附件直传、幂等创建 OA、严格回读和确定孤儿附件的补偿清理。

## 补助计算契约

不申请出差补助时，无需填写补助行程信息，Excel 中不生成补助行；OA 仍关联本人已通过的出差审批。申请补助时，员工只需选择出发、返回日期及上午/下午；系统内部按公司时区 12:00 的半天边界换算，不要求输入具体时分。

跨日行程：

- 出发日 12:00 前出发计 1 天，12:00 及以后计 0.5 天。
- 返回日 12:00 前返回计 0.5 天，12:00 及以后计 1 天。
- 中间完整自然日每天计 1 天。

同日往返：

- 跨过 12:00 计 1 天。
- 均在同一半天内计 0.5 天。
- 结束时间早于开始时间必须拒绝。

当前配置默认值来自制度截图，正式使用前需确认：

| 出差类型 | 默认标准 | 处理方式 |
|---|---:|---|
| 商务出差 | 100 元/天 | 按半天规则自动计算 |
| 市外项目短期 | 100 元/天 | 按半天规则自动计算 |
| 市外项目长期 | 150 元/天 | 超过 30 个自然日；按半天规则自动计算，标准由管理员配置 |
| 同市项目 | 50 元/天 | 按半天规则自动计算；用户只需勾选已按公司制度确认 |
| 公司内部出差 | 100 元/天 | 不超过 30 个自然日自动计算；超过 30 天补助为 0 |
| 境外出差 | 不补助 | 可关联审批，但不生成补助项且无补助价格设置 |

境内补助均按日期和上午/下午自动计算有效天数。员工选择“市外项目”后，系统按日期自动分类：连续 30 个自然日以内（含）使用短期标准，超过 30 个自然日使用长期标准。同市项目额外要求员工勾选制度确认；公司内部出差使用一个类别和一项可配置标准，超过 30 个自然日自动按 0 元计算。

## Excel 输出契约

- 保留公司模板的标题、合并单元格、字体、边框、行列尺寸和 A4 打印设置。
- 姓名、部门从 Session 获取；前端不能覆盖。
- 页面只选择“预算代码”，服务器从对应 OA 选项取得完整标签填入 Excel 项目栏，保留正式模板表头；网页管理入口只维护实际需要的设置。
- 明细按发生日期升序稳定排列，同日保持原始录入/上传顺序。
- Excel 币种为“人民币”；国外票据的 `originalCurrency/originalAmount` 是原币参考信息，Excel 只写员工确认的人民币 `amount`。币种未知或存在外币警告时也必须确认，不自动猜汇率或将原币数值视为人民币。
- 费用类别代码和中文名称只在后端公共契约中定义一次；模板现有 17 类全部可手工选择。自动识别覆盖已有样本支持的子集，不能识别的类别直接生成可编辑的“其他”费用行。
- 旅客运输发票优先按票面交通工具分类：铁路为“火车票”、航空为“飞机票”；已支持的公路/出租汽车类型及明确的“客运服务费”税目归为“市内交通费”。客运 PDF 同时从版面列恢复出发地和到达地作为可编辑说明；无法可靠判断时为“其他”并标记需要核对。
- 出差补助行的发生日期为返回日期，说明格式为“开始日期—结束日期，共 N 天出差补助”，票据张数为 0。
- 金额使用 `Decimal`；总额、人民币大写和票据张数由后端重新计算。
- 一份来源文件对应一张票据；费用带 `sourceFileId` 时，服务端将 `receiptCount` 规范为 `1`。无来源文件的手工汇总行仍可调整张数；行程单/付款凭证不计票据张数，付款门槛不按张数除算。
- 姓名、部门、项目、日期文本、说明等所有来自用户、钉钉或 OCR 的 Excel 文本统一通过安全写入函数；首字符为 `=`、`+`、`-`、`@` 时按纯文本转义，禁止形成公式。
- Excel 回归测试必须分别向上述字段写入四种危险前缀，重新打开 `.xlsx` 后断言其仍为字符串、没有公式单元格，并检查 OOXML 中未生成外部输入公式。
- Excel 明细不受模板预留行数限制；超过第 54 行时按模板样式动态扩行，并同步下移总计行、扩展类别下拉和打印区域。部署默认保留 200 条费用明细的技术保护上限，可选补助由后端额外生成。预览内容写入内存后直接下载；正式提交内容从锁定快照重新生成并按 OA 附件生命周期处理。

模板坐标只在 `backend/app/excel/template_contract.py` 中维护：`C2` 姓名、`E2` 部门、
`G2:I2` 项目；第 4–54 行的 `B`、`C`、`D:F`、`G`、`H`、`I` 依次为类别、日期、说明、
金额、币种、票据张数；第 55 行的 `C:F`、`G`、`I` 依次为人民币大写、总金额、总票据数。标准发生日期以 `M月D日` 紧凑显示；明细金额为普通数值格式，总金额使用原版人民币会计格式。
明细按发生日期升序，同日保持用户顺序，补助排在同日用户明细之后。

17 个稳定费用类别为：`airfare`（飞机票）、`rail_fare`（火车票）、`local_transport`（市内交通费）、`lodging`（住宿费）、`subsidy`（出差补助）、`office`（办公费）、`hospitality`（招待费）、`communications`（通讯费）、`employee_welfare`（福利费）、`consulting`（咨询费）、`advertising`（广告费）、`leasing`（租赁费）、`property_management`（物业费）、`utilities`（水电费）、`labor_service`（劳务费）、`conference`（会议费）、`other`（其他）。前端、Parser 和 Excel 服务都引用该公共契约，不各自维护枚举。

## 实现原则

- 从免登、自动保存、票据/OCR、PDF/Excel 到 OA 提交使用同一份服务端权威数据；正式提交后只读不可变快照。
- OCR 获取文本，Parser 负责票据类型和字段；新增费用类别不要求新增 Parser。
- 内置 Parser 明确分类时不允许管理员关键词覆盖；仅当结果为“其他”时应用管理员关键词，同一票据命中多个不同类别仍保留“其他”并提示核对。
- 无法可靠分类、缺少字段或识别失败时，前端保留带警告的可编辑费用行；未完成字段仍可自动保存，汇总只计算完整行，Excel 预览和正式提交要求补齐。前端对每个文件分别发起请求，一张失败或超时不会影响下一张。
- 原始图片和 PDF 都通过鉴权接口读取并生成临时 Blob 地址，在当前页面内预览；PDF 使用浏览器或钉钉 WebView 的内置 PDF 能力，不跳转浏览器，也不触发系统下载。
- 所有业务 API 必须鉴权，写接口必须校验 CSRF，管理员接口必须后端鉴权。
- CSRF token 只保存在前端内存；每次登录随新的 HttpOnly Session 生成新 token，同一登录 Session 内保持稳定，页面刷新或多标签页调用 `GET /api/me` 会取得同一个 token，避免标签页相互失效。
- Client Secret 只在后端；上传目录不可公开；原始 OCR 文本不入库、不写普通日志。
- 不提交含真实员工、项目或票据信息的样本；测试使用脱敏或合成 fixtures。

## Phase 2：钉钉免登配置

后端使用固定的组织应用配置完成免登，浏览器只会读取 `DINGTALK_CLIENT_ID` 和
`DINGTALK_CORP_ID`；`DINGTALK_CLIENT_SECRET`、`DINGTALK_AGENT_ID` 与 `SESSION_SECRET`
只存在后端环境变量中。
钉钉应用需要具备免登码换用户、读取用户详情和读取部门详情的权限，并将实际部署的
HTTP 或 HTTPS 地址按钉钉开放平台要求配置到应用中。首次部署使用 `ADMIN_USER_IDS` 配置至少一个初始管理员
钉钉 userId，多个值用英文逗号分隔。该列表始终生效且不能从页面移除；初始管理员登录后
可在系统设置中新增或撤销其他管理员，避免因误操作失去管理入口。

本地普通浏览器调试可以显式设置 `APP_ENV=development`、
`AUTH_MOCK_ENABLED=true` 和固定的 `AUTH_MOCK_USER_ID`、
`AUTH_MOCK_USER_NAME`、`AUTH_MOCK_DEPARTMENTS`。Mock 接口不接受请求传入的身份或管理员
标记，且生产配置发现 Mock 开启会直接拒绝启动。`.env.example` 默认关闭 Mock。HTTP 入口使用
`SESSION_COOKIE_SECURE=false`；只有确保所有用户入口都是 HTTPS 时才设为 `true`。生产环境仍需配置至少 32 字符的随机
`SESSION_SECRET`。

当前免登 API：

- `GET /api/config/public`：返回页面标题、CorpId、Client ID、开发 Mock 开关、上传/费用条数限制及布尔值 `oaSubmissionEnabled`；后者直接对应 OA worker 配置，不返回应用 Secret 或 AgentId。
- `POST /api/auth/dingtalk`：接收一次性 `authCode` 并建立服务端 Session。
- `GET /api/me`：读取当前身份并轮换仅保存在前端内存中的 CSRF token。
- `POST /api/me/department/from-travel-approval`：核验出差审批并将其中的部门绑定为本次报销部门。
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

## Phase 3：手工报销与计算

- `GET /api/expense-categories` 返回后端集中维护的 17 类费用契约；`subsidy` 由系统生成，不可手工选择。新增类别只改集中契约，OCR Parser 映射保持独立。
- `POST /api/calculate/totals` 要求 Session、由出差审批确定的部门和 CSRF。请求不接受姓名、部门、总额、人民币大写或自动类型的覆盖天数/标准。
- 五项境内每日标准由管理员配置，默认依次为 `100/100/150/50/100` 元。出差补助默认不申请；不连续审批分别计算，重叠审批合并后只计算一次。境内商务和市外项目自动计算；境内同市项目自动计算并要求制度确认；公司内部出差超过 30 天自动不补助；境外不生成补助。
- 金额在 API 中使用两位十进制字符串，后端使用 `Decimal`，前端即时预览使用整数分；最终费用金额、补助、合计、票据数和人民币大写以后端响应为准。
- Pinia 是当前编辑状态，SQLite 是已保存内容的权威来源。页面约 600 毫秒防抖自动保存公司/预算、未完成的补助输入、费用行和材料关系，并显示保存状态；前端串行执行需要 revision 的修改。刷新或换设备按企业、员工和部门恢复最近报销，提交先保存最新输入；员工无需创建、保存或管理草稿。服务端 API 保留 `draftId` 命名用于兼容和并发控制。
- 提交成功可“再报销一笔”，明确 `FAILED_FINAL` 可“重新填写”，均保留已有记录；结果不确定时继续跟踪原任务。尚未锁定且没有跟踪提交的内容遇到模板绑定变化，可在明确确认后“按新表单重新填写”，原记录保留，新表重新填写和上传材料。
- `POST /api/calculate/totals` 的每条计算费用保留 `id`、`source` 与完整费用字段；计算请求和 Excel 输入是不同契约，不能复用只保留 Excel 字段的裁剪函数。端到端请求模型测试覆盖此前的缺字段 422 问题。

## Phase 4：Excel 生成与下载

- 报销页先自动保存当前内容，再调用 `POST /api/reimbursements/drafts/{draftId}/excel-preview`，要求 Session、当前部门、CSRF 和保存后的 revision。`trip=null` 表示不申请补助，不生成补助行。
- 报销输入只选择 `budgetCodeValue`；后端用绑定 OA Schema 的完整选项标签作为项目，不信任客户端另传项目覆盖。费用行保存原币、来源和行程单关联等辅助字段，生成 Excel 时只投影必要的公司模板字段；补助由后端生成。
- 姓名和部门只取当前 Session；补助设置、有效天数、补助金额、费用合计、总额、票据数和人民币大写均在生成时重新计算。
- 后端只读加载 `backend/app/templates/expense_template.xlsx`，先做 ZIP 安全预检（10 MiB 文件、200 条目、20 MiB 单条目、50 MiB 总解压体积、200 压缩比上限，并拒绝重复或越界路径），再验证唯一可见 `费用报销模板`、标签、合并区域和精确的 17 类 `B4:B54` 字面列表验证。生成内容超过模板预留行时，服务按第 54 行样式扩展明细、类别下拉和合并区域，将总计行及打印区域同步下移；保持样例的 A4 纵向、页边距和打印设置。公式或自定义数据验证、普通公式、超链接、额外定义名、打印标题、条件格式、表格/计算列、透视、图表、图片/绘图、切片器、批注/注释、外部关系或链接、宏/VBA、数据连接、QueryTable、ExternalLink 和嵌入活动内容一律拒绝。写入 `BytesIO` 后再次验证并直接流式返回，不创建临时 Excel。
- 所有外部文本都会清理控制字符并限制长度；去除前导空白后以 `=`、`+`、`-`、`@` 开头的内容加文本转义前缀，确保不会形成公式。
- 文件名为 `差旅费报销单-姓名-预算代码标签.xlsx`，项目文件名片段独立限长，替换 `/\\:*?"<>|` 和控制字符，并通过 RFC 5987 的 `Content-Disposition` 返回。前端以 Blob 下载并及时释放对象 URL；Excel 项目正文保留完整标签并按需扩展标题行。

替换模板时必须先在副本中彻底脱敏，确保只有一个可见 `费用报销模板`，不含公式、超链接、额外定义名、
打印标题、条件格式、表格/计算列、透视、图表、图片/绘图、切片器、批注/注释、外部关系或链接、
宏/VBA、数据连接、QueryTable、ExternalLink 或嵌入活动内容，并通过上述 ZIP 预检和严格部件白名单；
同时保持基础模板的上述坐标、`D4:F54`/`C55:F55` 合并及精确的 17 类 `B4:B54` 字面列表验证。运行时只允许生成服务按受控规则扩展这些区域，并继续保持样例的 A4 纵向、页边距和打印设置。随后运行完整 Excel 回归测试。不得修改桌面业务样例原件，也不要通过放宽模板契约来掩盖模板版本差异。

## Phase 5：持久材料、票据关联与本地 OCR

当前报销页使用报销记录级文件接口，依靠服务端持久内容完成预览、恢复和正式提交：

- `POST /api/reimbursements/drafts/{draftId}/files` 按 `expectedRevision` 上传一份 `EXPENSE_SOURCE` 或 `ATTACHMENT_ONLY`；证明材料另传查询参数 `attachmentKind=itinerary|payment_proof|hotel_bill|other`（默认 `other`），响应持久返回该用途，PATCH 可修改用途。费用来源只能为 `other`。元数据和 OCR 状态写入 SQLite，文件字节写入受控 staging。票据 PDF 单页，证明材料 PDF 最多 30 页；页面刷新、进程重启或换设备后通过身份和归属检查恢复。
- `GET /api/reimbursements/drafts/{draftId}/files/{fileId}/content` 返回图片/PDF 用于预览，校验企业、员工、部门、文件状态、有效期、大小和 SHA-256，响应禁止缓存；浏览器关闭预览时释放 Blob URL。
- `POST /api/reimbursements/drafts/{draftId}/files/{fileId}/ocr` 持久结构化 OCR 候选和状态。费用行通过 `sourceFileId` 关联票据；每份终态票据正式提交前必须计入费用或被员工明确选择仅作为材料保留。自动保存允许尚未完成这项选择；后端完整性检查会拦截未处理票据。
- 行程单通过费用行 `itineraryFileIds` 关联同一报销中的活动 `ATTACHMENT_ONLY + itinerary`，付款凭证通过 `paymentProofFileIds` 关联活动 `ATTACHMENT_ONLY + payment_proof`；普通材料不能冒充任一种证明。网约车必须关联至少一份行程单，允许多笔费用分别选择同一份多行程 PDF。删除、改角色或改用途会原子清理旧引用并重新要求补齐。
- 每笔住宿费必须通过 `hotelBillFileIds` 关联至少一份活动的住宿明细文件；姓名、入住/离店日期、价格等字段只做尽力识别，字段缺失不单独提示，也不阻止提交。
- 行程单识别结果保存在文件的 `ocrResult`，以 `kind=itinerary` 与票据候选区分，包含汇总和逐段行程，不新增费用或金额。前端只对完整、无警告且唯一的候选自动关联：先看明确的同类发票号/订单号，再看人民币金额、发生日期及有方向的起终点；号码矛盾、路线反向、候选重复或部分识别时交人工。已有手工关联不被覆盖，文件名不作为匹配证据。
- 员工手工改变或清空行程单关联时，费用输入保存 `itineraryAutoMatchDisabled=true`，刷新后仍尊重人工选择；只有显式重新自动匹配才恢复为 `false`。该严格布尔值默认 `false`，只记录编辑意图，不进入金额计算或不可变提交快照。
- 行程单 PDF 仍允许上传/打印全部最多 30 页；识别先检查全部页面，再逐页优先读取原生文字，扫描/混合页最多回退本地 OCR 5 页。整份文件共用现有 OCR 超时（默认 120 秒）和单进程准入槽，同文档复用模型。超限、缺关键字段、币种/金额/日期冲突或未解析续页时 `complete=false` 并带警告，仅供人工参考；识别是否完整不改变最终打印的原 PDF 页面，不把外币金额当作人民币匹配。
- 付款要求由后端按员工确认的人民币票面 `amount > 500.00` 独立计算，不信任客户端必需标记，不使用原币数额、不除以 `receiptCount`，本期不引入分摊/部分报销金额或票据验真。铁路 `railType` 为 `high_speed/emu/regular/unknown`；仅 `category=rail_fare` 且 `high_speed` 暂时豁免。服务端已识别的确切铁路类型不可用客户端值覆盖，未知类型和未分类/失败的 `other` 允许员工补选；已知非铁路证据不能伪标铁路取得豁免。复核和正式提交均严格校验。
- 新增出租车和国外票据本地解析器；国外票据保存 `originalCurrency/originalAmount`，人民币 `amount` 留给员工填写并确认 `cnyAmountConfirmed`。来源为 `foreign_receipt`、含外币警告或 `requiresCnyConfirmation` 时，即使币种未知也必须确认。
- 未提交文件保留到 `expiresAt`。新版本只有汇总 PDF 和 Excel 从 OA 回读并均为 `LINKED` 后才删除原件和生成文件的本地副本；不确定状态保留。详见 [文件生命周期](docs/DINGTALK_OA_INTEGRATION_DESIGN.md#13-文件保留和清理规则)。

### 提交材料汇总

服务器按每笔费用“来源票据 → 对应行程单 → 对应付款凭证”，最后追加其余材料的顺序生成 `票据汇总.pdf`，同一个文件 ID 只放入一次；同一笔报销再次选择字节完全相同的文件时直接提示并跳过，不重复上传或识别。原 PDF 逐页保留文字、图像、尺寸和旋转；照片按 EXIF 方向纠正、等比放入 A4、透明背景转白，保持完整图片边界。合并结果直接使用原件内容，用于阅读打印，不以 OCR 文本重建票面，也不承诺保留原 PDF 数字签名效力。

提交使用 `snapshotVersion=6`，冻结附件用途、证明材料关联、铁路类型、逐张出差审批对应的补助项、来源类别和日期区间；上传记录严格为顺序 0 的 `GENERATED_PDF` 和顺序 1 的 `GENERATED_EXCEL`，OA 附件控件仅两项。汇总最多 500 页并受单对象容量限制；生成或上传失败使用持久检查点恢复，原件只有本地来源记录。

数据库迁移头为 `20260914_0018`。部署前应一致性备份 SQLite 与 staging，再通过原启动命令执行迁移并重启加载新代码。

### 文件解析和资源防护

- 持久材料上传使用 multipart 字段 `file`，要求 Session、已由出差审批确定部门和 CSRF。每个请求只携带一个文件；后端先取得全局准入和 Session 上传租约，再流式解析正文。单文件最多 20 MiB，请求体上限 25 MiB（含 multipart 余量）；`Content-Length` 只用于提前拒绝，流式计数才是权威限制。单笔报销默认最多保留 200 个文件、合计 100 MiB。
- Nginx 的 `client_max_body_size` 设为 25 MiB，为一个 20 MiB 文件的 multipart 边界和请求头预留余量。
- 文件扩展名、magic bytes 和实际解析结果必须一致。Pillow 图片解码、PDF 预检/文本提取和 OCR 在可终止的标准库 `spawn` 工作进程中执行。文件验证使用独立单槽并只短暂等待；OCR 仍固定为一个执行槽，并使用进程内有界 FIFO 等待队列（默认最多 8 个等待请求、等待 150 秒），不同用户按 OCR 请求到达顺序公平准入，不增加并行模型数量。队列满或超时返回可重试的 `OCR_BUSY`，单次执行超时或资源终止返回 `OCR_TIMEOUT` / `OCR_FAILED`；等待和执行总时限同时用于判断持久 `RUNNING` 状态是否真的中断。取消请求时，准入令牌会一直保留到该子进程终止并回收，然后同步关闭 spool 并删除 `.part`/最终文件。PDF 资源检查递归覆盖 Form XObject 及图片遮罩，并分别限制页面内容流数量（默认 512）和 XObject 总数（默认 100），同时限制嵌套深度、MediaBox/渲染像素、全部内嵌图片累计像素和每页解压后内容流字节数。
- 上传缓存路径只使用服务端 Session 哈希与随机 UUID；取消和退出会清理未落入报销 staging 的缓存。实现不跟随符号链接，不写 OCR 原文或 sidecar。
- 持久 OCR 接口每次处理一个报销文件，保存来源关系和状态；金额使用两位十进制字符串。
- 单页数字 PDF 先使用原生文本；文本过短、解析后仍缺金额或日期、火车票缺路线，或者旅客运输票据仍无法分类时，才执行一次 PaddleOCR 回退。客运 PDF 的起终点优先使用 pypdf 版面列提取；纯图片 OCR 若把出发地与到达地合成一个跨列文字框，则根据已识别的表头坐标分别裁切两个单元格并二次识别，不凭扁平文本猜测列边界。不记录姓名或证件号。基础开发/测试不会导入或加载 Paddle，测试用显式 fake engine；生产禁止 fake。
- 普通发票金额依次优先使用票面数字“价税合计”、大写“价税合计”、或明确的“合计 + 税额”；单独的未税“合计”不会被当成最终报销额。旧版增值税发票二维码只作为补充证据。二维码金额与票面总额一致，或“二维码金额 + 税额 = 票面价税合计”时可提高可靠性；冲突时保留票面总额并提示人工核对；票面缺少金额而只从二维码取得候选时也必须提示核对。二维码中的开票日期不覆盖火车票乘车日期或旅客运输发生日期。动态二维码和未知格式会被安全忽略，不上传、不访问二维码链接、不记录原始载荷。
- 管理员在“系统设置 → 票据分类关键词”按费用类别分组维护全部分类词；该区域显示在每日补助设置之前。设置页与报销页共用 `GET /api/expense-categories` 返回的后端类别契约：系统生成的“出差补助”不显示，“其他”显示为自动兜底且不能添加关键词。关键词按 OCR/PDF 文本的字面子串匹配，至少 2 个字符；所有词始终生效，均可新增、修改、移动类别或删除，不区分来源或启停状态，最多 500 条。不同费用类别同时命中时保留为“其他”；旅客运输发票只在票面“交通工具类型”或明确客运税目中匹配类别，不用起终点、酒店或公司名称作分类证据。火车票等专用票据仍结合版式结构识别，不会只因删除分类词而失去基础识别能力。`GET/POST/PUT/DELETE /api/admin/receipt-keywords` 均由后端管理员权限保护，写操作同时校验 CSRF。
- 本地 OCR 依赖精确锁定为 PaddleOCR 3.7.0、PaddlePaddle 3.3.1、OpenCV 4.10.0.84 和 pypdfium2 5.13.0；后两项用于本地二维码识别和单页 PDF 渲染。开发安装和正式镜像均默认包含 OCR。Linux/amd64 是正式部署路径并要求 AVX CPU；macOS/arm64 仅作为本机 CPU 开发测试路径。模型二进制不入库；正式镜像构建时按固定清单从官方源下载并校验 `PP-OCRv6_small_det` 和 `PP-OCRv6_small_rec`，内置为只读文件并附带来源/许可证说明。本地开发仍可使用经过同样校验的宿主模型挂载。运行时不会下载模型或回退云服务。
- 2026-09-03 已在当前 Apple Silicon Mac 用固定 SHA 清单中的两份官方模型完成本地 CPU 检查，并通过 Docker 的 Linux/amd64 模拟环境完成完整镜像构建、production 冷启动、资源上限和真实票据 OCR smoke。模型二进制由 `.gitignore` 排除；本地原生开发需按清单预置，正式镜像构建会自动取得模型；原生 Linux/amd64 目标机和断网部署仍需上线前复验。OCR 默认启用，依赖或模型不完整时 readiness 失败。

前端只提供一个“费用明细”区域；每笔费用集中显示票据、对应行程单/付款凭证、警告及一组操作，上传/识别中和未分配材料也显示在同一区域。批量选择后先为全部文件建立稳定占位，再逐份上传和逐份识别，显示整批进度；完成后统一加入费用。服务端返回 `FAILED`（包括自动分类失败且没有普通 `ocrResult`）时，批次明确显示“需要处理”而不是误报完成；单份上传失败可就地重试或移除，重试不会重放已成功文件；已上传但识别失败、中断或没有可用结果的材料可单独重新识别、确认用途或删除，已有可用识别结果时通过编辑修正而不再常驻显示重复识别入口。切换报销/部门或退出时不采用旧响应。前端从后端公开配置读取技术保护上限，默认可处理 200 条费用明细。员工可预览来源、编辑金额、处理异常识别、删除或仅保留材料；文件关系由持久 ID 维护，不再重复渲染完整附件卡片和完整费用表。页面展示结构化结果，原始 OCR 文本不入库。

新增解析规则复用现有本地模型，优先原生 PDF 文本，只在必要时执行 OCR；出租车/境外专门字段规则在本地轻量执行。实际耗时还包括图片大小、上传和每次工作进程初始化，应使用目标服务器实测值评估，不能用少数样本宣称全部国外票据的准确率或固定秒数。

上传流先进入 `<TEMP_DIR>/.spool`，单文件只允许 256 KiB 留在内存，超过后转为可计量的磁盘缓存；校验后立即转存到报销 staging 并关闭、删除缓存。全局缓存默认限制为 768 MiB，包含活动/残留 spool 和 `.part`；清理会跳过仍在使用的缓存。上传、识别和删除在 Session 租约内重新确认身份、公司和有效期。生产容器以固定非 root 用户运行，后端不直接发布端口，只由 Nginx 反向代理访问。持久报销文件由 revision、`expiresAt` 和提交状态机控制。

工作进程在 Linux 上保留宿主硬限额，只调整可恢复的软限额：文件/PDF 验证默认 512 MiB，OCR 默认 5 GiB 虚拟地址空间（Paddle/OpenCV 会预留较大虚拟映射，常驻内存显著低于此值），每页 PDF 解压后内容流默认最多 32 MiB，同时限制生成文件大小、打开文件数、CPU 时间并禁止 core dump。Compose 额外使用 3 GiB 容器实际内存和 128 PID 上限。正式 OCR 必须运行在支持这些限额的 Linux x86_64/amd64 环境，不提供绕过生产限额的开关。

## 开工前业务确认

- 公司正式空白 Excel 模板，以及预算代码完整标签在项目栏的显示和打印效果。
- 截图制度是否仍为现行版本，以及 12:00 整点归属。
- 五项境内补助标准、市外项目与公司内部出差的 30 天边界，以及同市项目制度确认责任人。
- 钉钉预算代码选项、管理员钉钉 userId。
- 钉钉 CorpId、Client ID、Client Secret、权限和应用访问地址。

公司正式模板版本和制度版本是业务验收依赖，不阻止开发闭环；模板版本仍阻止 Phase 4 正式业务验收，制度版本阻止补助功能及生产上线验收。开发期间只使用已脱敏模板和可配置默认值，不能把待确认内容固化成不可修改规则。

## 依赖锁状态

- 前端提交 npm v3 `package-lock.json`，本地安装与容器构建统一使用 `npm ci`，不允许在构建阶段改写锁文件；当前锁已通过全新 `npm ci`、测试、类型检查、Lint 和构建验证。
- 前端使用 TypeScript 官方的 7/6 并行过渡方案：`@typescript/native` 提供 TypeScript 7 原生 `tsc`，`typescript` 别名指向 TypeScript 6 API，供尚未兼容原生编译器 API 的 `vue-tsc` 和 `typescript-eslint` 使用。`npm run typecheck` 会同时运行两条检查链，不能删除其中任一依赖后只验证另一条。
- 容器构建使用 Node 24 LTS，后端运行时使用 Python 3.13.15；uv 固定为 0.12.9，入口使用 Nginx 1.30.4 stable。Python 3.14 尚无当前 PaddlePaddle 版本的 wheel，Node 26 仍为 Current，因此暂不采用。
- 本地 Python 由 `backend/.python-version` 固定为 3.13.15 并交给 uv 管理，不要求通过 Homebrew 安装 Python；macOS 自带 Python 不参与后端运行。
- 后端业务调用钉钉和 PaddlePaddle 依赖继续使用 `httpx` 0.28.1；测试环境另外锁定 `httpx2` 2.12.0，专供新版 Starlette `TestClient` 使用，不混用两套客户端处理业务请求。
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
真实 PaddleOCR；依赖或模型缺失时直接拒绝启动，不会静默关闭 OCR。

### macOS arm64 本地 OCR 联调

当前 Apple Silicon Mac 可以使用官方 macOS arm64 CPU wheel 做开发测试；不启用 GPU 或 HPI，
也不改变“生产 OCR 必须运行在 Linux x86_64/amd64”的安全边界。本地开发默认启用真实 Paddle：

1. 按 [模型目录说明](backend/models/README.md) 从官方地址取得两个推理模型，记录原始归档
   SHA-256，并解压到指定的两个目录。模型文件不提交仓库。
2. 安装锁定的 macOS arm64 OCR 依赖，先核对模型固定 SHA 清单，再做真实 Paddle 初始化和空白图最小推理：

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

当前 Mac 的真实 smoke 结果：`高铁1.pdf` 识别为火车票，日期 `2026-07-07`、金额 `473.50`、
路线“合肥南站-北京南站”；`打车发票1.pdf` 识别为市内交通费，发生日期 `2026-07-06`、金额
`10.13`，并能从票面行程表恢复起点-终点说明。这是单票据 OCR 检查记录；完整报销流程按 OA 集成设计的验收矩阵单独验证。

不要复制这组开发值到生产配置。`APP_ENV=production` 时，Mock、空或占位
钉钉配置、短 Session Secret、Fake OCR 都会使配置校验直接失败。

## 存活、就绪与部署边界

- `GET /api/health` 只表示 FastAPI 进程存活，不访问依赖。
- `GET /api/ready` 检查 SQLite 连接、当前 Alembic revision 与关键表字段、Excel 模板完整契约、临时目录可写性，以及
  报销持久暂存目录的受控权限、写入/删除能力和最小可用空间，并检查
  已启用 OCR 的模型精确文件集合、固定 SHA 清单和锁定包版本；返回内容只有组件状态，不包含路径、Secret 或异常细节。模型内容校验按文件 stat 指纹缓存，文件变化才重新计算 SHA；就绪检查不会初始化或加载 Paddle 模型。
- 模板严格校验结果按文件 stat 指纹缓存在最多 8 个条目的进程内缓存中；模板文件变化会重新
  执行完整 ZIP 与工作簿契约校验，常规 10 秒 readiness 轮询不会重复解析不变的 `.xlsx`。
- OCR 默认启用并纳入就绪检查；模型、固定 SHA 或 Paddle 版本不完整时返回 503，
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
发送。`.env.example` 只列空键名。

应用 Nginx 关闭版本标识和含查询参数的访问日志，设置 CSP、`nosniff`、no-referrer 与受限
Permissions Policy；`/api`、认证和下载均为 `no-store`，仅 Vite 哈希资源长期缓存。它不依据
`remote_addr` 或调用方提供的 `X-Forwarded-For` 限流：认证、上传、OCR 和 Excel 的 IP/账号限流
由受控的入口层执行，入口层必须丢弃并重建外部转发头。上传请求关闭 Nginx request
buffering；无法避免的 client/proxy temp 路径位于 Web 容器受限 tmpfs，不写镜像层或宿主目录。
Web 容器以非 root、只读根文件系统、全部 capability drop、no-new-privileges、内存和 PID 上限运行。
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
[镜像构建与发布配置](docs/GITHUB_ACTIONS_SETUP.md)。该流程在构建时下载并校验 OCR 模型，
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

- 后端结构化日志只记录 request ID、方法、URL path、状态、耗时和安全错误类型；不记录查询
  参数、Cookie、票据 OCR 原文、证件号、票号或完整路线。Nginx access log 默认关闭。
- 服务启动和运行期间会清理遗留上传缓存；退出会立即清理当前 Session 尚未落入报销 staging 的缓存，不会越过报销权限和状态机删除持久文件。过期服务端 Session 在启动及请求期按 5 分钟门限清理。
- SQLite 持久卷保存配置、Session、自动保存内容、附件元数据、模板目录、不可变提交快照、任务状态和远端操作检查点。原件、汇总 PDF 和 Excel 保存在独立 `reimbursement_staging` 持久卷。两个持久卷必须做一致性备份和恢复；只恢复一份会造成数据库哈希与文件不匹配，系统应停止提交而不是继续使用错误文件。短期 `sessions` 不作为业务恢复来源。
- 完整恢复演练应在隔离环境恢复同一时点的 SQLite 和 `reimbursement_staging`，执行当前 migration，运行 `/api/ready`，并核对草稿文件 SHA-256、提交快照和远端检查点。只恢复项目和设置的初始化演练不等于业务数据恢复。

## 本地 OCR 制品上架步骤

1. 只从 `backend/app/ocr/model_manifest.json` 记录的官方来源取得
   `PP-OCRv6_small_det` 和 `PP-OCRv6_small_rec`；该机器可读文件是来源、归档 SHA、模型文件
   SHA 和目录清单 SHA 的唯一记录。模型二进制不提交仓库。
2. macOS/arm64 可通过上一节的默认命令做 CPU 开发 smoke；正式环境仍在 Linux/amd64、支持
   AVX 的目标机构建默认包含 OCR 的镜像，将两个已核验目录放在
   `OCR_MODEL_HOST_DIR` 下并只读挂载。
3. Compose 默认启用 OCR 并使用容器内标准模型目录，无需另外设置启用开关；不配置任何云 OCR 地址或 Token。运行时
   不下载模型，也不会在失败时回退付费服务。
4. 先运行 `make ocr-models-check` 和 `make ocr-runtime-check`，再确认 `/api/ready` 为 200；最后
   用公司提供的脱敏独立票据样本执行真实 smoke test。模型目录/依赖不完整时部署保持未就绪，
   修复制品后再上线。

## 正式验收仍依赖的外部输入

- 真实钉钉租户的 CorpId、应用凭据、权限、应用访问地址、多部门用户和管理员账号联调。
- 公司当前正式空白模板，并在公司 Microsoft Excel 中核对打开、打印和版式；现有脱敏模板
  只证明开发闭环。
- Paddle 模型制品以及公司脱敏 JPG/PNG/独立单页 PDF 样本，用于 Linux 目标机准确率、资源
  上限和断网 smoke test。
- 原生 Linux/amd64 生产机上的断网冷启动、持久卷恢复和实际 HTTP/HTTPS 入口验收。
- 公司确认现行补助制度、12:00 整点归属、预算代码选项、网约车材料和境外人民币金额确认方式，以及管理员钉钉 userId。
