# WeWe RSS 持久化部署与订阅源管理

## 持久化部署

1. 复制 `.env.example` 为 `.env`，设置随机的 `WEWE_AUTH_CODE`，不要把真实令牌写入仓库。
2. 启动服务：

   ```bash
   docker compose up -d wewe-rss practice-xiaoda wechat-daily-import
   ```

3. 检查状态：

   ```bash
   docker compose ps
   curl -fsS http://127.0.0.1:4000/feeds
   ```

WeWe 的数据库固定为 `/app/data/wewe-rss.db`，由 Compose 命名卷 `wewe_data` 持久化。容器重启、升级或服务器重启不会清除微信读书账号和已经订阅的公众号。升级前先执行：

```bash
./scripts/backup_wewe_data.sh
```

`WEWE_IMAGE` 通过 `.env` 控制。当前默认值沿用已验证的 `cooderl/wewe-rss-sqlite:latest`；生产部署第一次拉取成功后，建议把它改成实际验证过的版本标签或 digest。升级镜像时显式设置新值，并先备份卷。

## 订阅公众号

完整候选名单仍由 `wechat_sources.py` 管理，当前验证状态见 `config/wewe-subscriptions.json`。

WeWe 的订阅入口要求一篇真实的 canonical 链接，格式必须是：

```text
https://mp.weixin.qq.com/s/<article-id>
```

拿到链接后，每次只验证和添加一个账号，确认解析出的名称与名单完全一致，再等待至少 24 小时后处理下一个账号。出现 429、验证码、`WeReadError429`、`Unauthorized` 或“今日小黑屋”时，当天停止操作，不做账号/IP/设备轮换。

当前已验证并订阅：

- 清华大学社会实践：`MP_WXS_2399645822`

已解析并排入低频订阅队列（每次最多新增一个）：

- 清华大学学生公益：`MP_WXS_2394734625`
- 无限之声：`MP_WXS_3092096216`
- 清华紫荆之声：`MP_WXS_3252748391`
- 清华大学学生会：`MP_WXS_3531191868`
- 清华大学学生社团：`MP_WXS_2398854781`

其余名单已经保留在待验证队列，不会因为名称相似而盲目订阅。清华大学学生公益、无限之声虽然有历史文章记录，但现有历史链接不能作为 WeWe 的 canonical 身份证明，需要补一篇标准 `/s/` 链接后再验证。

已完成身份解析的账号会进入 `queued_for_subscription` 队列。每日任务一次只激活一个，脚本为 `scripts/activate_next_wewe_subscription.py`；WeWe 自身负责后续低频文章刷新。

## 当前日更策略

服务器已取消人工核验，因此采集端只扫描当前最有时效价值的四个账号：

- 清华大学学生会
- 清华大学学生社团
- 清华紫荆之声
- 清华大学学生公益

默认只看最近 28 天，并按发布时间倒序处理；不再从“清华大学社会实践”回补历史，也不扫描院系号。标题先经过共享机会规则：资源/基地推介、简报、总结纪实、说明、活动结束、结项成果、预告回顾等默认不推；标题同时含“招募”“报名”“招新”或“志愿者”时保留，交给服务器再次判断。

正文不足 200 字时不丢弃。只要正文或原文配图存在，就保留 `images` URL 供服务器 OCR。普通重复推送按 `source_url` 安全合并；确定是延期、资格放宽或抓漏正文时，导出使用 `--correction`，让每条 JSONL 带 `"correction": true`。

微信读书账号 `status=0` 时立即停止刷新，不重试、不轮换账号/IP/设备。重新扫码并确认 `status=1` 后，仍应先对单个优先 Feed 做一页验证；如果状态再次回到 `0`，当天停止上游刷新。已缓存文章和已入库机会不受影响。

## 历史写作语料回采

历史总结、纪实、心得和志愿服务复盘不参与日更机会补量，但可以单独回采给写作辅助使用。`corpus` 模式使用高精度标题规则，排除招募、报名、通知、预告和公示，并在每条记录中加入 `"corpus_only": true`：

```bash
python3 scripts/wewe_export_handoff.py \
  --mode corpus \
  --account 清华大学社会实践 \
  --account 建院宣传中心 \
  --account 水木华声 \
  --since 2026-03-01 \
  --need 15 \
  --delay 1.5 \
  --output data/exports/historical_corpus.jsonl
```

回采只读取 WeWe 已缓存的文章索引，再按候选逐篇获取全文；不要用它刷新整个历史列表。推送前先运行 `scripts/import_articles.py FILE --check`，并在临时数据库验证这些文章均返回 `not_opportunity`。远端投稿接口应将 `corpus_only` 视为硬隔离标记：保存原文、图片、来源和发布日期，但不创建项目卡。旧版服务器即使忽略这个新字段，高精度标题规则也应使文章进入 `not_opportunity`；批量投稿前仍需抽样验证。

## 本机持久化恢复

本机非 Docker 验证统一使用项目内的 `data/wewe-rss.db`，该路径已被 `.gitignore` 排除且文件权限应为 `0600`。不要再把 `/private/tmp/.../wewe-rss.db` 当作运行库。启动与备份：

```bash
./scripts/start_local_wewe.sh
./scripts/backup_wewe_data.sh
```

`scripts/resolve_wewe_links.py`、`scripts/wewe_sync_feed_page.py` 和 `scripts/activate_next_wewe_subscription.py` 默认读取同一个持久化文件，也可通过 `WEWE_DB_PATH` 显式覆盖。原临时库在完成迁移核验前保留为只读回滚副本；确认生产命名卷和本地备份都可恢复后再人工清理。
