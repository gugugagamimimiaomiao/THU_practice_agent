.PHONY: run test check daily-wechat-update

run:
	python3 server.py --port 8765

test:
	python3 -m unittest discover -s tests -v

check:
	python3 -m py_compile domain.py database.py chat_adapter.py security.py collector_settings.py collector_scheduler.py wechat_ingest.py wechat_image_ocr.py server.py scripts/daily_wechat_update.py scripts/werss_collector.py scripts/werss_daily_worker.py
	node --check static/app.js
	python3 -m unittest discover -s tests -v

daily-wechat-update:
	python3 scripts/daily_wechat_update.py
