import { useState } from "react";
import { useQueryClient, useSuspenseQuery } from "@tanstack/react-query";
import { downloadAdminInternalBatchZip, getAdminInternalBatchZips } from "../api/client";
import { formatBytes, formatDate } from "../lib/format";
import type { AdminInternalBatchZip } from "../types";

function sourceLabel(source: AdminInternalBatchZip["source"]) {
  return source === "tos" ? "TOS" : "本地";
}

function completionText(zip: AdminInternalBatchZip) {
  const skipped = zip.failed + zip.cancelled + zip.processing;
  return skipped ? `${zip.succeeded}/${zip.total}，跳过 ${skipped}` : `${zip.succeeded}/${zip.total}`;
}

export function AdminZipStoragePage() {
  const queryClient = useQueryClient();
  const [pageNumber, setPageNumber] = useState(1);
  const { data: zipPage, isFetching } = useSuspenseQuery({
    queryKey: ["admin-internal-batch-zips", pageNumber],
    queryFn: () => getAdminInternalBatchZips(pageNumber),
    refetchInterval: 15_000,
  });

  const refresh = () => queryClient.invalidateQueries({ queryKey: ["admin-internal-batch-zips"] });
  const zips = zipPage.items;
  const totalBytes = zips.reduce((sum, zip) => sum + zip.sizeBytes, 0);
  const tosCount = zips.filter((zip) => zip.source === "tos").length;
  const localCount = zips.length - tosCount;

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

      <div className="admin-stats">
        <div><span>当前页 ZIP</span><strong>{zips.length}</strong></div>
        <div><span>TOS</span><strong>{tosCount}</strong></div>
        <div><span>本地</span><strong>{localCount}</strong></div>
        <div><span>当前页容量</span><strong>{formatBytes(totalBytes)}</strong></div>
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
              <th>下载</th>
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
                  <td>{formatBytes(zip.sizeBytes)}</td>
                  <td>{completionText(zip)}</td>
                  <td>{formatDate(zip.updatedAt)}</td>
                  <td>
                    <button className="primary compact" type="button" onClick={() => downloadAdminInternalBatchZip(zip)}>
                      下载
                    </button>
                  </td>
                </tr>
              ))
            ) : (
              <tr><td className="empty" colSpan={7}>暂无可直接下载的 ZIP。</td></tr>
            )}
          </tbody>
        </table>
        <div className="pagination-bar">
          <span>
            第 {zipPage.page.page} / {zipPage.page.totalPages} 页，共 {zipPage.page.total} 个 ZIP
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
