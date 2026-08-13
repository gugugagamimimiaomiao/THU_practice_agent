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
