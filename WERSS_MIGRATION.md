# 微信公众号采集迁移：WeWe RSS 主链路

## 结论

> 2026-08-12 最终实测更新：`we-mp-rss` 能授权和搜索，但历史列表接口
> 持续返回 `ret=200013 / freq control`，不再作为默认主链路。默认改为
> WeWe RSS（微信读书登录）发现公开文章，`scripts/wewe_collector.py` 按标题
> 预筛后按需读取公开正文。`we-mp-rss` 仅保留在
> `legacy-wechat-backend` Compose profile 中用于诊断。

WeWe RSS 最小实测结果：公开文章识别公众号成功；一次最新同步得到 101 篇
真实元数据；抽查 5/5 篇全文成功；适配器 3/3 篇正文成功；首批实际回补
20/20 篇正文和首图 OCR 成功，最终项目库达到 51 篇来源文章、32 条项目。

隐私边界：WeWe RSS 会把微信读书登录令牌发送给配置的第三方中转服务
`weread.111965.xyz`。必须取得账号持有者明确同意；令牌只保存在 WeWe 自己的
数据卷中，不得输出到日志、项目数据库或 Git。上游仓库已归档，因此保留
公开文章链接直接导入作为降级路径。

原采集链路依赖微信公众平台后台的文章搜索/列表接口。该核心接口在 2026-07-30 被关闭，继续刷新 Token 或 Cookie 无法恢复批量历史文章同步。

本项目改用 [rachelos/we-mp-rss](https://github.com/rachelos/we-mp-rss) 作为独立上游。它负责扫码授权、公众号订阅、抓取和定时更新；`scripts/werss_collector.py` 以只读方式读取其 SQLite 数据库，再交给现有的分类、OCR、去重和项目卡抽取流程。

选择依据（核对日期：2026-08-12）：

| 项目 | 维护状态 | 批量/增量 | 结论 |
|---|---|---|---|
| `rachelos/we-mp-rss` | 当日仍有提交，源码版本 1.5.2 | 订阅、定时任务、RSS/API、SQLite/MySQL | 采用 |
| `wechat-article/wechat-article-exporter` | 2026-07-30 宣布停止维护 | 原可批量；核心上游接口已关闭 | 淘汰 |
| `qiye45/wechatDownload` | 2026-06 仍更新 | 适合已有链接/合集下载 | 仅作为正文回补备选 |

## 首次启动与授权

需要 Docker 和 Docker Compose。凭据保存在 `werss_data` 卷，不写入实践小搭数据库或 Git 仓库。

先在 `.env` 中把 `WERSS_ADMIN_PASSWORD` 换成长随机密码；Compose 会拒绝在该值缺失时启动，避免沿用上游示例的默认管理员密码。

```bash
docker compose build --pull we-mp-rss
docker compose up -d we-mp-rss
```

本仓库使用 `Dockerfile.werss` 给上游镜像打一个最小兼容补丁：当前上游
`apis/auth.py` 会在按 `WERSS_AUTH_WEB` 选择驱动后，再次无条件导入 Playwright
驱动，导致默认 API 二维码模式被覆盖。补丁只删除这两个重复导入，并显式设置
`WERSS_AUTH_WEB=False`；不修改上游采集、数据库或订阅逻辑。

Compose 同时设置 `GATHER.MODEL=free_publish`。上游 1.5.2 的旧 `web` 采集器
文件仍残留 Git 合并冲突标记，会在首次添加公众号时报 Python 语法错误；
`free_publish` 是该版本文档标记的新版多端点模式，不依赖这个损坏文件。

2026-08-12 实机授权后进一步验证：公众号搜索和订阅成功；但
`free_publish`、`publish` 返回 HTTP 404，`appmsgpublish`、`appmsg` 返回
微信 `ret=200013 / freq control`。Playwright 兜底能够加载已保存 Cookie，
但后台页面未在 30 秒内完成加载。因此“服务能授权”不等于“历史文章接口
当前没有频控”。遇到 `200013` 时当天停止重试，只保留每天一次的低频回补，
否则可能延长限制。

如果页面一直显示“正在获取二维码”，先检查服务，而不是继续等待：

```bash
docker compose ps we-mp-rss
docker compose logs --tail=100 we-mp-rss
curl --max-time 5 http://127.0.0.1:8001/docs >/dev/null
```

若 8001 端口无法连接，浏览器中的弹窗只是旧页面残留，刷新或扫码都不会成功。
重新执行上面的 `build --pull` 和 `up -d`，确认日志没有退出后再打开授权页。

打开 `http://127.0.0.1:8001`，使用 we-mp-rss 管理员页面完成一次扫码授权，然后订阅 `wechat_sources.py` 中需要监测的公众号。确认 we-mp-rss 页面中已经出现文章标题和正文后，再启动实践小搭：

```bash
docker compose up -d practice-xiaoda
```

## 回补到 50 条

“50 条”分为两个指标：上游文章至少 50 篇；经过招募过滤和去重后，机会库项目数量可能少于 50。不要用活动回顾、行前预告或重复文章凑项目数。

先让 we-mp-rss 完成订阅的历史同步，然后运行：

```bash
docker compose exec practice-xiaoda python3 scripts/backfill_wechat.py \
  --since 2026-01-01 --count 50 --batch-size 6 --pause 10
```

检查结果：

```bash
docker compose exec practice-xiaoda python3 scripts/ops_report.py
```

所有新项目默认是 `needs_review`。截止日期、资格、地点、实践时间和经费须人工核验后才能标记为 `published`。

## 每日自动更新

we-mp-rss 容器设置了 `ENABLE_JOB=True`，负责定期更新订阅。Compose 中的 `wechat-daily-import` worker 默认每天 08:25 读取最近两天的数据并导入实践小搭，重叠两天用于抵抗上游延迟，重复文章由原有规则合并。

```bash
docker compose up -d we-mp-rss practice-xiaoda wechat-daily-import
docker compose logs -f wechat-daily-import
```

可用 `.env` 中的 `WECHAT_DAILY_TIME` 修改运行时间。若部署平台不允许常驻 worker，再改用 Scheduled Job 或宿主机 cron：

```cron
25 8 * * * cd /opt/THU_practice_agent && docker compose exec -T practice-xiaoda python3 scripts/daily_wechat_update.py --since "$(date -v-2d +\%F)" >> /var/log/practice-xiaoda-wechat.log 2>&1
```

Linux 的日期参数改为 `date -d '2 days ago' +\%F`。也可以在开发者采集页面把采集器路径设为 `/app/scripts/werss_collector.py` 并启用每日时间；we-mp-rss 模式不需要再填写旧 Token/Cookie。不要同时启用 worker、cron 和页面内调度，以免产生无意义的重复扫描。

## 本地非 Docker 模式

把 we-mp-rss 数据目录挂载或放到本机后设置：

```bash
export WECHAT_COLLECTOR_PATH="$PWD/scripts/werss_collector.py"
export WECHAT_COLLECTOR_PYTHON=python3
export WERSS_DB_PATH=/absolute/path/to/we-mp-rss/data/we_mp_rss.db
python3 scripts/daily_wechat_update.py --since 2026-08-10 --count 50
```

适配器用 SQLite `mode=ro` 打开数据库，不修改 we-mp-rss 的任何数据。若部署使用 MySQL，应改用 we-mp-rss 的 API/RSS 适配器，而不是此 SQLite 适配器。
