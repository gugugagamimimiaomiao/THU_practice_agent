# 实践小搭部署与清小搭接入

## 1. 生成密钥

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

为 `XIAODA_API_KEY` 和 `ADMIN_API_KEY` 分别生成两个不同的值。不要把真实密钥写进 Git、镜像或截图。

## 2. 本地启动与协议验收

```bash
cp .env.example .env
# 编辑 .env，至少设置 XIAODA_API_KEY=... 和 ADMIN_API_KEY=...
docker compose up --build
```

健康检查：`http://127.0.0.1:8000/health`。协议地址是 `http://127.0.0.1:8000/v1`。本地地址不能填入清小搭——平台明确要求公网可访问，不接受 localhost 或内网地址；但**不要求 HTTPS**，公网 IP 加端口的 HTTP 地址即可（已实测通过）。

## 3. 最省事的公网方案：香港轻量服务器 + systemd（实际在用的方案）

**实测结论：清小搭接受 `http://公网IP:端口/v1`，不要求 HTTPS，也不需要域名。**
四项自动检测（连通性 / 凭证校验 / 最小对话 / OpenAI 格式）都能通过。所以最短路径是：

1. 买一台**海外或香港地域**的入门 Linux 服务器（免备案；大陆地域要用域名跑 HTTPS 得先备案，时间上通常来不及）。1 核 1G 起步即可——本项目零第三方依赖、不跑模型。
2. 把仓库内容放到 `/opt/practice-xiaoda`。
3. 执行安装脚本，它会生成密钥、写好 systemd 单元、设置开机自启与崩溃重启：

```bash
bash deploy/systemd-install.sh
```

4. 在云厂商控制台的**防火墙 / 安全组**放行 TCP 8000（最容易漏的一步）。
5. 取密钥：`sudo grep XIAODA_API_KEY /etc/practice-xiaoda.env`
6. 清小搭里填 `http://<公网IP>:8000/v1` + 该密钥，鉴权选 Bearer Token。

> 脚本把 `RATE_LIMIT_PER_MINUTE` 设为 600 而非默认的 60。限流按 Bearer token 的指纹分桶，
> 而清小搭对所有终端用户使用同一个 token——默认值会被全体用户共享，评审期间十几个人
> 同时对话就会集体收到 429。

### 安装脚本顺带装好的三件事

不用另外配置，`systemd-install.sh` 执行完就已经在跑：

**健康自检（每分钟）** — 进程活着不等于能服务，所以它真的打一次 `/v1/chat/completions`，请求带 `X-Health-Probe: 1` 头，服务端据此把自检排除在活动统计之外。连续失败 3 次才告警（避免单次网络抖动误报）；在 `/etc/practice-xiaoda.env` 里填 `HEALTH_ALERT_SCKEY`（Server 酱的 SendKey）就推送到微信，不填只写日志。

**数据库备份（每日 04:30）** — 用 SQLite 的 backup API 而不是 `cp`。服务常驻、随时可能在写，直接复制有可能拷到写了一半的状态，等真要恢复时才发现备份本身是坏的。备份后 gzip 并跑一次 `integrity_check`，保留 14 天。爬虫接上之后库里是人工核验过的真实项目，重建成本远高于这点磁盘。

**日志轮转（每周，保留 6 份）** — 自检每分钟一条，一天一千四百多行。磁盘写满时 SQLite 的报错信息很难定位，与其事后排查不如提前压住。

查看当前状态：

```bash
cd /opt/practice-xiaoda && python3 scripts/ops_report.py
```

一次性列出服务与定时器状态、磁盘占用、写作模型可用性、自检成败与告警、最近一次备份的时间和体积、项目库健康度（演示数据占比、缺字段分布、待核验积压）、对话量与兜底率、没接住的问题原话、请求来源分布。异常项会自己标出来。

需要 HTTPS、自定义域名或容器化时，用下面的方案。

## 4. 备选：域名 + Docker + Caddy 自动 HTTPS

这是本项目的正式部署首选：SQLite 需要稳定磁盘、服务需常驻且主要服务国内学生。选择香港地域可以避免中国大陆网站备案流程，同时通常比欧美区域更适合国内访问。选一台可运行 Docker 的入门 Linux 实例即可；不要为了此 MVP 购买高规格 GPU 或数据库套餐。

在 DNS 中为域名添加 A 记录指向服务器公网 IP，开放 TCP 80/443（以及 SSH 22）。然后在服务器项目目录中：

```bash
cp .env.example .env
# 编辑 .env：设定 DOMAIN、XIAODA_API_KEY、ADMIN_API_KEY
docker compose -f compose.yaml -f compose.public.yaml up -d --build
```

`Caddyfile` 会自动申请和续期 HTTPS 证书。证书签发后，清小搭地址就是 `https://DOMAIN/v1`。数据库继续保存在 Docker volume `/data` 中；上线前请把该卷备份到可信位置。

适合快速演示而不想运维时，选 Railway：直接连接仓库、使用已提供的 `railway.json`、添加 `/data` Volume 和环境变量即可。Render 也可部署，但同样必须购买/启用持久磁盘；不建议用会休眠的免费实例承接清小搭探测。具体价格会随地区和促销变化，请在下单页核对当前月付价格，而不要以旧教程中的数字为准。

官方入口：[腾讯云 Lighthouse](https://www.tencentcloud.com/products/lighthouse)、[Railway Pricing](https://railway.com/pricing)、[Render Pricing](https://render.com/pricing)。

## 5. Docker 运行

```bash
docker build -t practice-xiaoda .
docker run --rm -p 8000:8000 \\
  -e XIAODA_API_KEY='replace-me' -e ADMIN_API_KEY='replace-admin-me' \\
  -v practice-xiaoda-data:/data practice-xiaoda
```

SQLite 文件位于 `/data/practice_xiaoda.db`，公网部署必须绑定持久卷，否则重启会丢失导入和审核数据。

## 6. Railway / Render

仓库已经包含 `railway.json`、`render.yaml` 和 `Dockerfile`。创建服务后设置：

- `PRACTICE_XIAODA_ENV=production`
- `XIAODA_API_KEY=<随机长密钥>`
- `ADMIN_API_KEY=<另一随机长密钥>`
- `PRACTICE_XIAODA_DB=/data/practice_xiaoda.db`
- `PUBLIC_DASHBOARD=false`

Render 必须启用持久磁盘并挂载到 `/data`；Railway 也要添加 Volume 挂载 `/data`。平台自带 HTTPS 域名可直接用于清小搭；不要把管理面板公开。

## 7. 清小搭向导填写

1. 选择“标准协议接入”。
2. API 地址填到版本段：`http://公网IP:8000/v1` 或 `https://你的公网域名/v1`（不要再填 `/chat/completions`）。
3. API 密钥填 `XIAODA_API_KEY`，鉴权选择 `Bearer Token`。
4. 流式终止符填 `[DONE]`，usage 位置选“stop 帧内”。
5. 能力声明只勾选流式；当前版本不勾选视觉和工具。
6. 通过探测后再填写名称、描述、开场白并提交审核。

探测对应：`GET /v1/models`、`POST /v1/chat/completions`；错误密钥返回 401，`stream` 必须是 JSON 布尔值，`model` 缺失、空或 null 均可，SSE 顺序为 role → content* → stop(含 usage) → `[DONE]`。

## 8. 运维与安全

- 只通过 HTTPS 暴露服务；定期轮换 `XIAODA_API_KEY`。
- 生产模式下管理 API 还需要 `ADMIN_API_KEY`，且管理面板默认关闭。
- 真实微信公众号 Cookie、Token 只放平台 Secret，不写 `.env`、数据库或日志。
- 当前限流器是单进程内存实现；多副本部署前应换成网关或 Redis 限流。
- 备份 `/data/practice_xiaoda.db`，并制定删除过期文章与用户反馈的策略。
- 公众号链接导入可直接读取公开 `mp.weixin.qq.com` 正文；为提高成功率可设置 `WECHAT_COLLECTOR_PATH` 和 `WECHAT_COLLECTOR_PYTHON`。本地产品可在“开发者采集”页手动填写微信 Cookie/Token；公网部署仍应限制开发者页面访问，且绝不能把凭证写进镜像、仓库或 SQLite。
- 每日自动更新使用 `make daily-wechat-update`，应由单独的 cron/云定时任务运行；默认目标是“清华大学社会实践”“无限之声”“清华大学学生公益”。采集器凭证只能配置在该任务的 Secret 环境中。
