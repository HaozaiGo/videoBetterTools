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

测试：

```bash
uv --directory backend run pytest
```

详细设计见 [docs/architecture.md](/Users/jason/Desktop/Company/modelPlaza/docs/architecture.md)。
