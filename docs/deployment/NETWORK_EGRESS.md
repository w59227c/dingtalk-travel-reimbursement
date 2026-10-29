# 生产服务器出口放行清单

适用于本项目预构建镜像及 `docker compose pull && docker compose up -d --wait --wait-timeout 300` 部署方式。

## 应用运行时

以下均由后端容器主动发起 HTTPS 连接；允许 TCP 443 出站及其已建立连接的返回流量。

| 目标域名 | 用途 | 是否必需 |
| --- | --- | --- |
| `api.dingtalk.com` | 钉钉 token、审批模板、审批列表／详情、报销提交及附件接口 | 必需 |
| `oapi.dingtalk.com` | 钉钉免登、员工与部门信息等旧版接口 | 必需 |
| `api.github.com` | GitHub 官方 API，读取 holiday-cn 年度 JSON 正文 | 自动获取日历时必需 |
| `raw.githubusercontent.com` | 同一 holiday-cn 文件的备用下载通道 | 建议；主 API 正常时不访问 |
| `trans.dingtalk.com` 及其子域 `*.trans.dingtalk.com` | 钉钉返回的附件签名上传地址 | 有附件的 OA 提交时必需 |

DNS 还需允许服务器／容器向企业批准的 DNS 解析器访问 UDP 53 和 TCP 53。解析器 IP 由生产网络提供；无需因此允许访问任意公共 DNS。

日历只访问下列固定资源，不需要放行整个 GitHub 网站或任意下载 URL：

```text
GET https://api.github.com/repos/NateScarlet/holiday-cn/contents/{year}.json?ref=master
Accept: application/vnd.github.raw+json

GET https://raw.githubusercontent.com/NateScarlet/holiday-cn/master/{year}.json
```

两条通道是同一仓库、分支和年度文件，服务端直接接收并校验 JSON，不跟随重定向或使用第三方镜像。主 API 无需 GitHub 账号或 token。一次最多尝试 API、raw、API 三次，短退避及所有请求合计最多 20 秒；API 明确限流后本轮不再请求 API。404 视为尚未提供，不进行无意义重试；已有完整年度不会重复获取或覆盖。日历提示区提供同一份网络说明。

附件默认仅接受 HTTPS、443 端口且位于 `trans.dingtalk.com` 及其子域的地址。如果企业使用了其他合法钉钉上传域名，必须先核验钉钉实际返回的域名，再同时调整 `DINGTALK_STORAGE_UPLOAD_HOST_SUFFIXES` 和防火墙；不要为规避上传失败放开任意域名、IP URL 或关闭 TLS 校验。

OCR 模型、Excel 模板、前端及 PDF 渲染资源都在镜像内。正常运行不需要 PyPI、npm、模型下载站点、中国政府网或提莫接口；粘贴录入在本地解析，服务器不访问管理员粘贴的网址。

## 更新镜像时

由 Docker 守护进程访问下列 TCP 443 出口，须区别于容器运行时规则：

| 目标域名 | 用途 |
| --- | --- |
| `ghcr.io` | 获取公开镜像令牌、清单和镜像入口 |
| `pkg-containers.githubusercontent.com` | GHCR 镜像层的下载地址 |

本项目服务器只拉取上述两个 GHCR 镜像；不在服务器执行构建，因此不需要为该部署方式开放 Docker Hub、PyPI、npm 或 GitHub Actions runner 的全部域名。若企业不允许直接拉取，可由批准的镜像同步通道同步到内网仓库，再由服务器拉取。

GitHub 官方列出的容器下载域名见[网络连接要求](https://docs.github.com/en/actions/reference/runners/github-hosted-runners#communication-requirements-for-github-hosted-runners)；此处仅取本项目实际使用的 GHCR 通道，不套用整份 runner 白名单。

## 严格网络配置

- 使用 FQDN／SNI 域名策略，并允许跟随企业 DNS 的地址更新。GitHub CDN 和钉钉域名可能解析到多个动态 IP，不要把一次解析结果永久写死为完整白名单。
- 只支持 IP ACL 时，需要网络团队维护域名对应的动态 IP 集合，或提供经过批准的受控出口；一次临时解析得到的 IP 不具备长期稳定性。
- 日历客户端禁用环境代理，不能仅设置 `HTTPS_PROXY` 就认为已接入代理；此清单按允许后端容器直连固定 HTTPS 域名编写。TLS 检查设备须正确部署受信任企业 CA，不能通过关闭证书校验解决连接失败。
- 这些是出站要求。应用入站端口仍由 `APP_PORT`／现有反向代理决定，不需要新增入站端口。

## 只读验证

更新后可在部署目录执行以下命令。它只下载并验证当前年度文件，不修改数据库，不打印数据正文或凭据：

```bash
docker compose exec -T backend python - <<'PY'
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
from app.services.attendance_calendar_sync import fetch_holiday_cn
from app.services.holiday_calendar_import import parse_calendar_content

year = datetime.now(ZoneInfo("Asia/Shanghai")).year
content = asyncio.run(fetch_holiday_cn(year))
parsed = parse_calendar_content(content)
print({"year": parsed.year, "format": parsed.source_format, "valid": parsed.can_save})
assert parsed.year == year and parsed.source_format == "holiday_cn" and parsed.can_save
PY
```

2026-10-29 的 ali 后端容器实测中，DNS 解析正常，raw 通道连续三次连接失败／超时，GitHub API 连续三次 HTTP 200。这说明当时主要受 raw 节点链路影响；改用 API 优先并保留短重试能降低偶发失败，但不能代替生产防火墙放行。
