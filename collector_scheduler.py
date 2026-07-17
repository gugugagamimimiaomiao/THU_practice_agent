"""Small in-process scheduler for the local MVP developer panel."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from collector_settings import credentials, load_private, public_status, record_run


ROOT = Path(__file__).resolve().parent


class DailyCollectorScheduler:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._running = False
        self._process: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._progress: dict[str, object] = {"percent": 0, "stage": "idle", "label": "尚未启动采集"}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="practice-xiaoda-collector", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(20):
            settings = load_private()
            now = datetime.now().astimezone()
            if not settings.get("enabled") or settings.get("daily_time") != now.strftime("%H:%M"):
                continue
            if settings.get("last_scheduled_date") == now.date().isoformat():
                continue
            self.trigger(scheduled_date=now.date().isoformat())

    def trigger(self, *, scheduled_date: str = "", since: str = "", count: int | None = None, accounts: list[str] | None = None) -> tuple[bool, str]:
        settings = load_private()
        selected_accounts = accounts or list(settings.get("accounts") or [])
        token, cookie = credentials()
        if not settings.get("collector_path"):
            return False, "请先填写公众号采集器脚本路径。"
        if not token or not cookie:
            return False, "请先填写微信 Token 和 Cookie。"
        with self._lock:
            if self._running:
                return False, "采集任务正在运行，请稍后查看状态。"
            self._running = True
            self._progress = {"percent": 1, "stage": "starting", "label": "正在启动采集任务"}
        worker = threading.Thread(target=self._run, args=(settings, token, cookie, scheduled_date, since, count, selected_accounts), daemon=True)
        worker.start()
        return True, "采集任务已启动。"

    def _update_progress(self, event: dict) -> None:
        if event.get("event") != "progress":
            return
        try:
            percent = max(0, min(100, int(event.get("percent", 0))))
        except (TypeError, ValueError):
            return
        progress: dict[str, object] = {
            "percent": percent,
            "stage": str(event.get("stage") or "working")[:40],
            "label": str(event.get("label") or "正在采集")[:160],
        }
        for key in ("current", "total"):
            if isinstance(event.get(key), int):
                progress[key] = max(0, event[key])
        with self._lock:
            self._progress = progress

    @staticmethod
    def _final_result(output: str) -> dict:
        for line in reversed(output.splitlines()):
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and "ok" in parsed:
                return parsed
        return {}

    def _run(self, settings: dict, token: str, cookie: str, scheduled_date: str, since: str, count: int | None, accounts: list[str]) -> None:
        try:
            environment = {**os.environ, "WECHAT_TOKEN": token, "WECHAT_COOKIE": cookie, "WECHAT_COLLECTOR_PATH": settings["collector_path"]}
            if settings.get("collector_python"):
                environment["WECHAT_COLLECTOR_PYTHON"] = settings["collector_python"]
            command = [sys.executable, str(ROOT / "scripts" / "daily_wechat_update.py"), "--collector", settings["collector_path"], "--accounts", *accounts]
            if since:
                command.extend(["--since", since])
            if count and count > 0:
                command.extend(["--count", str(min(count, 100))])
            with self._lock:
                self._process = subprocess.Popen(
                    command, cwd=ROOT, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
                )
                process = self._process
            lines: queue.Queue[tuple[str, str | None]] = queue.Queue()
            stdout_lines: list[str] = []
            stderr_lines: list[str] = []

            def drain(stream: object, name: str) -> None:
                try:
                    for line in stream:  # type: ignore[union-attr]
                        lines.put((name, line.rstrip("\n")))
                finally:
                    lines.put((name, None))

            threading.Thread(target=drain, args=(process.stdout, "stdout"), daemon=True).start()
            threading.Thread(target=drain, args=(process.stderr, "stderr"), daemon=True).start()
            open_streams = {"stdout", "stderr"}
            deadline = time.monotonic() + 900
            timed_out = False
            while open_streams:
                if time.monotonic() >= deadline and process.poll() is None:
                    process.kill()
                    timed_out = True
                try:
                    source, line = lines.get(timeout=0.25)
                except queue.Empty:
                    continue
                if line is None:
                    open_streams.discard(source)
                    continue
                if source == "stdout":
                    stdout_lines.append(line)
                    try:
                        self._update_progress(json.loads(line))
                    except json.JSONDecodeError:
                        pass
                else:
                    stderr_lines.append(line)
            return_code = 124 if timed_out else process.wait()
            output = "\n".join(stdout_lines) or "\n".join(stderr_lines) or "采集任务结束，无额外输出"
            parsed = self._final_result(output)
            if parsed.get("ok"):
                ocr = parsed.get("ocr") if isinstance(parsed.get("ocr"), dict) else {}
                summary = (
                    f"扫描 {parsed.get('seen', 0)} 篇 · OCR 审计 {ocr.get('articles_processed', 0)} 篇 · "
                    f"候选 {parsed.get('candidate', 0)} 篇 · 非候选 {parsed.get('non_candidate', 0)} 篇 · "
                    f"新增 {parsed.get('imported', 0)} 篇 · 合并 {parsed.get('merged', 0)} 篇 · "
                    f"缺正文跳过 {parsed.get('skipped_no_content', 0)} 篇 · "
                    f"配图下载 {ocr.get('downloaded', 0)}/{ocr.get('attempted', 0)} 张 · "
                    f"已处理 {ocr.get('processed', 0)}/{ocr.get('attempted', 0)} 张 · "
                    f"识别到文字 {ocr.get('completed', 0)} 张"
                )
                with self._lock:
                    self._progress = {"percent": 100, "stage": "completed", "label": f"采集完成：{summary}"}
            else:
                summary = str(parsed.get("error") or f"每日采集退出码 {return_code}")
                with self._lock:
                    self._progress = {"percent": 0, "stage": "failed", "label": summary}
            record_run(summary, scheduled_date=scheduled_date)
        except Exception as exc:  # pragma: no cover - background protection
            summary = f"每日采集异常：{type(exc).__name__}"
            with self._lock:
                self._progress = {"percent": 0, "stage": "failed", "label": summary}
            record_run(summary, scheduled_date=scheduled_date)
        finally:
            with self._lock:
                self._process = None
                self._running = False

    def status(self) -> dict:
        with self._lock:
            # If a child has already exited but its worker is delayed while
            # saving status, never leave the UI stuck in “running”.
            if self._process is not None and self._process.poll() is not None:
                self._running = False
            running = self._running
            progress = dict(self._progress)
        return {**public_status(), "running": running, "progress": progress}

    def stop(self) -> None:
        self._stop.set()
