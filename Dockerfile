# 应急无线电演练通联日志互证服务
FROM python:3.11-slim

# 不生成 .pyc、日志直出
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    QSO_HOST=0.0.0.0 \
    QSO_PORT=8080 \
    QSO_DB_PATH=/data/qso.db

WORKDIR /app

# 先装依赖（利用层缓存）
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝应用代码
COPY app ./app
COPY scripts ./scripts

# 持久化数据目录（SQLite 数据库，含 WAL/SHM 文件）
RUN mkdir -p /data
VOLUME ["/data"]

# 容器启动时幂等初始化数据库（建表/WAL），再启动服务
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('QSO_PORT','8080')+'/health', timeout=3)" || exit 1

CMD ["sh", "-c", "python -m scripts.init_db && uvicorn app.main:app --host ${QSO_HOST} --port ${QSO_PORT}"]
