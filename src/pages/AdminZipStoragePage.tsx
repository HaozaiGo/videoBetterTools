import { useState } from "react";
import { useQueryClient, useSuspenseQuery } from "@tanstack/react-query";
import { downloadAdminInternalBatchZip, getAdminInternalBatchZips } from "../api/client";
import { formatBytes, formatDate } from "../lib/format";
import type { AdminInternalBatchZip, AdminInternalBatchZipStatus } from "../types";

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
  const skipped = zip.failed + zip.cancelled + zip.processing;
  return skipped ? `${zip.succeeded}/${zip.total}，跳过 ${skipped}` : `${zip.succeeded}/${zip.total}`;
}

export function AdminZipStoragePage() {
  const queryClient = useQueryClient();
  const [activeStatus, setActiveStatus] = useState<AdminInternalBatchZipStatus>("ready");
  const [pageNumber, setPageNumber] = useState(1);
  const { data: zipPage, isFetching } = useSuspenseQuery({
    queryKey: ["admin-internal-batch-zips", activeStatus, pageNumber],
    queryFn: () => getAdminInternalBatchZips(pageNumber, 50, activeStatus),
    refetchInterval: 15_000,
  });

  const refresh = () => queryClient.invalidateQueries({ queryKey: ["admin-internal-batch-zips"] });
  const zips = zipPage.items;
  const totalBytes = zips.reduce((sum, zip) => sum + (zip.sizeBytes || zip.estimatedSizeBytes || 0), 0);
  const tosCount = zips.filter((zip) => zip.source === "tos").length;
  const localCount = zips.length - tosCount;
  const activeTab = zipTabs.find((tab) => tab.status === activeStatus) ?? zipTabs[0];

  function selectTab(status: AdminInternalBatchZipStatus) {
    setActiveStatus(status);
    setPageNumber(1);
  }

  return (
    <section className="admin-layout zip-storage-layout">
      <div className="page-head">
        <div>
          <h1>ZIP储存</h1>
          <p>查看已生成、可直接下载的批次 ZIP。</p>
        </div>
        <button className="ghost compact" type="button" onClick={refresh} disabled={isFetching}>
          {isFetching ? "刷新中" : "刷新"}
        </button>
      </div>

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
                  <td>{completionText(zip)}</td>
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
              <tr><td className="empty" colSpan={7}>{activeTab.empty}</td></tr>
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
    </section>
  );
}
