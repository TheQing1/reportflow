#!/usr/bin/env bash
# 一键启动：后端 (8000) + 前端 (5173)。
#
#   ./scripts/start.sh              # 起两个服务
#   ./scripts/start.sh --real       # 先用真实语料初始化知识库再起
#   ./scripts/start.sh --live       # 不用回放缓存，每次真调模型
#
# 为什么默认用回放缓存（LLM_MODE=auto）：
# 调试界面时同一个 prompt 会被反复发送，直连模型既慢又花钱，而且每次输出不同，
# 分不清「我改坏了」还是「模型这次就这么写」。命中缓存则毫秒级、结果确定、¥0。
# 要验收真实生成效果时再传 --live。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PYTHON:-python}"
MODE="auto"
INIT_REAL=0

for arg in "$@"; do
  case "$arg" in
    --real) INIT_REAL=1 ;;
    --live) MODE="live" ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "未知参数：$arg（可选 --real / --live）" >&2; exit 1 ;;
  esac
done

cd "$ROOT"

# 依赖检查：缺什么说清楚，不要让用户面对一段 traceback
if ! "$PY" -c "import fastapi, uvicorn" 2>/dev/null; then
  echo "缺少后端依赖，请先安装："
  echo "  cd backend && pip install -e ." >&2
  exit 1
fi
if [ ! -d frontend/node_modules ]; then
  echo "缺少前端依赖，请先安装："
  echo "  cd frontend && npm install" >&2
  exit 1
fi

# 知识库为空时用自带语料兜底，否则检索全空、正文全是「资料未涉及」。
# 这里刻意**不吞输出**：用户需要知道当前喂给模型的是虚构数据还是真实数据，
# 否则看到一份数字编得很整齐的研报，会误以为那是真的。
if [ "$INIT_REAL" = "1" ]; then
  (cd backend && "$PY" -m app.cli corpus init --real --docs "$ROOT/data/docs" || true)
else
  (cd backend && "$PY" -m app.cli corpus init --docs "$ROOT/data/docs" || true)
fi
echo
echo "切到真实语料：./scripts/start.sh --real"
echo "切回合成语料（阶段三评测基线）：cd backend && python -m scripts.seed_corpus --clean"
echo

echo "后端 http://127.0.0.1:8000   前端 http://localhost:5173   (LLM_MODE=$MODE)"
echo "Ctrl-C 同时停止两个服务"
echo

# trap 保证 Ctrl-C 时两个进程一起收掉，否则后端会留在后台占着 8000
cleanup() { jobs -pr | xargs -r kill 2>/dev/null || true; }
trap cleanup EXIT INT TERM

(
  cd backend
  LLM_MODE="$MODE" "$PY" -m app.api --port 8000
) &
(
  cd frontend
  npm run dev
) &

wait
