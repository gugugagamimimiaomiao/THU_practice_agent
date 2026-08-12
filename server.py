#!/usr/bin/env python3
"""HTTP server and JSON API for 实践小搭 MVP.

Run: python3 server.py --port 8765
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import signal
import sys
import threading
import time
import traceback
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import llm
from database import Database
from domain import deep_merge, extract_project, generate_asset, now_iso, recommend_local_sites, recommend_projects
from chat_adapter import (
    ChatRequestError,
    PracticeChatAdapter,
    completion_payload,
    model_list,
    openai_error,
    resolve_max_tokens,
    stream_events,
    truncate_to_tokens,
    validate_chat_request,
)
from security import (
    RATE_LIMITER,
    chat_auth_ready,
    is_production,
    token_fingerprint,
    verify_bearer,
)
from wechat_ingest import collector_credentials_present, import_wechat_link
from collector_scheduler import DailyCollectorScheduler
from collector_settings import credentials as collector_credentials, delete_profile, public_status, save_from_developer, select_profile
from opportunity_filter import candidate_decision
from route_lookup import query_routes


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
MAX_BODY = 2 * 1024 * 1024
DB = Database()
CHAT = PracticeChatAdapter(DB)
SCHEDULER = DailyCollectorScheduler()


class APIError(Exception):
    def __init__(self, status: int, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details


class OpenAIAPIError(APIError):
    def __init__(
        self,
        status: int,
        message: str,
        *,
        error_type: str = "invalid_request_error",
        param: str | None = None,
        code: str | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(status, message)
        self.error_type = error_type
        self.param = param
        self.code = code
        self.retry_after = retry_after


class Handler(BaseHTTPRequestHandler):
    server_version = "PracticeXiaoda/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stdout.write(f"[{self.log_date_time_string()}] {fmt % args}\n")

    def _headers(self, status: int, content_type: str, length: int | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store" if content_type.startswith("application/json") else "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Request-ID", self.request_id)
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.end_headers()

    @property
    def request_id(self) -> str:
        if not hasattr(self, "_request_id"):
            incoming = self.headers.get("X-Request-ID", "").strip()
            self._request_id = incoming[:100] if incoming else f"pxd_{uuid.uuid4().hex[:20]}"
        return self._request_id

    def json_response(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=None).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise APIError(400, "Content-Length 无效")
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise APIError(413, "请求体超过 2MB 限制")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("JSON root must be an object")
            return payload
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise APIError(400, "JSON 请求格式无效", str(exc))

    def safe(self, action) -> None:
        try:
            action()
        except OpenAIAPIError as exc:
            if exc.retry_after:
                body = json.dumps(
                    openai_error(exc.message, error_type=exc.error_type, param=exc.param, code=exc.code),
                    ensure_ascii=False,
                ).encode("utf-8")
                self.send_response(exc.status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Retry-After", str(exc.retry_after))
                self.send_header("X-Request-ID", self.request_id)
                self.end_headers()
                self.wfile.write(body)
            else:
                self.json_response(
                    openai_error(exc.message, error_type=exc.error_type, param=exc.param, code=exc.code),
                    exc.status,
                )
        except APIError as exc:
            self.json_response({"error": exc.message, "details": exc.details}, exc.status)
        except KeyError as exc:
            self.json_response({"error": "未找到请求的项目", "details": str(exc)}, 404)
        except ValueError as exc:
            self.json_response({"error": str(exc)}, 422)
        except BrokenPipeError:
            pass
        except (ConnectionResetError, BrokenPipeError):
            # 客户端提前断开——用户关掉页面、网络抖动、或者公网上的扫描器
            # 打完就跑。这是预期内的，不是故障；打整段 traceback 只会把日志刷脏，
            # 真出问题时反而看不见。连接都没了，也没法再回什么给对方。
            self.close_connection = True
        except Exception as exc:  # pragma: no cover - final API boundary
            traceback.print_exc()
            self.json_response({"error": "服务器处理失败", "details": str(exc)}, 500)

    def do_OPTIONS(self) -> None:
        self._headers(204, "text/plain", 0)

    def require_chat_auth(self, *, rate_limit: bool = False) -> None:
        if not chat_auth_ready():
            raise OpenAIAPIError(
                503,
                "XIAODA_API_KEY is not configured",
                error_type="server_error",
                code="api_key_not_configured",
            )
        authorization = self.headers.get("Authorization")
        if not verify_bearer(authorization):
            raise OpenAIAPIError(
                401,
                "Invalid or missing API key",
                error_type="authentication_error",
                code="invalid_api_key",
            )
        if rate_limit:
            identity = token_fingerprint(authorization, self.client_address[0])
            allowed, _, retry_after = RATE_LIMITER.check(identity)
            if not allowed:
                raise OpenAIAPIError(
                    429,
                    "Rate limit exceeded",
                    error_type="rate_limit_error",
                    code="rate_limit_exceeded",
                    retry_after=retry_after,
                )

    def require_admin_auth(self) -> None:
        if is_production() and not verify_bearer(self.headers.get("Authorization"), admin=True):
            raise APIError(401, "生产模式下访问管理 API 需要 ADMIN_API_KEY")

    def do_GET(self) -> None:
        self.safe(self.handle_get)

    def do_POST(self) -> None:
        self.safe(self.handle_post)

    def do_PATCH(self) -> None:
        self.safe(self.handle_patch)

    def handle_get(self) -> None:
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        if path in {"/health", "/api/health"}:
            ready = chat_auth_ready()
            self.json_response({
                "status": "ok" if ready or not is_production() else "degraded",
                "ready": ready,
                "service": "实践小搭",
                "version": "1.0.0",
                "environment": "production" if is_production() else "development",
                "database": str(DB.path),
                # 写作模型只影响推送文案这一个能力，用不了会自动降级到要点清单，
                # 所以它不参与整体健康判定——但要能一眼看出配没配、配的是哪家。
                "writing_model": llm.status(),
                "time": now_iso(),
            }, 200 if ready or not is_production() else 503)
            return
        if path == "/v1/models":
            self.require_chat_auth()
            self.json_response(model_list())
            return
        if path.startswith("/api/"):
            self.require_admin_auth()
        if path == "/api/stats":
            self.json_response({"stats": DB.stats(), "activity": DB.recent_activity()})
            return
        if path == "/api/projects":
            projects = DB.list_projects(
                status=query.get("status", [""])[0],
                query=query.get("q", [""])[0],
                include_expired=query.get("include_expired", ["1"])[0] != "0",
            )
            self.json_response({"projects": projects, "count": len(projects)})
            return
        match = re.fullmatch(r"/api/projects/([^/]+)", path)
        if match:
            project = DB.get_project(match.group(1))
            if not project:
                raise APIError(404, "项目不存在")
            self.json_response({"project": project, "versions": DB.versions(project["id"])})
            return
        if path == "/api/review-queue":
            projects = DB.list_projects(status="needs_review")
            self.json_response({"projects": projects, "count": len(projects)})
            return
        if path == "/api/export":
            projects = DB.list_projects()
            body = json.dumps({"exported_at": now_iso(), "projects": projects}, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition", "attachment; filename=practice-xiaoda-projects.json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/collector/status":
            self.json_response({
                "configured": bool(os.getenv("WECHAT_COLLECTOR_PATH")),
                "credential_present": collector_credentials_present(),
                "message": "高成功率批量采集需配置 WECHAT_COLLECTOR_PATH 与 WECHAT_TOKEN/WECHAT_COOKIE（或采集器专用凭证文件）；接口永不返回凭据内容。",
            })
            return
        if path == "/api/developer/collector":
            self.json_response(SCHEDULER.status())
            return
        self.serve_static(path)

    def handle_post(self) -> None:
        path = urlparse(self.path).path
        if path == "/v1/chat/completions":
            self.require_chat_auth(rate_limit=True)
            payload = self.read_json()
            try:
                messages, stream, model = validate_chat_request(payload)
                max_tokens = resolve_max_tokens(payload)
            except ChatRequestError as exc:
                raise OpenAIAPIError(400, str(exc), param=exc.param, code=exc.code)
            result = CHAT.reply(messages)
            # 记录 max_tokens：正常对话本不该带它。若平台网关某天开始下发一个
            # 默认上限，回复会被静默截断，这条日志能让我们立刻看出来，
            # 而不是等用户反馈"话说一半"。
            # 健康自检每分钟打一次真实对话，用来确认"进程活着"之外还"答得出来"。
            # 但它不该写进活动日志——一天一千四百多条，真实用户的行为会被彻底淹没，
            # 而活动日志正是我们之后判断"大家都在问什么"的唯一依据。
            if self.headers.get("X-Health-Probe") != "1":
                note = {"intent": result.intent, "project_id": result.project_id}
                if max_tokens is not None:
                    note["max_tokens"] = max_tokens
                    if result.stream_factory is None:
                        note["truncated"] = truncate_to_tokens(result.content, max_tokens)[1]
                # 没接住的问题要留下原话，否则只知道"兜底率 12%"却不知道该补什么。
                # 只记未命中的这一类，且截断到 80 字——够看出意图，不留完整聊天记录。
                if result.intent in {"fallback", "writing_help", "about_practice"}:
                    latest_user = next(
                        (m["content"] for m in reversed(messages) if m["role"] == "user" and m["content"].strip()),
                        "",
                    )
                    note["asked"] = latest_user.strip()[:80]
                DB.log("chat", f"完成清小搭对话：{result.intent}", note)
            if stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache, no-transform")
                self.send_header("Connection", "close")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("X-Request-ID", self.request_id)
                self.end_headers()
                # 走大模型的回复用分片工厂，边生成边转发；规则类回复仍是整段字符串。
                source = result.stream_factory() if result.stream_factory else result.content
                # 规则类回复在几毫秒内就能把几十帧全写完再关连接。浏览器实测里
                # 有一半的会话「内容都显示出来了，但页面的忙碌状态一直不解除」，
                # 而且回答越长越容易触发（588 字 vs 289 字）——像是下游没跟上这种
                # 瞬间灌完就断开的节奏。给规则类回复加一点点节流，让流的形态接近
                # 真实生成；每帧 12ms，一段 600 字的回答也只多花 0.2 秒，用户感知不到。
                # 走模型的回复本来就是逐段到达的，不额外加。
                pace = 0.012 if result.stream_factory is None else 0.0
                for event in stream_events(messages, source, model, max_tokens=max_tokens):
                    self.wfile.write(event.encode("utf-8"))
                    self.wfile.flush()
                    if pace:
                        time.sleep(pace)
                # 把 data: [DONE] 真正推出去之后再关连接。写完立刻关闭时，
                # 结束帧可能和 FIN 挤在一起，下游先看到连接断开、后处理缓冲，
                # 就可能判定成异常中断而不是正常收尾。
                try:
                    self.wfile.flush()
                except (ConnectionResetError, BrokenPipeError):
                    pass
                time.sleep(0.05)
                self.close_connection = True
                return
            self.json_response(completion_payload(messages, result.resolve(), model, max_tokens=max_tokens))
            return
        self.require_admin_auth()
        payload = self.read_json()
        if path == "/api/ingest":
            self.ingest(payload)
            return
        if path == "/api/recommend":
            profile = payload.get("profile", payload)
            projects = DB.list_projects(include_expired=True)
            result = recommend_projects(projects, profile)
            DB.log("recommend", f"完成推荐：{len(result['eligible'])} 个正式匹配", {"profile": {k: v for k, v in profile.items() if k not in {"name"}}})
            self.json_response(result)
            return
        if path == "/api/generate":
            project_id = payload.get("project_id")
            project = DB.get_project(project_id)
            if not project:
                raise APIError(404, "请选择有效项目")
            result = generate_asset(project, payload.get("kind", "application"), payload.get("context", {}))
            DB.log("generate", f"已生成{payload.get('kind', 'application')}草稿", {"project_id": project_id})
            self.json_response(result)
            return
        if path == "/api/workspace/local-sites":
            project_id = str(payload.get("project_id") or "").strip()
            project = DB.get_project(project_id)
            if not project:
                raise APIError(404, "请选择有效项目")
            context = payload.get("context") or {}
            if not isinstance(context, dict):
                raise APIError(422, "地点推荐上下文必须是对象")
            self.json_response(recommend_local_sites(project, context))
            return
        if path == "/api/workspace/transport":
            project_id = str(payload.get("project_id") or "").strip()
            project = DB.get_project(project_id)
            if not project:
                raise APIError(404, "请选择有效项目")
            sites = payload.get("selected_sites") or []
            if not isinstance(sites, list) or not all(isinstance(item, dict) for item in sites):
                raise APIError(422, "交通查询的地点必须来自已选外联地点")
            location = project.get("location") or {}
            city = str(location.get("city") or location.get("detail") or location.get("province") or "").strip()
            result = query_routes(hotel=str(payload.get("hotel") or ""), sites=sites, city=city)
            DB.log("transport_lookup", "查询了路线工作台交通信息", {"project_id": project_id, "count": len(result.get("routes") or [])})
            self.json_response(result)
            return
        if path == "/api/feedback":
            feedback_id = DB.add_feedback(payload)
            self.json_response({"ok": True, "feedback_id": feedback_id}, 201)
            return
        if path == "/api/developer/collector":
            status = save_from_developer(payload)
            DB.log("collector_settings", "开发者更新了公众号采集设置", {"enabled": status["enabled"], "accounts": status["accounts"]})
            self.json_response(status)
            return
        if path == "/api/developer/collector/run":
            since = str(payload.get("since") or "").strip()
            if since and not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", since):
                raise APIError(422, "历史补全起始日期必须为 YYYY-MM-DD")
            raw_count = payload.get("count")
            count = None
            if raw_count not in {None, ""}:
                try:
                    count = int(raw_count)
                except (TypeError, ValueError):
                    raise APIError(422, "历史补全篇数必须是整数")
                if not 1 <= count <= 100:
                    raise APIError(422, "历史补全篇数范围为 1–100")
            raw_accounts = payload.get("accounts")
            accounts = None
            if raw_accounts is not None:
                if not isinstance(raw_accounts, list) or not all(isinstance(item, str) for item in raw_accounts):
                    raise APIError(422, "历史回补公众号必须是字符串列表")
                accounts = list(dict.fromkeys(item.strip() for item in raw_accounts if item.strip()))[:12]
                if not accounts:
                    raise APIError(422, "请至少提供一个历史回补公众号")
            started, message = SCHEDULER.trigger(since=since, count=count, accounts=accounts)
            self.json_response({"ok": started, "message": message, "status": SCHEDULER.status()}, 202 if started else 422)
            return
        if path == "/api/developer/collector/select":
            self.json_response(select_profile(str(payload.get("profile_id") or "")))
            return
        if path == "/api/developer/collector/delete":
            self.json_response(delete_profile(str(payload.get("profile_id") or "")))
            return
        raise APIError(404, "API 路径不存在")

    def ingest(self, payload: dict[str, Any]) -> None:
        input_type = payload.get("input_type", "copied_text")
        if input_type not in {"copied_text", "ocr_text", "admin_notice", "wechat_url"}:
            raise APIError(422, "不支持的导入类型")
        raw_text = str(payload.get("raw_text", "")).strip()
        source_url = str(payload.get("source_url", "")).strip()
        if not raw_text:
            if not source_url:
                raise APIError(422, "请粘贴通知正文、OCR 文本，或提供公众号链接")
            if input_type == "wechat_url":
                _, stored_cookie = collector_credentials()
                result = import_wechat_link(DB, payload, fallback_cookie=stored_cookie)
                self.json_response(result, 201 if result["status"] == "imported" else 202)
                return
            article_id = DB.insert_article({
                **payload,
                "input_type": input_type,
                "collector_status": "needs_text",
                "collector_warning": "仅保存链接，尚未获得文章全文",
            })
            DB.log("lead", "已保存待补全文的公众号线索", {"article_id": article_id})
            self.json_response({
                "status": "needs_text",
                "article_id": article_id,
                "action_required": "微信正文未提供。请复制正文或粘贴截图 OCR 文本后重新导入。",
                "truthfulness_note": "系统未将验证码页或链接元数据当作文章全文。",
            }, 202)
            return
        article_id = DB.insert_article({**payload, "collector_status": "success"})
        decision = candidate_decision({"title": str(payload.get("title") or ""), "content": raw_text})
        if not decision["candidate"]:
            removed = 0
            if decision["hard_excluded"]:
                removed = DB.delete_projects_by_source(source_url, note="手动导入识别为非招募内容")
            DB.log("lead", "已保存非招募来源审计，未进入机会库", {"article_id": article_id, "reasons": decision["reasons"], "removed": removed})
            self.json_response({
                "status": "not_opportunity", "article_id": article_id,
                "action_required": "内容已保存为来源审计，但无明确招募/报名行动信号，不会进入机会库。",
                "truthfulness_note": "行前预告、实践纪实和活动回顾不能作为可报名机会推荐。",
                "decision_reasons": decision["reasons"],
            }, 202)
            return
        project = extract_project(raw_text, {**payload, "input_type": input_type})
        project["article_id"] = article_id
        duplicate = DB.find_duplicate(project)
        merged = False
        if duplicate:
            project["id"] = duplicate["id"]
            project["created_at"] = duplicate.get("created_at", project["created_at"])
            project["risk_notes"] = list(dict.fromkeys(duplicate.get("risk_notes", []) + project.get("risk_notes", [])))
            merged = True
        project = DB.upsert_project(project, note="从新文章导入并合并" if merged else "首次导入")
        self.json_response({
            "status": "imported",
            "article_id": article_id,
            "project": project,
            "merged_duplicate": merged,
            "review_required": project["status"] == "needs_review",
        }, 201)

    def handle_patch(self) -> None:
        path = urlparse(self.path).path
        self.require_admin_auth()
        match = re.fullmatch(r"/api/projects/([^/]+)", path)
        if not match:
            raise APIError(404, "API 路径不存在")
        payload = self.read_json()
        project = DB.review_project(match.group(1), payload)
        self.json_response({"project": project, "message": "审核结果已保存"})

    def serve_static(self, path: str) -> None:
        if is_production() and os.getenv("PUBLIC_DASHBOARD", "false").lower() not in {"1", "true", "yes"}:
            raise APIError(404, "生产环境管理面板默认关闭；请使用清小搭对话接口或显式设置 PUBLIC_DASHBOARD=true")
        if path in {"", "/"}:
            target = STATIC / "index.html"
        else:
            target = (STATIC / path.lstrip("/")).resolve()
            if STATIC.resolve() not in target.parents:
                raise APIError(403, "禁止访问该路径")
        if not target.is_file():
            target = STATIC / "index.html" if "." not in path.rsplit("/", 1)[-1] else target
        if not target.is_file():
            raise APIError(404, "文件不存在")
        body = target.read_bytes()
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if mime.startswith("text/") or mime in {"application/javascript", "application/json"}:
            mime += "; charset=utf-8"
        self._headers(200, mime, len(body))
        self.wfile.write(body)


class PracticeHTTPServer(ThreadingHTTPServer):
    """把 listen backlog 从标准库默认的 5 提高到 128。

    压测发现：并发 60 时约四分之一的连接直接被内核 reset（ConnectionReset），
    与业务代码无关——只是等待队列太浅，握手阶段就被丢弃。评审期间如果一个班
    同时点开智能体，就会撞上这个。128 对应常见的 SOMAXCONN，内存代价可忽略。
    """

    request_queue_size = 128
    daemon_threads = True


def main() -> None:
    parser = argparse.ArgumentParser(description="运行实践小搭 MVP")
    parser.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8765")))
    parser.add_argument(
        "--grace-seconds",
        type=float,
        default=float(os.getenv("SHUTDOWN_GRACE_SECONDS", "3")),
        help="收到停止信号后，留给进行中的请求写完响应的时间",
    )
    args = parser.parse_args()
    server = PracticeHTTPServer((args.host, args.port), Handler)
    SCHEDULER.start()
    print(f"实践小搭已启动：http://{args.host}:{args.port}")
    print("按 Ctrl+C 停止服务")

    # 收到 SIGTERM（systemctl restart / stop 就是发这个）时先停止接受新连接，
    # 再给正在写响应的线程一小段时间收尾。默认的 daemon 线程会被直接砍断，
    # 用户那边就是"话说到一半没了"——重新部署时正好在对话的人会撞上。
    # 这里只做有界的优雅退出：不等待空闲的 keep-alive 连接，避免停机卡住。
    def request_stop(signum: int, _frame: Any) -> None:
        print(f"收到信号 {signum}，停止接受新连接，最多再等 {args.grace_seconds} 秒让进行中的请求收尾")
        threading.Thread(target=server.shutdown, daemon=True).start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, request_stop)
        except (ValueError, AttributeError):  # 非主线程或该平台没有此信号
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        deadline = time.monotonic() + max(0.0, args.grace_seconds)
        while time.monotonic() < deadline and threading.active_count() > 2:
            time.sleep(0.05)
        SCHEDULER.stop()
        server.server_close()


if __name__ == "__main__":
    main()
