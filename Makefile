.PHONY: run test check daily-wechat-update wewe-post-login wewe-scan-and-refresh

run:
	python3 server.py --port 8765

test:
	python3 -m unittest discover -s tests -v

check:
	python3 -m py_compile domain.py database.py chat_adapter.py security.py collector_settings.py collector_scheduler.py wechat_ingest.py wechat_image_ocr.py server.py scripts/daily_wechat_update.py scripts/werss_collector.py scripts/werss_daily_worker.py scripts/wewe_post_login.py
	node --check static/app.js
	python3 -m unittest discover -s tests -v

daily-wechat-update:
	python3 scripts/daily_wechat_update.py

wewe-post-login:
	python3 scripts/wewe_post_login.py

wewe-scan-and-refresh:
	./scripts/wewe_scan_and_refresh.sh
