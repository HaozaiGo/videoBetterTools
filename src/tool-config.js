export const tools = [
  {
    slug: "remove-watermark",
    category: "video",
    categoryName: "视频工具",
    name: "视频去水印",
    summary: "自动消除视频中的各类图形水印、LOGO、文字水印，以及移动水印等。",
    route: "/tools/video/remove-watermark",
    icon: "spark",
    status: "online",
    provider: "mock-video-provider",
    pricing: {
      mode: "duration",
      unitSeconds: 10,
      unitCredits: 5,
      minimumCredits: 10,
      priorityMultiplier: { standard: 1, express: 1.45 },
    },
    inputs: ["priority", "watermarkCount"],
  },
  {
    slug: "remove-subtitle",
    category: "video",
    categoryName: "视频工具",
    name: "视频去字幕",
    summary: "自动清除视频中的各类字幕及纯文字水印，不破坏画面其他部分。",
    route: "/tools/video/remove-subtitle",
    icon: "caption",
    status: "online",
    provider: "mock-video-provider",
    pricing: {
      mode: "duration",
      unitSeconds: 10,
      unitCredits: 4,
      minimumCredits: 8,
      priorityMultiplier: { standard: 1, express: 1.4 },
    },
    inputs: ["priority"],
  },
  {
    slug: "object-removal",
    category: "video",
    categoryName: "视频工具",
    name: "万能视频消除",
    summary: "消除动态水印、不需要的人或物、马赛克或遮挡，并尽量恢复视频内容。",
    route: "/tools/video/object-removal",
    icon: "grid",
    status: "disabled",
    provider: "mock-video-provider",
    pricing: {
      mode: "duration",
      unitSeconds: 10,
      unitCredits: 7,
      minimumCredits: 14,
      resolutionMultiplier: { "720p": 1, "1080p": 1.25, "2K": 1.75, "4K": 2.4 },
      priorityMultiplier: { standard: 1, express: 1.5 },
    },
    inputs: ["duration", "resolution", "priority", "maskComplexity"],
  },
  {
    slug: "enhance",
    category: "video",
    categoryName: "视频工具",
    name: "视频转高清",
    summary: "一键修复视频画质，提升画面清晰度，并输出 1080P、2K 或 4K 高清视频。",
    route: "/tools/video/enhance",
    icon: "hd",
    status: "online",
    provider: "mock-video-provider",
    pricing: {
      mode: "duration",
      unitSeconds: 10,
      unitCredits: 8,
      minimumCredits: 16,
      resolutionMultiplier: { "720p": 1, "1080p": 1.25, "2K": 1.9, "4K": 2.8 },
      priorityMultiplier: { standard: 1, express: 1.55 },
    },
    inputs: ["duration", "resolution", "priority"],
  },
  {
    slug: "translate",
    category: "video",
    categoryName: "视频工具",
    name: "视频翻译",
    summary: "上传中文视频，自动翻译成多国语言并写入字幕，输出可直接发布的 MP4。",
    route: "/tools/video/translate",
    icon: "translate",
    status: "online",
    provider: "mock-video-provider",
    pricing: {
      mode: "duration",
      unitSeconds: 10,
      unitCredits: 6,
      minimumCredits: 12,
      resolutionMultiplier: { "720p": 1, "1080p": 1, "2K": 1, "4K": 1 },
      priorityMultiplier: { standard: 1, express: 1.35 },
    },
    inputs: ["duration", "targetLanguage", "subtitlePlacement", "keepAudio", "priority"],
  },
  {
    slug: "image-cleanup",
    category: "image",
    categoryName: "图片工具",
    name: "万能图片消除",
    summary: "消除图片上的各类水印、标记、手写笔记、不想要的人或物等。",
    route: "/tools/image/image-cleanup",
    icon: "erase",
    status: "online",
    provider: "mock-image-provider",
    pricing: {
      mode: "image",
      unitCredits: 3,
      minimumCredits: 3,
      priorityMultiplier: { standard: 1, express: 1.25 },
    },
    inputs: ["imageCount", "priority"],
  },
  {
    slug: "background-change",
    category: "image",
    categoryName: "图片工具",
    name: "图片换背景",
    summary: "修改图片背景，通过光影重构，让新背景完整融合。",
    route: "/tools/image/background-change",
    icon: "paint",
    status: "online",
    provider: "mock-image-provider",
    pricing: {
      mode: "image",
      unitCredits: 4,
      minimumCredits: 4,
      priorityMultiplier: { standard: 1, express: 1.25 },
    },
    inputs: ["imageCount", "priority"],
  },
];

export const categories = [
  { id: "video", name: "视频工具" },
  { id: "image", name: "图片工具" },
];

export function getTool(slug) {
  return tools.find((tool) => tool.slug === slug);
}

export function estimateCredits(tool, form = {}) {
  const priority = form.priority || "standard";
  const priorityMultiplier = tool.pricing.priorityMultiplier?.[priority] || 1;

  if (tool.pricing.mode === "image") {
    const count = Number(form.imageCount || 1);
    return Math.max(tool.pricing.minimumCredits, Math.ceil(count * tool.pricing.unitCredits * priorityMultiplier));
  }

  const seconds = Number(form.duration || 30);
  const units = Math.ceil(seconds / tool.pricing.unitSeconds);
  const resolution = form.resolution || "1080p";
  const resolutionMultiplier = tool.pricing.resolutionMultiplier?.[resolution] || 1;
  const complexity = Number(form.watermarkCount || 1) > 1 || form.maskComplexity === "complex" ? 1.25 : 1;
  const estimate = units * tool.pricing.unitCredits * resolutionMultiplier * priorityMultiplier * complexity;
  return Math.max(tool.pricing.minimumCredits, Math.ceil(estimate));
}
