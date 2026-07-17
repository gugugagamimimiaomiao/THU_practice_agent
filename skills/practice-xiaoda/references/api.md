# 实践小搭 API

默认服务地址：`http://127.0.0.1:8765`。所有写接口使用 JSON。

## 清小搭/OpenAI 兼容协议

生产环境设置 `XIAODA_API_KEY` 后，使用 `Authorization: Bearer <key>`：

- `GET /v1/models` 返回 OpenAI model list；错误密钥为 HTTP 401。
- `POST /v1/chat/completions` 接受 `messages`、布尔 `stream`、可选 `model` 与 `max_tokens`。
- 非流式响应含 `choices[0].message.content` 与 `usage`。
- 流式响应严格为 role 首帧、content 增量帧、stop（含 usage）帧，最后 `data: [DONE]`。
- 当前明确不支持图片输入；请先提交 OCR 文本。

清小搭中 `baseUrl` 填 `https://你的域名/v1`，鉴权选择 Bearer Token，终止符 `[DONE]`，usage 位置选择 stop 帧内。

## 导入

`POST /api/ingest`

```json
{
  "input_type": "copied_text|ocr_text|admin_notice|wechat_url",
  "source_account": "清华大学社会实践",
  "source_url": "https://mp.weixin.qq.com/...",
  "title": "可选标题",
  "raw_text": "完整通知正文"
}
```

当 `input_type` 是 `wechat_url` 且仅提供有效的 `mp.weixin.qq.com` URL 时，服务会先尝试自动读取正文并照常返回导入的项目卡。若遇到验证、频控、网络错误或特殊文章导致正文不可读，返回 HTTP 202：

```json
{
  "status": "fetch_failed",
  "action_required": "链接已保存，但尚未读取正文。请稍后重试，或在微信中打开后粘贴正文/截图 OCR。"
}
```

## 查询与复核

- `GET /api/projects?status=published&q=乡村`
- `GET /api/projects/{id}`
- `GET /api/review-queue`
- `PATCH /api/projects/{id}`：提交需修正字段及 `status`。

## 推荐

`POST /api/recommend`

```json
{
  "profile": {
    "department": "社科学院",
    "grade": "大三",
    "available_start": "2026-08-01",
    "available_end": "2026-08-25",
    "themes": ["乡村振兴", "公共治理"],
    "preferred_locations": ["北京", "云南"],
    "reimbursement_preference": "required|preferred|not_important"
  }
}
```

响应分为 `eligible`、`potential` 和 `excluded`，每项包含分数、理由、警告或排除原因。

## 行动材料

`POST /api/generate`

```json
{
  "project_id": "PROJECT_ID",
  "kind": "application|outreach|interview|itinerary|report",
  "context": {
    "name": "真实姓名或留空",
    "department": "院系",
    "strengths": "真实经历",
    "motivation": "真实动机"
  }
}
```

输出是 Markdown 草稿与风险提示。不得未经用户检查直接发送或提交。
