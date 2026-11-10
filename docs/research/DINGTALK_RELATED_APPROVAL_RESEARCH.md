# 钉钉“关联出差审批单”API 调研

调研日期：2026-09-03（Asia/Shanghai）
资料口径：只采用钉钉开放平台官方文档；未在官方文档中明确说明的能力单独标为“待 PoC”，不按已支持处理。

## 结论

如果当前“差旅费报销申请”是该创建接口支持的普通 OA 模板，企业内部 H5 应用可以实现下面的完整流程：

1. H5 免登取得当前员工的 `userId`。
2. 后端分别查询该员工发起的“境内出差申请”和“境外出差申请”。
3. H5 自己展示可选列表，员工选择一条或多条已经通过的出差申请。
4. 后端调用“发起审批实例”接口创建“差旅费报销申请”，把选中的 `processInstanceId` 写入“关联审批单”控件。

其中，“查询当前员工发起的审批”和“创建报销审批时写入关联关系”都有官方接口及参数依据，并已在测试企业完成分项可行性验证。当前未发现钉钉公开的 H5 JSAPI 可以让自建 H5 直接唤起截图中的原生“关联审批单”弹窗；该弹窗应视为钉钉原生 OA 表单内部能力。实现使用自建选择页面，服务器在保存和提交时重新复核关联实例。

[发起审批实例](https://open.dingtalk.com/document/development/create-an-approval-instance) 明确注明，“假勤、人事、财税、法务、商旅等套件暂不支持直接通过本接口发起审批实例”。测试企业的已配置模板已证明可由接口创建；上线前仍必须用生产模板的真实 `processCode` 执行 inspect、Schema 映射和创建能力预检。不支持创建接口的模板不得在模板目录中启用。

## 一、查询当前员工发起的出差申请

### 1. 官方接口

调用 [获取审批实例 ID 列表](https://open.dingtalk.com/document/development/obtain-an-approval-list-of-instance-ids)：

```http
POST https://api.dingtalk.com/v1.0/workflow/processes/instanceIds/query
```

该接口只返回审批实例 ID，不直接返回标题、表单内容等详情。拿到 ID 后，再逐条调用 [获取单个审批实例详情](https://open.dingtalk.com/document/development/obtains-the-details-of-a-single-approval-instance-pop)：

```http
GET https://api.dingtalk.com/v1.0/workflow/processInstances?processInstanceId=实例ID
```

详情中包含标题、发起人 `originatorUserId`、状态 `status`、审批结果 `result`、业务编号 `businessId`、表单数据和操作记录等。

### 2. 查询“当前员工自己发起的、已经通过的出差申请”

境内、境外出差申请是两个模板，应分别调用一次列表接口，然后合并结果：

```json
{
  "processCode": "境内出差申请的processCode",
  "startTime": 1754179200000,
  "endTime": 1764547199000,
  "nextToken": 0,
  "maxResults": 20,
  "userIds": ["当前登录员工的userId"],
  "statuses": ["COMPLETED"]
}
```

第二次只把 `processCode` 换成“境外出差申请”的模板编号。

这里需要注意：

- `userIds` 是发起人过滤条件，最多传 10 个；本项目只传当前登录员工的一个 `userId`。
- `statuses` 支持 `RUNNING`（审批中）、`TERMINATED`（已撤销）、`COMPLETED`（审批完成）；不传表示查询所有状态。
- `COMPLETED` 只表示流程已经结束，不能单凭它判断“审批通过”。应继续读取实例详情，只保留 `result == "agree"` 的记录。官方详情响应示例同时展示了 `status` 和 `result` 字段。
- 后端仍应再次验证详情中的 `originatorUserId` 等于当前登录员工，不能只依赖前端传来的 userId。

因此推荐的筛选逻辑是：

```text
processCode 属于境内/境外出差模板
AND originatorUserId == 当前登录员工
AND status == COMPLETED
AND result == agree
```

### 3. 分页和时间范围

官方列表接口的分页规则：

- 首次请求传 `nextToken = 0`。
- 后续请求传上一次响应中的 `nextToken`。
- `maxResults` 每页最多 20。
- 循环获取的实例 ID 总数最多 10,000。

普通版本的时间范围限制：

- 只传 `startTime` 时，该时间距当前不能超过 120 天，`endTime` 默认当前时间。
- 同时传 `startTime` 和 `endTime` 时，单次时间跨度不能超过 120 天，并且 `startTime` 距当前不能超过 365 天。
- 官方页面说明 OA 高级版最多可查询 5 年内数据；本项目不应先假设已经购买该能力。

产品上建议默认显示最近 120 天，提供“加载更多”或日期筛选。搜索姓名、标题、审批编号不是这个列表接口的服务端参数；如果需要截图中的搜索框，要在获取实例详情后由本系统本地筛选，或者按时间分段继续查询。

### 4. 前后端建议接口

前端不直接持有钉钉应用密钥，也不直接调用服务端审批 API。建议新增本系统自己的接口，例如：

```http
GET /api/travel-approvals?type=domestic&from=2026-05-01&to=2026-09-01
```

后端从当前登录 Session 取得员工 `userId`，不接受前端任意指定其他员工。返回给 H5 的每一项可以包含：

```json
{
  "processInstanceId": "SYNTHETIC-TRAVEL-001",
  "businessId": "审批页展示的业务编号",
  "title": "测试员工提交的境内出差申请",
  "status": "COMPLETED",
  "result": "agree",
  "startTime": "2026-06-30",
  "endTime": "2026-07-07",
  "project": "DEMO-001 示例项目"
}
```

`processInstanceId` 用于最终关联；`businessId` 或页面上的“审批编号”只适合展示，不能代替 `processInstanceId`。

## 二、创建报销审批时填写“关联审批单”

### 1. 官方传值格式

[发起审批实例](https://open.dingtalk.com/document/development/create-an-approval-instance) 接口的 `formComponentValues` 用于给审批表单控件赋值。官方 [FormComponentValues 参数说明](https://open.dingtalk.com/document/development/oa-formcomponent-message#9bcb6b14dbz03) 给出的“关联审批单”示例为：

```json
{
  "name": "关联审批单",
  "value": "[\"instance-id-1\", \"instance-id-2\"]"
}
```

关键点：

- `value` 不是普通数组，而是“JSON 数组序列化后的字符串”。
- 数组成员是被关联审批的 `processInstanceId`。
- 不是审批页面上展示的审批编号，也不是模板的 `processCode`。
- 官方示例一次传两个实例 ID，因此 API 传值格式明确支持多个关联审批实例；传一条时仍使用只含一个元素的数组字符串。
- `name` 要与当前报销模板中该控件的标题一致，例如当前可能叫“关联审批单”。上线前必须读取或人工确认真实模板字段名。

### 2. 允许关联哪些模板

同一份官方补充参数文档把该控件定义为 `RelateField`，并说明 `availableTemplates` 是“可被关联的审批模板列表”；列表为空时表示可关联所有审批模板实例。每个允许模板由名称和 `processCode` 标识。

因此应检查当前“差旅费报销申请”模板中的关联控件是否已经允许：

- 境内出差申请的 `processCode`；
- 境外出差申请的 `processCode`。

截图已经显示这两类入口，说明当前模板很可能已配置，但仍应在管理员后台或模板 Schema 中核对，不依靠截图下结论。

### 3. 创建请求中的示意

```json
{
  "originatorUserId": "当前员工userId",
  "processCode": "差旅费报销申请processCode",
  "deptId": 123456,
  "formComponentValues": [
    {
      "name": "预算代码",
      "value": "DEMO-001 示例项目"
    },
    {
      "name": "关联审批单",
      "value": "[\"出差申请processInstanceId-1\",\"出差申请processInstanceId-2\"]"
    }
  ]
}
```

创建审批接口的官方“特别提醒”明确要求：关联审批单控件传入的实例 ID 必须是当前组织下真实存在的审批实例 ID。接口需要“工作流实例写权限”。

另外，若不传 `approvers`，官方说明会复用 OA 后台配置的审批流程，包括条件审批、或签、会签、发起人自选等。本项目应优先不传 `approvers`，让现有报销模板继续决定审批人；不要在本系统里重新实现审批链。

创建接口本身不支持钉钉注明的假勤、人事、财税、法务、商旅等套件模板；这项限制与是否传入关联审批无关。应先验证目标报销模板能够通过 API 创建，再继续验证关联控件。

## 三、H5 是否能直接打开钉钉原生“关联审批单”弹窗

### 已证实

- 钉钉公开了审批实例列表查询 API，因此本系统可以自行取得候选数据。
- 钉钉公开了创建审批时给 `RelateField` 传值的格式，因此员工在本系统选完后可以真正写成 OA 的关联关系。
- 官方 `dd.requestAuthInfo` JSAPI 有“审批—授权获取审批实例数据”场景，但其支持表明确写：H5 微应用不支持，仅移动端小程序支持；并且该接口是授权弹窗，不是返回审批列表的关联审批选择器。[requestAuthInfo](https://open.dingtalk.com/tools/explorer/jsapi?id=10296)

### 未发现明确官方能力

截至本次调研，在钉钉公开的 H5 JSAPI 文档中未发现一个可由企业自建 H5 直接调用、并返回所选 `processInstanceId` 的“关联审批单选择器”接口。不能因为钉钉原生 OA 表单中存在截图所示弹窗，就推断 H5 可以单独唤起它。

这属于“未发现公开文档”，不等于断言钉钉内部绝对没有私有或定向开放能力。若公司钉钉管理员或钉钉技术支持确认存在专享能力，可另做 PoC；在拿到明确官方接口前，产品设计应按“自建选择页”实施。

### 推荐的自建选择页

界面可以接近截图，但只保留本业务需要的内容：

```text
关联出差申请

[境内出差] [境外出差]
[仅显示审批通过]  [最近120天]

□ 测试员工提交的境内出差申请
  2026-06-30 至 2026-07-07
  DEMO-001 示例项目
  审批通过

[确定]
```

员工点击“确定”后，前端只把选中的 `processInstanceId` 发给本系统后端。后端在创建报销 OA 前再次检查：实例属于当前员工、模板类型正确、结果为通过，然后写入 `RelateField`。

## 四、权限和可见性限制

### 官方明确要求

- “获取审批实例 ID 列表”目前标注只支持企业内部应用，并要求“工作流实例读权限”。
- “获取单个审批实例详情”要求“工作流实例读权限”。
- “发起审批实例”支持企业内部应用和第三方企业应用，要求“工作流实例写权限”。
- 列表接口的官方描述是获取“权限范围内的相关部门审批实例 ID 列表”，因此应用权限范围会影响查询结果。
- 创建接口要求发起人的 `userId` 有效；复用模板流程时需要发起人所属的合法 `deptId`。错误码中还包括“没有发起审批的权限”和“无操作审批流的权限”。
- 关联实例必须属于当前组织且真实存在。

### 本项目必须额外执行的权限收口

服务端 API 使用的是应用凭证，返回范围可能大于当前员工在页面上应看到的范围。因此即便接口允许查询更多记录，本系统也只能展示和接受：

```text
originatorUserId == 当前登录员工userId
```

不能让前端提交一个任意 `userId` 查询其他员工，也不能只验证“实例存在”就接受前端提交的关联 ID。提交前应重新读取详情并校验归属、模板和审批结果，防止员工篡改请求关联他人的审批。

### 待 PoC

- 公司当前内部应用是否已经开通“工作流实例读权限”和“工作流实例写权限”。
- 应用的组织/人员权限范围是否覆盖所有需要使用智能报销的员工。
- 普通员工发起的境内、境外出差申请是否都能被该应用查询到。
- 当前报销模板的发起人可见范围是否包含这些员工。
- 当前关联控件是否确实允许同时关联两条，及是否存在管理员配置的数量限制；API 示例支持多 ID，但仍需用真实模板验证实际 UI 和提交规则。

## 五、附件与创建审批（简述）

附件与关联审批是两个独立控件。官方 `FormComponentValues` 附件示例要求把 `spaceId`、`fileId`、`fileName`、`fileSize`、`fileType` 组成的对象数组序列化后传给附件控件。文件必须先进入审批对应的钉盘空间，不能把本系统的本地文件路径直接写入创建请求。

钉钉官方说明审批附件操作需要服务端接口与客户端 JSAPI 配合；`dd.uploadAttachmentToDingTalk` 返回上述文件元数据，但公开支持表中 H5 微应用只明确支持 Android/iOS，Mac/Windows 标为不支持。[审批附件操作流程](https://open.dingtalk.com/document/development/new-version-of-attachment-approval-process)、[上传附件到钉盘/从钉盘选择文件](https://open.dingtalk.com/tools/explorer/jsapi?id=10318)

因此，关联审批能力可以独立先验证；但整个“自动创建报销 OA”上线前，仍需单独验证原始材料和服务器生成 Excel 的附件上传路径。

## 六、建议的 PoC 验收步骤

### PoC A：查询候选出差申请

1. 取得两个真实模板的 `processCode`。
2. 用测试员工 H5 免登后的 `userId`，分别查询两个模板最近 120 天的 `COMPLETED` 实例。
3. 获取每个实例详情，只展示 `originatorUserId` 相同且 `result == "agree"` 的记录。
4. 验证审批中、已撤销、已拒绝、其他员工的实例均不会进入候选列表。
5. 验证翻页、无结果和跨 120 天时间分段。

### PoC B：写入关联审批

1. 先用当前“差旅费报销申请”的真实 `processCode` 发起最小请求，确认它不是接口不支持的套件模板。
2. 复制正式“差旅费报销申请”做测试模板。
3. 使用一条境内出差申请的 `processInstanceId` 创建报销审批。
4. 再使用两条实例 ID 创建一次，验证多选展示。
5. 打开创建后的 OA，确认显示的是可点击的真正关联审批，而不是普通文本。
6. 分别测试已通过、审批中、已撤销、他人发起、其他企业/无效实例 ID，记录真实模板的接受规则。

### PoC C：权限和安全

1. 用应用范围内、范围外员工各测试一次列表查询和创建。
2. 篡改前端请求中的 `processInstanceId`，确认后端能够拒绝关联他人实例。
3. 确认创建接口不传 `approvers` 时，实际审批人和原 OA 后台流程完全一致。

## 七、已证实与待验证清单

| 项目 | 判断 | 依据/下一步 |
|---|---|---|
| 按模板查询审批实例 | 已证实 | 列表接口必填 `processCode` |
| 按当前员工筛选其发起实例 | 已证实 | `userIds` 为发起人列表；传当前员工一个 ID |
| 按流程状态筛选 | 已证实 | 支持 RUNNING、TERMINATED、COMPLETED |
| 直接筛选“审批通过” | 需组合实现 | 先查 COMPLETED，再按详情 `result == agree` 筛选 |
| 境内、境外两模板一起查询 | 需调用两次 | 单次列表请求只接收一个 `processCode` |
| 创建报销时建立真正关联 | 已证实 | `RelateField` 接受审批实例 ID 数组字符串 |
| 关联多条审批 | 参数格式已证实 | 官方示例包含两个实例 ID；真实模板展示规则待 PoC |
| H5 直接唤起原生关联弹窗 | 未发现公开接口 | 按自建选择页设计，除非钉钉提供另行确认的专享能力 |
| 保留原 OA 审批流程 | 已证实 | 创建时不传 `approvers`，复用后台流程 |
| 当前报销模板能否经 API 创建 | 必须先 PoC | 官方明确排除假勤、人事、财税、法务、商旅等套件 |
| 原始附件和 Excel 自动带入 | 待独立 PoC | 需先获得审批钉盘的文件元数据 |

## 官方资料

- [获取审批实例 ID 列表](https://open.dingtalk.com/document/development/obtain-an-approval-list-of-instance-ids)
- [获取单个审批实例详情](https://open.dingtalk.com/document/development/obtains-the-details-of-a-single-approval-instance-pop)
- [发起审批实例](https://open.dingtalk.com/document/development/create-an-approval-instance)
- [FormComponent / FormComponentValues 补充参数说明](https://open.dingtalk.com/document/development/oa-formcomponent-message)
- [审批附件操作流程](https://open.dingtalk.com/document/development/new-version-of-attachment-approval-process)
- [requestAuthInfo](https://open.dingtalk.com/tools/explorer/jsapi?id=10296)
- [uploadAttachmentToDingTalk](https://open.dingtalk.com/tools/explorer/jsapi?id=10318)
