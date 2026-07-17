from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class StaticSmokeTests(unittest.TestCase):
    def test_required_assets_and_workflows_exist(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        for page in ["dashboard", "projects", "ingest", "recommend", "workspace"]:
            self.assertIn(f'id="page-{page}"', html)
        for endpoint in ["/api/ingest", "/api/recommend", "/api/generate", "/api/feedback"]:
            self.assertIn(endpoint, js)
        for filename in ["Dockerfile", ".env.example", "compose.yaml", "compose.public.yaml", "Caddyfile", "railway.json", "render.yaml", "chat_adapter.py", "security.py", "collector_settings.py", "collector_scheduler.py", "wechat_ingest.py", "DEPLOYMENT.md"]:
            self.assertTrue((ROOT / filename).exists(), filename)
        self.assertTrue((ROOT / "scripts" / "daily_wechat_update.py").exists())
        server = (ROOT / "server.py").read_text(encoding="utf-8")
        for endpoint in ["/health", "/v1/models", "/v1/chat/completions"]:
            self.assertIn(endpoint, server)


if __name__ == "__main__":
    unittest.main()
