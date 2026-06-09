import { Link, useRouterState } from "@tanstack/react-router";
import { useMutation, useQueryClient, useSuspenseQuery } from "@tanstack/react-query";
import { useForm, useStore } from "@tanstack/react-form";
import { useEffect, useMemo, useRef, useState, type PointerEvent } from "react";
import { createTask, getBootstrap, uploadAsset } from "../api/client";
import { formatCredits } from "../lib/format";
import { ToolIcon } from "../lib/tool-icons";
import type { BootstrapState, ToolFormValues, WatermarkRegion } from "../types";
import { estimateCredits } from "../tool-config.js";

const defaultValues: ToolFormValues = {
  duration: 30,
  resolution: "1080p",
  priority: "standard",
  imageCount: 1,
  watermarkCount: 1,
  maskComplexity: "normal",
  languageCount: 1,
  mode: "manual",
  regions: [],
  keepAudio: true,
  targetLanguage: "en",
  subtitlePlacement: "bottom",
  enhanceMode: "quality",
  modelAdapter: "propainter",
  inpaintMethod: "telea",
  inpaintRadius: 5,
  maskPadding: 8,
  maskStrategy: "subtitle-text",
  textLightThreshold: 155,
};

function clamp(value: number) {
  return Math.max(0, Math.min(1, value));
}

function formatBytes(bytes: number) {
  if (!bytes) return "0 MB";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value >= 10 || unitIndex === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[unitIndex]}`;
}

function buildRegion(start: { x: number; y: number }, end: { x: number; y: number }): WatermarkRegion {
  // 用归一化坐标保存框选区域，避免前端预览尺寸和真实视频分辨率不一致时出现偏移。
  const x = clamp(Math.min(start.x, end.x));
  const y = clamp(Math.min(start.y, end.y));
  const width = Math.max(0, Math.min(1 - x, Math.abs(end.x - start.x)));
  const height = Math.max(0, Math.min(1 - y, Math.abs(end.y - start.y)));
  return { x, y, width, height };
}

type MediaBox = {
  left: number;
  top: number;
  width: number;
  height: number;
};

type SubmitProgress = {
  stage: string;
  percent: number;
  uploadedBytes: number;
  totalBytes: number;
};

type BatchUploadItem = {
  id: string;
  name: string;
  size: number;
  status: "pending" | "uploading" | "creating" | "created" | "failed";
  stage: string;
  percent: number;
  taskId?: string;
  error?: string;
};

function fileBatchId(file: File, index: number) {
  return `${file.name}-${file.size}-${file.lastModified}-${index}`;
}

function readVideoDuration(file: File) {
  return new Promise<number | null>((resolve) => {
    if (!file.type.startsWith("video/")) {
      resolve(null);
      return;
    }
    const video = document.createElement("video");
    const url = URL.createObjectURL(file);
    const cleanup = () => {
      URL.revokeObjectURL(url);
      video.removeAttribute("src");
      video.load();
    };
    video.preload = "metadata";
    video.onloadedmetadata = () => {
      const seconds = video.duration;
      cleanup();
      resolve(seconds && Number.isFinite(seconds) ? Math.max(1, Math.ceil(seconds)) : null);
    };
    video.onerror = () => {
      cleanup();
      resolve(null);
    };
    video.src = url;
  });
}

export function ToolPage() {
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  const queryClient = useQueryClient();
  const { data } = useSuspenseQuery({ queryKey: ["bootstrap"], queryFn: getBootstrap });
  const [files, setFiles] = useState<File[]>([]);
  const [fileDurations, setFileDurations] = useState<Record<string, number>>({});
  const [batchItems, setBatchItems] = useState<BatchUploadItem[]>([]);
  const [activeFileIndex, setActiveFileIndex] = useState(0);
  const [notice, setNotice] = useState("");
  const [submitProgress, setSubmitProgress] = useState<SubmitProgress | null>(null);
  const [videoPreviewUrl, setVideoPreviewUrl] = useState("");
  const [regionsByFileId, setRegionsByFileId] = useState<Record<string, WatermarkRegion[]>>({});
  const [draftRegion, setDraftRegion] = useState<WatermarkRegion | null>(null);
  const [isSelectingRegion, setIsSelectingRegion] = useState(false);
  const [mediaBox, setMediaBox] = useState<MediaBox | null>(null);
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const overlayRef = useRef<HTMLDivElement | null>(null);
  const dragStartRef = useRef<{ x: number; y: number } | null>(null);
  const appendInputRef = useRef<HTMLInputElement | null>(null);
  const tool = data.tools.find((item) => item.route === pathname);
  const isWatermarkTool = tool?.slug === "remove-watermark";
  const isSubtitleTool = tool?.slug === "remove-subtitle";
  const isEnhanceTool = tool?.slug === "enhance";
  const isTranslateTool = tool?.slug === "translate";
  const isMaskVideoTool = isWatermarkTool || isSubtitleTool;
  const showVideoPreview = Boolean(videoPreviewUrl && (isMaskVideoTool || isEnhanceTool || isTranslateTool));
  const regionNoun = isSubtitleTool ? "字幕" : "水印";
  const selectedFile = files[activeFileIndex] || files[0] || null;
  const selectedFileId = selectedFile ? fileBatchId(selectedFile, files[activeFileIndex] ? activeFileIndex : 0) : "";
  const regions = selectedFileId ? regionsByFileId[selectedFileId] || [] : [];
  const selectedFileCount = files.length;
  const selectedFilesSize = useMemo(() => files.reduce((total, item) => total + item.size, 0), [files]);
  const isBatchUpload = selectedFileCount > 1;

  const updateBatchItem = (id: string, patch: Partial<BatchUploadItem>) => {
    setBatchItems((items) => items.map((item) => (item.id === id ? { ...item, ...patch } : item)));
  };

  const removeBatchFile = (removeIndex: number) => {
    if (createTaskMutation.isPending) {
      setNotice("任务提交中，当前文件不能移除。");
      return;
    }
    const removedFile = files[removeIndex];
    if (!removedFile) return;
    const nextFiles = files.filter((_, index) => index !== removeIndex);
    const nextRegionsByFileId: Record<string, WatermarkRegion[]> = {};
    const nextDurations: Record<string, number> = {};
    const nextBatchItems = nextFiles.map((item, index) => {
      const oldIndex = index >= removeIndex ? index + 1 : index;
      const oldId = fileBatchId(item, oldIndex);
      const nextId = fileBatchId(item, index);
      const existingItem = batchItems.find((batchItem) => batchItem.id === oldId);
      if (regionsByFileId[oldId]) {
        nextRegionsByFileId[nextId] = regionsByFileId[oldId];
      }
      if (fileDurations[oldId]) {
        nextDurations[nextId] = fileDurations[oldId];
      }
      return {
        id: nextId,
        name: item.name,
        size: item.size,
        status: existingItem?.status || "pending",
        stage: existingItem?.stage || "等待提交",
        percent: existingItem?.percent || 0,
        taskId: existingItem?.taskId,
        error: existingItem?.error,
      };
    });

    setFiles(nextFiles);
    setBatchItems(nextBatchItems);
    setRegionsByFileId(nextRegionsByFileId);
    setFileDurations(nextDurations);
    setDraftRegion(null);
    setIsSelectingRegion(false);
    setSubmitProgress(null);
    setActiveFileIndex((currentIndex) => {
      if (!nextFiles.length) return 0;
      if (currentIndex === removeIndex) return Math.min(removeIndex, nextFiles.length - 1);
      if (currentIndex > removeIndex) return currentIndex - 1;
      return currentIndex;
    });
    setNotice(nextFiles.length ? `已移除 ${removedFile.name}。` : "已清空待处理文件。");
  };

  const setSelectedFiles = (nextFiles: File[], options: { append?: boolean } = {}) => {
    if (!nextFiles.length) return;
    const mergedFiles = options.append ? [...files, ...nextFiles] : nextFiles;
    if (!mergedFiles.length) return;
    setFiles(mergedFiles);
    setFileDurations({});
    if (!options.append) {
      setActiveFileIndex(0);
    }
    setBatchItems(
      mergedFiles.map((item, index) => ({
        id: fileBatchId(item, index),
        name: item.name,
        size: item.size,
        status: "pending",
        stage: "等待提交",
        percent: 0,
      })),
    );
    if (!options.append) {
      setRegionsByFileId({});
      setDraftRegion(null);
      setIsSelectingRegion(false);
    }
    setSubmitProgress(null);
    setNotice(options.append ? `已追加 ${nextFiles.length} 个处理文件。` : "");
  };

  const createTaskMutation = useMutation({
    mutationFn: async (values: ToolFormValues) => {
      if (!tool) throw new Error("工具不存在");
      if (!files.length) throw new Error("请先选择一个文件");
      setNotice("");
      const buildParams = (duration: number, fileRegions: WatermarkRegion[]) => {
        const taskValues = {
          ...values,
          duration,
          resolution: tool.inputs.includes("resolution") ? values.resolution : "",
        };
        return isMaskVideoTool
          ? {
              ...taskValues,
              mode: "manual" as const,
              regions: fileRegions,
              removalTarget: isSubtitleTool ? "subtitle" : "watermark",
              maskStrategy: values.maskStrategy,
            }
          : isEnhanceTool
            ? {
                duration,
                resolution: values.resolution,
                enhanceMode: values.enhanceMode,
                keepAudio: values.keepAudio,
                priority: values.priority,
              }
            : isTranslateTool
              ? {
                  duration,
                  targetLanguage: values.targetLanguage,
                  subtitlePlacement: values.subtitlePlacement,
                  keepAudio: values.keepAudio,
                  priority: values.priority,
                }
              : taskValues;
      };
      let latestState: BootstrapState | null = null;
      let createdCount = 0;
      let failedCount = 0;

      for (const [index, currentFile] of files.entries()) {
        const itemId = fileBatchId(currentFile, index);
        const duration = fileDurations[itemId] || values.duration;
        const fileRegions = regionsByFileId[itemId] || [];
        try {
          updateBatchItem(itemId, { status: "uploading", stage: "上传视频", percent: 0, error: "" });
          setSubmitProgress({ stage: `上传第 ${index + 1}/${files.length} 个文件`, percent: 0, uploadedBytes: 0, totalBytes: currentFile.size });
          const upload = await uploadAsset({
            file: currentFile,
            kind: tool.pricing.mode === "image" ? "image" : "video",
            durationSeconds: duration,
            onProgress: ({ percent, stage, uploadedBytes, totalBytes }) => {
              updateBatchItem(itemId, { percent, stage });
              setSubmitProgress({
                percent,
                stage: files.length > 1 ? `${stage}（${index + 1}/${files.length}）` : stage,
                uploadedBytes,
                totalBytes,
              });
            },
          });
          updateBatchItem(itemId, { status: "creating", stage: "创建任务", percent: 100 });
          setSubmitProgress({ stage: `创建第 ${index + 1}/${files.length} 个任务`, percent: 100, uploadedBytes: currentFile.size, totalBytes: currentFile.size });
          const payload = await createTask({ toolSlug: tool.slug, inputAssetId: upload.asset.id, params: buildParams(duration, fileRegions) });
          latestState = payload.state;
          createdCount += 1;
          updateBatchItem(itemId, { status: "created", stage: "任务已创建", percent: 100, taskId: payload.task.id });
          queryClient.setQueryData<BootstrapState>(["bootstrap"], payload.state);
        } catch (error) {
          failedCount += 1;
          updateBatchItem(itemId, {
            status: "failed",
            stage: "创建失败",
            error: error instanceof Error ? error.message : "任务创建失败",
          });
        }
      }

      if (!createdCount) {
        throw new Error("批量任务创建失败，请查看文件列表中的失败原因。");
      }

      return { state: latestState, createdCount, failedCount };
    },
    onSuccess: (payload) => {
      if (payload.state) {
        queryClient.setQueryData<BootstrapState>(["bootstrap"], payload.state);
      }
      setFiles([]);
      setFileDurations({});
      setActiveFileIndex(0);
      setRegionsByFileId({});
      setDraftRegion(null);
      setSubmitProgress(null);
      setNotice(payload.failedCount ? `已创建 ${payload.createdCount} 个任务，${payload.failedCount} 个文件失败。` : `已创建 ${payload.createdCount} 个任务，积分已冻结。`);
    },
    onError: (error) => {
      setSubmitProgress(null);
      setNotice(error.message);
    },
  });

  const form = useForm({
    defaultValues,
    onSubmit: async ({ value }) => createTaskMutation.mutateAsync(value),
  });
  const values = useStore(form.store, (state) => state.values);
  const selectedModelAdapter = values.modelAdapter;
  const isOpenCvAdapter = selectedModelAdapter === "opencv-inpaint";
  const isTextMaskAdapter = selectedModelAdapter === "opencv-inpaint" || selectedModelAdapter === "propainter";
  const isFastBlurAdapter = selectedModelAdapter === "ffmpeg-delogo";
  const showTextMaskControls = isMaskVideoTool && isTextMaskAdapter;
  const showTextThreshold = showTextMaskControls && values.maskStrategy === "subtitle-text";
  const showOpenCvControls = isMaskVideoTool && isOpenCvAdapter;
  const showMaskPadding = isMaskVideoTool && !isFastBlurAdapter;
  const estimate = useMemo(() => (tool ? estimateCredits(tool, values) : 0), [tool, values]);

  useEffect(() => {
    let cancelled = false;
    setFileDurations({});
    if (!files.length || tool?.pricing.mode === "image") {
      return () => {
        cancelled = true;
      };
    }

    Promise.all(
      files.map(async (item, index) => {
        const duration = await readVideoDuration(item);
        return [fileBatchId(item, index), duration] as const;
      }),
    ).then((entries) => {
      if (cancelled) return;
      const nextDurations = Object.fromEntries(entries.filter((entry): entry is readonly [string, number] => Boolean(entry[1])));
      setFileDurations(nextDurations);
    });

    return () => {
      cancelled = true;
    };
  }, [files, tool?.pricing.mode]);

  useEffect(() => {
    if (isMaskVideoTool && !isTextMaskAdapter && values.maskStrategy !== "rectangle") {
      form.setFieldValue("maskStrategy", "rectangle");
    }
  }, [form, isMaskVideoTool, isTextMaskAdapter, values.maskStrategy]);

  useEffect(() => {
    if (!selectedFile || !selectedFile.type.startsWith("video/")) {
      setVideoPreviewUrl("");
      setMediaBox(null);
      return;
    }
    // 本地预览只在浏览器内生成临时 URL，不提前上传；提交任务时再走后端资产接口。
    const url = URL.createObjectURL(selectedFile);
    setVideoPreviewUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [selectedFile]);

  const updateMediaBox = () => {
    const video = videoRef.current;
    const overlay = overlayRef.current;
    if (!video || !overlay || !video.videoWidth || !video.videoHeight) return;

    const videoRect = video.getBoundingClientRect();
    const overlayRect = overlay.getBoundingClientRect();
    const mediaAspect = video.videoWidth / video.videoHeight;
    const boxAspect = videoRect.width / videoRect.height;

    let width = videoRect.width;
    let height = videoRect.height;
    let left = videoRect.left - overlayRect.left;
    let top = videoRect.top - overlayRect.top;

    if (boxAspect > mediaAspect) {
      width = videoRect.height * mediaAspect;
      left += (videoRect.width - width) / 2;
    } else {
      height = videoRect.width / mediaAspect;
      top += (videoRect.height - height) / 2;
    }

    setMediaBox({ left, top, width, height });
  };

  const handleLoadedMetadata = () => {
    updateMediaBox();
    const seconds = videoRef.current?.duration;
    if (seconds && Number.isFinite(seconds)) {
      const roundedSeconds = Math.max(1, Math.ceil(seconds));
      if (values.duration !== roundedSeconds) {
        form.setFieldValue("duration", roundedSeconds);
      }
    }
  };

  useEffect(() => {
    if (!videoPreviewUrl) return;
    updateMediaBox();
    window.addEventListener("resize", updateMediaBox);
    return () => window.removeEventListener("resize", updateMediaBox);
  }, [videoPreviewUrl]);

  const pointFromEvent = (event: PointerEvent<HTMLDivElement>) => {
    const rect = overlayRef.current?.getBoundingClientRect();
    if (!rect || !mediaBox) return null;
    // 将鼠标位置映射到真实视频画面区域，排除播放器左右/上下黑边后再保存 0-1 坐标。
    return {
      x: clamp((event.clientX - rect.left - mediaBox.left) / mediaBox.width),
      y: clamp((event.clientY - rect.top - mediaBox.top) / mediaBox.height),
    };
  };

  const startRegion = (event: PointerEvent<HTMLDivElement>) => {
    if (!isSelectingRegion) return;
    const point = pointFromEvent(event);
    if (!point) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    dragStartRef.current = point;
    setDraftRegion(buildRegion(point, point));
  };

  const moveRegion = (event: PointerEvent<HTMLDivElement>) => {
    const start = dragStartRef.current;
    const point = pointFromEvent(event);
    if (!start || !point) return;
    setDraftRegion(buildRegion(start, point));
  };

  const finishRegion = (event: PointerEvent<HTMLDivElement>) => {
    const start = dragStartRef.current;
    const point = pointFromEvent(event);
    dragStartRef.current = null;
    if (!start || !point) {
      setDraftRegion(null);
      return;
    }
    const nextRegion = buildRegion(start, point);
    setDraftRegion(null);
    if (nextRegion.width < 0.015 || nextRegion.height < 0.015) {
      setNotice(`框选区域太小，请覆盖完整${regionNoun}。`);
      return;
    }
    // 每个视频独立保存一个框选区域，批量任务提交时会按文件分别传给后端。
    setRegionsByFileId((items) => ({ ...items, [selectedFileId]: [nextRegion] }));
    setIsSelectingRegion(false);
    setNotice(`已为当前视频框选${regionNoun}区域，可切换其他视频继续设置。`);
  };

  if (!tool || tool.status === "disabled") {
    return (
      <section className="panel">
        <h1>工具暂未开放</h1>
        <p>请返回工具广场选择一个可用工具。</p>
        <Link className="primary" to="/tools">
          返回工具广场
        </Link>
      </section>
    );
  }

  const available = data.account.availableCredits;
  const accept = tool.pricing.mode === "image" ? "image/*" : "video/*";
  const totalEstimate = selectedFileCount
    ? files.reduce((total, item, index) => {
        const duration = fileDurations[fileBatchId(item, index)] || values.duration;
        return total + estimateCredits(tool, { ...values, duration });
      }, 0)
    : estimate;
  const submitButtonLabel =
    !selectedFileCount
      ? "先选择文件"
      : available < totalEstimate
      ? "余额不足"
      : createTaskMutation.isPending && submitProgress?.stage === "创建任务"
        ? "正在创建任务..."
        : createTaskMutation.isPending && submitProgress
          ? `${submitProgress.stage} ${submitProgress.percent}%`
          : isBatchUpload
            ? `批量创建 ${selectedFileCount} 个任务`
            : "上传并创建任务";

  return (
    <div className="tool-page">
      <Link className="back-link" to="/tools">
        ‹ 返回工具搜索首页
      </Link>
      {notice ? <div className="notice">{notice}</div> : null}
      <form
        className={`tool-detail${isEnhanceTool ? " enhance-detail" : ""}${isMaskVideoTool ? " mask-detail" : ""}${isTranslateTool ? " translate-detail" : ""}`}
        onSubmit={(event) => {
          event.preventDefault();
          if (isMaskVideoTool) {
            const firstMissingIndex = files.findIndex((item, index) => !(regionsByFileId[fileBatchId(item, index)] || []).length);
            if (firstMissingIndex >= 0) {
              setActiveFileIndex(firstMissingIndex);
              setNotice(`请先为第 ${firstMissingIndex + 1} 个视频框选${regionNoun}区域。`);
              setDraftRegion(null);
              setIsSelectingRegion(true);
              return;
            }
          }
          if (isMaskVideoTool && regions.length === 0) {
            setNotice(`请先在视频预览中框选${regionNoun}区域。`);
            return;
          }
          form.handleSubmit();
        }}
      >
        <div className="detail-main">
          <div className="detail-title">
            <ToolIcon type={tool.icon} />
            <div>
              <h1>{tool.name}</h1>
              <p>{tool.summary}</p>
              <div className="detail-badges">
                <span>{tool.pricing.mode === "image" ? "图片工具" : "视频工具"}</span>
                <span>{tool.status === "online" ? "已上线" : "即将上线"}</span>
                {isEnhanceTool ? <span>远端 GPU 超分</span> : null}
                {isMaskVideoTool ? <span>区域框选修复</span> : null}
                {isTranslateTool ? <span>英文硬字幕</span> : null}
                {tool.pricing.mode !== "image" ? <span>支持批处理</span> : null}
              </div>
            </div>
          </div>
          <label className="upload-zone">
            <input
              className="file-input"
              type="file"
              accept={accept}
              multiple={tool.pricing.mode !== "image"}
              onChange={(event) => {
                const nextFiles = Array.from(event.target.files || []);
                setSelectedFiles(nextFiles);
                event.currentTarget.value = "";
              }}
            />
            <div className="upload-visual">+</div>
            <strong>
              {selectedFileCount ? (isBatchUpload ? `已选择 ${selectedFileCount} 个文件` : selectedFile?.name) : tool.pricing.mode === "image" ? "选择图片文件" : "选择视频文件"}
            </strong>
            <span>{selectedFileCount ? (isBatchUpload ? `合计 ${formatBytes(selectedFilesSize)}，可逐个视频设置选区。` : `${formatBytes(selectedFile?.size || 0)}，创建任务时会上传到后端。`) : "超过 32MB 自动使用分片与断点续传。"}</span>
          </label>
          {batchItems.length ? (
            <div className="batch-file-list" aria-label="批量文件列表">
              <div className="batch-file-list-head">
                <strong>{isBatchUpload ? "批量文件" : "待处理文件"}</strong>
                <div>
                  <span>{formatBytes(selectedFilesSize || batchItems.reduce((total, item) => total + item.size, 0))}</span>
                  {tool.pricing.mode !== "image" ? (
                    <>
                      <input
                        ref={appendInputRef}
                        className="file-input"
                        type="file"
                        accept={accept}
                        multiple
                        onChange={(event) => {
                          const nextFiles = Array.from(event.target.files || []);
                          setSelectedFiles(nextFiles, { append: true });
                          event.currentTarget.value = "";
                        }}
                      />
                      <button className="append-file-button" type="button" disabled={createTaskMutation.isPending} onClick={() => appendInputRef.current?.click()}>
                        追加处理文件
                      </button>
                    </>
                  ) : null}
                </div>
              </div>
              <ul>
                {batchItems.map((item, index) => {
                  const itemRegions = regionsByFileId[item.id] || [];
                  const isActiveFile = index === activeFileIndex;
                  return (
                    <li className={`batch-file-item ${item.status}${isActiveFile ? " active" : ""}`} key={item.id}>
                      <div>
                        <strong>{item.name}</strong>
                        <span>
                          {formatBytes(item.size)}
                          {isMaskVideoTool ? ` / ${itemRegions.length ? "已框选" : "待框选"}` : ""}
                        </span>
                      </div>
                      <div className="batch-file-progress">
                        <span>{item.error || item.stage}</span>
                        <div className="submit-progress-track">
                          <span style={{ width: `${item.percent}%` }} />
                        </div>
                      </div>
                      <div className="batch-file-actions">
                        <button
                          className="remove-file-button"
                          type="button"
                          disabled={createTaskMutation.isPending}
                          onClick={() => removeBatchFile(index)}
                          aria-label={`移除 ${item.name}`}
                        >
                          移除
                        </button>
                        {isMaskVideoTool ? (
                          <button
                            className="set-region-button"
                            type="button"
                            disabled={createTaskMutation.isPending}
                            onClick={() => {
                              setActiveFileIndex(index);
                              setDraftRegion(null);
                              setIsSelectingRegion(false);
                              setNotice(itemRegions.length ? `正在预览第 ${index + 1} 个视频，可重新框选${regionNoun}。` : `正在预览第 ${index + 1} 个视频，请框选${regionNoun}区域。`);
                            }}
                          >
                            {itemRegions.length ? "重设选区" : "设置选区"}
                          </button>
                        ) : null}
                      </div>
                    </li>
                  );
                })}
              </ul>
            </div>
          ) : null}
          {showVideoPreview ? (
            <section className="video-region-editor" aria-label={isMaskVideoTool ? `${regionNoun}区域框选` : "视频预览"}>
              <div className="video-preview-wrap">
                <video ref={videoRef} src={videoPreviewUrl} controls playsInline preload="metadata" onLoadedMetadata={handleLoadedMetadata} onLoadedData={updateMediaBox} />
                {isMaskVideoTool ? (
                  <div
                    ref={overlayRef}
                    className={`region-overlay${isSelectingRegion ? " active" : ""}`}
                    onPointerDown={startRegion}
                    onPointerMove={moveRegion}
                    onPointerUp={finishRegion}
                    onPointerCancel={() => {
                      dragStartRef.current = null;
                      setDraftRegion(null);
                      setIsSelectingRegion(false);
                    }}
                  >
                    {[...regions, ...(draftRegion ? [draftRegion] : [])].map((region, index) => (
                      <div
                        className="region-box"
                        key={`${region.x}-${region.y}-${index}`}
                        style={{
                          left: mediaBox ? `${mediaBox.left + region.x * mediaBox.width}px` : `${region.x * 100}%`,
                          top: mediaBox ? `${mediaBox.top + region.y * mediaBox.height}px` : `${region.y * 100}%`,
                          width: mediaBox ? `${region.width * mediaBox.width}px` : `${region.width * 100}%`,
                          height: mediaBox ? `${region.height * mediaBox.height}px` : `${region.height * 100}%`,
                        }}
                      />
                    ))}
                  </div>
                ) : null}
              </div>
              {isMaskVideoTool ? (
                <div className="region-help">
                  <span>{isSelectingRegion ? `在第 ${activeFileIndex + 1} 个视频上拖拽框选${regionNoun}区域` : regions.length ? `第 ${activeFileIndex + 1} 个视频已选择 1 个${regionNoun}区域` : `可先播放预览，再为第 ${activeFileIndex + 1} 个视频框选${regionNoun}区域`}</span>
                  <button
                    className="link-button"
                    type="button"
                    onClick={() => {
                      setDraftRegion(null);
                      setIsSelectingRegion((value) => !value);
                    }}
                  >
                    {isSelectingRegion ? "取消框选" : `框选${regionNoun}`}
                  </button>
                  {regions.length && isBatchUpload ? (
                    <button
                      className="link-button"
                      type="button"
                      onClick={() => {
                        setRegionsByFileId(Object.fromEntries(files.map((item, index) => [fileBatchId(item, index), regions])));
                        setNotice(`已把当前${regionNoun}选区复制到全部视频。`);
                      }}
                    >
                      复制到全部
                    </button>
                  ) : null}
                  {regions.length ? (
                    <button
                      className="link-button"
                      type="button"
                      onClick={() => {
                        setRegionsByFileId((items) => {
                          const nextItems = { ...items };
                          delete nextItems[selectedFileId];
                          return nextItems;
                        });
                        setNotice("");
                        setIsSelectingRegion(false);
                      }}
                    >
                      清除
                    </button>
                  ) : null}
                </div>
              ) : (
                <div className="region-help">
                  <span>已读取视频预览，时长会自动用于计费。</span>
                </div>
              )}
            </section>
          ) : null}
          <div className="form-grid">
            {tool.inputs.includes("duration") && !isEnhanceTool ? (
              <form.Field name="duration">
                {(field) => (
                  <label>
                    视频时长（自动）
                    <input type="number" min={1} max={7200} value={field.state.value} readOnly={Boolean(videoPreviewUrl)} onChange={(event) => field.handleChange(Number(event.target.value))} />
                  </label>
                )}
              </form.Field>
            ) : null}
            {tool.inputs.includes("resolution") ? (
              <form.Field name="resolution">
                {(field) => (
                  <label>
                    输出清晰度
                    <select value={field.state.value} onChange={(event) => field.handleChange(event.target.value)}>
                      {["720p", "1080p", "2K", "4K"].map((item) => (
                        <option value={item} key={item}>
                          {item}
                        </option>
                      ))}
                    </select>
                  </label>
                )}
              </form.Field>
            ) : null}
            {isEnhanceTool ? (
              <>
                <form.Field name="enhanceMode">
                  {(field) => (
                    <label>
                      增强模式
                      <select value={field.state.value} onChange={(event) => field.handleChange(event.target.value as ToolFormValues["enhanceMode"])}>
                        <option value="quality">高质量超分</option>
                        <option value="natural">自然增强</option>
                      </select>
                    </label>
                  )}
                </form.Field>
                <form.Field name="keepAudio">
                  {(field) => (
                    <label className="checkbox-field">
                      <input type="checkbox" checked={field.state.value} onChange={(event) => field.handleChange(event.target.checked)} />
                      保留原音频
                    </label>
                  )}
                </form.Field>
              </>
            ) : null}
            {isTranslateTool ? (
              <>
                <form.Field name="targetLanguage">
                  {(field) => (
                    <label>
                      目标语言
                      <select value={field.state.value} onChange={(event) => field.handleChange(event.target.value as ToolFormValues["targetLanguage"])}>
                        <option value="en">英文</option>
                      </select>
                    </label>
                  )}
                </form.Field>
                <form.Field name="subtitlePlacement">
                  {(field) => (
                    <label>
                      字幕位置
                      <select value={field.state.value} onChange={(event) => field.handleChange(event.target.value as ToolFormValues["subtitlePlacement"])}>
                        <option value="bottom">底部</option>
                        <option value="middle-lower">中下</option>
                        <option value="top">顶部</option>
                      </select>
                    </label>
                  )}
                </form.Field>
                <form.Field name="keepAudio">
                  {(field) => (
                    <label className="checkbox-field">
                      <input type="checkbox" checked={field.state.value} onChange={(event) => field.handleChange(event.target.checked)} />
                      保留原音频
                    </label>
                  )}
                </form.Field>
              </>
            ) : null}
            {tool.inputs.includes("imageCount") ? (
              <form.Field name="imageCount">
                {(field) => (
                  <label>
                    图片数量
                    <input type="number" min={1} max={200} value={field.state.value} onChange={(event) => field.handleChange(Number(event.target.value))} />
                  </label>
                )}
              </form.Field>
            ) : null}
            {tool.inputs.includes("watermarkCount") ? (
              <form.Field name="watermarkCount">
                {(field) => (
                  <label>
                    水印数量
                    <select value={field.state.value} onChange={(event) => field.handleChange(Number(event.target.value))}>
                      <option value={1}>单个</option>
                      <option value={2}>多个</option>
                    </select>
                  </label>
                )}
              </form.Field>
            ) : null}
            {isMaskVideoTool ? (
              <>
                <form.Field name="modelAdapter">
                  {(field) => (
                    <label>
                      修复模式
                      <select
                        value={field.state.value}
                        onChange={(event) => {
                          const nextAdapter = event.target.value as ToolFormValues["modelAdapter"];
                          field.handleChange(nextAdapter);
                          if (nextAdapter !== "opencv-inpaint" && nextAdapter !== "propainter") {
                            form.setFieldValue("maskStrategy", "rectangle");
                          }
                        }}
                      >
                        <option value="propainter">高质量修复</option>
                        <option value="opencv-inpaint">轻量修复</option>
                        <option value="ffmpeg-delogo">快速遮盖</option>
                      </select>
                    </label>
                  )}
                </form.Field>
                {showTextMaskControls ? (
                  <>
                    <form.Field name="maskStrategy">
                      {(field) => (
                        <label>
                          遮罩策略
                          <select value={field.state.value} onChange={(event) => field.handleChange(event.target.value as ToolFormValues["maskStrategy"])}>
                            <option value="subtitle-text">{isSubtitleTool ? "字幕文字精修" : "文字水印精修"}</option>
                            {isSubtitleTool ? <option value="dark-subtitle-line">黑色字幕整行修复</option> : null}
                            <option value="rectangle">整块区域修复</option>
                          </select>
                        </label>
                      )}
                    </form.Field>
                    {showTextThreshold ? (
                      <form.Field name="textLightThreshold">
                        {(field) => (
                          <label>
                            {isSubtitleTool ? "字幕亮度阈值" : "文字亮度阈值"}
                            <input type="number" min={80} max={245} value={field.state.value} onChange={(event) => field.handleChange(Number(event.target.value))} />
                          </label>
                        )}
                      </form.Field>
                    ) : null}
                  </>
                ) : null}
                {showOpenCvControls ? (
                  <>
                    <form.Field name="inpaintMethod">
                      {(field) => (
                        <label>
                          模型算法
                          <select value={field.state.value} onChange={(event) => field.handleChange(event.target.value as ToolFormValues["inpaintMethod"])}>
                            <option value="telea">Telea</option>
                            <option value="ns">Navier-Stokes</option>
                          </select>
                        </label>
                      )}
                    </form.Field>
                    <form.Field name="inpaintRadius">
                      {(field) => (
                        <label>
                          修复半径
                          <input type="number" min={1} max={32} value={field.state.value} onChange={(event) => field.handleChange(Number(event.target.value))} />
                        </label>
                      )}
                    </form.Field>
                  </>
                ) : null}
                {showMaskPadding ? (
                  <form.Field name="maskPadding">
                    {(field) => (
                      <label>
                        遮罩扩展
                        <input type="number" min={0} max={80} value={field.state.value} onChange={(event) => field.handleChange(Number(event.target.value))} />
                      </label>
                    )}
                  </form.Field>
                ) : null}
                <form.Field name="keepAudio">
                  {(field) => (
                    <label className="checkbox-field">
                      <input type="checkbox" checked={field.state.value} onChange={(event) => field.handleChange(event.target.checked)} />
                      保留原音频
                    </label>
                  )}
                </form.Field>
              </>
            ) : null}
            {tool.inputs.includes("maskComplexity") ? (
              <form.Field name="maskComplexity">
                {(field) => (
                  <label>
                    消除复杂度
                    <select value={field.state.value} onChange={(event) => field.handleChange(event.target.value as ToolFormValues["maskComplexity"])}>
                      <option value="normal">普通</option>
                      <option value="complex">复杂</option>
                    </select>
                  </label>
                )}
              </form.Field>
            ) : null}
            {tool.inputs.includes("languageCount") ? (
              <form.Field name="languageCount">
                {(field) => (
                  <label>
                    目标语言数
                    <input type="number" min={1} max={12} value={field.state.value} onChange={(event) => field.handleChange(Number(event.target.value))} />
                  </label>
                )}
              </form.Field>
            ) : null}
            {tool.inputs.includes("priority") ? (
              <form.Field name="priority">
                {(field) => (
                  <label>
                    队列优先级
                    <select value={field.state.value} onChange={(event) => field.handleChange(event.target.value as ToolFormValues["priority"])}>
                      <option value="standard">标准</option>
                      <option value="express">加急</option>
                    </select>
                  </label>
                )}
              </form.Field>
            ) : null}
          </div>
        </div>
        <aside className="quote-panel">
          <span className="quote-kicker">任务结算</span>
          <h2>费用预估</h2>
          <div className="quote-number">{formatCredits(totalEstimate)}</div>
          <dl>
            {isBatchUpload ? (
              <div>
                <dt>本次数量</dt>
                <dd>{selectedFileCount} 个文件</dd>
              </div>
            ) : null}
            <div>
              <dt>计费方式</dt>
              <dd>{tool.pricing.mode === "image" ? "按图片数量" : `按 ${tool.pricing.unitSeconds} 秒阶梯`}</dd>
            </div>
            <div>
              <dt>最低消费</dt>
              <dd>{formatCredits(tool.pricing.minimumCredits)}</dd>
            </div>
            <div>
              <dt>可用余额</dt>
              <dd>{formatCredits(available)}</dd>
            </div>
          </dl>
          <button className="primary wide" type="submit" disabled={!selectedFileCount || available < totalEstimate || createTaskMutation.isPending}>
            {submitButtonLabel}
          </button>
          {createTaskMutation.isPending && submitProgress ? (
            <div className="submit-progress" aria-live="polite">
              <div className="submit-progress-head">
                <span>{submitProgress.stage}</span>
                <strong>{submitProgress.percent}%</strong>
              </div>
              <div className="submit-progress-track">
                <span style={{ width: `${submitProgress.percent}%` }} />
              </div>
              <p>
                {submitProgress.stage === "创建任务"
                  ? "视频已上传，正在提交处理参数。"
                  : `已上传 ${formatBytes(submitProgress.uploadedBytes)} / ${formatBytes(submitProgress.totalBytes)}`}
              </p>
            </div>
          ) : null}
          <p className="fine-print">创建任务后后端冻结积分；供应商回调成功后扣费，失败则释放冻结积分。</p>
        </aside>
      </form>
    </div>
  );
}
