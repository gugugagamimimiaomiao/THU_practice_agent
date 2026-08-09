import json
import os
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

# 开发机上常见的本地代理（Clash / v2ray 等）会被 urllib 自动读取，
# 连发往 127.0.0.1 的测试请求也会被转发出去并返回 502。
# 这里显式使用空代理的 opener，让测试只依赖本进程启动的回环服务。
_DIRECT = build_opener(ProxyHandler({}))

import server as app_server
from chat_adapter import PracticeChatAdapter
from database import Database


class ServerProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_env = dict(os.environ)
        os.environ["PRACTICE_XIAODA_ENV"] = "production"
        os.environ["XIAODA_API_KEY"] = "test-secret"
        os.environ["ADMIN_API_KEY"] = "admin-secret"
        os.environ["RATE_LIMIT_PER_MINUTE"] = "1000"
        cls.tempdir = tempfile.TemporaryDirectory()
        app_server.DB = Database(os.path.join(cls.tempdir.name, "protocol.db"))
        app_server.CHAT = PracticeChatAdapter(app_server.DB)
        try:
            cls.httpd = app_server.ThreadingHTTPServer(("127.0.0.1", 0), app_server.Handler)
        except PermissionError as exc:
            cls.tempdir.cleanup()
            os.environ.clear()
            os.environ.update(cls.old_env)
            raise unittest.SkipTest(f"environment forbids loopback test server: {exc}")
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=3)
        cls.tempdir.cleanup()
        os.environ.clear()
        os.environ.update(cls.old_env)

    def call(self, path, *, method="GET", payload=None, key="test-secret"):
        headers = {}
        if key is not None:
            headers["Authorization"] = f"Bearer {key}"
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method, headers=headers)
        started = time.monotonic()
        try:
            response = _DIRECT.open(request, timeout=5)
            body = response.read()
            return response.status, dict(response.headers), body, time.monotonic() - started
        except HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read(), time.monotonic() - started

    def test_health_and_models(self):
        status, _, body, elapsed = self.call("/health", key=None)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "ok")
        self.assertLess(elapsed, 5)

        status, _, body, elapsed = self.call("/v1/models")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["object"], "list")
        self.assertLess(elapsed, 5)

        status, _, body, _ = self.call("/v1/models", key="wrong")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"]["type"], "authentication_error")

    def test_non_streaming_minimal_chat(self):
        status, headers, body, elapsed = self.call("/v1/chat/completions", method="POST", payload={
            "max_tokens": 1,
            "model": None,
            "messages": [
                {"role": "system", "content": "你是实践助手"},
                {"role": "user", "content": "你好"},
            ],
        })
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("application/json"))
        parsed = json.loads(body)
        self.assertIsInstance(parsed["choices"][0]["message"]["content"], str)
        self.assertEqual(parsed["choices"][0]["finish_reason"], "stop")
        self.assertEqual(set(parsed["usage"]), {"prompt_tokens", "completion_tokens", "total_tokens"})
        self.assertLess(elapsed, 5)

    def test_invalid_stream_missing_messages_and_image_return_400(self):
        cases = [
            {"stream": "false", "messages": [{"role": "user", "content": "你好"}]},
            {"stream": False},
            {"messages": [{"role": "user", "content": [
                {"type": "text", "text": "识别"},
                {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}},
            ]}]},
        ]
        for payload in cases:
            status, _, body, _ = self.call("/v1/chat/completions", method="POST", payload=payload)
            self.assertEqual(status, 400, body.decode("utf-8"))
            self.assertIn("error", json.loads(body))

    def test_streaming_sse_is_complete_and_ordered(self):
        status, headers, body, elapsed = self.call("/v1/chat/completions", method="POST", payload={
            "stream": True,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "你好"}],
        })
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/event-stream"))
        self.assertLess(elapsed, 5)
        text = body.decode("utf-8")
        blocks = [block for block in text.split("\n\n") if block]
        self.assertEqual(blocks[-1], "data: [DONE]")
        frames = [json.loads(block[len("data: "):]) for block in blocks[:-1]]
        self.assertEqual(frames[0]["choices"][0]["delta"], {"role": "assistant"})
        role_frames = [frame for frame in frames if "role" in frame["choices"][0]["delta"]]
        stop_frames = [frame for frame in frames if frame["choices"][0]["finish_reason"] is not None]
        self.assertEqual(len(role_frames), 1)
        self.assertEqual(len(stop_frames), 1)
        self.assertEqual(stop_frames[0]["choices"][0]["finish_reason"], "stop")
        self.assertEqual(set(stop_frames[0]["usage"]), {"prompt_tokens", "completion_tokens", "total_tokens"})

    def test_production_requires_chat_key_and_admin_key(self):
        original_chat = os.environ.pop("XIAODA_API_KEY")
        try:
            status, _, body, _ = self.call("/v1/models")
            self.assertEqual(status, 503)
            self.assertEqual(json.loads(body)["error"]["code"], "api_key_not_configured")
        finally:
            os.environ["XIAODA_API_KEY"] = original_chat

        status, _, _, _ = self.call("/api/projects", key="test-secret")
        self.assertEqual(status, 401)
        status, _, body, _ = self.call("/api/projects", key="admin-secret")
        self.assertEqual(status, 200)
        self.assertIn("projects", json.loads(body))


if __name__ == "__main__":
    unittest.main()
