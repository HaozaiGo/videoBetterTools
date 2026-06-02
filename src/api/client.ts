import type { AdminSummary, AdminUser, Asset, AuthUser, BootstrapState, Task, ToolFormValues, UserCreateInput } from "../types";

const tokenKey = "model_plaza_auth_token";

export function getAuthToken() {
  return localStorage.getItem(tokenKey);
}

export function setAuthToken(token: string) {
  localStorage.setItem(tokenKey, token);
}

export function clearAuthToken() {
  localStorage.removeItem(tokenKey);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  const token = getAuthToken();
  if (token && !headers.has("Authorization")) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  const response = await fetch(path, { ...init, headers });
  const payload = await response.json();
  if (!response.ok) {
    if (response.status === 401) {
      clearAuthToken();
      if (!location.pathname.startsWith("/login")) {
        location.assign("/login");
      }
    }
    throw new Error(payload.error || "请求失败");
  }
  return payload as T;
}

export function getBootstrap() {
  return request<BootstrapState>("/api/bootstrap");
}

export function uploadAsset(input: { file: File; kind: "video" | "image"; durationSeconds?: number }) {
  return uploadAssetWithStorage(input);
}

async function uploadAssetWithStorage(input: { file: File; kind: "video" | "image"; durationSeconds?: number }) {
  const presign = await presignAsset(input.kind, input.durationSeconds || 0, input.file.name);
  if (presign.mode === "tos-put") {
    if (!presign.assetId || !presign.storageKey) {
      throw new Error("上传签名缺少资产信息");
    }
    const uploadResponse = await fetch(presign.uploadUrl, {
      method: presign.method,
      headers: presign.headers || {},
      body: input.file,
    });
    if (!uploadResponse.ok) {
      throw new Error("上传到火山存储失败");
    }
    return request<{ asset: Asset }>("/api/assets/complete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        assetId: presign.assetId,
        kind: input.kind,
        originalName: input.file.name,
        mimeType: input.file.type || "application/octet-stream",
        storageKey: presign.storageKey,
        sizeBytes: input.file.size,
        durationSeconds: input.durationSeconds || 0,
      }),
    });
  }

  const body = new FormData();
  body.append("file", input.file);
  body.append("kind", input.kind);
  body.append("durationSeconds", String(input.durationSeconds || 0));
  return request<{ asset: Asset }>("/api/assets", {
    method: "POST",
    body,
  });
}

export function presignAsset(kind: "video" | "image", durationSeconds = 0, originalName = "upload.bin") {
  return request<{
    mode: string;
    method: string;
    uploadUrl: string;
    headers?: Record<string, string>;
    fields: Record<string, unknown>;
    assetId?: string;
    storageKey?: string;
    publicUrl?: string;
  }>(
    `/api/assets/presign?kind=${kind}&durationSeconds=${durationSeconds}&originalName=${encodeURIComponent(originalName)}`,
    { method: "POST" },
  );
}

export function createTask(input: { toolSlug: string; inputAssetId: string; params: ToolFormValues }) {
  return request<{ task: Task; state: BootstrapState }>("/api/tasks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export function recharge(credits: number) {
  return request<{ state: BootstrapState }>("/api/recharge", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ credits }),
  });
}

export function failProviderJob(providerJobId: string) {
  return request<{ duplicated: boolean; state: BootstrapState }>("/api/provider/callback", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      providerJobId,
      status: "failed",
      errorCode: "MANUAL_TEST_FAILED",
      callbackId: `${providerJobId}:manual-failed:${Date.now()}`,
    }),
  });
}

export function login(input: { email: string; password: string }) {
  return request<{ token: string; user: AuthUser }>("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export function register(input: UserCreateInput) {
  return request<{ token: string; user: AuthUser }>("/api/auth/register", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export function getAdminSummary() {
  return request<AdminSummary>("/api/admin/summary");
}

export function getAdminUsers() {
  return request<AdminUser[]>("/api/admin/users");
}

export function getAdminTasks() {
  return request<Task[]>("/api/admin/tasks");
}

export function createAdminUser(input: UserCreateInput) {
  return request<AuthUser>("/api/admin/users", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export function rechargeAdminUser(userId: string, credits: number) {
  return request<{ ok: true }>(`/api/admin/users/${userId}/recharge`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ credits }),
  });
}
