# 公众号采集、OCR 与三周回补验收

## 需要的数据

不要提供微信聊天记录、联系人、个人微信密码、支付信息或短信验证码。

批量公众号采集只需要：

1. 微信公众平台后台的临时 `token` 和完整 `Cookie`，放在服务器 Secret、环境变量或本地开发者采集配置中，不发到群聊、不提交 GitHub。
2. 12 至 20 条人工标注样本：
   - 至少 5 条仍可报名的实践/志愿招募；
   - 至少 5 条实践总结、行前预告、活动回顾或已结束招募；
   - 至少 3 条关键字段只写在海报图片中的文章。
3. 每条样本只记录文章 URL、是否应进入机会库，以及能从原图人工确认的字段。不要提交图片原件；OCR 会在测试时从微信 CDN 临时下载并立即删除。

真实标注文件使用 `data/collection_quality_samples.jsonl`，该文件已被 `.gitignore` 排除。格式参考 `data/collection_quality_samples.example.jsonl`。

## 第一阶段：小样本质量测试

先采集 3 至 5 个账号最近一天的数据，不直接写入正式数据库。人工核对后运行：

```bash
python3 scripts/evaluate_collection_quality.py data/collection_quality_samples.jsonl
```

建议通过门槛：

- 招募分类 precision >= 0.95，避免总结文章进入机会库；
- 招募分类 recall >= 0.85，避免漏掉海报型招募；
- 报名截止、实践日期、地点、资格等 OCR 关键字段准确率 >= 0.90；
- 公众号精确匹配率 100%；同名模糊匹配必须进入审计而不是入库；
- 正文或图片下载失败必须有明确失败原因，不能生成猜测字段。

## 第二阶段：回补最近三周

质量门槛通过后执行可断点续跑的分批任务：

```bash
SEED_DEMO_DATA=false python3 scripts/backfill_wechat.py \
  --since 2026-07-22 \
  --count 30 \
  --batch-size 6 \
  --pause 180
```

脚本每批只抓 6 个公众号，批次之间冷却 3 分钟，并把进度写入 `data/wechat_backfill_state.json`。凭证失效、微信频控或进程中断后，重新执行同一命令即可从未完成批次继续。

所有新项目默认保持 `needs_review`。报名截止、资格、地点、时间和经费经过人工核对后才能改为 `published`。

## 第三阶段：服务器容量

先测试规则回复路径：

```bash
python3 scripts/load_test.py \
  --base-url https://你的域名/v1 \
  --api-key "$XIAODA_API_KEY" \
  --requests 300 \
  --concurrency 60
```

建议通过门槛：成功率 >= 99%，P95 < 1 秒。启用 DeepSeek/OpenAI 等写作模型后，需要另测模型路径；其容量由上游模型的并发、限流和首 token 延迟决定，不能用规则回复压测结果替代。

采集和 OCR 应由独立定时任务运行，不要和 Web 请求在同一个高峰时段竞争 CPU。默认 OCR 仅使用 2 个工作线程；1 至 2 核服务器保留此值，4 核以上才考虑设置 `WECHAT_OCR_WORKERS=3`。
