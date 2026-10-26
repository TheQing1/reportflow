# ReportFlow 前端镜像：构建阶段产出静态文件，运行阶段用 nginx 托管并反代 /api
# build context 必须是仓库根目录：
#   docker build -f frontend/Dockerfile .

FROM node:22-alpine AS build

WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
# npm ci：严格按 lock 安装，保证与本地/CI 的构建结果一致
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM nginx:alpine

COPY --from=build /build/dist /usr/share/nginx/html
# 覆盖默认配置：SPA 路由回退 + /api 反代到后端（含 SSE 必需的关缓冲设置）
COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
