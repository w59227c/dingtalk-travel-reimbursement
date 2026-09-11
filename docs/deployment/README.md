# 服务器部署

适用于 Linux x86_64/amd64、支持 AVX 且已安装 Docker Compose v2 的服务器。镜像公开可拉取，服务器无需登录 GHCR；OCR 模型、Excel 模板和 Web 配置已包含在镜像中。

## 首次部署

把本目录的 `compose.yaml` 和项目根目录的 `.env` 上传到服务器同一目录，例如：

```text
/opt/dingtalk-travel-reimbursement/
├── compose.yaml
└── .env
```

`.env` 不需要添加 `IMAGE_TAG`，缺省会使用 `main`；也可以显式设置：

```dotenv
IMAGE_TAG=main
```

然后执行：

```bash
cd /opt/dingtalk-travel-reimbursement
chmod 600 .env
docker compose config --quiet
docker compose pull
docker compose up -d --wait --wait-timeout 300
docker compose ps
```

访问端口由 `.env` 中的 `APP_BIND_ADDRESS` 和 `APP_PORT` 决定。当前项目 `.env` 使用 `0.0.0.0:5173`，对应 `http://服务器IP:5173`。检查服务：

```bash
curl -fsS http://127.0.0.1:5173/api/ready
docker compose logs --tail=100 backend web
```

## 更新

确认 GitHub Actions 构建成功后，在服务器执行：

```bash
cd /opt/dingtalk-travel-reimbursement
docker compose pull
docker compose up -d --wait --wait-timeout 300
```

镜像名和 `.env` 不需要随版本修改，`main` 会指向最新一次通过验证的主线镜像。

## 回滚

把 `.env` 中的标签改为需要回滚的完整 commit，例如：

```dotenv
IMAGE_TAG=sha-38bc78ab5419d29611040b72728d6e483ac27740
```

然后重新执行 `docker compose pull` 和 `docker compose up -d --wait --wait-timeout 300`。恢复自动跟随主线时改回 `IMAGE_TAG=main`。

## 数据

`sqlite_data` 保存数据库，`reimbursement_staging` 保存报销附件和生成文件。升级或重建容器不会删除它们；不要执行 `docker compose down -v`。这两个 volume 应在同一时间点一起备份。

首次验收前可保持 `DINGTALK_OA_WORKER_ENABLED=false`；需要正式处理 OA 提交时再改为 `true` 并重启服务。
