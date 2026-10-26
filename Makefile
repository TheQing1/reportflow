# ReportFlow 常用命令入口
#
# Windows 用户若没有 make，可用 scripts/start.bat（启动）或直接看下方命令；
# 本文件主要用于 Linux / macOS / CI，以及让人一眼看清项目该怎么用。
#
#   make help       查看全部命令

.DEFAULT_GOAL := help
.PHONY: help install install-dev test test-cov lint fmt dev docker-up docker-down corpus-real corpus-sample clean

help: ## 显示帮助
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## 安装后端运行时依赖
	cd backend && pip install -r requirements.txt

install-dev: ## 安装后端开发依赖 + 前端依赖
	cd backend && pip install -r requirements-dev.txt
	cd frontend && npm install

test: ## 运行后端测试
	cd backend && python -m pytest -q

test-cov: ## 运行测试并输出覆盖率
	cd backend && python -m pytest -q --cov=app --cov-report=term-missing

lint: ## 静态检查
	cd backend && ruff check .

fmt: ## 自动修复 + 格式化
	cd backend && ruff check --fix . && ruff format .

dev: ## 本地启动全栈（后端 8000 + 前端 5173）
	./scripts/start.sh

docker-up: ## Docker 启动全栈
	docker compose up --build

docker-down: ## 停止并移除容器
	docker compose down

corpus-real: ## 知识库切换为真实语料（替换，非叠加）
	cd backend && python -m app.cli corpus init --real

corpus-sample: ## 知识库切回合成语料（评测基线）
	cd backend && python scripts.seed_corpus --clean

clean: ## 清理缓存与构建产物（不动 data/ 里的知识库与数据库）
	rm -rf backend/.pytest_cache backend/.ruff_cache backend/htmlcov backend/.coverage
	rm -rf frontend/dist
	find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
