# 文档索引

## 当前产品文档

- [`V1_IMPLEMENTATION_PLAN.md`](V1_IMPLEMENTATION_PLAN.md)：V1 范围、业务规则、接口、OCR、Excel、安全与验收契约。
- 仓库根目录 [`README.md`](../README.md)：开发、配置、运行、部署与日常维护入口。
- [`GITHUB_ACTIONS_SETUP.md`](GITHUB_ACTIONS_SETUP.md)：镜像自动发布、生产 Secrets/Variables 首次登记与自动部署操作。

## 部署设计

- [`GITHUB_ACTIONS_DOCKER_DESIGN.md`](GITHUB_ACTIONS_DOCKER_DESIGN.md)：GitHub Actions 镜像发布、构建时下载 OCR 模型及生产配置自动注入方案。

## 调研与已完成实验

`research/` 只保留仍会影响维护、权限或依赖决策的依据，不属于当前运行链路：

- [`research/DINGTALK_APPROVAL_ATTACHMENT_STORAGE_LIFECYCLE.md`](research/DINGTALK_APPROVAL_ATTACHMENT_STORAGE_LIFECYCLE.md)：审批附件归属、引用和清理边界。
- [`research/DINGTALK_APPROVAL_MUTATION_AND_DRAFT_RESEARCH.md`](research/DINGTALK_APPROVAL_MUTATION_AND_DRAFT_RESEARCH.md)：审批提交后修改能力与本系统草稿方案。
- [`research/DINGTALK_PERMISSION_CODES.md`](research/DINGTALK_PERMISSION_CODES.md)：生产所需钉钉权限编码。
- [`research/DINGTALK_RELATED_APPROVAL_RESEARCH.md`](research/DINGTALK_RELATED_APPROVAL_RESEARCH.md)：查询并关联出差审批的接口依据。
- [`research/FREE_LOCAL_OCR_RESEARCH.md`](research/FREE_LOCAL_OCR_RESEARCH.md)：免费本地 OCR 方案。
- [`research/OPENCV5_UPGRADE_ASSESSMENT.md`](research/OPENCV5_UPGRADE_ASSESSMENT.md)：保持 PaddleOCR 所需 OpenCV 版本约束的依据。
- [`research/PASSENGER_INVOICE_FIELDS_AND_MULTI_TRIP_RESEARCH.md`](research/PASSENGER_INVOICE_FIELDS_AND_MULTI_TRIP_RESEARCH.md)：旅客运输字段与多行程材料判断依据。
