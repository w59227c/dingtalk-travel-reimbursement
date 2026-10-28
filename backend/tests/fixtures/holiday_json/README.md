# 年度 JSON 格式回归样本

2026-10-28 从用户指定的两个公开来源保存 2023—2026 年共 8 份响应，仅用于离线回归测试。具体来源与响应大小见 `sources.json`。生产代码不读取这些样本、不访问接口、也不获取 JSON 中的 `$schema` 或 `papers`。

- 提莫：`https://timor.tech/api/holiday/year/{year}/`，必须保留年度后的斜杠。`code` 必须为整数 0；`holiday` 中日期键为 MM-DD，条目包含完整 date、name 和严格布尔 holiday，true 为休息、false 为补班。以所有日期共同确定适用年，混年或月度／单日结果拒绝。wage/rest/after/target 等元数据不参与出勤或金额计算。
- holiday-cn：`https://raw.githubusercontent.com/NateScarlet/holiday-cn/master/{year}.json`，使用整数 year 及 days 数组中的 date/name/isOffDay。year 是原政府通知的适用年份，允许含相邻年日期，沿用原通知的跨年合并规则。isOffDay 必须是布尔值，true 为休息、false 为补班。

测试逐条比较日期、名称和放假／补班标志，再对每个适用年的全部自然日与政府通知预期逐日对照默认出勤结果。部分提莫年度数据额外列出连休周末，这些额外例外不改变工作周默认结果；holiday-cn 的 2023 文件包含 2022-12-31，不能误认成 2022 年年度数据。

另覆盖空结果、接口报错、格式不完整、重复字段、重复日期、无效日期、字符串／数字冒充布尔值、年份不符、只有网址、单日／月度缺项、只读解析、确认保存、摘要篡改及丢失响应后的幂等重试。七类节日覆盖校验用于拦截明显不完整的年度数据，不能证明第三方数据每一天都正确，因此页面仍要求核对预览。

公开数据来源及格式说明：[提莫](https://timor.tech/api/holiday)、[holiday-cn](https://github.com/NateScarlet/holiday-cn)。holiday-cn 样本沿用项目 MIT 许可，原许可见 `holiday-cn-LICENSE`。
