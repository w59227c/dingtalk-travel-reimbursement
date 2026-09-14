# 文档索引

## 当前业务与技术契约

- 仓库根目录 [`README.md`](../README.md)：当前产品范围、业务规则，以及开发、配置、运行和部署入口。
- [`DINGTALK_OA_INTEGRATION_DESIGN.md`](DINGTALK_OA_INTEGRATION_DESIGN.md)：草稿、材料、OCR、Excel、OA 提交、状态机、恢复和清理设计。

上述两份文档是当前实现的权威说明。代码、迁移和测试发生变化时应同步维护。

## 部署、构建与发布

- [`deployment/README.md`](deployment/README.md)：服务器部署、升级、回滚和运行检查；同目录保存部署用 `compose.yaml`。
- [`GITHUB_ACTIONS_SETUP.md`](GITHUB_ACTIONS_SETUP.md)：镜像自动构建、验证与 GHCR 发布操作。
- [`GITHUB_ACTIONS_DOCKER_DESIGN.md`](GITHUB_ACTIONS_DOCKER_DESIGN.md)：GitHub Actions 镜像发布、构建时下载 OCR 模型及 `.env` 边界设计。

## 项目概览与历史计划

- [`智能差旅费报销项目汇报.md`](智能差旅费报销项目汇报.md)：面向非开发人员的项目能力概览，不作为细节业务契约。
- [`V1_IMPLEMENTATION_PLAN.md`](V1_IMPLEMENTATION_PLAN.md)：早期范围与分阶段实施记录，只作为历史背景；当前行为以上述权威文档、迁移和测试为准。

## 调研与已完成实验

`research/` 只保留仍会影响维护、权限或依赖决策的依据，不属于当前运行链路：

- [`research/DINGTALK_APPROVAL_ATTACHMENT_STORAGE_LIFECYCLE.md`](research/DINGTALK_APPROVAL_ATTACHMENT_STORAGE_LIFECYCLE.md)：审批附件归属、引用和清理边界。
- [`research/DINGTALK_APPROVAL_MUTATION_AND_DRAFT_RESEARCH.md`](research/DINGTALK_APPROVAL_MUTATION_AND_DRAFT_RESEARCH.md)：审批提交后修改能力与本系统草稿方案。
- [`research/DINGTALK_PERMISSION_CODES.md`](research/DINGTALK_PERMISSION_CODES.md)：生产所需钉钉权限编码。
- [`research/DINGTALK_RELATED_APPROVAL_RESEARCH.md`](research/DINGTALK_RELATED_APPROVAL_RESEARCH.md)：查询并关联出差审批的接口依据。
- [`research/FREE_LOCAL_OCR_RESEARCH.md`](research/FREE_LOCAL_OCR_RESEARCH.md)：免费本地 OCR 方案。
- [`research/github-actions-docker-sources.md`](research/github-actions-docker-sources.md)：GitHub Actions、GHCR、Buildx 和运行配置的官方资料核对。
- [`research/OPENCV5_UPGRADE_ASSESSMENT.md`](research/OPENCV5_UPGRADE_ASSESSMENT.md)：保持 PaddleOCR 所需 OpenCV 版本约束的依据。
- [`research/PASSENGER_INVOICE_FIELDS_AND_MULTI_TRIP_RESEARCH.md`](research/PASSENGER_INVOICE_FIELDS_AND_MULTI_TRIP_RESEARCH.md)：旅客运输字段与多行程材料判断依据。
