# 片刻修AI工具广场原型

这是一个轻量版工具平台骨架，目标是先把“广场入口、独立工具 URL、统一积分计价、文件上传、任务队列、供应商回调、充值流水”的产品底座跑起来。

## 启动

```bash
npm run dev
```

开发环境会同时启动：

```text
前端 Vite: http://localhost:5173
后端 API:  http://localhost:8010
Redis/RQ:  后台任务 worker
```

示例工具 URL：

```text
http://localhost:5173/tools/video/remove-watermark
http://localhost:5173/tools/video/remove-subtitle
http://localhost:5173/tools/video/enhance
http://localhost:5173/tools/image/image-cleanup
```

生产构建：

```bash
npm run build
npm run start
```

构建后 `server.js` 会优先服务 `dist/index.html`，同时继续提供 `/api/*` 和 `/uploads/*`。

## 当前实现

- 工具广场从 `src/tool-config.js` 的工具注册表渲染，不需要为每张卡片手写页面。
- 每个工具都有独立 URL，刷新后由 `server.js` fallback 到单页应用。
- 前端已升级为 React + TanStack Router + TanStack Query + TanStack Form + TanStack Table。
- 计费采用积分模型：后端预估消耗、创建任务冻结积分、供应商成功回调后扣费、失败回调后释放冻结积分。
- 文件上传走 `POST /api/assets`，文件保存到 `data/uploads`，资产记录保存到 PostgreSQL。
- 视频去水印最小版本已接入本地 worker：前端上传视频并手动框选水印区域，后端用 FFmpeg `delogo` 生成处理后 MP4。
- 任务系统走后端持久化，字段包括：`toolSlug`、`inputAssetId`、`estimatedCredits`、`frozenCredits`、`chargedCredits`、`providerJobId`、`status`。
- 数据持久化在 PostgreSQL，异步任务队列使用 Redis/RQ。

视频去水印 MVP 需要本机安装 FFmpeg/FFprobe：

```bash
brew install ffmpeg
```

当前算法适合固定位置的 LOGO、角标、文字水印；移动水印、复杂纹理修复、人物遮挡恢复后续再接 OpenCV 掩码或模型级 inpainting。

## API

```text
GET  /api/bootstrap
POST /api/auth/login
POST /api/auth/register
GET  /api/auth/me
POST /api/assets/presign
POST /api/assets
POST /api/tasks
POST /api/provider/callback
POST /api/recharge
GET  /api/admin/summary
GET  /api/admin/users
POST /api/admin/users
POST /api/admin/users/{user_id}/recharge
GET  /api/admin/tasks
```

演示管理员：

```text
email: demo@modelplaza.local
password: demo123456
```

开发环境默认允许未带 token 的请求使用演示账号，方便本地调试；生产环境应设置 `ALLOW_DEMO_WITHOUT_AUTH=false` 并替换 `AUTH_SECRET`。

前端用户流程：

```text
/login      登录/注册
/admin      管理员创建用户、查看用户、给用户补积分
退出登录     清除 localStorage token 并返回登录页
```

前端 API client 会把 token 写入 `localStorage` 并自动携带：

```text
Authorization: Bearer <token>
```

供应商回调示例：

```bash
curl -X POST http://localhost:8010/api/provider/callback \
  -H 'Content-Type: application/json' \
  -d '{"providerJobId":"mock_xxx","status":"failed","errorCode":"PROVIDER_FAILED","callbackId":"unique-callback-id"}'
```

## 下一步生产化

已补充 FastAPI + PostgreSQL + Redis/RQ worker 架构。生产化时继续替换：

1. `data/uploads` 的本地存储适配器换成 R2/OSS/S3 实现。
2. mock provider 换成真实模型供应商任务创建与回调签名校验。
3. 真实支付接入后替换模拟充值接口。
4. 告警接入 Sentry/日志平台。

Docker Compose:

```bash
docker compose up --build
```

## GitHub Actions 部署

推送 `main` 分支会触发 `.github/workflows/deploy.yml`，构建前端后通过 SSH 部署到：

```text
huangguojie@35.220.200.97:/opt/videoBetterTools
```

需要在 GitHub 仓库的 `Settings -> Secrets and variables -> Actions` 配置：

```text
DEPLOY_SSH_KEY     SSH 私钥内容，对应服务器登录 key
PRODUCTION_ENV     可选，生产环境变量，参考 deploy/production.env.example
```

远端会使用 `docker-compose.prod.yml` 启动 `web/api/worker/postgres/redis`。首次部署如果没有提供 `PRODUCTION_ENV`，脚本会在服务器自动生成 `/opt/videoBetterTools/shared/.env` 的基础配置，后续可直接在服务器或 GitHub Secret 中替换正式密钥。

服务器已有 Caddy 占用 `80/443` 时，项目默认暴露在 `WEB_PORT=8003`，再由现有网关或安全组决定是否对外开放。

平台 worker 默认 `WORKER_REPLICAS=8`，GPU 服务当前按 `MODEL_PLAZA_GPU_DEVICE_IDS=3,4,5,6` 和 `MODEL_PLAZA_GPU_WORKERS_PER_DEVICE=2` 分配，也就是 GPU3/GPU4/GPU5/GPU6 各最多同时处理 2 个模型任务，平台侧最多同时提交 8 个远端视频任务。GPU 服务设置 `MODEL_PLAZA_GPU_UPLOAD_RESULTS=0` 后只生成结果，平台 worker 通过 GPU API 拉取 `output.mp4` 再上传对象存储，避免 GPU 侧被结果上传链路占住。ProPainter 并发仍需持续观察显存。

GPU 结果清理默认开启：成功任务保留 24 小时，失败/取消任务保留 48 小时；`runner-work` 中间目录默认 1 小时后清理，`api-jobs` 所在磁盘超过 80% 时会从最老的终态任务开始清到 70%。可通过 `MODEL_PLAZA_GPU_CLEANUP_*` 环境变量调整，也可调用 `POST /maintenance/cleanup` 手动触发一次。

测试：

```bash
uv --directory backend run pytest
```

详细设计见 [docs/architecture.md](/Users/jason/Desktop/Company/modelPlaza/docs/architecture.md)。
