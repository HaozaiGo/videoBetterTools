import { Link } from "@tanstack/react-router";
import { useMutation, useQueryClient, useSuspenseQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState, type PointerEvent } from "react";
import { createTask, downloadInternalBatchZip, getBootstrap, getInternalBatchStatus, uploadAsset } from "../api/client";
import { formatCredits } from "../lib/format";
import { translateTargetLanguages, type TranslateTargetLanguage } from "../lib/translate-languages";
import type { BootstrapState, InternalBatchStatus, WatermarkRegion } from "../types";
import { estimateCredits } from "../tool-config.js";

type MediaBox = {
  left: number;
  top: number;
  width: number;
  height: number;
};

type BatchItem = {
  id: string;
  name: string;
  size: number;
  status: "pending" | "uploading" | "creating" | "created" | "failed";
  stage: string;
  percent: number;
  taskId?: string;
  error?: string;
};

type SubtitleModelAdapter = "propainter" | "opencv-inpaint" | "ffmpeg-delogo";
type SubtitleMaskStrategy = "subtitle-text" | "dark-subtitle-line" | "rectangle";
type InpaintMethod = "telea" | "ns";

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

function fileBatchId(file: File, index: number) {
  return `${file.name}-${file.size}-${file.lastModified}-${index}`;
}

function createBatchId() {
  return typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : `batch-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function buildRegion(start: { x: number; y: number }, end: { x: number; y: number }): WatermarkRegion {
  const x = clamp(Math.min(start.x, end.x));
  const y = clamp(Math.min(start.y, end.y));
  const width = Math.max(0, Math.min(1 - x, Math.abs(end.x - start.x)));
  const height = Math.max(0, Math.min(1 - y, Math.abs(end.y - start.y)));
  return { x, y, width, height };
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

export function InternalBatchWorkflowPage() {
  const queryClient = useQueryClient();
  const { data } = useSuspenseQuery({ queryKey: ["bootstrap"], queryFn: getBootstrap });
  const workflowTool = data.tools.find((tool) => tool.slug === "subtitle-translate-workflow");
  const [files, setFiles] = useState<File[]>([]);
  const [items, setItems] = useState<BatchItem[]>([]);
  const [durations, setDurations] = useState<Record<string, number>>({});
  const [activeIndex, setActiveIndex] = useState(0);
  const [regionsByFileId, setRegionsByFileId] = useState<Record<string, WatermarkRegion[]>>({});
  const [draftRegion, setDraftRegion] = useState<WatermarkRegion | null>(null);
  const [isSelecting, setIsSelecting] = useState(false);
  const [videoUrl, setVideoUrl] = useState("");
  const [mediaBox, setMediaBox] = useState<MediaBox | null>(null);
  const [notice, setNotice] = useState("");
  const [batchName, setBatchName] = useState("");
  const [activeBatch, setActiveBatch] = useState<{ id: string; name: string; status?: InternalBatchStatus } | null>(null);
  const [targetLanguage, setTargetLanguage] = useState<TranslateTargetLanguage>("en");
  const [subtitlePlacement, setSubtitlePlacement] = useState<"bottom" | "middle-lower" | "top">("bottom");
  const [keepAudio, setKeepAudio] = useState(true);
  const [priority, setPriority] = useState<"standard" | "express">("standard");
  const [modelAdapter, setModelAdapter] = useState<SubtitleModelAdapter>("propainter");
  const [maskStrategy, setMaskStrategy] = useState<SubtitleMaskStrategy>("subtitle-text");
  const [maskPadding, setMaskPadding] = useState(8);
  const [textLightThreshold, setTextLightThreshold] = useState(155);
  const [inpaintMethod, setInpaintMethod] = useState<InpaintMethod>("telea");
  const [inpaintRadius, setInpaintRadius] = useState(5);
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const overlayRef = useRef<HTMLDivElement | null>(null);
  const dragStartRef = useRef<{ x: number; y: number } | null>(null);
  const appendInputRef = useRef<HTMLInputElement | null>(null);
  const selectedFile = files[activeIndex] || null;
  const selectedFileId = selectedFile ? fileBatchId(selectedFile, activeIndex) : "";
  const regions = selectedFileId ? regionsByFileId[selectedFileId] || [] : [];
  const selectedSize = useMemo(() => files.reduce((total, item) => total + item.size, 0), [files]);
  const totalEstimate = workflowTool
    ? files.reduce((total, file, index) => {
        const duration = durations[fileBatchId(file, index)] || 30;
        return total + estimateCredits(workflowTool, { duration, priority });
      }, 0)
    : 0;
  const available = data.account.availableCredits;
  const showTextThreshold = modelAdapter !== "ffmpeg-delogo" && maskStrategy === "subtitle-text";
  const showMaskPadding = modelAdapter !== "ffmpeg-delogo";
  const showOpenCvControls = modelAdapter === "opencv-inpaint";

  useEffect(() => {
    if (modelAdapter === "ffmpeg-delogo" && maskStrategy !== "rectangle") {
      setMaskStrategy("rectangle");
    }
  }, [maskStrategy, modelAdapter]);

  const setSelectedFiles = (nextFiles: File[], options: { append?: boolean } = {}) => {
    if (!nextFiles.length) return;
    const mergedFiles = options.append ? [...files, ...nextFiles] : nextFiles;
    setFiles(mergedFiles);
    setItems(
      mergedFiles.map((file, index) => ({
        id: fileBatchId(file, index),
        name: file.name,
        size: file.size,
        status: "pending",
        stage: "等待框选",
        percent: 0,
      })),
    );
    setDurations({});
    if (!options.append) {
      setActiveIndex(0);
      setRegionsByFileId({});
      setDraftRegion(null);
    }
    setNotice(options.append ? `已追加 ${nextFiles.length} 个视频。` : "");
  };

  const updateItem = (id: string, patch: Partial<BatchItem>) => {
    setItems((currentItems) => currentItems.map((item) => (item.id === id ? { ...item, ...patch } : item)));
  };

  const removeFile = (removeIndex: number) => {
    if (submitMutation.isPending) return;
    const nextFiles = files.filter((_, index) => index !== removeIndex);
    const nextRegions: Record<string, WatermarkRegion[]> = {};
    const nextItems = nextFiles.map((file, index) => {
      const oldIndex = index >= removeIndex ? index + 1 : index;
      const oldId = fileBatchId(file, oldIndex);
      const nextId = fileBatchId(file, index);
      if (regionsByFileId[oldId]) nextRegions[nextId] = regionsByFileId[oldId];
      return {
        id: nextId,
        name: file.name,
        size: file.size,
        status: "pending" as const,
        stage: nextRegions[nextId]?.length ? "已框选" : "等待框选",
        percent: 0,
      };
    });
    setFiles(nextFiles);
    setItems(nextItems);
    setRegionsByFileId(nextRegions);
    setActiveIndex((index) => (nextFiles.length ? Math.min(index, nextFiles.length - 1) : 0));
    setNotice(nextFiles.length ? "已移除视频。" : "已清空视频。");
  };

  useEffect(() => {
    let cancelled = false;
    setDurations({});
    Promise.all(files.map(async (file, index) => [fileBatchId(file, index), await readVideoDuration(file)] as const)).then((entries) => {
      if (cancelled) return;
      setDurations(Object.fromEntries(entries.filter((entry): entry is readonly [string, number] => Boolean(entry[1]))));
    });
    return () => {
      cancelled = true;
    };
  }, [files]);

  useEffect(() => {
    if (!selectedFile || !selectedFile.type.startsWith("video/")) {
      setVideoUrl("");
      setMediaBox(null);
      return;
    }
    const url = URL.createObjectURL(selectedFile);
    setVideoUrl(url);
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

  useEffect(() => {
    if (!videoUrl) return;
    updateMediaBox();
    window.addEventListener("resize", updateMediaBox);
    return () => window.removeEventListener("resize", updateMediaBox);
  }, [videoUrl]);

  const pointFromEvent = (event: PointerEvent<HTMLDivElement>) => {
    const rect = overlayRef.current?.getBoundingClientRect();
    if (!rect || !mediaBox) return null;
    return {
      x: clamp((event.clientX - rect.left - mediaBox.left) / mediaBox.width),
      y: clamp((event.clientY - rect.top - mediaBox.top) / mediaBox.height),
    };
  };

  const startRegion = (event: PointerEvent<HTMLDivElement>) => {
    if (!isSelecting) return;
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
    setDraftRegion(null);
    if (!start || !point) return;
    const nextRegion = buildRegion(start, point);
    if (nextRegion.width < 0.015 || nextRegion.height < 0.015) {
      setNotice("框选区域太小，请覆盖完整字幕行。");
      return;
    }
    setRegionsByFileId((currentRegions) => ({ ...currentRegions, [selectedFileId]: [nextRegion] }));
    updateItem(selectedFileId, { stage: "已框选" });
    setIsSelecting(false);
    setNotice(`已为第 ${activeIndex + 1} 个视频框选字幕区域。`);
  };

  const statusMutation = useMutation({
    mutationFn: getInternalBatchStatus,
    onSuccess: (status) => {
      setActiveBatch({ id: status.id, name: status.name, status });
    },
    onError: (error) => {
      setNotice(error instanceof Error ? error.message : "批次状态刷新失败");
    },
  });

  const downloadMutation = useMutation({
    mutationFn: async () => {
      if (!activeBatch) throw new Error("还没有可下载的批次");
      await downloadInternalBatchZip(activeBatch.id, activeBatch.name);
    },
    onError: (error) => {
      setNotice(error instanceof Error ? error.message : "批次压缩包下载失败");
    },
  });

  const submitMutation = useMutation({
    mutationFn: async () => {
      if (!workflowTool) throw new Error("内部工作流暂不可用");
      if (!files.length) throw new Error("请先上传视频");
      const trimmedBatchName = batchName.trim();
      if (!trimmedBatchName) throw new Error("请先输入本次批量任务的总名称");
      const firstMissingIndex = files.findIndex((file, index) => !(regionsByFileId[fileBatchId(file, index)] || []).length);
      if (firstMissingIndex >= 0) {
        setActiveIndex(firstMissingIndex);
        setIsSelecting(true);
        throw new Error(`请先为第 ${firstMissingIndex + 1} 个视频框选字幕区域`);
      }
      const internalBatchId = createBatchId();
      let latestState: BootstrapState | null = null;
      let createdCount = 0;
      let failedCount = 0;

      for (const [index, file] of files.entries()) {
        const id = fileBatchId(file, index);
        const duration = durations[id] || 30;
        try {
          updateItem(id, { status: "uploading", stage: "上传视频", percent: 0, error: "" });
          const upload = await uploadAsset({
            file,
            kind: "video",
            durationSeconds: duration,
            onProgress: ({ percent, stage }) => updateItem(id, { percent, stage: `${stage}（${index + 1}/${files.length}）` }),
          });
          updateItem(id, { status: "creating", stage: "创建去字幕+翻译任务", percent: 100 });
          const payload = await createTask({
            toolSlug: workflowTool.slug,
            inputAssetId: upload.asset.id,
            params: {
              duration,
              mode: "manual",
              regions: regionsByFileId[id],
              removalTarget: "subtitle",
              modelAdapter,
              maskStrategy,
              maskPadding,
              textLightThreshold,
              inpaintMethod,
              inpaintRadius,
              internalBatchId,
              internalBatchName: trimmedBatchName,
              targetLanguage,
              subtitlePlacement,
              keepAudio,
              priority,
            },
          });
          latestState = payload.state;
          createdCount += 1;
          queryClient.setQueryData<BootstrapState>(["bootstrap"], payload.state);
          updateItem(id, { status: "created", stage: "工作流任务已创建", percent: 100, taskId: payload.task.id });
        } catch (error) {
          failedCount += 1;
          updateItem(id, {
            status: "failed",
            stage: "创建失败",
            error: error instanceof Error ? error.message : "任务创建失败",
          });
        }
      }

      if (!createdCount) throw new Error("批量工作流任务创建失败，请查看文件列表。");
      return { state: latestState, createdCount, failedCount, batchId: internalBatchId, batchName: trimmedBatchName };
    },
    onSuccess: (payload) => {
      if (payload.state) queryClient.setQueryData<BootstrapState>(["bootstrap"], payload.state);
      setActiveBatch({ id: payload.batchId, name: payload.batchName });
      statusMutation.mutate(payload.batchId);
      setNotice(payload.failedCount ? `已创建 ${payload.createdCount} 个工作流任务，${payload.failedCount} 个失败。` : `已创建 ${payload.createdCount} 个工作流任务。`);
    },
    onError: (error) => {
      setNotice(error instanceof Error ? error.message : "任务创建失败");
    },
  });

  const visibleBatchStatus = statusMutation.data || activeBatch?.status || null;

  if (!workflowTool) {
    return (
      <section className="panel">
        <h1>内部工作流暂不可用</h1>
        <p>后端还没有启用去字幕加翻译工作流。</p>
        <Link className="primary" to="/tools">
          返回工具广场
        </Link>
      </section>
    );
  }

  return (
    <div className="internal-workflow-page">
      <Link className="back-link" to="/tools">
        ‹ 返回工具搜索首页
      </Link>
      {notice ? <div className="notice">{notice}</div> : null}
      <section className="internal-workflow-head">
        <span>INTERNAL BATCH WORKFLOW</span>
        <h1>批量去字幕并翻译</h1>
        <p>上传多条视频，逐条框选原字幕区域，选择目标语言后创建顺序处理任务。</p>
      </section>

      <section className="internal-workflow-grid">
        <div className="internal-workflow-main">
          <label className="upload-zone internal-upload-zone">
            <input
              className="file-input"
              type="file"
              accept="video/*"
              multiple
              onChange={(event) => {
                setSelectedFiles(Array.from(event.target.files || []));
                event.currentTarget.value = "";
              }}
            />
            <div className="upload-visual">+</div>
            <strong>{files.length ? `已选择 ${files.length} 个视频` : "批量选择视频"}</strong>
            <span>{files.length ? `合计 ${formatBytes(selectedSize)}，可逐个框选字幕区域。` : "支持多选；大文件会自动使用分片上传。"}</span>
          </label>

          {items.length ? (
            <div className="batch-file-list">
              <div className="batch-file-list-head">
                <strong>批量视频</strong>
                <div>
                  <span>{formatBytes(selectedSize)}</span>
                  <input
                    ref={appendInputRef}
                    className="file-input"
                    type="file"
                    accept="video/*"
                    multiple
                    onChange={(event) => {
                      setSelectedFiles(Array.from(event.target.files || []), { append: true });
                      event.currentTarget.value = "";
                    }}
                  />
                  <button className="append-file-button" type="button" disabled={submitMutation.isPending} onClick={() => appendInputRef.current?.click()}>
                    追加视频
                  </button>
                </div>
              </div>
              <ul>
                {items.map((item, index) => {
                  const itemRegions = regionsByFileId[item.id] || [];
                  return (
                    <li className={`batch-file-item ${item.status}${index === activeIndex ? " active" : ""}`} key={item.id}>
                      <div>
                        <strong>{item.name}</strong>
                        <span>
                          {formatBytes(item.size)} / {itemRegions.length ? "已框选" : "待框选"}
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
                          className="set-region-button"
                          type="button"
                          disabled={submitMutation.isPending}
                          onClick={() => {
                            setActiveIndex(index);
                            setDraftRegion(null);
                            setIsSelecting(false);
                            setNotice(itemRegions.length ? `正在查看第 ${index + 1} 个视频，可重新框选。` : `请为第 ${index + 1} 个视频框选字幕区域。`);
                          }}
                        >
                          {itemRegions.length ? "重设选区" : "设置选区"}
                        </button>
                        <button className="remove-file-button" type="button" disabled={submitMutation.isPending} onClick={() => removeFile(index)}>
                          移除
                        </button>
                      </div>
                    </li>
                  );
                })}
              </ul>
            </div>
          ) : null}

          {videoUrl ? (
            <section className="video-region-editor">
              <div className="video-preview-wrap">
                <video ref={videoRef} src={videoUrl} controls playsInline preload="metadata" onLoadedData={updateMediaBox} onLoadedMetadata={updateMediaBox} />
                <div
                  ref={overlayRef}
                  className={`region-overlay${isSelecting ? " active" : ""}`}
                  onPointerDown={startRegion}
                  onPointerMove={moveRegion}
                  onPointerUp={finishRegion}
                  onPointerCancel={() => {
                    dragStartRef.current = null;
                    setDraftRegion(null);
                    setIsSelecting(false);
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
              </div>
              <div className="region-help">
                <span>{isSelecting ? `拖拽框选第 ${activeIndex + 1} 个视频的字幕区域` : regions.length ? `第 ${activeIndex + 1} 个视频已框选字幕区域` : `为第 ${activeIndex + 1} 个视频框选字幕区域`}</span>
                <button className="link-button" type="button" onClick={() => setIsSelecting((value) => !value)}>
                  {isSelecting ? "取消框选" : "框选字幕"}
                </button>
                {regions.length ? (
                  <button
                    className="link-button"
                    type="button"
                    onClick={() => {
                      setRegionsByFileId(Object.fromEntries(files.map((file, index) => [fileBatchId(file, index), regions])));
                      setItems((currentItems) => currentItems.map((item) => ({ ...item, stage: "已框选" })));
                      setNotice("已把当前选区复制到全部视频。");
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
                      setRegionsByFileId((currentRegions) => {
                        const nextRegions = { ...currentRegions };
                        delete nextRegions[selectedFileId];
                        return nextRegions;
                      });
                      updateItem(selectedFileId, { stage: "等待框选" });
                    }}
                  >
                    清除
                  </button>
                ) : null}
              </div>
            </section>
          ) : null}
        </div>

        <aside className="quote-panel internal-workflow-panel">
          <span className="quote-kicker">工作流设置</span>
          <h2>去字幕 + 翻译</h2>
          <label>
            批次总名称
            <input value={batchName} onChange={(event) => setBatchName(event.target.value)} placeholder="例如：1688口播素材-0618" />
          </label>
          <div className="internal-settings-group">
            <strong>去字幕参数</strong>
            <label>
              修复模式
              <select
                value={modelAdapter}
                onChange={(event) => {
                  const nextAdapter = event.target.value as SubtitleModelAdapter;
                  setModelAdapter(nextAdapter);
                  if (nextAdapter === "ffmpeg-delogo") {
                    setMaskStrategy("rectangle");
                  }
                }}
              >
                <option value="propainter">高质量修复 ProPainter</option>
                <option value="opencv-inpaint">轻量修复 OpenCV</option>
                <option value="ffmpeg-delogo">快速遮盖 FFmpeg</option>
              </select>
            </label>
            <label>
              遮罩策略
              <select value={maskStrategy} disabled={modelAdapter === "ffmpeg-delogo"} onChange={(event) => setMaskStrategy(event.target.value as SubtitleMaskStrategy)}>
                <option value="subtitle-text">字幕文字精修</option>
                <option value="dark-subtitle-line">黑色字幕整行修复</option>
                <option value="rectangle">整块区域修复</option>
              </select>
            </label>
            {showTextThreshold ? (
              <label>
                字幕亮度阈值
                <input type="number" min={80} max={245} value={textLightThreshold} onChange={(event) => setTextLightThreshold(Number(event.target.value))} />
              </label>
            ) : null}
            {showMaskPadding ? (
              <label>
                遮罩扩展
                <input type="number" min={0} max={80} value={maskPadding} onChange={(event) => setMaskPadding(Number(event.target.value))} />
              </label>
            ) : null}
            {showOpenCvControls ? (
              <>
                <label>
                  OpenCV 算法
                  <select value={inpaintMethod} onChange={(event) => setInpaintMethod(event.target.value as InpaintMethod)}>
                    <option value="telea">Telea</option>
                    <option value="ns">Navier-Stokes</option>
                  </select>
                </label>
                <label>
                  修复半径
                  <input type="number" min={1} max={32} value={inpaintRadius} onChange={(event) => setInpaintRadius(Number(event.target.value))} />
                </label>
              </>
            ) : null}
          </div>
          <div className="internal-settings-group">
            <strong>翻译参数</strong>
          <label>
            目标语言
            <select value={targetLanguage} onChange={(event) => setTargetLanguage(event.target.value as TranslateTargetLanguage)}>
              {translateTargetLanguages.map((language) => (
                <option key={language.value} value={language.value}>
                  {language.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            字幕位置
            <select value={subtitlePlacement} onChange={(event) => setSubtitlePlacement(event.target.value as "bottom" | "middle-lower" | "top")}>
              <option value="bottom">底部</option>
              <option value="middle-lower">中下</option>
              <option value="top">顶部</option>
            </select>
          </label>
          <label>
            队列优先级
            <select value={priority} onChange={(event) => setPriority(event.target.value as "standard" | "express")}>
              <option value="standard">标准</option>
              <option value="express">加急</option>
            </select>
          </label>
          <label className="checkbox-field">
            <input type="checkbox" checked={keepAudio} onChange={(event) => setKeepAudio(event.target.checked)} />
            保留原音频
          </label>
          </div>
          <div className="internal-workflow-summary">
            <dl>
              <div>
                <dt>视频数量</dt>
                <dd>{files.length}</dd>
              </div>
              <div>
                <dt>费用预估</dt>
                <dd>{totalEstimate === 0 ? "内部通道免积分" : formatCredits(totalEstimate)}</dd>
              </div>
              <div>
                <dt>可用余额</dt>
                <dd>{formatCredits(available)}</dd>
              </div>
            </dl>
          </div>
          {activeBatch ? (
            <div className="internal-batch-download">
              <strong>{activeBatch.name}</strong>
              {visibleBatchStatus ? (
                <span>
                  已完成 {visibleBatchStatus.succeeded}/{visibleBatchStatus.total}
                  {visibleBatchStatus.failed ? `，失败 ${visibleBatchStatus.failed}` : ""}
                  {visibleBatchStatus.cancelled ? `，已取消 ${visibleBatchStatus.cancelled}` : ""}
                </span>
              ) : (
                <span>批次已创建，可刷新状态。</span>
              )}
              <div>
                <button className="ghost compact" type="button" disabled={statusMutation.isPending} onClick={() => statusMutation.mutate(activeBatch.id)}>
                  {statusMutation.isPending ? "刷新中..." : "刷新状态"}
                </button>
                <button className="primary compact" type="button" disabled={!visibleBatchStatus?.downloadReady || downloadMutation.isPending} onClick={() => downloadMutation.mutate()}>
                  {downloadMutation.isPending ? "准备中..." : "下载压缩包"}
                </button>
              </div>
            </div>
          ) : null}
          <button className="primary wide" type="button" disabled={!files.length || !batchName.trim() || available < totalEstimate || submitMutation.isPending} onClick={() => submitMutation.mutate()}>
            {!files.length ? "先上传视频" : !batchName.trim() ? "先输入批次名称" : available < totalEstimate ? "余额不足" : submitMutation.isPending ? "正在创建..." : `创建 ${files.length} 个工作流任务`}
          </button>
          <p className="fine-print">每个视频会先去除原字幕，再对处理后的视频生成目标语言硬字幕。</p>
        </aside>
      </section>
    </div>
  );
}
