FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000 \
    HOST=0.0.0.0 \
    PRACTICE_XIAODA_ENV=production \
    PRACTICE_XIAODA_DB=/data/practice_xiaoda.db \
    PUBLIC_DASHBOARD=false

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tesseract-ocr tesseract-ocr-chi-sim \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir requests

RUN addgroup --system app && adduser --system --ingroup app app \
    && mkdir -p /data \
    && chown -R app:app /data /app

COPY --chown=app:app . /app

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('PORT', '8000') + '/health', timeout=4)"

# -u 关掉输出缓冲。虽然上面已经设了 PYTHONUNBUFFERED=1，但这个变量可以被
# 运行时的 -e 覆盖掉，而排查问题时最不该丢的就是日志——实际遇到过日志停在
# 一个多小时之前、误以为服务已经挂了的情况。两处都写上，成本为零。
CMD ["python3", "-u", "server.py"]
