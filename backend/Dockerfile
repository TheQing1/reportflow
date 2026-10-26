# ReportFlow 后端镜像
#
# 关键约束：容器内必须保持与仓库一致的目录层级。
# app/core/config.py 用 `Path(__file__).parents[3]` 推导 PROJECT_ROOT，
# 进而定位 data/、sample-corpus/、llm_cache/。
# 若把 app/ 单独拷到根目录，PROJECT_ROOT 会退化成 "/"，语料与数据目录全部错位。
#
# build context 必须是仓库根目录：
#   docker build -f backend/Dockerfile .

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # 容器内数据统一落在 /app/data，由 compose 挂卷持久化
    DATABASE_URL=sqlite:////app/data/reportflow.db \
    DOCS_DIR=/app/data/docs \
    LLM_CACHE_DIR=/app/data/llm_cache

WORKDIR /app

# 先单独拷依赖清单：改代码不会让 pip 缓存失效，重建更快
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend/ ./backend/
# 两套自带语料随镜像分发：空库启动时自动用 sample-corpus 兜底
COPY sample-corpus/ ./sample-corpus/
COPY real-corpus/ ./real-corpus/

RUN mkdir -p /app/data/docs /app/data/llm_cache /app/data/output

EXPOSE 8000

WORKDIR /app/backend

# 不用 curl：slim 镜像没装，用标准库探测 /api/health
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"

# create_app 是工厂函数（依赖注入 settings / runtime），必须加 --factory
CMD ["python", "-m", "uvicorn", "app.api.app:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000"]
