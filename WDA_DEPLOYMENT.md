# wechat-download-api 接入：部署、对接与风险边界

对象：[tmwgsicp/wechat-download-api](https://github.com/tmwgsicp/wechat-download-api)（AGPL-3.0，FastAPI + SQLite）。
本文只讲两件事：怎么把它接到实践小搭的投稿链路上，以及**哪些结论是实测的、哪些没验证**。

## 一、它和现在的 WeWe RSS 差在哪

| | WeWe RSS（现主链路） | wechat-download-api |
|---|---|---|
| 登录 | 微信读书账号，令牌经第三方中转 `weread.111965.xyz` | 微信公众平台扫码，**需要自己有一个公众号**（订阅号即可），凭证只存本机 `.env` |
| 加订阅 | 要先有一条 canonical `/s/` 链接，**每次一个、间隔 24 小时** | 按名称搜索拿 fakeid 直接订阅，支持批量粘名称 |
| 正文 | 缓存索引 + 逐篇取全文，`status=0` 就得停 | 轮询时直接抓全文入库（`RSS_FETCH_FULL_CONTENT=true`，默认开） |
| 存储 | `data/wewe-rss.db` | `data/rss.db`，`articles` 表已含 `plain_content` / `content` / `publish_time` / `link` |
| 取数据 | 读它的 SQLite | 两条路都行：只读 SQLite，或 `GET /api/feed/articles.json?since=` 游标增量 |
| 抗风控 | 无 | 正文抓取走 curl_cffi（Chrome TLS 指纹）+ SOCKS5 代理池；**列表接口不走**（见第五节） |

对"维护数据库"这件事最实在的改善是三点：**订阅不再一天一个**、**正文和图片跟着轮询一起入库**、**有一个现成的 `since` 游标接口做增量**。

## 二、部署（已经放好了，你只剩双击）

代码已经拉到 `~/Desktop/agent/wechat-download-api`，`.env` 也从模板生成好了（权限 0600）。

**启动**：双击那个目录里的 **`启动.command`**。第一次会自己建虚拟环境、装依赖（几分钟），起服务，然后自动打开扫码页。之后再双击就是秒开——依赖装过就跳过。
**停止**：双击 **`停止.command`**。数据和凭证都不动。

> macOS 第一次双击可能弹「无法打开，因为来自身份不明的开发者」：右键 →「打开」→ 再点「打开」就行；或者在终端里 `bash ~/Desktop/agent/wechat-download-api/启动.command`。

上游自带的 `bash start.sh` 也能用，区别是它在前台跑、关掉终端窗口服务就没了；`启动.command` 是后台跑 + 写日志到 `logs/service.log` + 自动等健康检查通过，日更更合适。有 Docker 的话 `docker compose up -d` 同样可以。

`.env` 里当前需要动的几项：

```bash
SITE_URL=http://localhost:5000     # 图片代理拼地址用，本机跑就这个
RSS_POLL_INTERVAL=3600             # 轮询间隔（秒）
ARTICLES_PER_POLL=10               # 每号每轮拉几批列表
RSS_FETCH_FULL_CONTENT=true        # 轮询时顺带抓全文，保持 true
PROXY_URLS=                        # SOCKS5 代理池，见第五节
WEBHOOK_URL=                       # 企业微信机器人；填了才会收到登录过期预警
```

起来之后：`http://localhost:5000/api/health` 应该返回 `{"status":"healthy", ...}`；或者直接跑体检脚本（见第三节末尾）。

**扫码登录**：浏览器打开 `http://localhost:5000/login.html`，用**公众号管理员的微信**扫码。凭证写进 `.env`，**有效期约 4 天**，过期后所有对微信的调用都返回 `success:false` 并提示重新扫码（`ret=200003 / 200040`）。配了 `WEBHOOK_URL` 会在到期前 24 小时和 6 小时各推一次。

**订阅公众号**：`http://localhost:5000/rss.html` 搜名称、批量添加。名单直接用仓库里的 `wechat_sources.py`；当前日更只需要"清华大学学生会 / 学生社团 / 紫荆之声 / 学生公益"这四个。

## 三、对接实践小搭：`scripts/wda_collector.py`

新增的适配器只做一件事——把它库里已抓到的文章转成 `DATA_HANDOFF.md` 里那六个字段，**不做任何字段抽取**，截止日期、资格、经费照旧由服务端 `domain.py` 解析。

```bash
# 同机部署：直接只读它的 SQLite，正文用 plain_content，最干净
python3 scripts/wda_collector.py \
    --db ~/wechat-download-api/data/rss.db \
    --output data/exports/wda_batch.jsonl

# 它跑在别的机器 / Docker 里：走 HTTP
python3 scripts/wda_collector.py --api http://127.0.0.1:5000 \
    --output data/exports/wda_batch.jsonl
```

然后照旧两步：

```bash
python3 scripts/import_articles.py data/exports/wda_batch.jsonl --check   # 自查
python3 scripts/push_article.py    data/exports/wda_batch.jsonl           # 投稿
```

适配器做了这些事，都有测试覆盖（`tests/test_wda_collector.py`，14 条）：

- **增量游标**：上次导到哪存在 `data/wda_state.json`，第二次跑只出新文章；`--since 2026-08-01` 可覆盖。
- **图片地址还原**：它入库时把图换成了 `http://localhost:5000/api/image?url=…` 的本机代理地址，这种地址服务器那边打不开。适配器一律还原成 `mmbiz.qpic.cn` 原图，OCR 才拉得到。
- **正文没抓到的不投**：`content` 为空的单独计数，不会当成"空正文的正常文章"混进去。
- **图文型推送不丢**：正文短但有图的照样投，交给服务端 OCR——这是 `DATA_HANDOFF.md` 明确要求的。
- **标题预筛**：复用 `opportunity_filter.candidate_decision`，不另写一套规则；`--all-titles` 关掉。
- **按 `source_url` 去重**：默认比对 `data/practice_xiaoda.db`。
- **只读打开**上游数据库（`mode=ro`），不会写坏它的库。

每天固定跑一次的话（时间自己挑）：

```cron
30 8 * * * cd ~/practice-xiaoda-mvp && python3 scripts/wda_collector.py --db ~/wechat-download-api/data/rss.db --output data/exports/wda_daily.jsonl && python3 scripts/push_article.py data/exports/wda_daily.jsonl >> ~/wda.log 2>&1
```

### 体检：`scripts/wda_check.py`

不用自己去翻库和日志，一条命令看清现在能不能用：

```bash
python3 scripts/wda_check.py
```

它按顺序查：服务在不在 → 登录态还剩几小时 → 每个订阅号各拿到几篇/几篇有正文/上次轮询多久前 → 有没有被自动拉黑的号 → 日志里有没有 `ret=200013` 频控和图形验证。最后给一句结论：通了就直接把下一条命令打出来；没通就说明卡在哪、当天该不该停手。全程只读。

### 每天固定跑

别和 WeWe 那条链路同时开着投同一批号——重复文章虽然会按 `source_url` 合并，但两边都在刷微信，风控是加在一起算的。

## 四、已经实测的部分

在沙箱里跑了一遍（Python 3.11）：

- `pip install -r requirements.txt` + `python3 app.py` 起服务成功，`/api/health` 返回 `http_engine: curl_cffi (Chrome TLS)`。
- `data/rss.db` 自动建表，`articles` 表结构与上面描述一致。
- 造了 3 条样例文章（一条完整招募、一条总结回顾、一条正文未抓到）写进它的库，两种读法（SQLite / HTTP）跑通，产出的 JSONL **过了 `import_articles.py --check`**，两种读法的 `images` 完全一致、正文只差段落空行。
- 把产出的记录喂给 `domain.extract_project`，能正常解析出地点、招募对象、联系人等字段，两种读法结果一致。
- 仓库全量测试 271 条通过（含新增的 14 条）。
- `启动.command` 走了一遍完整流程：建 venv、装依赖、起服务、等健康检查、按登录态决定打开哪一页；第二次运行会跳过装依赖并认出服务已在跑；`停止.command` 停得掉、重复停不报错。（在 Linux 沙箱验证，macOS 上唯一的差别是 `open` 命令能真的把浏览器打开。）
- `wda_check.py` 的各条判断都造数据验过：未登录、订阅号一篇没拿到、日志里出现 `ret=200013 / freq control`、图形验证计数——都能认出来并给出对应结论。

## 五、没验证的部分，以及为什么要当回事

**这些都需要一个真实的公众号登录态才能验证，沙箱里做不到，别当成已验证结论：**

1. **历史列表接口仍然是 `appmsgpublish`**。就是 2026-07-30 之后让 `we-mp-rss` 持续返回 `ret=200013 / freq control` 的那个接口（见 `WERSS_MIGRATION.md`）。更关键的是：这个请求走的是**普通 httpx，没有 TLS 指纹模拟、不走代理池**（`utils/rss_poller.py` 的 `_fetch_article_list`、`routes/articles.py`）。宣传里的抗风控只作用在**正文抓取**上（`utils/http_client.py` 走 curl_cffi + 代理池）。所以"它能绕开频控"这件事，**没有证据，得你登录后实测**。
2. 代码里**没有对 `ret=200013` 的专门处理**——列表接口非 0 返回一律记一条 warning 然后返回空列表。真撞上频控，表现是"轮询很正常但一直没有新文章"，不会报错。**这一点要盯日志**：`docker compose logs -f` 或 `logs/` 里搜 `WeChat API error`。
3. **验证码触发 8 次会自动把该公众号拉黑**（`utils/rss_store.py`，阈值写死 8），之后轮询直接跳过它。被拉黑的号在 `/blacklist.html` 里看和解封。第一次跑完记得去看一眼，别让某个号悄悄黑了。
4. **代理池**：`RSS_FETCH_FULL_CONTENT=true` 且订阅多的时候，官方明确建议配 2–3 个 SOCKS5 代理。你自己没有代理就先把订阅号数量压到四个、`RSS_POLL_INTERVAL` 拉到 3600 以上，观察几天再加。
5. **AGPL-3.0**：私有部署随便用；**改了它的代码再对外提供服务，就得开源改动**。我们的适配器写在实践小搭这边、只读它的库和调它的公开接口，不构成对它的修改——保持这个边界就没问题。
6. **它要求你有公众号**。没有的话整条路走不通，这是硬前提。
7. 隐私上比 WeWe 好一些：不再有第三方中转，凭证只落本机 `.env`。但 `.env` 里就是可用的登录态，**别提交、别外传**，权限设 `0600`。

## 六、建议的推进顺序

1. 双击 `启动.command`，用**公众号管理员微信**扫码。
2. 在自动打开的 `rss.html` 里**只订阅 1 个号**（建议"清华大学学生社团"）。
3. 等一轮轮询（默认 1 小时），跑 `python3 scripts/wda_check.py`：
   - **有正文** → 频控这关过了，继续第 4 步。
   - **轮询过但一篇没拿到 / 日志里有 200013** → 当天停手，别加号也别缩短轮询间隔，WeWe 那条链路先别停。
4. 跑 `wda_collector.py` 导一批，再 `import_articles.py --check`，人工看几条正文对不对得上原文。
5. 没问题再把订阅加到四个号，观察两三天，每天跑一次 `wda_check.py` 看黑名单和验证码计数。
6. 稳定之后再考虑用它替掉 WeWe 的日更，并在 `README.md` / `WEWE_DEPLOYMENT.md` 里更新主链路说明。
