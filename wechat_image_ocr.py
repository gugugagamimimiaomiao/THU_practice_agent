"""Optional, bounded OCR for WeChat article images.

The module is intentionally conservative: it downloads only official WeChat
image hosts, has strict size/time bounds, and returns empty text when the OCR
runtime is absent.  The caller must then leave the card in review rather than
inventing values from an image.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
# Batch ingestion preserves at most 20 source images.  OCR must cover that
# complete source set; silently taking only the first eight can hide a date or
# location printed on a later poster.
# A normal WeChat article can contain a cover, posters, QR codes and several
# text posters.  The upper bound is configurable for production safety, but
# the default covers a complete rich article rather than silently stopping at 8.
MAX_IMAGES = max(1, int(os.getenv("WECHAT_OCR_MAX_IMAGES", "40")))
MAX_IMAGE_BYTES = 8 * 1024 * 1024
OCR_TIMEOUT_SECONDS = 15
DOWNLOAD_ATTEMPTS = 1
OCR_WORKERS = max(1, min(3, int(os.getenv("WECHAT_OCR_WORKERS", "2"))))
ALLOWED_IMAGE_HOSTS = {"mmbiz.qpic.cn", "mmbiz.qlogo.cn"}


@dataclass
class OCRResult:
    text: str
    attempted: int
    completed: int
    failed: int
    runtime_available: bool
    downloaded: int = 0
    processed: int = 0
    failures: list[str] = field(default_factory=list)
    details: list[dict[str, object]] = field(default_factory=list)


def is_allowed_image_url(url: str) -> bool:
    parsed = urlparse(url.strip())
    return parsed.scheme == "https" and parsed.hostname in ALLOWED_IMAGE_HOSTS


def _download_once(url: str, destination: Path, cookie: str) -> tuple[bool, str]:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; PracticeXiaoda/1.0)",
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Referer": "https://mp.weixin.qq.com/",
    }
    if cookie:
        headers["Cookie"] = cookie
    try:
        with urlopen(Request(url, headers=headers), timeout=12) as response:
            data = response.read(MAX_IMAGE_BYTES + 1)
    except HTTPError as exc:
        return False, f"下载返回 HTTP {exc.code}"
    except URLError:
        return False, "图片下载网络错误"
    except (OSError, TimeoutError):
        return False, "图片下载超时或被中断"
    if not data:
        return False, "图片下载为空"
    if len(data) > MAX_IMAGE_BYTES:
        return False, "图片超过 8MB 限制"
    destination.write_bytes(data)
    return True, ""


def _download_with_curl(url: str, destination: Path) -> tuple[bool, str]:
    """Fallback for environments where Python's HTTPS stack cannot reach mmbiz.

    Public mmbiz assets do not need a cookie.  In particular, do not pass a
    cookie here: command-line arguments are observable on some hosts.
    """
    curl = shutil.which("curl")
    if not curl:
        return False, "Python 图片下载失败，且系统没有 curl 兜底"
    try:
        run = subprocess.run(
            [
                curl, "--fail", "--location", "--silent", "--show-error", "--http1.1",
                "--connect-timeout", "8", "--max-time", "15", "--retry", "1", "--retry-all-errors", "--retry-delay", "1",
                "--user-agent", "Mozilla/5.0 (compatible; PracticeXiaoda/1.0)",
                "--referer", "https://mp.weixin.qq.com/", "--output", str(destination), url,
            ],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "curl 图片下载超时"
    except OSError:
        return False, "无法启动 curl 图片下载"
    if run.returncode != 0:
        return False, f"curl 图片下载失败（退出码 {run.returncode}）"
    try:
        size = destination.stat().st_size
    except OSError:
        return False, "curl 未写入图片文件"
    if not size:
        return False, "curl 下载图片为空"
    if size > MAX_IMAGE_BYTES:
        destination.unlink(missing_ok=True)
        return False, "图片超过 8MB 限制"
    return True, ""


def _download_with_requests_runtime(url: str, destination: Path) -> tuple[bool, str]:
    """Use the collector virtualenv's requests stack as a TLS fallback."""
    runtime = os.getenv("WECHAT_OCR_HTTP_PYTHON") or os.getenv("WECHAT_COLLECTOR_PYTHON", "")
    helper = ROOT / "scripts" / "wechat_image_fetch.py"
    if not runtime or not helper.is_file():
        return False, "未配置 requests 下载器"
    try:
        run = subprocess.run([runtime, str(helper), url, str(destination)], capture_output=True, text=True, timeout=28, check=False)
    except subprocess.TimeoutExpired:
        return False, "requests 图片下载超时"
    except OSError:
        return False, "无法启动 requests 图片下载器"
    if run.returncode != 0:
        return False, f"requests 图片下载失败（退出码 {run.returncode}）"
    try:
        size = destination.stat().st_size
    except OSError:
        return False, "requests 未写入图片文件"
    if not size:
        return False, "requests 下载图片为空"
    if size > MAX_IMAGE_BYTES:
        destination.unlink(missing_ok=True)
        return False, "图片超过 8MB 限制"
    return True, ""


def _download(url: str, destination: Path, cookie: str) -> tuple[bool, str]:
    """Retry public WeChat image URLs, including a cookie-free fallback.

    Article cookies are useful for some protected images but can be rejected by
    a public mmbiz CDN.  Retrying once without them improves reliability while
    never exposing the credential in output.
    """
    # mmbiz article images are normally public.  curl is deliberately first:
    # it is more tolerant of the CDN's TLS/proxy behavior than Python's
    # urllib in some desktop and container environments.
    downloaded, last_reason = _download_with_curl(url, destination)
    if downloaded:
        return True, ""
    reasons = [last_reason]
    downloaded, reason = _download_with_requests_runtime(url, destination)
    if downloaded:
        return True, ""
    reasons.append(reason)
    credential_variants = [cookie, ""] if cookie else [""]
    for current_cookie in dict.fromkeys(credential_variants):
        for _ in range(DOWNLOAD_ATTEMPTS):
            downloaded, reason = _download_once(url, destination, current_cookie)
            if downloaded:
                return True, ""
            last_reason = reason
            time.sleep(0.2)
    reasons.append(last_reason)
    return False, "；".join(dict.fromkeys(reasons))


def _ocr_image(command: str, image_path: Path, language: str) -> tuple[str, str]:
    """Try poster and sparse-text layouts, keeping the richer result."""
    texts: list[str] = []
    last_reason = "OCR 未识别到文字"
    for psm in ("6", "11"):
        try:
            run = subprocess.run(
                [command, str(image_path), "stdout", "-l", language, "--psm", psm],
                capture_output=True, text=True, timeout=OCR_TIMEOUT_SECONDS, check=False,
            )
        except subprocess.TimeoutExpired:
            last_reason = "OCR 超时"
            continue
        except OSError:
            return "", "无法启动 OCR"
        text = (run.stdout or "").strip()
        if text:
            texts.append(text)
            if len(text) >= 20:
                break
        elif run.returncode != 0:
            last_reason = "OCR 运行失败"
    return max(texts, key=len) if texts else "", last_reason


def _ocr_one(index: int, url: str, folder: Path, command: str, language: str, cookie: str) -> tuple[int, str, dict[str, object], str | None]:
    image_path = folder / f"article-image-{index}.img"
    downloaded, reason = _download(url, image_path, cookie)
    if not downloaded:
        return index, "", {"index": index + 1, "status": "download_failed", "reason": reason}, reason
    text, reason = _ocr_image(command, image_path, language)
    if text:
        return index, text, {"index": index + 1, "status": "text_found", "characters": len(text)}, None
    return index, "", {"index": index + 1, "status": "ocr_empty_or_failed", "reason": reason}, reason


def ocr_wechat_images(urls: list[str], *, cookie: str = "") -> OCRResult:
    command = os.getenv("TESSERACT_CMD", "tesseract")
    if not shutil.which(command):
        return OCRResult("", 0, 0, 0, False)
    language = os.getenv("WECHAT_OCR_LANG", "chi_sim+eng")
    allowed = list(dict.fromkeys(url for url in urls if is_allowed_image_url(url)))[:MAX_IMAGES]
    if not allowed:
        return OCRResult("", 0, 0, 0, True)
    texts: list[tuple[int, str]] = []
    failures: list[str] = []
    details: list[dict[str, object]] = []
    completed = 0
    downloaded_count = 0
    processed = 0
    with tempfile.TemporaryDirectory(prefix="practice-xiaoda-ocr-") as folder:
        with ThreadPoolExecutor(max_workers=min(OCR_WORKERS, len(allowed))) as pool:
            futures = [pool.submit(_ocr_one, index, url, Path(folder), command, language, cookie) for index, url in enumerate(allowed)]
            for future in as_completed(futures):
                index, text, detail, reason = future.result()
                details.append(detail)
                if detail["status"] != "download_failed":
                    downloaded_count += 1
                    processed += 1
                if text:
                    completed += 1
                    texts.append((index, text))
                elif reason:
                    failures.append(f"配图 {index + 1}：{reason}")
    details.sort(key=lambda item: int(item["index"]))
    return OCRResult(
        "\n".join(text for _, text in sorted(texts)), len(allowed), completed, len(allowed) - completed, True,
        downloaded_count, processed, failures, details,
    )
