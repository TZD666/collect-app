# 收集App — 容器镜像
# 基础镜像：python:3.12-slim（轻量，无多余系统包）
FROM python:3.12-slim

WORKDIR /app

# ── 第一层：安装依赖（利用 Docker 层缓存；代码改动不触发重新 pip install）──
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# ── 第二层：复制应用代码（明确列目录，不用 COPY . 避免把 .env/data 等打进镜像）──
COPY app/ ./app/
COPY web/ ./web/
COPY run.py ./

# ── 运行时默认环境变量 ──
# 多用户模式（服务器部署推荐）；单机本地用 docker compose 覆盖或直接改 .env
ENV AUTH_MODE=multi
# 容器内监听全部网口，宿主机通过端口映射访问
ENV HOST=0.0.0.0

# 注意：AGENT_TASKS 存在内存中，必须单 worker 运行。
# run.py → uvicorn 默认 workers=1，不要在 compose 里加 --workers 参数。
EXPOSE 8770

CMD ["python", "run.py"]
