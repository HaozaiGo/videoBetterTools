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

export function ToolPage() {
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  const queryClient = useQueryClient();
  const { data } = useSuspenseQuery({ queryKey: ["bootstrap"], queryFn: getBootstrap });
  const [file, setFile] = useState<File | null>(null);
  const [notice, setNotice] = useState("");
  const [videoPreviewUrl, setVideoPreviewUrl] = useState("");
  const [regions, setRegions] = useState<WatermarkRegion[]>([]);
  const [draftRegion, setDraftRegion] = useState<WatermarkRegion | null>(null);
  const [isSelectingRegion, setIsSelectingRegion] = useState(false);
  const [mediaBox, setMediaBox] = useState<MediaBox | null>(null);
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const overlayRef = useRef<HTMLDivElement | null>(null);
  const dragStartRef = useRef<{ x: number; y: number } | null>(null);
  const tool = data.tools.find((item) => item.route === pathname);
  const isWatermarkTool = tool?.slug === "remove-watermark";
  const isSubtitleTool = tool?.slug === "remove-subtitle";
  const isEnhanceTool = tool?.slug === "enhance";
  const isTranslateTool = tool?.slug === "translate";
  const isMaskVideoTool = isWatermarkTool || isSubtitleTool;
  const showVideoPreview = Boolean(videoPreviewUrl && (isMaskVideoTool || isEnhanceTool || isTranslateTool));
  const regionNoun = isSubtitleTool ? "字幕" : "水印";

  const createTaskMutation = useMutation({
    mutationFn: async (values: ToolFormValues) => {
      if (!tool) throw new Error("工具不存在");
      if (!file) throw new Error("请先选择一个文件");
      const taskValues = {
        ...values,
        resolution: tool.inputs.includes("resolution") ? values.resolution : "",
      };
      const params = isMaskVideoTool
        ? {
            ...taskValues,
            mode: "manual" as const,
            regions,
            removalTarget: isSubtitleTool ? "subtitle" : "watermark",
            maskStrategy: values.maskStrategy,
          }
        : isEnhanceTool
          ? {
              duration: values.duration,
              resolution: values.resolution,
              enhanceMode: values.enhanceMode,
              keepAudio: values.keepAudio,
              priority: values.priority,
            }
          : isTranslateTool
            ? {
                duration: values.duration,
                targetLanguage: values.targetLanguage,
                subtitlePlacement: values.subtitlePlacement,
                keepAudio: values.keepAudio,
                priority: values.priority,
              }
          : taskValues;
      const upload = await uploadAsset({
        file,
        kind: tool.pricing.mode === "image" ? "image" : "video",
        durationSeconds: values.duration,
        onProgress: ({ percent, stage }) => {
          setNotice(`${stage}：${percent}%`);
        },
      });
      return createTask({ toolSlug: tool.slug, inputAssetId: upload.asset.id, params });
    },
    onSuccess: (payload) => {
      queryClient.setQueryData<BootstrapState>(["bootstrap"], payload.state);
      setFile(null);
      setRegions([]);
      setDraftRegion(null);
      setNotice("任务已创建，积分已冻结。");
    },
    onError: (error) => setNotice(error.message),
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
    if (isMaskVideoTool && !isTextMaskAdapter && values.maskStrategy !== "rectangle") {
      form.setFieldValue("maskStrategy", "rectangle");
    }
  }, [form, isMaskVideoTool, isTextMaskAdapter, values.maskStrategy]);

  useEffect(() => {
    if (!file || !file.type.startsWith("video/")) {
      setVideoPreviewUrl("");
      setMediaBox(null);
      return;
    }
    // 本地预览只在浏览器内生成临时 URL，不提前上传；提交任务时再走后端资产接口。
    const url = URL.createObjectURL(file);
    setVideoPreviewUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [file]);

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
    // MVP 先保留一个水印矩形；重新拖拽会覆盖旧区域，交互更直接。
    setRegions([nextRegion]);
    setIsSelectingRegion(false);
    setNotice(`已框选${regionNoun}区域，可重新拖拽覆盖。`);
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
              </div>
            </div>
          </div>
          <label className="upload-zone">
            <input
              className="file-input"
              type="file"
              accept={accept}
              onChange={(event) => {
                setFile(event.target.files?.[0] || null);
                setRegions([]);
                setDraftRegion(null);
                setIsSelectingRegion(false);
                setNotice("");
              }}
            />
            <div className="upload-visual">+</div>
            <strong>{file ? file.name : tool.pricing.mode === "image" ? "选择图片文件" : "选择视频文件"}</strong>
            <span>{file ? `${(file.size / 1024 / 1024).toFixed(2)} MB，创建任务时会上传到后端。` : "超过 32MB 自动使用分片与断点续传。"}</span>
          </label>
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
                  <span>{isSelectingRegion ? `在画面上拖拽框选${regionNoun}区域` : regions.length ? `已选择 1 个${regionNoun}区域` : `可先播放预览，再框选${regionNoun}区域`}</span>
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
                  {regions.length ? (
                    <button
                      className="link-button"
                      type="button"
                      onClick={() => {
                        setRegions([]);
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
          <div className="quote-number">{formatCredits(estimate)}</div>
          <dl>
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
          <button className="primary wide" type="submit" disabled={available < estimate || createTaskMutation.isPending}>
            {available < estimate ? "余额不足" : createTaskMutation.isPending ? "正在创建..." : "上传并创建任务"}
          </button>
          <p className="fine-print">创建任务后后端冻结积分；供应商回调成功后扣费，失败则释放冻结积分。</p>
        </aside>
      </form>
    </div>
  );
}
