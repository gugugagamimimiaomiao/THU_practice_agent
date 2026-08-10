"""可选的外部大模型接入。

设计原则只有一条，但很硬：**模型只用来"写"，绝不用来"查"。**

项目推荐、报名截止、参与资格、报销条件这些事实，一律走规则 + SQLite，
保证零幻觉、每个字段都能回查原文——那是这个产品的立身之本。模型只负责
把已经核验过的事实组织成通顺的文字（推送文案、报告修改、外联信函）。

同样重要的是降级：模型不可用（没配 Key、超时、限流、欠费）时必须回落到
原来的模板输出，而不是把外部服务的故障变成用户看到的报错。

依赖仍然是零：DeepSeek 兼容 OpenAI 协议，用标准库 urllib 直接调即可，
不引入 openai 包。
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class LLMUnavailable(RuntimeError):
    """模型这次用不了。调用方应当回退到模板输出，不要把它抛给用户。"""


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def api_key() -> str:
    # DEEPSEEK_API_KEY 优先，LLM_API_KEY 作为换厂商时的通用名。
    return _env("DEEPSEEK_API_KEY") or _env("LLM_API_KEY")


def base_url() -> str:
    return _env("LLM_BASE_URL", "https://api.deepseek.com").rstrip("/")


def model_name() -> str:
    return _env("LLM_MODEL", "deepseek-chat")


def timeout_seconds() -> float:
    try:
        return max(5.0, float(_env("LLM_TIMEOUT_SECONDS", "45")))
    except ValueError:
        return 45.0


def is_enabled() -> bool:
    return bool(api_key())


def status() -> dict[str, Any]:
    """给 /api/health 和开发者页用，不回显密钥。"""
    return {
        "enabled": is_enabled(),
        "base_url": base_url(),
        "model": model_name(),
        "timeout_seconds": timeout_seconds(),
    }


def _request(payload: dict[str, Any], *, stream: bool):
    if not is_enabled():
        raise LLMUnavailable("未配置模型 API Key")
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        f"{base_url()}/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key()}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
        },
        method="POST",
    )
    try:
        return urlopen(request, timeout=timeout_seconds())
    except HTTPError as exc:  # 401 / 402 / 429 / 5xx
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:  # pragma: no cover - 读不出来就算了
            pass
        raise LLMUnavailable(f"模型返回 HTTP {exc.code}：{detail}") from exc
    except URLError as exc:
        raise LLMUnavailable(f"连接模型服务失败：{exc.reason}") from exc
    except Exception as exc:  # 超时等
        raise LLMUnavailable(f"调用模型失败：{type(exc).__name__}") from exc


def complete(system_prompt: str, user_prompt: str, *, temperature: float = 0.7) -> str:
    """一次性拿到完整回复。"""
    payload = {
        "model": model_name(),
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    with _request(payload, stream=False) as response:
        raw = response.read().decode("utf-8", "replace")
    try:
        data = json.loads(raw)
        return data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError) as exc:
        raise LLMUnavailable("模型返回结构无法解析") from exc


def stream(system_prompt: str, user_prompt: str, *, temperature: float = 0.7) -> Iterator[str]:
    """逐段产出。

    首个分片拿到之前的等待时间才是用户真正感知到的"慢"，所以能流式就流式：
    生成一段几百字的推送，非流式要等十几秒白屏，流式一两秒就开始出字。
    """
    payload = {
        "model": model_name(),
        "temperature": temperature,
        "stream": True,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    response = _request(payload, stream=True)
    produced = False
    try:
        for line in response:
            text = line.decode("utf-8", "replace").strip()
            if not text.startswith("data:"):
                continue
            chunk = text[len("data:"):].strip()
            if chunk == "[DONE]":
                break
            try:
                delta = json.loads(chunk)["choices"][0].get("delta", {})
            except (ValueError, KeyError, IndexError):
                continue
            piece = delta.get("content") or ""
            if piece:
                produced = True
                yield piece
    finally:
        response.close()
    if not produced:
        raise LLMUnavailable("模型没有返回任何内容")


def ping() -> tuple[bool, str]:
    """给运维和自检用：真的打一次最小请求，返回 (是否可用, 说明)。"""
    if not is_enabled():
        return False, "未配置 API Key"
    started = time.perf_counter()
    try:
        text = complete("你是一个测试探针。", "回复两个字：可用", temperature=0)
    except LLMUnavailable as exc:
        return False, str(exc)
    elapsed = (time.perf_counter() - started) * 1000
    return True, f"可用，往返 {elapsed:.0f}ms，返回 {text.strip()[:20]!r}"
