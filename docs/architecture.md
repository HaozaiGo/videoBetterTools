# 工具平台落地设计

## 产品边界

主平台负责账号、余额、充值、工具广场、任务列表、资产管理和供应商调度。具体工具只负责自己的输入参数、价格规则、处理器和结果展示。

## 当前前端架构

```text
React + TypeScript
TanStack Router：工具 URL、任务页、充值页
TanStack Query：账户、任务、流水、轮询刷新
TanStack Form：工具参数表单
TanStack Table：任务列表、钱包流水
Vite：开发服务与前端构建
```

开发时前端跑在 `5173`，通过 Vite proxy 访问 `4173` 的后端 API。生产构建后，`server.js` 会优先服务 `dist` 目录。

## URL 设计

```text
/tools
/tools/video/remove-watermark
/tools/video/remove-subtitle
/tools/video/object-removal
/tools/video/enhance
/tools/video/translate
/tools/image/image-cleanup
/tools/image/background-change
/tasks
/billing
```

## 工具注册表

每个工具至少需要这些字段：

```ts
type ToolDefinition = {
  slug: string;
  category: "video" | "image";
  name: string;
  summary: string;
  route: string;
  status: "online" | "coming" | "disabled";
  pricing: PricingRule;
  inputs: string[];
  provider?: string;
};
```

当前原型把工具配置放在 `src/tool-config.js`，前后端共同引用同一份配置。后续可以迁移到数据库，并在管理后台配置工具状态、单价、供应商和展示顺序。

## 计费模型

使用积分作为平台内部货币：

```text
人民币充值 -> 积分入账 -> 提交任务冻结积分 -> 成功扣费 / 失败退款
```

视频工具建议按时长阶梯计费：

```text
预估积分 = ceil(视频秒数 / 计费步长) * 单价 * 清晰度倍率 * 优先级倍率 * 复杂度倍率
```

图片工具建议按张计费：

```text
预估积分 = 图片数量 * 单张积分 * 优先级倍率
```

## 关键数据表

```text
users
  id
  email
  name

wallets
  user_id
  credits
  frozen_credits

wallet_ledger
  id
  user_id
  type
  amount
  task_id
  note
  created_at

tools
  slug
  category
  name
  status
  pricing_json
  provider

assets
  id
  user_id
  kind
  storage_key
  duration_seconds
  width
  height
  size_bytes
  expires_at

tasks
  id
  user_id
  tool_slug
  input_asset_id
  output_asset_id
  status
  estimated_credits
  frozen_credits
  charged_credits
  provider
  provider_job_id
  error_code
  created_at
  completed_at
```

## 任务状态

```text
created -> uploaded -> queued -> processing -> succeeded
                                  -> failed
                                  -> cancelled
```

结算规则：

- 创建任务时冻结积分。
- 成功时释放冻结积分并扣除实际积分。
- 失败、取消、供应商超时时释放冻结积分。
- 重试任务不能重复冻结，必须复用原任务或创建子任务并继承结算上下文。

## 当前 API 原型

```text
GET /api/bootstrap
  返回账户、工具、任务、流水。

POST /api/auth/login
  演示账号登录，返回 Bearer token。

POST /api/auth/register
  创建普通用户并返回 token。

POST /api/assets/presign
  返回上传适配器信息。当前为本地表单上传，后续可换成 R2/OSS/S3 预签 URL。

POST /api/assets
  multipart/form-data 上传文件，返回 asset_id。

POST /api/tasks
  参数：toolSlug、inputAssetId、params。
  后端重新计算价格，余额充足才冻结积分并创建 providerJobId。

POST /api/provider/callback
  参数：providerJobId、status、callbackId、outputUrl、chargedCredits、errorCode。
  callbackId 用于幂等，成功扣费，失败释放冻结。

POST /api/recharge
  本地模拟充值入账。

GET /api/admin/*
  后台管理统计、用户、任务和流水查询。

POST /api/admin/users
  管理员创建用户，可设置初始积分。

POST /api/admin/users/{user_id}/recharge
  管理员给指定用户补积分。
```

## 生产化替换点

- `data/db.json` 替换为数据库，并给钱包更新加事务。
- `data/uploads` 替换为对象存储，并用短期签名 URL 上传。
- 回调接口增加供应商签名校验、时间戳、防重放。
- 成功扣费流水增加唯一业务键，避免重复扣费。
- 失败退款按错误码区分：供应商失败全退，用户输入不合规可不扣或部分扣。

## 容易踩坑

- 不能只在前端判断余额，后端必须用事务检查和冻结。
- 供应商回调可能重复到达，扣费接口必须幂等。
- 视频时长、分辨率要以后端解析结果为准，不能信任前端传参。
- 结果文件要设置保存期限，否则存储和 CDN 成本会失控。
- 每个工具的失败原因要结构化，方便退款、重试和客服排查。
- 工具价格要可配置，不要写死在业务代码里。
