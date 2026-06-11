import type { AdminSummary, AdminUser, Asset, AuthUser, BootstrapState, GpuMetrics, Task, UserCreateInput } from "../types";

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

export async function openAuthenticatedFile(path: string) {
  const previewWindow = window.open("about:blank", "_blank");
  const headers = new Headers();
  const token = getAuthToken();
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  const response = await fetch(path, { headers });
  if (!response.ok) {
    previewWindow?.close();
    throw new Error("临时结果尚未准备好");
  }
  const blob = await response.blob();
  const objectUrl = URL.createObjectURL(blob);
  if (previewWindow) {
    previewWindow.location.href = objectUrl;
  } else {
    window.open(objectUrl, "_blank");
  }
  setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000);
}

export async function openTaskResult(taskId: string) {
  const previewWindow = window.open("about:blank", "_blank");
  try {
    const payload = await request<{ url: string }>(`/api/tasks/${taskId}/result-link`);
    if (previewWindow) {
      previewWindow.location.href = payload.url;
    } else {
      window.open(payload.url, "_blank");
    }
  } catch (error) {
    previewWindow?.close();
    throw error;
  }
}

export function uploadAsset(input: UploadAssetInput) {
  return uploadAssetWithStorage(input);
}

type UploadAssetInput = {
  file: File;
  kind: "video" | "image";
  durationSeconds?: number;
  onProgress?: (progress: UploadProgress) => void;
};

type UploadProgress = { percent: number; uploadedBytes: number; totalBytes: number; stage: string };

const multipartThresholdBytes = 32 * 1024 * 1024;
const multipartChunkSize = 8 * 1024 * 1024;

type MultipartStatus = {
  uploadId: string;
  assetId: string;
  chunkSize: number;
  totalChunks: number;
  uploadedChunks: number[];
};

function multipartStorageKey(input: UploadAssetInput) {
  return `model-plaza-multipart:${input.kind}:${input.file.name}:${input.file.size}:${input.file.lastModified}`;
}

function emitUploadProgress(input: Pick<UploadAssetInput, "file" | "onProgress">, progress: Partial<UploadProgress> & { stage: string }) {
  const totalBytes = progress.totalBytes || input.file.size || 1;
  const uploadedBytes = Math.min(progress.uploadedBytes || 0, totalBytes);
  const percent = progress.percent ?? Math.round((uploadedBytes / totalBytes) * 100);
  input.onProgress?.({
    stage: progress.stage,
    percent: Math.max(0, Math.min(100, percent)),
    uploadedBytes,
    totalBytes,
  });
}

function parseResponsePayload<T>(text: string): T {
  if (!text) return {} as T;
  return JSON.parse(text) as T;
}

function getErrorMessage(payload: unknown) {
  if (payload && typeof payload === "object") {
    const candidate = payload as { error?: unknown; detail?: unknown };
    if (typeof candidate.error === "string") return candidate.error;
    if (typeof candidate.detail === "string") return candidate.detail;
  }
  return "请求失败";
}

function requestWithUploadProgress<T>(
  path: string,
  init: {
    method: string;
    body: XMLHttpRequestBodyInit;
    headers?: Record<string, string>;
    onProgress?: UploadAssetInput["onProgress"];
    progressTotalBytes: number;
    progressStage: string;
  },
) {
  return new Promise<T>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open(init.method, path);
    const token = getAuthToken();
    if (token && !init.headers?.Authorization) {
      xhr.setRequestHeader("Authorization", `Bearer ${token}`);
    }
    for (const [key, value] of Object.entries(init.headers || {})) {
      xhr.setRequestHeader(key, value);
    }
    xhr.upload.onprogress = (event) => {
      if (!event.lengthComputable) return;
      const percent = Math.round((event.loaded / event.total) * 100);
      init.onProgress?.({
        stage: init.progressStage,
        percent,
        uploadedBytes: Math.min(event.loaded, init.progressTotalBytes),
        totalBytes: init.progressTotalBytes,
      });
    };
    xhr.onload = () => {
      let payload: unknown = {};
      try {
        payload = parseResponsePayload(xhr.responseText);
      } catch {
        payload = {};
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(payload as T);
        return;
      }
      if (xhr.status === 401) {
        clearAuthToken();
        if (!location.pathname.startsWith("/login")) {
          location.assign("/login");
        }
      }
      reject(new Error(getErrorMessage(payload)));
    };
    xhr.onerror = () => reject(new Error("网络请求失败"));
    xhr.send(init.body);
  });
}

function putFileWithProgress(
  url: string,
  init: {
    method: string;
    headers?: Record<string, string>;
    file: File;
    onProgress?: UploadAssetInput["onProgress"];
  },
) {
  return new Promise<void>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open(init.method, url);
    for (const [key, value] of Object.entries(init.headers || {})) {
      xhr.setRequestHeader(key, value);
    }
    xhr.upload.onprogress = (event) => {
      if (!event.lengthComputable) return;
      init.onProgress?.({
        stage: "上传视频",
        percent: Math.round((event.loaded / event.total) * 100),
        uploadedBytes: Math.min(event.loaded, init.file.size),
        totalBytes: init.file.size,
      });
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve();
        return;
      }
      reject(new Error("上传到火山存储失败"));
    };
    xhr.onerror = () => reject(new Error("网络请求失败"));
    xhr.send(init.file);
  });
}

function uploadAssetViaBackend(input: UploadAssetInput) {
  const body = new FormData();
  body.append("file", input.file);
  body.append("kind", input.kind);
  body.append("durationSeconds", String(input.durationSeconds || 0));
  emitUploadProgress(input, { stage: "准备上传", percent: 0, uploadedBytes: 0 });
  return requestWithUploadProgress<{ asset: Asset }>("/api/assets", {
    method: "POST",
    body,
    onProgress: input.onProgress,
    progressStage: "上传视频",
    progressTotalBytes: input.file.size,
  });
}

async function uploadAssetWithMultipart(input: UploadAssetInput) {
  const storageKey = multipartStorageKey(input);
  let uploadId = localStorage.getItem(storageKey) || "";
  let status: MultipartStatus | null = null;

  if (uploadId) {
    try {
      status = await request<MultipartStatus>(`/api/assets/multipart/${uploadId}`);
    } catch {
      uploadId = "";
      localStorage.removeItem(storageKey);
    }
  }

  if (!uploadId || !status) {
    status = await request<MultipartStatus>("/api/assets/multipart/init", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        kind: input.kind,
        originalName: input.file.name,
        mimeType: input.file.type || "application/octet-stream",
        sizeBytes: input.file.size,
        durationSeconds: input.durationSeconds || 0,
        chunkSize: multipartChunkSize,
      }),
    });
    uploadId = status.uploadId;
    localStorage.setItem(storageKey, uploadId);
  }

  const uploaded = new Set(status.uploadedChunks || []);
  const chunkSize = status.chunkSize || multipartChunkSize;
  const totalChunks = status.totalChunks || Math.ceil(input.file.size / chunkSize);
  for (let index = 0; index < totalChunks; index += 1) {
    if (uploaded.has(index)) {
      input.onProgress?.({
        percent: Math.floor((uploaded.size / totalChunks) * 100),
        uploadedBytes: Math.min(uploaded.size * chunkSize, input.file.size),
        totalBytes: input.file.size,
        stage: "断点续传中",
      });
      continue;
    }
    const start = index * chunkSize;
    const end = Math.min(start + chunkSize, input.file.size);
    const body = new FormData();
    body.append("file", input.file.slice(start, end), input.file.name);
    const result = await request<{ uploadedChunks: number[]; progressPercent: number }>(`/api/assets/multipart/${uploadId}/chunks/${index}`, {
      method: "POST",
      body,
    });
    uploaded.clear();
    for (const chunkIndex of result.uploadedChunks || []) {
      uploaded.add(chunkIndex);
    }
    input.onProgress?.({
      percent: result.progressPercent,
      uploadedBytes: Math.min(uploaded.size * chunkSize, input.file.size),
      totalBytes: input.file.size,
      stage: "上传分片",
    });
  }

  const completed = await request<{ asset: Asset }>(`/api/assets/multipart/${uploadId}/complete`, { method: "POST" });
  localStorage.removeItem(storageKey);
  input.onProgress?.({ percent: 100, uploadedBytes: input.file.size, totalBytes: input.file.size, stage: "上传完成" });
  return completed;
}

async function uploadAssetWithStorage(input: UploadAssetInput) {
  if (input.file.size >= multipartThresholdBytes) {
    return uploadAssetWithMultipart(input);
  }

  const presign = await presignAsset(input.kind, input.durationSeconds || 0, input.file.name);
  if (presign.mode === "tos-put") {
    if (!presign.assetId || !presign.storageKey) {
      throw new Error("上传签名缺少资产信息");
    }
    emitUploadProgress(input, { stage: "准备上传", percent: 0, uploadedBytes: 0 });
    try {
      await putFileWithProgress(presign.uploadUrl, {
        method: presign.method,
        headers: presign.headers || {},
        file: input.file,
        onProgress: input.onProgress,
      });
    } catch {
      return uploadAssetViaBackend(input);
    }
    emitUploadProgress(input, { stage: "登记素材", percent: 100, uploadedBytes: input.file.size });
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

  return uploadAssetViaBackend(input);
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

export function createTask(input: { toolSlug: string; inputAssetId: string; params: Record<string, unknown> }) {
  return request<{ task: Task; state: BootstrapState }>("/api/tasks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export function cancelTask(taskId: string) {
  return request<{ task: Task; state: BootstrapState }>(`/api/tasks/${taskId}/cancel`, {
    method: "POST",
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

export function getAdminGpuMetrics() {
  return request<GpuMetrics>("/api/admin/gpu");
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
