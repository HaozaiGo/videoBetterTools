import { Fragment, type FormEvent, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { getAdminInternalBatches, getAdminInternalBatchStatus, regenerateAdminInternalBatchZip, retryAdminInternalBatchTasks, uploadAdminInternalBatchMissingEpisode, uploadAdminInternalBatchTaskRetry } from "../api/client";
import { formatDate } from "../lib/format";
import type { AdminInternalBatch, AdminInternalBatchStatus, Task, TaskStatus } from "../types";

const pageSize = 50;
const batchStatusTabs: { status: AdminInternalBatchStatus; label: string }[] = [
  { status: "all", label: "全部批次" },
  { status: "processing", label: "处理中" },
  { status: "succeeded", label: "已完成" },
  { status: "failed", label: "失败" },
];

function internalBatchStatusLabel(status: AdminInternalBatch["status"]) {
  if (status === "succeeded") return "已完成";
  if (status === "failed") return "失败";
  return "处理中";
}

function batchStatusText(batch: AdminInternalBatch) {
  const parts = [];
  if (batch.activeProcessing > 0) parts.push(`${batch.activeProcessing} 个处理中/排队`);
  if (batch.missing > 0) parts.push(`缺少 ${batch.missing} 个任务`);
  if (batch.failed > 0) parts.push(`${batch.failed} 个失败`);
  if (batch.cancelled > 0) parts.push(`${batch.cancelled} 个取消`);
  return parts.length ? parts.join("，") : "全部任务已完成";
}

function batchProgressPercent(batch: AdminInternalBatch) {
  if (batch.total <= 0) return 0;
  return Math.round((batch.succeeded / batch.total) * 100);
}

function batchZipReady(batch: AdminInternalBatch) {
  return batch.total > 0 && batch.created >= batch.total && batch.succeeded >= batch.total && batch.processing <= 0 && batch.failed <= 0 && batch.cancelled <= 0;
}

function taskStatusLabel(status: TaskStatus | "missing") {
  if (status === "succeeded") return "成功";
  if (status === "failed") return "失败";
  if (status === "cancelled") return "已取消";
  if (status === "queued") return "排队中";
  if (status === "processing") return "处理中";
  if (status === "missing") return "未创建";
  return status;
}

function episodeIndex(task: Task, fallback: number) {
  const value = task.params?.internalBatchIndex;
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : fallback;
}

type EpisodeRow =
  | { kind: "task"; episode: number; task: Task }
  | { kind: "missing"; episode: number };

function episodeRows(tasks: Task[], total: number): EpisodeRow[] {
  const taskRows = tasks.map((task, index) => ({ kind: "task" as const, episode: episodeIndex(task, index + 1), task }));
  const used = new Set(taskRows.map((row) => row.episode));
  const missingRows: EpisodeRow[] = [];
  for (let episode = 1; episode <= total; episode += 1) {
    if (!used.has(episode)) {
      missingRows.push({ kind: "missing", episode });
    }
  }
  return [...taskRows, ...missingRows].sort((left, right) => left.episode - right.episode);
}

function InternalBatchDetail({ batch }: { batch: AdminInternalBatch }) {
  const queryClient = useQueryClient();
  const [message, setMessage] = useState("");
  const [uploadingEpisode, setUploadingEpisode] = useState<number | null>(null);
  const [uploadingTaskId, setUploadingTaskId] = useState("");
  const detailQuery = useQuery({
    queryKey: ["admin-internal-batch-detail", batch.userId, batch.batchId],
    queryFn: () => getAdminInternalBatchStatus(batch.userId, batch.batchId),
    refetchInterval: (query) => {
      const state = query.state.data;
      return state?.processing ? 5_000 : false;
    },
  });
  const retryMutation = useMutation({
    mutationFn: () => retryAdminInternalBatchTasks(batch.userId, batch.batchId),
    onSuccess: (payload) => {
      setMessage(`已重新生成 ${payload.retried} 个失败/取消任务`);
      queryClient.setQueryData(["admin-internal-batch-detail", batch.userId, batch.batchId], payload.batch);
      queryClient.invalidateQueries({ queryKey: ["admin-internal-batches"] });
      queryClient.invalidateQueries({ queryKey: ["admin-gpu"] });
    },
    onError: (error) => setMessage(error instanceof Error ? error.message : "重新生成失败"),
  });
  const missingUploadMutation = useMutation({
    mutationFn: ({ episode, file }: { episode: number; file: File }) =>
      uploadAdminInternalBatchMissingEpisode({ userId: batch.userId, batchId: batch.batchId, episode, file }),
    onSuccess: (payload, variables) => {
      setMessage(`第 ${variables.episode} 集已补传并创建任务`);
      setUploadingEpisode(null);
      queryClient.setQueryData(["admin-internal-batch-detail", batch.userId, batch.batchId], payload.batch);
      queryClient.invalidateQueries({ queryKey: ["admin-internal-batches"] });
      queryClient.invalidateQueries({ queryKey: ["admin-gpu"] });
    },
    onError: (error) => {
      setMessage(error instanceof Error ? error.message : "补传失败");
      setUploadingEpisode(null);
    },
  });
  const taskUploadRetryMutation = useMutation({
    mutationFn: ({ taskId, file }: { taskId: string; file: File }) =>
      uploadAdminInternalBatchTaskRetry({ userId: batch.userId, batchId: batch.batchId, taskId, file }),
    onSuccess: (payload, variables) => {
      setMessage(`已补传并插队重跑任务 ${variables.taskId}`);
      setUploadingTaskId("");
      queryClient.setQueryData(["admin-internal-batch-detail", batch.userId, batch.batchId], payload.batch);
      queryClient.invalidateQueries({ queryKey: ["admin-internal-batches"] });
      queryClient.invalidateQueries({ queryKey: ["admin-gpu"] });
    },
    onError: (error) => {
      setMessage(error instanceof Error ? error.message : "补传重跑失败");
      setUploadingTaskId("");
    },
  });
  const detail = detailQuery.data;
  const retryableCount = (detail?.failed || 0) + (detail?.cancelled || 0);
  const rows = detail ? episodeRows(detail.tasks, detail.total) : [];

  function uploadMissingEpisode(episode: number, file: File | undefined) {
    if (!file) return;
    setUploadingEpisode(episode);
    setMessage("");
    missingUploadMutation.mutate({ episode, file });
  }

  function uploadRetryTask(taskId: string, file: File | undefined) {
    if (!file) return;
    setUploadingTaskId(taskId);
    setMessage("");
    taskUploadRetryMutation.mutate({ taskId, file });
  }

  return (
    <div className="internal-batch-detail-card">
      <div className="internal-batch-detail-head">
        <div>
          <strong>{detail?.name || batch.batchName}</strong>
          <span>
            {detail ? `完成 ${detail.succeeded}/${detail.total}，失败 ${detail.failed}，处理中 ${detail.processing}` : "正在读取批次明细"}
          </span>
        </div>
        <div>
          <button className="ghost compact" type="button" onClick={() => detailQuery.refetch()} disabled={detailQuery.isFetching}>
            {detailQuery.isFetching ? "刷新中" : "刷新明细"}
          </button>
          <button className="primary compact" type="button" onClick={() => retryMutation.mutate()} disabled={!retryableCount || retryMutation.isPending}>
            {retryMutation.isPending ? "入队中" : `插队重新生成失败项${retryableCount ? ` ${retryableCount}` : ""}`}
          </button>
        </div>
      </div>
      {message ? <div className={message.includes("失败") || message.includes("insufficient") || message.includes("missing") ? "inline-error-message" : "inline-success-message"}>{message}</div> : null}
      <div className="internal-episode-list">
        <table>
          <thead>
            <tr>
              <th>集数</th>
              <th>文件</th>
              <th>状态</th>
              <th>进度</th>
              <th>完成时间</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {rows.length ? (
              rows.map((row) =>
                row.kind === "missing" ? (
                  <tr key={`missing-${row.episode}`}>
                    <td>第 {row.episode} 集</td>
                    <td className="subtle">未创建任务</td>
                    <td><span className="status queued">{taskStatusLabel("missing")}</span></td>
                    <td>-</td>
                    <td>-</td>
                    <td>
                      <input
                        id={`missing-upload-${batch.batchId}-${row.episode}`}
                        className="visually-hidden"
                        type="file"
                        accept="video/*"
                        disabled={missingUploadMutation.isPending || taskUploadRetryMutation.isPending}
                        onChange={(event) => {
                          uploadMissingEpisode(row.episode, event.target.files?.[0]);
                          event.target.value = "";
                        }}
                      />
                      <button
                        className="ghost compact"
                        type="button"
                        disabled={missingUploadMutation.isPending || taskUploadRetryMutation.isPending}
                        onClick={() => document.getElementById(`missing-upload-${batch.batchId}-${row.episode}`)?.click()}
                      >
                        {uploadingEpisode === row.episode ? "补传中" : "补传视频"}
                      </button>
                    </td>
                  </tr>
                ) : (
                  <tr key={row.task.id}>
                    <td>第 {row.episode} 集</td>
                    <td>
                      <strong>{row.task.inputAssetName || row.task.id}</strong>
                      <em className="subtle">{row.task.providerJobId}</em>
                    </td>
                    <td><span className={`status ${row.task.status}`}>{taskStatusLabel(row.task.status)}</span></td>
                    <td>
                      <div className="internal-episode-progress">
                        <span>{row.task.progressPercent}%</span>
                        <em>{row.task.progressStage || "-"}</em>
                      </div>
                    </td>
                    <td>{formatDate(row.task.completedAt)}</td>
                    <td>
                      {row.task.status === "failed" || row.task.status === "cancelled" ? (
                        <>
                          <input
                            id={`task-upload-retry-${row.task.id}`}
                            className="visually-hidden"
                            type="file"
                            accept="video/*"
                            disabled={missingUploadMutation.isPending || taskUploadRetryMutation.isPending}
                            onChange={(event) => {
                              uploadRetryTask(row.task.id, event.target.files?.[0]);
                              event.target.value = "";
                            }}
                          />
                          <button
                            className="ghost compact"
                            type="button"
                            disabled={missingUploadMutation.isPending || taskUploadRetryMutation.isPending}
                            onClick={() => document.getElementById(`task-upload-retry-${row.task.id}`)?.click()}
                          >
                            {uploadingTaskId === row.task.id ? "补传中" : "补传重跑"}
                          </button>
                        </>
                      ) : (
                        "-"
                      )}
                    </td>
                  </tr>
                ),
              )
            ) : (
              <tr>
                <td colSpan={6} className="empty">
                  暂无集数明细。
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function InternalTasksPage() {
  const queryClient = useQueryClient();
  const [activeStatus, setActiveStatus] = useState<AdminInternalBatchStatus>("all");
  const [pageNumber, setPageNumber] = useState(1);
  const [pageInput, setPageInput] = useState("1");
  const [searchInput, setSearchInput] = useState("");
  const [nameQuery, setNameQuery] = useState("");
  const [expandedBatchId, setExpandedBatchId] = useState("");
  const [zipBatchId, setZipBatchId] = useState("");
  const [zipMessage, setZipMessage] = useState("");
  const { data: batchPage, isFetching } = useQuery({
    queryKey: ["admin-internal-batches", activeStatus, pageNumber, nameQuery],
    queryFn: () => getAdminInternalBatches(pageNumber, pageSize, activeStatus, nameQuery),
    refetchInterval: (query) => {
      const state = query.state.data;
      return state?.items.some((batch) => batch.status === "processing") ? 10_000 : false;
    },
  });
  const page = batchPage?.page ?? { page: 1, perPage: pageSize, total: 0, totalPages: 1, hasPrevious: false, hasNext: false };
  const batches = batchPage?.items ?? [];
  const totalPages = Math.max(1, page.totalPages);
  const counts = useMemo(
    () => ({
      processing: batches.filter((batch) => batch.status === "processing").length,
      succeeded: batches.filter((batch) => batch.status === "succeeded").length,
      failed: batches.filter((batch) => batch.status === "failed").length,
    }),
    [batches],
  );
  const regenerateZipMutation = useMutation({
    mutationFn: (batch: AdminInternalBatch) => regenerateAdminInternalBatchZip(batch.userId, batch.batchId),
    onSuccess: (payload) => {
      const failedMessage = payload.failed.length ? `，${payload.failed.length} 个分包删除失败` : "";
      setZipMessage(payload.queued ? `已插队重新生成 ZIP，共 ${payload.partCount} 个分包，清理旧 ZIP ${payload.deleted} 个${failedMessage}` : `ZIP 未入队${failedMessage}`);
      setZipBatchId("");
      queryClient.invalidateQueries({ queryKey: ["admin-internal-batches"] });
      queryClient.invalidateQueries({ queryKey: ["admin-internal-batch-zips"] });
    },
    onError: (error) => {
      setZipMessage(error instanceof Error ? error.message : "重新生成 ZIP 失败");
      setZipBatchId("");
    },
  });

  function selectStatus(status: AdminInternalBatchStatus) {
    setActiveStatus(status);
    setPageNumber(1);
    setPageInput("1");
    setExpandedBatchId("");
  }

  function submitSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const nextQuery = searchInput.trim();
    if (nextQuery === nameQuery && pageNumber === 1) {
      queryClient.invalidateQueries({ queryKey: ["admin-internal-batches"] });
    }
    setNameQuery(nextQuery);
    setPageNumber(1);
    setPageInput("1");
    setExpandedBatchId("");
  }

  function clearSearch() {
    setSearchInput("");
    setNameQuery("");
    setPageNumber(1);
    setPageInput("1");
    setExpandedBatchId("");
  }

  function goToPage(pageValue: number) {
    const nextPage = Math.max(1, Math.min(totalPages, pageValue));
    setPageNumber(nextPage);
    setPageInput(String(nextPage));
    setExpandedBatchId("");
  }

  function handlePageJump(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const nextPage = Number.parseInt(pageInput, 10);
    if (Number.isNaN(nextPage)) {
      setPageInput(String(pageNumber));
      return;
    }
    goToPage(nextPage);
  }

  return (
    <section className="task-page internal-tasks-page">
      <div className="page-head">
        <div>
          <h1>内部任务</h1>
          <p>按批次查看批量去字幕并翻译队列。</p>
        </div>
        <div className="task-toolbar">
          <form className="internal-batch-filter" onSubmit={submitSearch}>
            <label>
              <span>批次名称</span>
              <input type="search" placeholder="搜索批次名称" value={searchInput} onChange={(event) => setSearchInput(event.target.value)} />
            </label>
            <button className="primary compact" type="submit">
              查询
            </button>
            <button className="ghost compact" type="button" onClick={clearSearch} disabled={!searchInput && !nameQuery}>
              清空
            </button>
          </form>
          <button className="ghost compact" type="button" onClick={() => queryClient.invalidateQueries({ queryKey: ["admin-internal-batches"] })}>
            {isFetching ? "刷新中" : "刷新"}
          </button>
        </div>
      </div>

      <div className="zip-storage-tabs" role="tablist" aria-label="内部任务状态">
        {batchStatusTabs.map((tab) => (
          <button
            key={tab.status}
            className={activeStatus === tab.status ? "active" : ""}
            type="button"
            role="tab"
            aria-selected={activeStatus === tab.status}
            onClick={() => selectStatus(tab.status)}
          >
            <span>{tab.label}</span>
            <strong>{batchPage?.tabs?.[tab.status] ?? 0}</strong>
          </button>
        ))}
      </div>

      <div className="task-metrics">
        <div>
          <span>本页处理中</span>
          <strong>{counts.processing}</strong>
        </div>
        <div>
          <span>本页已完成</span>
          <strong>{counts.succeeded}</strong>
        </div>
        <div>
          <span>本页失败</span>
          <strong>{counts.failed}</strong>
        </div>
      </div>

      {zipMessage ? <p className="inline-status-message">{zipMessage}</p> : null}

      <div className="panel table-panel">
        <table className="internal-batch-table">
          <thead>
            <tr>
              <th className="internal-batch-expand-cell"></th>
              <th>批次</th>
              <th>处理情况</th>
              <th>完成集数</th>
              <th>创建时间</th>
              <th>更新时间</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {batches.length ? (
              batches.map((batch) => {
                const percent = batchProgressPercent(batch);
                const isExpanded = expandedBatchId === batch.batchId;
                const canRegenerateZip = batchZipReady(batch);
                const zipBusy = regenerateZipMutation.isPending && zipBatchId === batch.batchId;
                const zipDisabled = !canRegenerateZip || regenerateZipMutation.isPending;
                return (
                  <Fragment key={batch.batchId}>
                    <tr>
                      <td className="internal-batch-expand-cell">
                        <button
                          className="detail-toggle icon-only"
                          type="button"
                          aria-label={`${isExpanded ? "收起" : "展开"} ${batch.batchName}`}
                          aria-expanded={isExpanded}
                          onClick={() => setExpandedBatchId(isExpanded ? "" : batch.batchId)}
                        >
                          {isExpanded ? "⌃" : "⌄"}
                        </button>
                      </td>
                      <td>
                        <strong>{batch.batchName}</strong>
                        <em className="subtle">{batch.batchId}</em>
                      </td>
                      <td>
                        <div className="status-cell">
                          <span className={`status ${batch.status === "succeeded" ? "succeeded" : batch.status === "failed" ? "failed" : "processing"}`}>
                            {internalBatchStatusLabel(batch.status)}
                          </span>
                          <em className="subtle">{batchStatusText(batch)}</em>
                        </div>
                      </td>
                      <td>
                        <div className="internal-batch-progress">
                          <div>
                            <strong>{batch.succeeded}/{batch.total}</strong>
                            <span>{percent}%</span>
                          </div>
                          <div className="task-progress-track">
                            <span style={{ width: `${percent}%` }} />
                          </div>
                          <em className="subtle">已创建 {batch.created}/{batch.total}</em>
                        </div>
                      </td>
                      <td>{formatDate(batch.createdAt)}</td>
                      <td>{formatDate(batch.updatedAt)}</td>
                      <td className="internal-batch-actions">
                        <button
                          className="primary compact"
                          type="button"
                          disabled={zipDisabled}
                          title={canRegenerateZip ? "删除旧 ZIP，并把新 ZIP 任务插队到最前" : "批次全部成功后才可重新生成 ZIP"}
                          onClick={() => {
                            setZipBatchId(batch.batchId);
                            setZipMessage("");
                            regenerateZipMutation.mutate(batch);
                          }}
                        >
                          {zipBusy ? "入队中" : "立刻重新生成ZIP"}
                        </button>
                      </td>
                    </tr>
                    {isExpanded ? (
                      <tr className="internal-batch-detail-row">
                        <td colSpan={7}>
                          <InternalBatchDetail batch={batch} />
                        </td>
                      </tr>
                    ) : null}
                  </Fragment>
                );
              })
            ) : (
              <tr>
                <td colSpan={7} className="empty">
                  暂无批量去字幕并翻译批次。
                </td>
              </tr>
            )}
          </tbody>
        </table>
        <div className="pagination-bar">
          <span>
            第 {page.page} / {page.totalPages} 页，共 {page.total} 个批次
          </span>
          <form className="pagination-jump" onSubmit={handlePageJump}>
            <label htmlFor="internal-batch-page-jump">跳至</label>
            <input
              id="internal-batch-page-jump"
              type="number"
              min="1"
              max={totalPages}
              inputMode="numeric"
              value={pageInput}
              onChange={(event) => setPageInput(event.target.value)}
              disabled={isFetching}
            />
            <span>页</span>
            <button className="ghost compact" type="submit" disabled={isFetching}>
              跳转
            </button>
          </form>
          <div className="pagination-actions">
            <button className="ghost compact" type="button" onClick={() => goToPage(pageNumber - 1)} disabled={!page.hasPrevious || isFetching}>
              上一页
            </button>
            <button className="ghost compact" type="button" onClick={() => goToPage(pageNumber + 1)} disabled={!page.hasNext || isFetching}>
              下一页
            </button>
          </div>
        </div>
      </div>
    </section>
  );
}
