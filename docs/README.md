# 文档索引

## 当前业务与技术契约

- 仓库根目录 [`README.md`](../README.md)：项目概览、本地启动、部署和检查入口。
- [`PRODUCT_CONTRACT.md`](PRODUCT_CONTRACT.md)：当前业务范围、补助规则、Excel 契约、自动保存与材料限制。
- [`DINGTALK_OA_INTEGRATION_DESIGN.md`](DINGTALK_OA_INTEGRATION_DESIGN.md)：草稿、材料、OCR、Excel、OA 提交、状态机、恢复和清理设计。

产品契约和 OA 集成设计是当前实现的权威说明。业务、迁移和状态机变化时同步维护对应章节，README 保持精简。

## 部署、构建与发布

- [`DEVELOPMENT.md`](DEVELOPMENT.md)：依赖、本地启动、模型准备和真实钉钉联调。
- [`OPERATIONS.md`](OPERATIONS.md)：就绪检查、隐私、备份、模型挂载与源码部署。
- [`deployment/README.md`](deployment/README.md)：服务器部署、升级、回滚和运行检查；同目录保存部署用 `compose.yaml`。
- [`GITHUB_ACTIONS_SETUP.md`](GITHUB_ACTIONS_SETUP.md)：镜像自动构建、验证与 GHCR 发布操作。

## 项目概览

- [`智能差旅费报销项目汇报.md`](智能差旅费报销项目汇报.md)：面向非开发人员的项目能力概览，不作为细节业务契约。

## 调研依据

`research/` 保留附件生命周期、权限、审批关联和票据判断的专题依据；依赖与构建依据直接放在对应开发、集成和发布文档中。

- [`research/DINGTALK_APPROVAL_ATTACHMENT_STORAGE_LIFECYCLE.md`](research/DINGTALK_APPROVAL_ATTACHMENT_STORAGE_LIFECYCLE.md)：审批附件归属、引用和清理边界。
- [`research/DINGTALK_PERMISSION_CODES.md`](research/DINGTALK_PERMISSION_CODES.md)：生产所需钉钉权限编码。
- [`research/DINGTALK_RELATED_APPROVAL_RESEARCH.md`](research/DINGTALK_RELATED_APPROVAL_RESEARCH.md)：查询并关联出差审批的接口依据。
- [`research/PASSENGER_INVOICE_FIELDS_AND_MULTI_TRIP_RESEARCH.md`](research/PASSENGER_INVOICE_FIELDS_AND_MULTI_TRIP_RESEARCH.md)：旅客运输字段与多行程材料判断依据。
