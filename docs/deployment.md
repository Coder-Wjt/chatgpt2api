# 部署与升级

状态：当前

本项目的发布镜像默认是 `ghcr.io/yukkcat/chatgpt2api:latest`。标准 Compose 将服务绑定到 `127.0.0.1:3000`，使用 `chatgpt2api-runtime` 命名卷保存可更新的应用运行目录，并单独挂载本地 `data/` 和 `config.json`。运行时配置和数据不应提交到 Git。

## Docker 部署

```bash
git clone https://github.com/yukkcat/chatgpt2api.git
cd chatgpt2api
cp .env.example .env
# 将 .env 中的 CHATGPT2API_AUTH_KEY=your_secret_key_here 替换为私有密钥。
test -f config.json || printf '{}\n' > config.json
docker compose up -d
```

镜像中的 `/opt/chatgpt2api` 是只读应用种子，`/app` 是受管运行目录。首次启动或镜像版本变化时，入口脚本会用镜像种子刷新 `/app`，再按锁文件同步 Python 依赖；同一镜像正常重启时会保留控制台在线更新后的运行版本。业务数据始终留在独立的 `data/` 挂载中。

### Nginx HTTPS 上线

宿主机 Nginx 反代 `http://127.0.0.1:3000`，Compose 默认不再向公网网卡开放应用端口。
在现有 `.env` 中设置 `CHATGPT2API_BASE_URL=https://你的域名`，保留私有管理员密钥。
使用 [`../deploy/nginx.conf.example`](../deploy/nginx.conf.example) 对照现有站点配置，替换域名和证书路径后执行 `nginx -t`，验证通过再 reload。
示例配置位于 Nginx 的 `http` 上下文，提供 HTTPS、150 MiB 请求上限、每 IP 速率与连接数限制，并关闭响应缓冲以支持 SSE。
如果 Nginx 自身在容器内，应让它和应用共用私有 Docker 网络，并将 `proxy_pass` 改为应用服务名；容器里的 `127.0.0.1` 不是宿主机。不要为了反代而直接开放源站公网端口。

应用在 JSON/multipart 解析前校验实际请求字节数，包括无 Content-Length 和伪造长度的请求。
单请求最多 150 MiB，上传总时间最多 60 秒；每进程最多同时处理 8 个带请求体的写请求，SSE 在响应结束时释放容量；GET/HEAD 查询不占用该容量。
超出大小返回 413，容量不足返回 429，上传超时返回 408。图片编辑仍限制单图 50 MiB、合计 100 MiB、最多 16 张；聊天参考图每张 10 MiB，整段对话最多 10 张。
PPT/PSD 默认 2 个执行槽、4 个排队槽，每个 User Key 最多 2 个未完成任务，容量不足返回 429；重复 client_task_id 返回原任务。
上述容量均按进程计算，标准部署保持单 Uvicorn worker；不要通过多 worker 绕过限制。

从当前修复后的源码构建上线，使用构建 overlay，避免直接拉取尚未包含修复的旧 `latest`：

```bash
# 先按本文备份数据库、config.json 和文件；在实际部署目录执行。
docker compose -f docker-compose.yml -f docker-compose.build.yml build --pull
# 验证 Nginx 配置后再更新应用；保留原有业务数据挂载。
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --force-recreate --renew-anon-volumes
```

本地 PostgreSQL 部署须在上述两条命令中同时保留 `-f docker-compose.postgres.yml`。
构建 overlay 使用匿名 `/app` 运行卷，`--renew-anon-volumes` 保证每次从新镜像初始化代码，避免同版本旧运行卷遮蔽安全修复。业务数据仍在独立挂载中。
不要删除数据库卷；保留旧镜像及备份作为回滚来源。上线后验证 `/version`、未授权 `/v1/models` 返回 401、登录和 SSE 正常，并确认公网无法直连源站端口。

### 本地 PostgreSQL 18

需要由 Compose 一并运行 PostgreSQL 时，在 `.env` 中设置数据库密码：

```dotenv
POSTGRES_PASSWORD=replace_with_a_strong_password
```

然后同时加载基础 Compose 和 PostgreSQL overlay：

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d
```

`docker-compose.postgres.yml` 使用官方 `postgres:18-alpine` 镜像，等待数据库健康后再启动应用，并将数据持久化到 `chatgpt2api-postgres-data` 命名卷。数据库端口默认不暴露到宿主机。

`POSTGRES_PASSWORD` 会同时用于初始化数据库和构造 `DATABASE_URL`，因此请仅使用 URL 安全字符（字母、数字、下划线或连字符），不要在两个位置分别编码密码。
启用该模式后，后续启动、升级、查看状态和停止服务都应同时指定这两个 Compose 文件。

查看状态与日志：

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml ps
docker compose -f docker-compose.yml -f docker-compose.postgres.yml logs -f postgres app
```

`.env` 中的 `CHATGPT2API_AUTH_KEY` 优先于 `config.json` 的 `auth-key`。若要使用 `config.json`，先删除或注释 `.env` 中的该值，再填写 `auth-key`。

默认地址：

- 控制台：`http://localhost:3000`
- API：`http://localhost:3000/v1`

也可以使用仓库安装脚本：

```bash
curl -fsSL https://raw.githubusercontent.com/yukkcat/chatgpt2api/main/deploy/install.sh | sudo bash
```

安装脚本会明确询问 Application Database：

- SQLite 本地文件（默认）
- PostgreSQL 18 本地容器（Docker 模式）
- 已有 PostgreSQL URL

选择本地 PostgreSQL 时，脚本会自动生成并保存数据库密码，下载 `docker-compose.postgres.yml`，再与主 Compose 一起启动。重复运行安装脚本会复用已有密码。`CHATGPT2API_THREAD_TOKENS` 默认是 `120`，表示后端同步工作线程的并发容量，只要求正整数且不设置人为最高值；账号、代理和上游服务仍分别执行自己的并发限制。

## 本地开发

后端：

```bash
git clone https://github.com/yukkcat/chatgpt2api.git
cd chatgpt2api
uv sync
uv run main.py
```

Vue 控制台：

```bash
cd web-vue
npm install
npm run dev
```

前端开发服务器默认使用 Vite 端口；后端仍读取项目根目录的 `config.json` 和 `data/`。

## 存储边界

`DATABASE_URL` 选择 Application Database；未设置时使用
`data/chatgpt2api.db`。支持 SQLite 与 PostgreSQL 18，不再通过
`STORAGE_BACKEND` 选择 JSON、Git 或账号专用数据库。

选择数据库不是旧数据迁移操作，不会自动导入 JSON、JSONL、Git 或旧账号
SQLite 文件。图片文件及其相关索引仍按图片存储边界管理。完整边界见
[`storage-architecture.md`](storage-architecture.md)。

## 升级

升级前先在系统设置中执行一次 R2 备份，并确认状态为成功。备份归档始终包含
Application Database：SQLite 使用 `data/application-database.sqlite3`，PostgreSQL
使用 `data/application-database.pgdump`。图片任务记录、PPT / PSD 文件和图片目录
按备份设置选择；外部 WebDAV 仍需独立备份。

未配置 R2 时，应先停止服务再备份。SQLite 可以在停服后复制 `data/chatgpt2api.db`；
PostgreSQL 必须使用 `pg_dump --format=custom`，不能用 `tar data/` 代替数据库备份。
`config.json` 只保留 `auth-key` 等启动配置，也应单独保存：

```bash
pg_dump --format=custom --no-owner --no-privileges "$DATABASE_URL" \
  > backups/chatgpt2api-$(date +%Y%m%d-%H%M%S).pgdump
```

本地 PostgreSQL Compose 可直接在数据库容器内导出：

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml exec -T postgres \
  sh -c 'pg_dump --format=custom --no-owner --no-privileges \
  -U "$POSTGRES_USER" "$POSTGRES_DB"' \
  > backups/chatgpt2api-$(date +%Y%m%d-%H%M%S).pgdump
```

### 控制台在线更新

使用当前标准 Compose 部署时，版本弹窗可直接启动在线更新。后端负责下载发布包、校验 SHA-256 与更新清单、替换运行文件、同步依赖和安排容器重启；前端只展示后端任务，刷新页面或重启完成后仍可恢复任务结果。文件或依赖同步失败时会恢复旧文件，并按旧锁文件重新同步依赖。

源码运行和没有挂载受管 `/app` 运行目录的旧容器不会显示“立即更新”，只会给出 Git 或镜像升级提示。首次切换到受管运行目录时，应先更新仓库中的 Compose 文件，再执行一次镜像升级：

```bash
git pull --ff-only
docker compose pull
docker compose up -d
```

不要把 `/app` 运行时卷当作业务备份；它可以从发布镜像重新创建。不要使用 `docker compose down -v` 执行普通升级，因为该命令还会删除 Compose 管理的命名卷。

### 命令行升级

镜像部署升级：

```bash
docker compose pull
docker compose up -d
```

本地 PostgreSQL Compose 部署升级：

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml pull
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d
```

镜像部署固定或回退版本时，在 `.env` 设置 `CHATGPT2API_IMAGE=ghcr.io/yukkcat/chatgpt2api:<tag>`，再执行对应的 `pull` 与 `up`。镜像版本变化后，入口脚本会用该镜像刷新受管运行目录；Git 检出标签只影响源码运行，不会改变 Compose 使用的镜像版本。升级后检查：

```bash
docker compose ps
docker logs -f chatgpt2api
```

## 回滚与维护

先停止对应 Compose，再恢复经过验证的代码 / 镜像和备份数据；不要在运行时直接覆盖 `data/`。常用命令：

```bash
docker compose restart
docker compose down
```

`docker image prune` 只清理未使用镜像，不会替代数据备份。

### 聊天上游超时定位

查看 Compose 服务的 `chat_upstream_request` 日志：

```bash
docker compose logs --since 10m app | rg chat_upstream_request
```

同一个 `transport_id` 串联 `bootstrap`、`chat_requirements_prepare`、`chat_requirements_finalize` 和 `conversation` 阶段。`proxy_source` 表示出口来源，`proxy_host` 表示选中的代理主机，`http_primary_ip` 是 curl 报告的实际连接地址。`outcome=failed` 记录失败阶段、异常类型和耗时；连接计时在 curl 未提供时为零，不能把零解释为该阶段已成功完成。流式 `conversation` 的 `response` 仅表示收到响应头，不表示整个回答完成。

账号自定义代理优先于默认出口；账号设为继承才会采用默认配置。代理运行时扩展功能关闭不代表默认代理关闭。普通网站或 ChatGPT 首页可达，也不等于后续 Sentinel 和对话请求成功。日志不记录请求头、代理密码、令牌或对话正文；不要为排查开启原始 HTTP 凭据转储。

Web 对话的 `reasoning_effort` / `thinking_effort` 会适配上游枚举：`low`、`medium` 使用 `standard`，`high`、`xhigh`、`extended` 使用 `extended`；默认和 `none` 不发送该字段。这是上游两档能力的兼容映射，不表示上游提供独立的低、中、高三档。流式 HTTP 错误正文最多读取 8 KiB、等待 2 秒，避免错误信息为空或排错读取无限阻塞。

首页预热 GET 首次最多等待 10 秒，超时后最多使用剩余预算重试一次，总预算仍由调用方控制（通常 30 秒）。该策略不重发生成 POST。预热和 Sentinel 阶段失败归类为上游连接超时，不进入图片流恢复查询；已经进入生成阶段的恢复流程保持不变。
