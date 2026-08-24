# wechat-download-api 接入：部署、对接与风险边界

对象：[tmwgsicp/wechat-download-api](https://github.com/tmwgsicp/wechat-download-api)（AGPL-3.0，FastAPI + SQLite）。
本文只讲两件事：怎么把它接到实践小搭的投稿链路上，以及**哪些结论是实测的、哪些没验证**。

---

## 结论先写在这里（2026-08-24 实机验证）

> **它的「文章列表」这一半用不了，「抓正文」这一半能用。**
>
> 实机登录公众号「临安的雨」，订阅清华大学社会实践、学生会、学生公益、小研在线四个号，
> 手动触发一次轮询，结果是四个号**全部** `ret=200013 err_msg='freq control'`，文章 0 篇。
> 这和 `WERSS_MIGRATION.md` 里 `we-mp-rss` 的结局完全一致——换了工具，撞的是同一个
> `appmsgpublish` 接口。公众号搜索（`searchbiz`）全程 200 OK，所以不是登录态或账号的问题。
>
> **因此定位改为：不做发现，只做正文回补。** WeWe RSS 继续负责发现文章链接（它的弱项恰好
> 是抓正文容易 429），这个工具用 `POST /api/article` 按链接抓全文和图片——那条路径走
> curl_cffi 直接读文章页，不经过被频控的列表接口。对接见第三节的 `--links` 模式。
>
> 列表接口是否只是时间窗口限流，需要**隔天低频重试一次**才知道；在那之前不要加号、
> 不要缩短轮询间隔。`.env` 里的 `RSS_POLL_INTERVAL` 已经从 1 小时改成 6 小时。

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
SITE_URL=http://localhost:5000     # 图片代理拼地址用；换端口时要和 PORT 一起改
PORT=5000                          # macOS 上 5000 常被「隔空播放接收器」占着，启动脚本会自动换
RSS_POLL_INTERVAL=3600             # 轮询间隔（秒）
ARTICLES_PER_POLL=10               # 每号每轮拉几批列表
RSS_FETCH_FULL_CONTENT=true        # 轮询时顺带抓全文，保持 true
PROXY_URLS=                        # SOCKS5 代理池，见第五节
WEBHOOK_URL=                       # 企业微信机器人；填了才会收到登录过期预警
```

> **端口**：macOS 自带的「隔空播放接收器」（进程名 `ControlCe`）常年占着 5000，直接起会报 `address already in use`。`启动.command` 会自己发现这件事，按 5001 → 5002 → 5050 → 5678 → 8123 挑一个空的，并把 `.env` 里的 `PORT` 和 `SITE_URL` 一起改掉，然后把实际地址打在屏幕上。**下面写 5000 的地方，都以启动脚本实际输出的端口为准**；`wda_check.py` 也是读 `.env` 里的 `PORT`，不写死。

起来之后：`http://localhost:5000/api/health` 应该返回 `{"status":"healthy", ...}`；或者直接跑体检脚本（见第三节末尾），它会自己找对端口。

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
python3 scripts/wda_collector.py --api http://127.0.0.1:5001 \
    --output data/exports/wda_batch.jsonl
```

然后照旧两步：

```bash
python3 scripts/import_articles.py data/exports/wda_batch.jsonl --check   # 自查
python3 scripts/push_article.py    data/exports/wda_batch.jsonl           # 投稿
```

适配器做了这些事，都有测试覆盖（`tests/test_wda_collector.py`，22 条）：

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

### `--links` 模式：列表被频控时的主力用法

给一份文章链接清单，逐条走 `POST /api/article` 抓全文和图，产出同样的投稿 JSONL。
**不碰 `appmsgpublish`，所以不受 `ret=200013` 影响。**

```bash
python3 scripts/wda_collector.py --links data/exports/urls.txt \
    --links-api http://127.0.0.1:5001 \
    --output data/exports/wda_links.jsonl
```

清单每行三种写法都认，因为来源不一样：

```text
# 裸链接：公众号名用文章页里的 js_name 兜底
https://mp.weixin.qq.com/s/AbCdEf
# 带公众号名（空格或 Tab 分隔），以清单里写的为准
清华大学社会实践	https://mp.weixin.qq.com/s/GhIjKl
# WeWe 导出的 JSONL 可以直接喂进来
{"source_account": "清华紫荆之声", "source_url": "https://mp.weixin.qq.com/s/MnOpQr"}
```

几个约定：

- **默认 13 秒一条**。服务端限单 IP 5 次/分钟，13 秒刚好不撞。想快就把 `.env` 里的
  `RATE_LIMIT_PER_IP` 调大再用 `--pause 5`；但对微信那侧的间隔由 `RATE_LIMIT_ARTICLE_INTERVAL`
  管，不建议低于 3 秒，没配代理时更不建议。
- **撞限频不丢条**：服务端返回「请 N 秒后重试」时按它说的等，最多重试两次。
- **失败原因会打出来**，触发验证、登录失效、文章被删是三种不同的处理方式，不能混成一个失败计数。
- 链接是人挑好的，所以**默认不做招募标题预筛**；要筛加 `--filter-titles`。

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
- 仓库全量测试 279 条通过（含新增的 22 条）。
- `启动.command` 走了一遍完整流程：建 venv、装依赖、起服务、等健康检查、按登录态决定打开哪一页；第二次运行会跳过装依赖并认出服务已在跑；`停止.command` 停得掉、重复停不报错。（在 Linux 沙箱验证，macOS 上唯一的差别是 `open` 命令能真的把浏览器打开。）
- `wda_check.py` 的各条判断都造数据验过：未登录、订阅号一篇没拿到、日志里出现 `ret=200013 / freq control`、图形验证计数——都能认出来并给出对应结论；端口从 `.env` 读，服务换到 5001 后它自己跟过去。
- `--links` 模式用一个仿真服务端验过：三种清单写法都能解析、公众号名缺失时回退到 `author`、
  撞限频时按服务端说的秒数等待后重试成功、触发验证的那条保留原因不静默丢弃、图片地址还原、
  产出过 `import_articles.py --check`。**真实文章链接上的表现要在你机器上跑才算数**——
  沙箱没有登录态。
- 三个已经在实机踩到并修掉的坑：pip 连不上 PyPI（自动换清华/阿里/腾讯镜像）、`~/.zshrc` 里的代理指着一个没开的端口（探活后临时绕过，不改配置文件）、5000 被占（自动改用 5001 并同步 `.env` 的 `PORT` 和 `SITE_URL`）。三条路径都在沙箱里造场景验过。

## 五、没验证的部分，以及为什么要当回事

**这些都需要一个真实的公众号登录态才能验证，沙箱里做不到，别当成已验证结论：**

1. ~~**历史列表接口仍然是 `appmsgpublish`**~~ —— **2026-08-24 已实测，确认被频控**，见文首结论。
   当初的判断依据仍然成立、也解释了原因：这个请求走的是**普通 httpx，没有 TLS 指纹模拟、不走代理池**
   （`utils/rss_poller.py` 的 `_fetch_article_list`、`routes/articles.py`），宣传里的抗风控只作用在
   **正文抓取**上（`utils/http_client.py` 走 curl_cffi + 代理池）。**仍未验证的是**：这个频控是
   时间窗口限流还是长期封，要靠隔天低频重试来区分。
2. 代码里**没有对 `ret=200013` 的专门处理**——列表接口非 0 返回一律记一条 warning 然后返回空列表。真撞上频控，表现是"轮询很正常但一直没有新文章"，不会报错。**这一点要盯日志**：`docker compose logs -f` 或 `logs/` 里搜 `WeChat API error`。
3. **验证码触发 8 次会自动把该公众号拉黑**（`utils/rss_store.py`，阈值写死 8），之后轮询直接跳过它。被拉黑的号在 `/blacklist.html` 里看和解封。第一次跑完记得去看一眼，别让某个号悄悄黑了。
4. **代理池**：`RSS_FETCH_FULL_CONTENT=true` 且订阅多的时候，官方明确建议配 2–3 个 SOCKS5 代理。你自己没有代理就先把订阅号数量压到四个、`RSS_POLL_INTERVAL` 拉到 3600 以上，观察几天再加。
5. **AGPL-3.0**：私有部署随便用；**改了它的代码再对外提供服务，就得开源改动**。我们的适配器写在实践小搭这边、只读它的库和调它的公开接口，不构成对它的修改——保持这个边界就没问题。
6. **它要求你有公众号**。没有的话整条路走不通，这是硬前提。
7. 隐私上比 WeWe 好一些：不再有第三方中转，凭证只落本机 `.env`。但 `.env` 里就是可用的登录态，**别提交、别外传**，权限设 `0600`。

## 六、当前的推进顺序（列表已确认被频控后）

**今天**：双击 `停止.command`。不加号、不重试、不缩短间隔——重试只会把限制拖长。

**从明天起，两条线并行。**

线一，把它当正文抓取器用起来（不受频控影响，随时可做）：

1. 从 WeWe 那边导出一批链接，或手工整理一份清单。
2. `python3 scripts/wda_collector.py --links 清单文件 --output data/exports/wda_links.jsonl`
3. `python3 scripts/import_articles.py data/exports/wda_links.jsonl --check`，人工抽看几条正文。
4. 没问题就 `push_article.py` 投稿。

线二，每天**只测一次**列表接口，判断 200013 是临时限流还是长期封：

1. 双击 `启动.command`（`RSS_POLL_INTERVAL` 已改成 6 小时）。
2. 在 `rss.html` 点一次「立即轮询」，**只点一次**。
3. `python3 scripts/wda_check.py` 看结论。
4. 还是 200013 → 当天到此为止。连着三四天都是 → 按长期封处理，列表这条路放弃，
   `--links` 模式留作正文回补的常规手段。
5. 某天通了 → 那就是时间窗口限流，再按"只订一个号、观察两三天"的节奏加回来。

全程 WeWe 那条链路不要停——它现在仍是唯一的发现来源。
