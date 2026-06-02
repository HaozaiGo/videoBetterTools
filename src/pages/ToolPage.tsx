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
};

function clamp(value: number) {
  return Math.max(0, Math.min(1, value));
}

function buildRegion(start: { x: number; y: number }, end: { x: number; y: number }): WatermarkRegion {
  const x = clamp(Math.min(start.x, end.x));
  const y = clamp(Math.min(start.y, end.y));
  const width = Math.max(0, Math.min(1 - x, Math.abs(end.x - start.x)));
  const height = Math.max(0, Math.min(1 - y, Math.abs(end.y - start.y)));
  return { x, y, width, height };
}

export function ToolPage() {
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  const queryClient = useQueryClient();
  const { data } = useSuspenseQuery({ queryKey: ["bootstrap"], queryFn: getBootstrap });
  const [file, setFile] = useState<File | null>(null);
  const [notice, setNotice] = useState("");
  const [videoPreviewUrl, setVideoPreviewUrl] = useState("");
  const [regions, setRegions] = useState<WatermarkRegion[]>([]);
  const [draftRegion, setDraftRegion] = useState<WatermarkRegion | null>(null);
  const overlayRef = useRef<HTMLDivElement | null>(null);
  const dragStartRef = useRef<{ x: number; y: number } | null>(null);
  const tool = data.tools.find((item) => item.route === pathname);
  const isWatermarkTool = tool?.slug === "remove-watermark";

  const createTaskMutation = useMutation({
    mutationFn: async (values: ToolFormValues) => {
      if (!tool) throw new Error("工具不存在");
      if (!file) throw new Error("请先选择一个文件");
      const params = isWatermarkTool ? { ...values, mode: "manual" as const, regions, keepAudio: true } : values;
      const upload = await uploadAsset({
        file,
        kind: tool.pricing.mode === "image" ? "image" : "video",
        durationSeconds: values.duration,
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
  const estimate = useMemo(() => (tool ? estimateCredits(tool, values) : 0), [tool, values]);

  useEffect(() => {
    if (!file || !file.type.startsWith("video/")) {
      setVideoPreviewUrl("");
      return;
    }
    const url = URL.createObjectURL(file);
    setVideoPreviewUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [file]);

  const pointFromEvent = (event: PointerEvent<HTMLDivElement>) => {
    const rect = overlayRef.current?.getBoundingClientRect();
    if (!rect) return null;
    return {
      x: clamp((event.clientX - rect.left) / rect.width),
      y: clamp((event.clientY - rect.top) / rect.height),
    };
  };

  const startRegion = (event: PointerEvent<HTMLDivElement>) => {
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
      setNotice("框选区域太小，请覆盖完整水印。");
      return;
    }
    setRegions([nextRegion]);
    setNotice("已框选水印区域，可重新拖拽覆盖。");
  };

  if (!tool) {
    return (
      <section className="panel">
        <h1>工具不存在</h1>
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
    <>
      <Link className="back-link" to="/tools">
        ‹ 返回工具广场
      </Link>
      {notice ? <div className="notice">{notice}</div> : null}
      <form
        className="tool-detail"
        onSubmit={(event) => {
          event.preventDefault();
          if (isWatermarkTool && regions.length === 0) {
            setNotice("请先在视频预览中框选水印区域。");
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
                setNotice("");
              }}
            />
            <div className="upload-visual">+</div>
            <strong>{file ? file.name : tool.pricing.mode === "image" ? "选择图片文件" : "选择视频文件"}</strong>
            <span>{file ? `${(file.size / 1024 / 1024).toFixed(2)} MB，创建任务时会上传到后端。` : "文件会上传到后端并生成 asset_id。"}</span>
          </label>
          {isWatermarkTool && videoPreviewUrl ? (
            <section className="video-region-editor" aria-label="水印区域框选">
              <div className="video-preview-wrap">
                <video src={videoPreviewUrl} controls playsInline preload="metadata" />
                <div
                  ref={overlayRef}
                  className="region-overlay"
                  onPointerDown={startRegion}
                  onPointerMove={moveRegion}
                  onPointerUp={finishRegion}
                  onPointerCancel={() => {
                    dragStartRef.current = null;
                    setDraftRegion(null);
                  }}
                >
                  {[...regions, ...(draftRegion ? [draftRegion] : [])].map((region, index) => (
                    <div
                      className="region-box"
                      key={`${region.x}-${region.y}-${index}`}
                      style={{
                        left: `${region.x * 100}%`,
                        top: `${region.y * 100}%`,
                        width: `${region.width * 100}%`,
                        height: `${region.height * 100}%`,
                      }}
                    />
                  ))}
                </div>
              </div>
              <div className="region-help">
                <span>{regions.length ? "已选择 1 个水印区域" : "拖拽框选水印区域"}</span>
                {regions.length ? (
                  <button
                    className="link-button"
                    type="button"
                    onClick={() => {
                      setRegions([]);
                      setNotice("");
                    }}
                  >
                    清除
                  </button>
                ) : null}
              </div>
            </section>
          ) : null}
          <div className="form-grid">
            {tool.inputs.includes("duration") ? (
              <form.Field name="duration">
                {(field) => (
                  <label>
                    视频时长（秒）
                    <input type="number" min={1} max={7200} value={field.state.value} onChange={(event) => field.handleChange(Number(event.target.value))} />
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
    </>
  );
}
