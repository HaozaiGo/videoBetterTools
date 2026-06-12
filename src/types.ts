import type { TranslateTargetLanguage } from "./lib/translate-languages";

export type ToolCategory = "video" | "image";
export type ToolStatus = "online" | "coming" | "disabled";
export type PricingMode = "duration" | "image";
export type Priority = "standard" | "express";
export type TaskStatus = "queued" | "processing" | "succeeded" | "failed" | "cancelled";
export type LedgerType = "recharge" | "freeze" | "charge" | "refund";

export type PricingRule = {
  mode: PricingMode;
  unitSeconds?: number;
  unitCredits: number;
  minimumCredits: number;
  resolutionMultiplier?: Record<string, number>;
  priorityMultiplier?: Record<Priority, number>;
};

export type ToolDefinition = {
  slug: string;
  category: ToolCategory;
  categoryName: string;
  name: string;
  summary: string;
  route: string;
  icon: string;
  status: ToolStatus;
  provider?: string;
  pricing: PricingRule;
  inputs: string[];
};

export type CategoryDefinition = {
  id: ToolCategory;
  name: string;
};

export type Account = {
  id: string;
  name: string;
  email: string;
  credits: number;
  frozenCredits: number;
  availableCredits: number;
  role?: string;
};

export type AdminSummary = {
  users: number;
  tasks: number;
  assets: number;
  creditsCharged: number;
  queuedTasks: number;
  processingTasks: number;
  failedTasks: number;
};

export type AdminUser = {
  id: string;
  email: string;
  name: string;
  role: string;
  status: string;
  credits: number;
  frozenCredits: number;
  createdAt: number;
};

export type GpuDeviceMetric = {
  index: string;
  name: string;
  utilizationGpuPercent: number;
  utilizationMemoryPercent: number;
  memoryUsedMiB: number;
  memoryTotalMiB: number;
  temperatureGpu: number;
  powerDrawW: number;
  workerSlotsUsed: number;
  workerSlotsTotal: number;
};

export type GpuRunningJob = {
  id: string;
  status: string;
  jobType: string;
  assignedGpu: string;
  progressPercent: number;
  progressStage: string;
  runningSeconds: number;
  logPath: string;
};

export type GpuMetrics = {
  ok: boolean;
  timestamp: number;
  error?: string;
  gpuDevices: string[];
  workersPerGpu: number;
  slotCapacity: number;
  runningByGpu: Record<string, number>;
  gpus: GpuDeviceMetric[];
  runningJobs: GpuRunningJob[];
};

export type AuthUser = {
  id: string;
  email: string;
  name: string;
  role: string;
};

export type UserCreateInput = {
  email: string;
  password: string;
  name: string;
  role: string;
  initialCredits: number;
};

export type Asset = {
  id: string;
  kind: "video" | "image" | "result";
  originalName: string;
  mimeType: string;
  url: string;
  sizeBytes: number;
  durationSeconds: number;
  createdAt: number;
};

export type Task = {
  id: string;
  toolSlug: string;
  inputAssetId: string;
  inputAssetName: string;
  outputAssetId: string | null;
  status: TaskStatus;
  params: Record<string, unknown>;
  estimatedCredits: number;
  frozenCredits: number;
  chargedCredits: number;
  provider: string;
  providerJobId: string;
  errorCode: string | null;
  progressPercent: number;
  progressStage: string;
  createdAt: number;
  completedAt: number | null;
  outputUrl: string;
  previewUrl: string;
};

export type LedgerEntry = {
  id: string;
  type: LedgerType;
  amount: number;
  title: string;
  taskId: string | null;
  createdAt: number;
};

export type WatermarkRegion = {
  x: number;
  y: number;
  width: number;
  height: number;
  startTime?: number;
  endTime?: number | null;
};

export type BootstrapState = {
  account: Account;
  tools: ToolDefinition[];
  categories: CategoryDefinition[];
  tasks: Task[];
  ledger: LedgerEntry[];
};

export type ToolFormValues = {
  duration: number;
  resolution: string;
  priority: Priority;
  imageCount: number;
  watermarkCount: number;
  maskComplexity: "normal" | "complex";
  languageCount: number;
  mode: "manual" | "auto";
  regions: WatermarkRegion[];
  keepAudio: boolean;
  targetLanguage: TranslateTargetLanguage;
  subtitlePlacement: "bottom" | "middle-lower" | "top";
  enhanceMode: "quality" | "natural";
  modelAdapter: "opencv-inpaint" | "ffmpeg-delogo" | "propainter" | "e2fgvi";
  inpaintMethod: "telea" | "ns";
  inpaintRadius: number;
  maskPadding: number;
  maskStrategy: "rectangle" | "subtitle-text" | "dark-subtitle-line";
  textLightThreshold: number;
};
