# Shakedown runner 镜像（berth B7 依赖）
#
# 这个镜像是「执行器运行环境」：pytest + httpx + playwright。
# berth 的 `POST /scripts/run {"image": "runner"}` 会用到它。
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    "pytest>=8.0" \
    "httpx>=0.27" \
    "psycopg[binary]>=3.1" \
    "playwright>=1.44"

WORKDIR /workspace

ENTRYPOINT ["/bin/bash"]
