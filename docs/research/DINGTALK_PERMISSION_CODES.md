# 钉钉差旅报销权限编码核对

权限核对记录：2026-11-16；调用范围按当前本地实现整理，不代表追加了真实 OA 验收。

## 可直接搜索的权限标识

| 权限标识 | 本项目中的调用范围 | 核对依据 |
|---|---|---|
| `Workflow.Form.Read` | 读取报销模板 Schema、控件和选项 | 调用表单 Schema 接口时，钉钉真实 403 响应明确返回该缺失权限 |
| `Workflow.Instance.Read` | 查询报销人审批、核实差旅代报抄送记录、回读新建 OA；普通费用沿用同一权限查询本人审批 | 出差关联、代报抄送和 OA 回读联调；普通费用调用复用现有接口 |
| `Workflow.Instance.Write` | 创建差旅或普通费用报销 OA | 创建 OA 联调；普通费用调用复用现有接口，独立模板仍需验收 |
| `Storage.UploadInfo.Read` | 获取审批附件空间的服务器上传信息 | 服务器直传附件联调 |
| `Storage.File.Write` | 提交上传文件，取得可写入审批附件控件的文件 ID | 服务器直传附件联调 |
| `qyapi_base` | 调用 `/topapi/v2/user/getuserinfo`，通过免登码取得 `userId` | [钉钉官方文档](https://open.dingtalk.com/document/development/obtain-the-userid-of-a-user-by-using-the-log-free)的权限详情显示该标识 |
| `qyapi_get_member` | 调用 `/topapi/v2/user/get` 读取员工姓名、部门列表和 `unionId` | [钉钉官方文档](https://open.dingtalk.com/document/development/query-user-details)的“成员信息读权限”详情显示该标识 |
| `qyapi_get_department_list` | 读取部门详情和直接子部门，支持管理员浏览组织架构 | [钉钉部门详情文档](https://open.dingtalk.com/document/development/query-department-details0-v2)及[部门列表文档](https://open.dingtalk.com/document/orgapp/obtain-the-department-list-v2)。正式公司应用已只读取得根部门的子部门 |
| `qyapi_addresslist_search` | 调用 `/v1.0/contact/users/search` 按姓名选择代报对象或配置授权。使用准确员工 `userId` 时只需已有成员详情权限 | 正式公司应用以合成姓名只读查询时，钉钉 403 回包明确返回该缺失权限 |

Workflow 和 Storage 使用点号权限标识。上表中的免登和通讯录接口使用 `qyapi_` 前缀，按开发者后台显示的权限标识搜索和开通。

## 不属于权限编码的配置

H5 企业内免登、微应用首页地址、可信域名、应用可见范围和审批模板发起权限，都需要配置，但它们不是 `xxx.yyy.zzz` 格式的接口权限，因此不放进权限编码清单。

如后续迁移现有 `/topapi/v2/*` 调用，按目标接口重新核对权限标识。
