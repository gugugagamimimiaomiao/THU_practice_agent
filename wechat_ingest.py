"""Safe, evidence-preserving WeChat article ingestion.

The direct reader intentionally supports only public mp.weixin.qq.com article
URLs.  A configured upstream collector can be used for more reliable reads,
but verification, rate limits, and inaccessible articles are always reported
as a recoverable review state rather than being mistaken for article content.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from database import Database
from domain import extract_project, merge_project_versions
from opportunity_filter import candidate_decision


MAX_ARTICLE_BYTES = 2 * 1024 * 1024
FETCH_TIMEOUT_SECONDS = 20
WECHAT_HOST = "mp.weixin.qq.com"


@dataclass
class WeChatFetchResult:
    ok: bool
    source_url: str
    raw_text: str = ""
    title: str = ""
    source_account: str = ""
    publish_date: str = ""
    method: str = ""
    warning: str = ""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


class _ArticleParser(HTMLParser):
    BLOCK_BREAKS = {"p", "div", "section", "li", "h1", "h2", "h3", "br"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.content_depth = 0
        self.content_parts: list[str] = []
        self.title_parts: list[str] = []
        self.account_parts: list[str] = []
        self.publish_parts: list[str] = []
        self._capture_title = False
        self._capture_account = False
        self._capture_publish = False
        self.meta: dict[str, str] = {}

    @staticmethod
    def _attrs(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {key.lower(): (value or "") for key, value in attrs}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = self._attrs(attrs)
        if tag == "meta":
            key = (data.get("property") or data.get("name") or "").lower()
            if key and data.get("content"):
                self.meta[key] = data["content"].strip()
        element_id = data.get("id", "")
        class_name = data.get("class", "")
        if tag == "div" and (element_id == "js_content" or "rich_media_content" in class_name):
            self.content_depth = 1
        elif self.content_depth and tag != "br":
            self.content_depth += 1
        if self.content_depth and tag in self.BLOCK_BREAKS:
            self.content_parts.append("\n")
        self._capture_title = self._capture_title or (tag == "h1" and "rich_media_title" in class_name)
        self._capture_account = self._capture_account or (tag == "a" and element_id == "js_name")
        self._capture_publish = self._capture_publish or (tag == "em" and element_id == "publish_time")

    def handle_endtag(self, tag: str) -> None:
        if self.content_depth and tag in self.BLOCK_BREAKS:
            self.content_parts.append("\n")
        if self.content_depth and tag != "br":
            self.content_depth -= 1
        if tag == "h1":
            self._capture_title = False
        elif tag == "a":
            self._capture_account = False
        elif tag == "em":
            self._capture_publish = False

    def handle_data(self, data: str) -> None:
        if self.content_depth:
            self.content_parts.append(data)
        if self._capture_title:
            self.title_parts.append(data)
        if self._capture_account:
            self.account_parts.append(data)
        if self._capture_publish:
            self.publish_parts.append(data)

    @staticmethod
    def text(parts: list[str]) -> str:
        return "\n".join(line.strip() for line in "".join(parts).splitlines() if line.strip())

    def article(self) -> tuple[str, str, str, str]:
        title = self.text(self.title_parts) or self.meta.get("og:title", "")
        account = self.text(self.account_parts) or self.meta.get("og:article:author", "")
        publish = self.text(self.publish_parts)
        content = self.text(self.content_parts)
        return title, account, publish, content


def validate_wechat_url(url: str) -> tuple[bool, str]:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or parsed.hostname != WECHAT_HOST:
        return False, "仅支持 mp.weixin.qq.com 的公众号文章链接。"
    if parsed.port not in {None, 80, 443}:
        return False, "公众号链接端口无效。"
    return True, parsed.geturl()


def _blocked_page(html: str) -> bool:
    signals = ("请输入验证码", "环境异常", "访问过于频繁", "antispider", "安全验证")
    return any(signal in html for signal in signals) and "js_content" not in html


def _optional_wechat_cookie() -> str:
    """Use only an explicitly supplied process credential; never expose it."""
    return os.getenv("WECHAT_COOKIE", "").strip()


def collector_credentials_present() -> bool:
    return bool(os.getenv("WECHAT_TOKEN", "").strip() and _optional_wechat_cookie())


def _direct_read(url: str, *, temporary_cookie: str = "") -> WeChatFetchResult:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; PracticeXiaoda/1.0; +https://example.invalid)",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    # ``validate_wechat_url`` has already restricted this request to the
    # official article host, so an optional deployment Secret is never sent
    # to arbitrary user-provided URLs.  It is intentionally not logged.
    cookie = temporary_cookie.strip() or _optional_wechat_cookie()
    if cookie:
        headers["Cookie"] = cookie
    request = Request(url, headers=headers)
    try:
        with build_opener(_NoRedirect()).open(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
            if response.geturl() != url:
                return WeChatFetchResult(False, url, method="direct", warning="文章链接发生了不受信任的跳转。")
            raw = response.read(MAX_ARTICLE_BYTES + 1)
    except HTTPError as exc:
        return WeChatFetchResult(False, url, method="direct", warning=f"公众号页面返回 HTTP {exc.code}，可能需要在微信中打开或稍后重试。")
    except (URLError, OSError, TimeoutError):
        return WeChatFetchResult(False, url, method="direct", warning="暂时无法读取公众号正文，请稍后重试或改用正文/OCR 导入。")
    if len(raw) > MAX_ARTICLE_BYTES:
        return WeChatFetchResult(False, url, method="direct", warning="公众号正文超过导入大小限制，请改用正文/OCR 导入。")
    html = raw.decode("utf-8", errors="replace")
    if _blocked_page(html):
        return WeChatFetchResult(False, url, method="direct", warning="公众号页面触发验证或频控，未读取正文。")
    parser = _ArticleParser()
    try:
        parser.feed(html)
    except Exception:
        return WeChatFetchResult(False, url, method="direct", warning="公众号页面格式无法解析，未读取正文。")
    title, account, publish, content = parser.article()
    if len(content) < 20:
        return WeChatFetchResult(False, url, method="direct", warning="未解析到完整正文（可能为视频/图文特殊页或访问受限）。")
    raw_text = f"{title}\n{content}".strip() if title else content
    return WeChatFetchResult(True, url, raw_text, title, account, publish, "direct")


def _collector_read(url: str, collector_path: str, *, temporary_cookie: str = "") -> WeChatFetchResult:
    path = Path(collector_path).expanduser()
    if not path.is_file():
        return WeChatFetchResult(False, url, method="collector", warning="已配置的公众号采集器路径不可用。")
    python = os.getenv("WECHAT_COLLECTOR_PYTHON", sys.executable)
    with tempfile.TemporaryDirectory(prefix="practice-xiaoda-wechat-") as directory:
        output = Path(directory) / "article.json"
        try:
            environment = dict(os.environ)
            if temporary_cookie.strip():
                environment["WECHAT_COOKIE"] = temporary_cookie.strip()
            completed = subprocess.run(
                [python, str(path), "read", url, "--out", str(output)],
                capture_output=True,
                text=True,
                timeout=45,
                check=False,
                env=environment,
            )
        except (OSError, subprocess.TimeoutExpired):
            return WeChatFetchResult(False, url, method="collector", warning="公众号采集器执行失败或超时。")
        if completed.returncode != 0 or not output.is_file():
            return WeChatFetchResult(False, url, method="collector", warning="公众号采集器未取得全文（可能需要更新凭证、通过验证或稍后重试）。")
        try:
            record = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return WeChatFetchResult(False, url, method="collector", warning="公众号采集器返回格式无效。")
    content = str(record.get("content") or "").strip()
    if len(content) < 20:
        return WeChatFetchResult(False, url, method="collector", warning="公众号采集器没有获得可用正文。")
    title = str(record.get("title") or "").strip()
    return WeChatFetchResult(
        True, str(record.get("link") or url),
        f"{title}\n{content}".strip() if title else content,
        title, str(record.get("account") or "").strip(), str(record.get("publishDate") or "").strip(), "collector",
    )


def fetch_wechat_article(url: str, *, temporary_cookie: str = "") -> WeChatFetchResult:
    valid, normalized = validate_wechat_url(url)
    if not valid:
        return WeChatFetchResult(False, url, method="validation", warning=normalized)
    collector_path = os.getenv("WECHAT_COLLECTOR_PATH", "").strip()
    if collector_path:
        result = _collector_read(normalized, collector_path, temporary_cookie=temporary_cookie)
        if result.ok:
            return result
    return _direct_read(normalized, temporary_cookie=temporary_cookie)


def import_wechat_link(
    database: Database,
    payload: dict[str, Any],
    *,
    fetcher: Callable[[str], WeChatFetchResult] | None = None,
    fallback_cookie: str = "",
) -> dict[str, Any]:
    """Fetch one public article and turn it into a reviewable project card."""
    requested_url = str(payload.get("source_url", "")).strip()
    safe_payload = {key: value for key, value in payload.items() if key not in {"wechat_cookie", "wechat_token"}}
    temporary_cookie = str(payload.get("wechat_cookie") or fallback_cookie).strip()
    result = fetcher(requested_url) if fetcher else fetch_wechat_article(requested_url, temporary_cookie=temporary_cookie)
    if not result.ok:
        article_id = database.insert_article({
            **safe_payload,
            "input_type": "wechat_url",
            "collector_status": f"fetch_{result.method or 'failed'}",
            "collector_warning": result.warning,
            "source_url": result.source_url or requested_url,
        })
        database.log("wechat_link", "公众号链接抓取未取得正文", {"article_id": article_id, "method": result.method})
        return {
            "status": "fetch_failed",
            "article_id": article_id,
            "action_required": "链接已保存，但尚未读取正文。请稍后重试，或在微信中打开后粘贴正文/截图 OCR。",
            "truthfulness_note": result.warning or "系统未把验证页、摘要或链接元数据当作文章全文。",
        }

    metadata = {
        **safe_payload,
        "input_type": "wechat_url",
        "source_url": result.source_url,
        "source_account": str(payload.get("source_account") or result.source_account or "用户投稿").strip(),
        "title": str(payload.get("title") or result.title).strip(),
        "publish_date": result.publish_date,
    }
    return import_article_text(
        database,
        metadata,
        result.raw_text,
        collector_status=result.method,
        log_channel="wechat_link",
        origin_label="公众号链接自动抓取",
        correction=bool(payload.get("correction")),
    )


def import_article_text(
    database: Database,
    metadata: dict[str, Any],
    raw_text: str,
    *,
    collector_status: str = "supplied_text",
    log_channel: str = "ingest",
    origin_label: str = "外部导入",
    correction: bool = False,
) -> dict[str, Any]:
    """把一篇已经拿到正文的文章走完入库管线。

    抓取方式（现场抓链接、外部采集器交付的文件、人工粘贴）不同，但拿到正文
    之后要做的判断完全一样：存原文留证 → 判断是不是招募 → 保守抽取项目卡 →
    查重 → 入库待核验。

    这段逻辑必须只有一份。如果每条导入路径各写一遍，最先分叉的一定是
    「什么算招募内容」和「查重怎么算」这两处——而它们分叉之后，两条路
    导进来的项目会带着不同的判断标准躺在同一张表里，等发现时已经分不清
    哪条是哪条了。

    correction=True 表示"这次是订正，以我为准"。

    默认的合并是**补充**：新版本某个字段为空就沿用旧值，资格说明信息量下降
    也会被挡回去。这对"同一篇文章的不同完整度版本"是对的——转发版、被风控
    截断的版本，都不该把完整版洗掉。

    但数据维护方发现之前抽错了、或者主办方改了通知（延期、名额调整、资格
    放宽），订正后的内容常常比原来"更少"：把"仅限计算机系"改成"面向全校"，
    按补充规则会被判成信息量下降而拒绝。那样订正就悄悄失效了——最糟的一类
    bug，因为看起来一切正常。所以给一个显式开关，而不是让规则去猜。
    """
    article_id = database.insert_article({**metadata, "raw_text": raw_text, "collector_status": collector_status})
    decision = candidate_decision({"title": metadata.get("title", ""), "content": raw_text})
    if not decision["candidate"]:
        removed = 0
        if decision["hard_excluded"]:
            removed = database.delete_projects_by_source(metadata.get("source_url", ""), note=f"{origin_label}识别为非招募内容")
        database.log(log_channel, "正文已审计但未进入机会库", {"article_id": article_id, "reasons": decision["reasons"], "removed": removed})
        return {
            "status": "not_opportunity",
            "article_id": article_id,
            "action_required": "正文已保存为来源审计，但没有明确招募/报名行动信号，未进入机会库。",
            "truthfulness_note": "行前预告、实践纪实、活动回顾等内容不会作为可报名机会推荐。",
            "decision_reasons": decision["reasons"],
        }
    project = extract_project(raw_text, metadata)
    project["article_id"] = article_id
    duplicate = database.find_duplicate(project)
    merged = False
    if duplicate and correction:
        # 订正：整条以新版本为准，只保留身份和创建时间，让它仍是同一条项目
        # 而不是新开一条。历史不会丢——每一次导入的原文都在 articles 表里，
        # upsert_project 也会留版本记录，改错了能翻回去。
        project["id"] = duplicate["id"]
        project["created_at"] = duplicate.get("created_at", project.get("created_at"))
        merged = True
    elif duplicate:
        # 再次导入是补充而不是覆盖：转发版和风控截断版都可能比已有的更稀疏。
        project = merge_project_versions(duplicate, project)
        merged = True
    if merged:
        note = f"{origin_label}订正覆盖" if correction else f"{origin_label}后合并"
    else:
        note = f"{origin_label}并导入"
    project = database.upsert_project(project, note=note)
    database.log(log_channel, "已订正项目" if correction and merged else "已导入项目",
                 {"article_id": article_id, "project_id": project["id"], "method": collector_status})
    return {
        "status": "imported",
        "article_id": article_id,
        "corrected": bool(correction and merged),
        "project": project,
        "merged_duplicate": merged,
        "review_required": project["status"] == "needs_review",
        "collector_method": collector_status,
    }
