# 实践小搭 MVP

面向清华学生的社会实践机会决策与行动助手。它将公众号文章、复制通知、截图 OCR 文本和管理员投稿转换为带原文证据的项目卡，先对截止日期、时间冲突和资格限制执行硬过滤，再按主题、地点、经费与信息完整度给出个性化推荐，并继续生成报名、外联、访谈、行程和报告草稿。

本项目是可运行的比赛 MVP。**核心服务只用 Python 标准库**，不引入任何第三方 Python 或 JavaScript 包——推荐、过滤、检索、HTTP 服务、SQLite 存取全部如此。唯一的例外是抓取公众号配图的可选脚本 `scripts/wechat_image_fetch.py` 用到 `requests`，它写在 `try` 内部，装不装都不影响主流程。

### 行动工作台的交通查询（可选）

“路线任务”要求先选择当地外联地点并填写酒店位置。系统会把同一组已选点位带入访谈和路线；外联地点只作为待核验线索，不会虚构联系方式或预约状态。

若在服务环境配置 `AMAP_WEB_SERVICE_KEY`，点击“查询实时交通”会通过高德 Web 服务查询酒店到已选点位的公共交通（含线路名）、驾车和步行时间，并把结果写入路线草稿。高铁、飞机的实时班次仍以 12306、航司和机场官方渠道为准。未配置 Key 时，界面会保留“打开地图核验 + 粘贴结果”的完整手动路径，绝不把推测写成地铁、航班或车次。

## 立即运行

只需要 Python，跑起来**不用装任何包**。已在 Python 3.10 和 3.13 上验证通过。

macOS / Linux：

```bash
git clone https://github.com/Sonnette51/THU_practice_agent.git
cd THU_practice_agent
python3 server.py --port 8765
```

Windows（PowerShell）：

```powershell
git clone https://github.com/Sonnette51/THU_practice_agent.git
cd THU_practice_agent
py server.py --port 8765
```

> Windows 上通常没有 `python3` 这个命令，请用 `py` 或 `python`。

然后访问 [http://127.0.0.1:8765](http://127.0.0.1:8765)。

跑测试：

```bash
python3 -m unittest discover -s tests    # Windows: py -m unittest discover -s tests
```

`Makefile` 里的 `make run` / `make test` / `make check` 是同样命令的简写，但 Windows 默认没有 `make`，直接用上面的原始命令即可。

首次启动会在 `data/practice_xiaoda.db` 创建 SQLite 数据库并载入 8 条演示项目。所有演示项目都带有 `demo_data: true` 和醒目的“演示”标识，不能作为真实报名依据。

演示项目的日期是**相对当天生成**的：`seed_data.json` 里的日期按 `anchor_date` 书写，载入时整体平移到今天，因此任何时候首次启动都能看到处于报名中的项目，同时保留一条已过期项目用于演示过期状态。想重新生成一份贴近当天的演示库，删掉 `data/practice_xiaoda.db` 再启动即可。

## 团队协作：哪些文件不会上传，以及如何获得它们

仓库会上传**代码、测试、示例配置和演示项目**；每位同学在自己的电脑或服务器上生成、配置或保管以下本地文件：

| 本地文件 | 为什么不上传 | 同学如何获得/配置 |
|---|---|---|
| `.env` | 可含 API Key、部署密钥 | 从 `.env.example` 复制：`cp .env.example .env`，再各自填写部署所需的值 |
| `data/practice_xiaoda.db` | 含本地项目库、审核记录和用户反馈 | 首次运行 `python3 server.py --port 8765` 自动创建并载入演示数据；生产环境用挂载卷/备份恢复 |
| `data/collector_settings.json` | 可含微信采集 Token/Cookie | 开发者在“开发者采集”页面自行填写；凭据不会回显或上传 |
| `data/collector_audits/` 与 `data/exports/` | 可能含真实文章、项目或导出数据 | 由各自的采集/导出动作生成；需要共享时，请先脱敏、确认授权后用受控渠道传递 |

新同学克隆仓库后的最小启动流程：

```bash
git clone git@github.com:Sonnette51/THU_practice_agent.git
cd THU_practice_agent
cp .env.example .env          # Windows PowerShell: copy .env.example .env
python3 server.py --port 8765 # Windows PowerShell: py server.py --port 8765
```

不要把 `.env`、微信 Token/Cookie、真实数据库、真实用户导出或未脱敏联系人提交到 GitHub。项目根目录的 `.gitignore` 会默认拦截这些文件。

## 已实现的完整用户流程

网页端（本地开发模式）：

1. **项目总览**：查看项目状态、数据健康度、来源数量与最近活动。
2. **机会库**：搜索、状态筛选、主题筛选、证据详情、项目比较和人工字段修正。
3. **多源导入**：支持复制正文、OCR 文本、管理员通知和公众号链接；公众号链接可自动读取公开正文，失败时保守地保留线索并提示补充。
4. **保守抽取**：提取日期、地点、参与对象、报名截止、经费、报名方式、材料和联系方式。已针对两种常见公众号排版做过校正——小标题独占一行、值写在下一行；以及要素挤在一行用 `|` 分隔。
5. **证据与风险**：关键字段保存原文引用；OCR、缺来源和缺关键字段都会触发风险标记。
6. **审核状态机**：`draft → needs_review → published / expired / rejected`，并保存历史版本。
7. **重复合并**：按规范化 URL，或“标题 + 主办方 + 截止日期”识别更新文章。
8. **智能推荐**：返回“正式推荐、潜在机会、已排除”三组结果和可解释原因。
9. **行动工作台**：生成报名陈述、外联话术、访谈提纲、行程任务表和报告框架。
10. **反馈与导出**：记录草稿反馈，导出完整 JSON 项目库。
11. **清小搭 Skill**：`skills/practice-xiaoda/` 提供标准 `SKILL.md`、API 客户端和参考资料。

### 通过清小搭对话时能做什么

清小搭走的是 `/v1/chat/completions`，能力范围和网页端**不完全一样**，以下逐条实测过：

| 能做 | 说法示例 |
|---|---|
| 按条件推荐，并解释推荐与排除的理由 | 「我大三，八月有空，推荐乡村振兴方向的实践」 |
| 查看项目详情**及每个关键字段的原文引用** | 直接说出项目名 |
| 逐项比较两个项目 | 「比较前两个推荐项目」 |
| 生成报名理由 / 外联话术 / 访谈提纲 / 报告框架 | 「帮我写⋯⋯的报名理由」 |
| 把粘贴的招募通知转成带证据的项目卡 | 「导入这则通知：⋯⋯」 |
| 追问上一轮提到的项目 | 「第二个的报销怎么样？」 |

**对话里做不到**：行程任务表。它需要先选定当地点位和住宿位置，而对话中没有这些表单；询问时会说明原因并引导到行动工作台，不会替用户编造路线。

## 数据可靠性规则

- 只有 `published` 项目进入正式推荐。
- `needs_review` 项目只进入“潜在机会”。
- 已过截止日期的项目自动标记为 `expired`。
- 明确院系、年级和时间冲突属于硬排除条件，不能被兴趣分数覆盖。
- 报名截止、资格和报销均不从上下文猜测。
- 只给 `mp.weixin.qq.com` 公众号链接时，系统先自动读取公开正文并生成项目卡；验证、频控、视频/特殊图文或正文不可读时，系统保存链接并返回 `fetch_failed`，不会宣称读过文章。
- 动态项目使用 SQLite 精确查询；静态调研写作资料应通过独立 RAG 知识库接入。

## 写作模型：只用来「写」，不用来「判断」

系统可以接入一个 OpenAI 协议的大模型（默认 DeepSeek），配置 `DEEPSEEK_API_KEY` 即启用。

**它的职责边界是硬性的**：所有事实——有哪些项目、报名截止是哪天、谁能报、报销多少——一律来自 SQLite 里人工核验过的项目卡，模型碰不到也改不了。模型只负责把已经确定的事实写成通顺的推送文案、外联邮件、访谈提纲。

这样划分是因为项目的核心承诺是「不编」。让模型参与事实判断，就等于把一个会流畅地说出错误截止日期的部件放进了链路里。

不配 Key 也能跑：写作类请求会降级成结构化要点清单，功能不消失，只是不那么好读。模型超时或报错时同样降级，不会把错误抛给用户。

## 运维：线上跑起来之后

`deploy/systemd-install.sh` 会一次性装好三样东西：

| | 频率 | 做什么 |
|---|---|---|
| 主服务 | 常驻 | `python3 -u server.py`，异常退出 1 秒后自动拉起 |
| 健康自检 | 每分钟 | 真的打一次对话接口（进程活着不等于能服务）。连续失败 3 次才告警，配了 `HEALTH_ALERT_SCKEY` 就推微信 |
| 数据库备份 | 每日 04:30 | 用 SQLite 的 backup API 而非 `cp`（服务随时在写，直接复制可能拷到写了一半的状态），gzip 后做一次 `integrity_check`，保留 14 天 |

外加 logrotate：自检每分钟一条日志，一天一千四百多行，磁盘写满时 SQLite 的报错很难读懂，不如提前压住。

看当前状态用一条命令：

```bash
python3 scripts/ops_report.py            # 最近 24 小时
python3 scripts/ops_report.py --hours 72
python3 scripts/ops_report.py --json     # 给脚本用
```

它汇总服务与定时器状态、磁盘、模型可用性、自检成败、备份新鲜度、项目库健康度（演示数据占比、缺字段分布、待核验积压）、对话量与兜底率、**没接住的问题原话**、请求来源分布。异常项自己标出来。

写这个脚本是有教训的：排查「平台到底有没有转发请求」时，用 `grep` 过滤日志得到空结果就下了结论，没注意到日志含中文、`grep` 按二进制处理并给了 `binary file matches` 警告——空输出不等于没有匹配；而且当时的访问日志压根不记来源 IP，这个判断从一开始就做不到。所以这个脚本读日志不走 `grep`，遇到不含来源 IP 的旧格式日志会直接说「无法区分请求来源」，而不是给一个看起来很确定的 0。

## 主要 API

| 方法 | 路径 | 功能 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| GET | `/api/projects` | 查询项目库 |
| GET | `/api/projects/{id}` | 项目详情与版本 |
| POST | `/api/ingest` | 导入链接、文本或 OCR 文本 |
| PATCH | `/api/projects/{id}` | 人工审核与字段修正 |
| POST | `/api/recommend` | 个性化匹配 |
| POST | `/api/generate` | 生成五类行动材料 |
| POST | `/api/feedback` | 保存用户反馈 |
| GET | `/api/export` | 导出项目库 |
| GET | `/api/collector/status` | 查看微信采集适配器配置状态；不返回凭据 |
| GET | `/v1/models` | 清小搭/OpenAI 兼容模型探测（Bearer 鉴权） |
| POST | `/v1/chat/completions` | 清小搭/OpenAI 兼容对话（支持 SSE 流式） |

### 清小搭接入参数

将公网服务的版本段作为 `baseUrl`，例如 `https://your-domain.example/v1`；清小搭会自动请求
`/v1/models` 与 `/v1/chat/completions`。鉴权选择 **Bearer Token**，密钥填写部署环境中的
`XIAODA_API_KEY`。流式终止符使用 `data: [DONE]`，usage 在 stop 帧内，视觉能力不要勾选（当前版本会明确返回不支持图片输入）。

协议自测：

```bash
XIAODA_API_KEY=test-secret python3 server.py --host 127.0.0.1 --port 8765
curl -i http://127.0.0.1:8765/v1/models -H 'Authorization: Bearer test-secret'
curl -N http://127.0.0.1:8765/v1/chat/completions \\
  -H 'Authorization: Bearer test-secret' -H 'Content-Type: application/json' \\
  -d '{"stream":true,"max_tokens":1,"messages":[{"role":"user","content":"你好"}]}'
```

详细请求格式见 [Skill API 参考](skills/practice-xiaoda/references/api.md)。

## 真实微信公众号采集边界

MVP 的核心流程在无微信凭证时仍可完整运行。真实后台批量采集需要把已有公众号采集器作为上游线索适配器，并在服务进程外安全配置：

```bash
export WECHAT_COLLECTOR_PATH=/path/to/wechat_collector.py
export WECHAT_COLLECTOR_PYTHON=/path/to/collector/python
export WECHAT_TOKEN=your_token
export WECHAT_COOKIE=your_cookie
```

现在可直接在产品的“开发者采集”模块手动填写 Token/Cookie，或在“导入与审核”模块为单次链接临时填写 Cookie。单篇链接读取仅会把 Cookie 发往已校验的 `mp.weixin.qq.com` 主机，可降低匿名请求触发验证的概率。凭证不得写入仓库、SQLite 项目数据、浏览器本地存储或 Skill 包。凭证失效、微信验证码、全文不可访问都是正常状态，应降级到“用户复制正文”“截图 OCR 文本”或“管理员表单”，而不是绕过验证。

### 每日更新三个可信公众号（可选）

项目已包含 `scripts/daily_wechat_update.py`。它只在已配置上游采集器与 `WECHAT_TOKEN`/`WECHAT_COOKIE` 的独立定时工作进程中运行，默认采集“清华大学社会实践”“无限之声”“清华大学学生公益”，筛出标题含实践/招募/报名等信号的文章，再进入去重与人工审核流程。

```bash
export WECHAT_COLLECTOR_PATH=/path/to/wechat_collector.py
export WECHAT_COLLECTOR_PYTHON=/path/to/collector/python
export WECHAT_TOKEN=your_token
export WECHAT_COOKIE=your_cookie
make daily-wechat-update
```

在 Linux 上可由 cron 每天运行一次，例如 `15 8 * * * cd /opt/THU_practice_agent && make daily-wechat-update >> /var/log/practice-xiaoda-wechat.log 2>&1`。不要把 Cookie/Token 放进 crontab 命令、仓库或应用数据库；应放在部署平台的 Secret/环境变量中。

## 目录结构

```text
THU_practice_agent/
├── server.py                 # HTTP API 与静态站点
├── domain.py                 # 抽取、状态、推荐、材料生成规则
├── database.py               # SQLite 与版本/反馈记录
├── chat_adapter.py           # 清小搭对话适配：意图识别与回答组织
├── llm.py                    # 可选写作模型客户端（仅标准库实现）
├── seed_data.json            # 明确标注的演示项目
├── static/                   # 完整响应式前端
├── deploy/                   # systemd 安装、健康自检、数据库备份
├── scripts/ops_report.py     # 一条命令查看线上运行状态
├── scripts/bulk_import_links.py  # 批量导入公众号链接（可断点续跑）
├── Dockerfile + compose.yaml # 容器化与持久 SQLite 卷
├── compose.public.yaml        # 公网 HTTPS（Caddy）覆盖配置
├── DEPLOYMENT.md              # 清小搭与云平台部署说明
├── reports/                  # 121 轮浏览器黑盒测试记录（回归基线）
├── tests/                    # 单元与静态流程测试
└── skills/practice-xiaoda/   # 清小搭/Codex 标准 Skill 包
```

## 上线前需要替换的外部部分

- 将演示项目替换为已授权、已核验的真实项目数据。
- 连接学校统一身份认证或清小搭现有身份能力。
- 配置真实公众号采集凭证，并建立凭证失效告警。
- 接入获准使用的 OCR 服务和静态报告 RAG 知识库。
- 根据学校数据管理要求配置 HTTPS、访问控制、备份、日志脱敏和数据保留期。
- 对任何外部报名提交、邮件或消息发送保留用户最终确认。
