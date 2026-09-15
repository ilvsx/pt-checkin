# PT 自动签到系统 —— 容器镜像
# 仅依赖 Python 标准库；额外安装 cryptography 以获得 Cookie 加密存储能力。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PTCHECKIN_DATA=/data \
    PTCHECKIN_HOST=0.0.0.0 \
    PTCHECKIN_PORT=8787 \
    TZ=Asia/Shanghai

WORKDIR /app

LABEL org.opencontainers.image.title="pt-checkin" \
      org.opencontainers.image.description="PT 站点每日自动签到系统（HHClub / NexusPHP）：定时签到、记录查询、精确判定当日状态" \
      org.opencontainers.image.source="https://github.com/ilvsx/pt-checkin" \
      org.opencontainers.image.url="https://github.com/ilvsx/pt-checkin"

# 可选依赖：失败也不影响构建（程序会自动退化为仅混淆存储）
RUN pip install --no-cache-dir cryptography || true

COPY requirements.txt run.py ./
COPY ptcheckin ./ptcheckin

RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 8787

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/api/health', timeout=4).status==200 else 1)"

CMD ["python3", "run.py", "serve"]
