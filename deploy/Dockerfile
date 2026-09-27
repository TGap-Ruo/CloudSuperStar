FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    TZ=Asia/Shanghai \
    CHAOXING_DATA_DIR=/data

WORKDIR /app

# 先装依赖，利用镜像层缓存
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app
RUN rm -rf /app/.venv /app/data /app/_ref

VOLUME ["/data", "/config"]

# 默认启动定时调度；也可覆盖为：
#   docker run ... chaoxing-server run --all
ENTRYPOINT ["python", "-m", "server.cli"]
CMD ["--config", "/config/config.yaml", "serve"]
