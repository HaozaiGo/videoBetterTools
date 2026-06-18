import { useMutation, useQueryClient, useQuery, useSuspenseQuery } from "@tanstack/react-query";
import { createColumnHelper, flexRender, getCoreRowModel, useReactTable } from "@tanstack/react-table";
import { Fragment, useState } from "react";
import { cancelTask, getAuthToken, getBootstrap, getTasksPage } from "../api/client";
import { formatCredits, formatDate, statusLabel, taskProgressDisplay } from "../lib/format";
import { translateLanguageLabel } from "../lib/translate-languages";
import type { BootstrapState, Task } from "../types";

const columnHelper = createColumnHelper<Task>();
const pageSize = 50;

function failureReason(task: Task) {
  if (task.status !== "failed") return "";
  const reasons: Record<string, string> = {
    INPUT_ASSET_NOT_FOUND: "输入文件不存在或已过期，请重新上传后再试。",
    VIDEO_PROCESSING_FAILED: "远端视频处理失败，可能是模型报错、显存不足、视频编码不兼容或网络传输中断。",
    PROVIDER_FAILED: "供应商返回失败。",
    MANUAL_TEST_FAILED: "手动触发的失败回调，用于验证退款流程。",
  };
  return reasons[task.errorCode || ""] || "任务失败，系统已释放冻结积分。";
}

function paramSummary(task: Task) {
  const params = task.params || {};
  const items = [
    typeof params.resolution === "string" ? `清晰度 ${params.resolution}` : "",
    typeof params.enhanceMode === "string" ? `模式 ${params.enhanceMode === "natural" ? "自然增强" : "高质量超分"}` : "",
    typeof params.targetLanguage === "string" ? `目标语言 ${translateLanguageLabel(params.targetLanguage)}` : "",
    typeof params.subtitlePlacement === "string" ? `字幕位置 ${params.subtitlePlacement === "top" ? "顶部" : params.subtitlePlacement === "middle-lower" ? "中下" : "底部"}` : "",
    typeof params.priority === "string" ? `优先级 ${params.priority === "express" ? "加急" : "标准"}` : "",
    typeof params.keepAudio === "boolean" ? `音频 ${params.keepAudio ? "保留" : "不保留"}` : "",
  ].filter(Boolean);
  return items.length ? items.join(" / ") : "无额外参数";
}

function taskProgressLabel(task: Task, percent: number) {
  if (task.status === "queued") return "等待处理";
  if (task.status === "processing") {
    if (percent >= 95) return "收尾中";
    return "处理中";
  }
  return "";
}

function taskResultFallback(task: Task) {
  if (task.status === "failed") return "无结果";
  if (task.status === "cancelled") return "已取消";
  if (task.status === "succeeded") return "结果未入库";
  return "等待结果";
}

function taskResultHref(task: Task) {
  const href = task.previewUrl || task.outputUrl;
  if (!href) return "";
  const url = new URL(href, window.location.origin);
  const token = getAuthToken();
  if (token && url.origin === window.location.origin && url.pathname.startsWith("/api/")) {
    url.searchParams.set("access_token", token);
  }
  return url.href;
}

export function TasksPage() {
  const queryClient = useQueryClient();
  const { data } = useSuspenseQuery({ queryKey: ["bootstrap"], queryFn: getBootstrap });
  const [expandedTaskIds, setExpandedTaskIds] = useState<Set<string>>(() => new Set());
  const [currentPage, setCurrentPage] = useState(1);
  const tasksQuery = useQuery({
    queryKey: ["tasks", currentPage],
    queryFn: () => getTasksPage(currentPage, pageSize),
    initialData: currentPage === 1 ? { items: data.tasks, page: data.taskPage } : undefined,
    refetchInterval: (query) => {
      const state = query.state.data;
      return state?.items.some((task) => ["queued", "processing"].includes(task.status)) ? 1600 : false;
    },
  });
  const taskPage = tasksQuery.data || { items: data.tasks, page: data.taskPage };

  const cancelMutation = useMutation({
    mutationFn: cancelTask,
    onSuccess: (payload) => {
      queryClient.setQueryData<BootstrapState>(["bootstrap"], payload.state);
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
    },
  });

  const toggleTaskDetail = (taskId: string) => {
    setExpandedTaskIds((current) => {
      const next = new Set(current);
      if (next.has(taskId)) {
        next.delete(taskId);
      } else {
        next.add(taskId);
      }
      return next;
    });
  };

  const columns = [
    columnHelper.accessor("toolSlug", {
      header: "工具 / 供应商任务",
      cell: ({ row }) => {
        const tool = data.tools.find((item) => item.slug === row.original.toolSlug);
        return (
          <>
            <strong>{tool?.name || row.original.toolSlug}</strong>
            {row.original.inputAssetName ? <span className="task-file-name" title={row.original.inputAssetName}>{row.original.inputAssetName}</span> : null}
            <span className="subtle">{row.original.providerJobId}</span>
          </>
        );
      },
    }),
    columnHelper.accessor("status", {
      header: "状态",
      cell: ({ row }) => {
        const task = row.original;
        const showProgress = ["queued", "processing"].includes(task.status);
        const progress = taskProgressDisplay(task);
        return (
          <div className="status-cell">
            <span className={`status ${task.status}`}>{statusLabel(task.status)}</span>
            {showProgress ? (
              <div className="task-progress" aria-label={`任务进度 ${progress.percent}%`}>
                <div className="task-progress-head">
                  <span>{progress.percent}%</span>
                  <em>{taskProgressLabel(task, progress.percent)}</em>
                </div>
                <div className="task-progress-track">
                  <span style={{ width: `${progress.percent}%` }} />
                </div>
              </div>
            ) : null}
          </div>
        );
      },
    }),
    columnHelper.accessor("estimatedCredits", {
      header: "预估",
      cell: (info) => formatCredits(info.getValue()),
    }),
    columnHelper.accessor("createdAt", {
      header: "创建时间",
      cell: (info) => formatDate(info.getValue()),
    }),
    columnHelper.accessor("completedAt", {
      header: "完成时间",
      cell: (info) => formatDate(info.getValue()),
    }),
    columnHelper.display({
      id: "result",
      header: "结果",
      cell: ({ row }) => {
        const task = row.original;
        const canCancel = ["queued", "processing"].includes(task.status);
        const resultHref = taskResultHref(task);
        return (
          <div className="task-actions">
            {resultHref ? (
              <a href={resultHref} target="_blank" rel="noreferrer">
                查看结果
              </a>
            ) : (
              taskResultFallback(task)
            )}
            {canCancel ? (
              <button className="link-button" onClick={() => cancelMutation.mutate(task.id)} disabled={cancelMutation.isPending}>
                {task.status === "queued" ? "取消任务" : "请求取消"}
              </button>
            ) : null}
            <button className="detail-toggle" type="button" onClick={() => toggleTaskDetail(task.id)} aria-expanded={expandedTaskIds.has(task.id)}>
              <span>{expandedTaskIds.has(task.id) ? "收起详情" : "展开详情"}</span>
              <span aria-hidden="true">{expandedTaskIds.has(task.id) ? "⌃" : "⌄"}</span>
            </button>
          </div>
        );
      },
    }),
  ];

  const table = useReactTable({
    data: taskPage.items,
    columns,
    getCoreRowModel: getCoreRowModel(),
  });
  const processingCount = taskPage.items.filter((task) => ["queued", "processing"].includes(task.status)).length;
  const succeededCount = taskPage.items.filter((task) => task.status === "succeeded").length;
  const failedCount = taskPage.items.filter((task) => task.status === "failed").length;

  return (
    <section className="task-page">
      <div className="page-head">
        <div>
          <h1>任务列表</h1>
          <p>查看任务进度、失败原因和结果下载。</p>
        </div>
        <button
          className="ghost compact"
          onClick={() => {
            queryClient.invalidateQueries({ queryKey: ["bootstrap"] });
            queryClient.invalidateQueries({ queryKey: ["tasks"] });
          }}
        >
          刷新
        </button>
      </div>
      <div className="task-metrics">
        <div>
          <span>本页处理中</span>
          <strong>{processingCount}</strong>
        </div>
        <div>
          <span>本页已完成</span>
          <strong>{succeededCount}</strong>
        </div>
        <div>
          <span>本页失败</span>
          <strong>{failedCount}</strong>
        </div>
      </div>
      <div className="panel table-panel">
        <table>
          <thead>
            {table.getHeaderGroups().map((headerGroup) => (
              <tr key={headerGroup.id}>
                {headerGroup.headers.map((header) => (
                  <th key={header.id}>{flexRender(header.column.columnDef.header, header.getContext())}</th>
                ))}
              </tr>
            ))}
          </thead>
          <tbody>
            {table.getRowModel().rows.length ? (
              table.getRowModel().rows.map((row) => {
                const task = row.original;
                const isExpanded = expandedTaskIds.has(task.id);
                return (
                  <Fragment key={row.id}>
                    <tr>
                      {row.getVisibleCells().map((cell) => (
                        <td key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</td>
                      ))}
                    </tr>
                    {isExpanded ? (
                      <tr className="task-detail-row">
                        <td colSpan={columns.length}>
                          <div className="task-detail-card">
                            <div>
                              <span>任务参数</span>
                              <strong>{paramSummary(task)}</strong>
                            </div>
                            {task.status === "failed" ? (
                              <div className="task-error">
                                <span>失败原因</span>
                                <strong>{failureReason(task)}</strong>
                                {task.errorCode ? <em>错误码：{task.errorCode}</em> : null}
                              </div>
                            ) : null}
                          </div>
                        </td>
                      </tr>
                    ) : null}
                  </Fragment>
                );
              })
            ) : (
              <tr>
                <td colSpan={columns.length} className="empty">
                  暂无任务，从工具广场创建第一个任务。
                </td>
              </tr>
            )}
          </tbody>
        </table>
        <div className="pagination-bar">
          <span>
            第 {taskPage.page.page} / {taskPage.page.totalPages} 页，共 {taskPage.page.total} 条
          </span>
          <div>
            <button className="ghost compact" onClick={() => setCurrentPage((page) => Math.max(1, page - 1))} disabled={!taskPage.page.hasPrevious || tasksQuery.isFetching}>
              上一页
            </button>
            <button className="ghost compact" onClick={() => setCurrentPage((page) => page + 1)} disabled={!taskPage.page.hasNext || tasksQuery.isFetching}>
              下一页
            </button>
          </div>
        </div>
      </div>
    </section>
  );
}
