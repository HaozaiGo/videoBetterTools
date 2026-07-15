import { type FormEvent, useEffect, useMemo, useState } from "react";
import { useMutation, useQueryClient, useSuspenseQuery } from "@tanstack/react-query";
import { deleteAdminInternalBatchZips, downloadAdminInternalBatchZip, getAdminInternalBatchZips } from "../api/client";
import { formatBytes, formatDate } from "../lib/format";
import type { AdminInternalBatchZip, AdminInternalBatchZipSkippedTask, AdminInternalBatchZipStatus, TaskStatus } from "../types";

const zipTabs: { status: AdminInternalBatchZipStatus; label: string; empty: string }[] = [
  { status: "ready", label: "可下载", empty: "暂无可直接下载的 ZIP。" },
  { status: "processing", label: "处理中", empty: "暂无正在打包或上传的 ZIP。" },
  { status: "failed", label: "失败重试", empty: "暂无需要重试的 ZIP。" },
];

function sourceLabel(source: AdminInternalBatchZip["source"]) {
  if (!source) return "等待中";
  return source === "tos" ? "TOS" : "本地";
}

function completionText(zip: AdminInternalBatchZip) {
  const unfinished = zip.failed + zip.cancelled + zip.processing;
  return unfinished ? `${zip.succeeded}/${zip.total}，未完成 ${unfinished}` : `${zip.succeeded}/${zip.total}`;
}

function zipKey(zip: Pick<AdminInternalBatchZip, "userId" | "batchId" | "partIndex">) {
  return `${zip.userId}::${zip.batchId}::${zip.partIndex}`;
}

function statusLabel(status: TaskStatus) {
  if (status === "failed") return "失败";
  if (status === "cancelled") return "取消";
  if (status === "queued") return "排队中";
  if (status === "processing") return "处理中";
  if (status === "succeeded") return "成功";
  return status;
}

function skippedReason(task: AdminInternalBatchZipSkippedTask) {
  if (task.failureReason) return task.failureReason;
  if (task.progressStage) return task.progressStage;
  if (task.errorCode) return task.errorCode;
  return task.status === "cancelled" ? "任务已取消" : "仍未生成可打包结果";
}

export function AdminZipStoragePage() {
  const queryClient = useQueryClient();
  const [activeStatus, setActiveStatus] = useState<AdminInternalBatchZipStatus>("ready");
  const [pageNumber, setPageNumber] = useState(1);
  const [searchInput, setSearchInput] = useState("");
  const [nameQuery, setNameQuery] = useState("");
  const [selectedKeys, setSelectedKeys] = useState<Set<string>>(() => new Set());
  const [detailZip, setDetailZip] = useState<AdminInternalBatchZip | null>(null);
  const [notice, setNotice] = useState("");
  const { data: zipPage, isFetching } = useSuspenseQuery({
    queryKey: ["admin-internal-batch-zips", activeStatus, pageNumber, nameQuery],
    queryFn: () => getAdminInternalBatchZips(pageNumber, 50, activeStatus, nameQuery),
    refetchInterval: 15_000,
  });

  const refresh = () => queryClient.invalidateQueries({ queryKey: ["admin-internal-batch-zips"] });
  const zips = zipPage.items;
  const totalBytes = zips.reduce((sum, zip) => sum + (zip.sizeBytes || zip.estimatedSizeBytes || 0), 0);
  const tosCount = zips.filter((zip) => zip.source === "tos").length;
  const localCount = zips.length - tosCount;
  const activeTab = zipTabs.find((tab) => tab.status === activeStatus) ?? zipTabs[0];
  const pageKeys = useMemo(() => zips.map(zipKey), [zips]);
  const selectedZips = zips.filter((zip) => selectedKeys.has(zipKey(zip)));
  const allPageSelected = pageKeys.length > 0 && pageKeys.every((key) => selectedKeys.has(key));
  const somePageSelected = pageKeys.some((key) => selectedKeys.has(key));
  const deleteMutation = useMutation({
    mutationFn: () => deleteAdminInternalBatchZips(selectedZips.map((zip) => ({ userId: zip.userId, batchId: zip.batchId, partIndex: zip.partIndex }))),
    onSuccess: (payload) => {
      setSelectedKeys(new Set());
      setNotice(payload.failed.length ? `已删除 ${payload.deleted} 行，${payload.failed.length} 行删除失败。` : `已删除 ${payload.deleted} 行。`);
      refresh();
    },
    onError: (error) => setNotice(error instanceof Error ? error.message : "删除失败"),
  });

  useEffect(() => {
    setSelectedKeys((current) => new Set([...current].filter((key) => pageKeys.includes(key))));
  }, [pageKeys]);

  function selectTab(status: AdminInternalBatchZipStatus) {
    setActiveStatus(status);
    setPageNumber(1);
    setSelectedKeys(new Set());
    setDetailZip(null);
    setNotice("");
  }

  function submitSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const nextQuery = searchInput.trim();
    if (nextQuery === nameQuery && pageNumber === 1) {
      refresh();
    }
    setNameQuery(nextQuery);
    setPageNumber(1);
    setSelectedKeys(new Set());
    setDetailZip(null);
  }

  function clearSearch() {
    setSearchInput("");
    setNameQuery("");
    setPageNumber(1);
    setSelectedKeys(new Set());
    setDetailZip(null);
  }

  function toggleZip(zip: AdminInternalBatchZip) {
    const key = zipKey(zip);
    setSelectedKeys((current) => {
      const next = new Set(current);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
  }

  function togglePage() {
    setSelectedKeys((current) => {
      const next = new Set(current);
      if (allPageSelected) {
        pageKeys.forEach((key) => next.delete(key));
      } else {
        pageKeys.forEach((key) => next.add(key));
      }
      return next;
    });
  }

  function deleteSelected() {
    if (!selectedZips.length || deleteMutation.isPending) return;
    const confirmed = window.confirm(`确认删除选中的 ${selectedZips.length} 行？原任务和结果不会删除。`);
    if (!confirmed) return;
    deleteMutation.mutate();
  }

  return (
    <section className="admin-layout zip-storage-layout">
      <div className="page-head">
        <div>
          <h1>ZIP储存</h1>
          <p>查看已生成、可直接下载的批次 ZIP。</p>
        </div>
        <div className="zip-storage-head-actions">
          <button className="remove-file-button" type="button" onClick={deleteSelected} disabled={!selectedZips.length || deleteMutation.isPending}>
            {deleteMutation.isPending ? "删除中" : `删除所选${selectedZips.length ? ` ${selectedZips.length}` : ""}`}
          </button>
          <button className="ghost compact" type="button" onClick={refresh} disabled={isFetching}>
            {isFetching ? "刷新中" : "刷新"}
          </button>
        </div>
      </div>
      {notice ? <p className="page-message">{notice}</p> : null}

      <form className="zip-storage-filter" onSubmit={submitSearch}>
        <label>
          <span>名称搜索</span>
          <input value={searchInput} onChange={(event) => setSearchInput(event.target.value)} placeholder="输入批次名称" />
        </label>
        <button className="primary compact" type="submit">
          查询
        </button>
        <button className="ghost compact" type="button" onClick={clearSearch} disabled={!searchInput && !nameQuery}>
          清空
        </button>
      </form>

      <div className="zip-storage-tabs" role="tablist" aria-label="ZIP 储存状态">
        {zipTabs.map((tab) => (
          <button
            key={tab.status}
            className={activeStatus === tab.status ? "active" : ""}
            type="button"
            role="tab"
            aria-selected={activeStatus === tab.status}
            onClick={() => selectTab(tab.status)}
          >
            <span>{tab.label}</span>
            <strong>{zipPage.tabs?.[tab.status] ?? 0}</strong>
          </button>
        ))}
      </div>

      <div className="admin-stats">
        <div><span>当前页记录</span><strong>{zips.length}</strong></div>
        <div><span>TOS</span><strong>{tosCount}</strong></div>
        <div><span>本地/待处理</span><strong>{localCount}</strong></div>
        <div><span>{activeStatus === "ready" ? "当前页容量" : "预计容量"}</span><strong>{formatBytes(totalBytes)}</strong></div>
      </div>

      <div className="panel table-panel zip-storage-panel">
        <table className="zip-storage-table">
          <thead>
            <tr>
              <th className="zip-select-cell">
                <input
                  type="checkbox"
                  aria-label="选择当前页 ZIP"
                  checked={allPageSelected}
                  ref={(node) => {
                    if (node) node.indeterminate = somePageSelected && !allPageSelected;
                  }}
                  onChange={togglePage}
                />
              </th>
              <th>批次</th>
              <th>分包</th>
              <th>来源</th>
              <th>大小</th>
              <th>完成数</th>
              <th>更新时间</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {zips.length ? (
              zips.map((zip) => (
                <tr key={`${zip.batchId}-${zip.partIndex}`}>
                  <td className="zip-select-cell">
                    <input
                      type="checkbox"
                      aria-label={`选择 ${zip.filename || zip.batchName}`}
                      checked={selectedKeys.has(zipKey(zip))}
                      onChange={() => toggleZip(zip)}
                    />
                  </td>
                  <td>
                    <strong>{zip.batchName}</strong>
                    <em className="subtle">{zip.batchId}</em>
                  </td>
                  <td>
                    <strong>{zip.partCount > 1 ? `part${String(zip.partIndex).padStart(2, "0")}` : "完整包"}</strong>
                    <em className="subtle">{zip.filename}</em>
                  </td>
                  <td>
                    <span className={`status zip-source ${zip.source}`}>{sourceLabel(zip.source)}</span>
                  </td>
                  <td>{formatBytes(zip.sizeBytes || zip.estimatedSizeBytes || 0)}</td>
                  <td>
                    <div className="zip-completion">
                      <span>{completionText(zip)}</span>
                      {zip.skippedTasks?.length ? (
                        <button className="detail-toggle" type="button" onClick={() => setDetailZip(zip)}>
                          查看
                        </button>
                      ) : null}
                    </div>
                  </td>
                  <td>{formatDate(zip.updatedAt)}</td>
                  <td>
                    {zip.zipStatus === "ready" && zip.downloadUrl ? (
                      <button className="primary compact" type="button" onClick={() => downloadAdminInternalBatchZip(zip)}>
                        下载
                      </button>
                    ) : (
                      <span className={`zip-work-status ${zip.zipStage || zip.zipStatus}`}>{zip.message}</span>
                    )}
                  </td>
                </tr>
              ))
            ) : (
              <tr><td className="empty" colSpan={8}>{activeTab.empty}</td></tr>
            )}
          </tbody>
        </table>
        <div className="pagination-bar">
          <span>
            第 {zipPage.page.page} / {zipPage.page.totalPages} 页，共 {zipPage.page.total} 条记录
          </span>
          <div>
            <button className="ghost compact" type="button" onClick={() => setPageNumber((page) => Math.max(1, page - 1))} disabled={!zipPage.page.hasPrevious || isFetching}>
              上一页
            </button>
            <button className="ghost compact" type="button" onClick={() => setPageNumber((page) => page + 1)} disabled={!zipPage.page.hasNext || isFetching}>
              下一页
            </button>
          </div>
        </div>
      </div>
      {detailZip ? (
        <div className="zip-detail-backdrop" role="presentation" onMouseDown={() => setDetailZip(null)}>
          <section className="zip-detail-dialog" role="dialog" aria-modal="true" aria-label="跳过明细" onMouseDown={(event) => event.stopPropagation()}>
            <div className="zip-detail-head">
              <div>
                <strong>{detailZip.batchName}</strong>
                <span>{completionText(detailZip)}</span>
              </div>
              <button className="ghost compact" type="button" onClick={() => setDetailZip(null)}>
                关闭
              </button>
            </div>
            <div className="zip-detail-list">
              {detailZip.skippedTasks.map((task) => (
                <article key={task.taskId} className="zip-detail-item">
                  <div>
                    <strong>第 {task.episode} 集</strong>
                    <span>{task.inputAssetName || task.taskId}</span>
                  </div>
                  <span className={`status ${task.status}`}>{statusLabel(task.status)}</span>
                  <p>{skippedReason(task)}</p>
                </article>
              ))}
            </div>
          </section>
        </div>
      ) : null}
    </section>
  );
}
