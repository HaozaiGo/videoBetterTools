import { useEffect, useRef, useState } from "react";
import { downloadInternalBatchZipPart, getInternalBatchDownloadManifest, type InternalBatchDownloadManifest, type InternalBatchDownloadPart } from "../api/client";

type Props = {
  batchId: string;
  batchName: string;
  manifest: InternalBatchDownloadManifest | null;
  onManifestChange?: (manifest: InternalBatchDownloadManifest) => void;
  onStarted?: (part: InternalBatchDownloadPart) => void;
};

const partReadyPollMs = 5000;

function formatBytes(bytes?: number) {
  if (!bytes || bytes <= 0) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = bytes;
  let unitIndex = 0;
  while (size >= 1024 && unitIndex < units.length - 1) {
    size /= 1024;
    unitIndex += 1;
  }
  return `${size >= 10 || unitIndex === 0 ? size.toFixed(0) : size.toFixed(1)} ${units[unitIndex]}`;
}

export function InternalBatchDownloadParts({ batchId, batchName, manifest, onManifestChange, onStarted }: Props) {
  const [preparingPartIndex, setPreparingPartIndex] = useState<number | null>(null);
  const [readyPartIndexes, setReadyPartIndexes] = useState<Set<number>>(() => new Set());
  const [downloadStatus, setDownloadStatus] = useState("");
  const pollTimerRef = useRef<number | null>(null);

  useEffect(() => {
    return () => {
      if (pollTimerRef.current !== null) {
        window.clearInterval(pollTimerRef.current);
      }
    };
  }, []);

  if (!manifest?.parts.length) return null;

  const stopPolling = () => {
    if (pollTimerRef.current !== null) {
      window.clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  };

  const pollPartReady = (partIndex: number) => {
    stopPolling();
    pollTimerRef.current = window.setInterval(async () => {
      try {
        const latestManifest = await getInternalBatchDownloadManifest(batchId);
        onManifestChange?.(latestManifest);
        const latestPart = latestManifest.parts.find((item) => item.index === partIndex);
        if (latestPart && latestPart.sizeBytes > 0) {
          setReadyPartIndexes((current) => new Set(current).add(partIndex));
          setPreparingPartIndex(null);
          setDownloadStatus(`${latestPart.filename} 已生成，浏览器应开始下载；如未开始，可再次点击该分包。`);
          stopPolling();
        }
      } catch (error) {
        setPreparingPartIndex(null);
        setDownloadStatus(error instanceof Error ? error.message : "分包状态刷新失败");
        stopPolling();
      }
    }, partReadyPollMs);
  };

  return (
    <div className="internal-batch-parts-wrap">
      <div className="internal-batch-parts">
        {manifest.parts.map((part) => {
          const isPreparing = preparingPartIndex === part.index;
          const isBusy = preparingPartIndex !== null && !isPreparing;
          const isReady = part.sizeBytes > 0 || readyPartIndexes.has(part.index);
          const size = formatBytes(part.sizeBytes || part.estimatedSizeBytes);
          return (
            <button
              className={`ghost compact internal-batch-part${isPreparing ? " preparing" : ""}${isReady ? " ready" : ""}`}
              disabled={isBusy}
              key={part.index}
              type="button"
              onClick={() => {
                setDownloadStatus(isReady ? `已开始下载 ${part.filename}` : `正在生成 ${part.filename}，请等待浏览器开始下载。`);
                setPreparingPartIndex(isReady ? null : part.index);
                downloadInternalBatchZipPart(part, batchName);
                onStarted?.(part);
                if (!isReady) {
                  pollPartReady(part.index);
                }
              }}
            >
              <span>{isPreparing ? "生成中..." : `part${String(part.index).padStart(2, "0")}`}</span>
              {size ? <small>{isPreparing ? "准备下载" : isReady ? size : `约 ${size}`}</small> : null}
            </button>
          );
        })}
      </div>
      {downloadStatus ? <em className="internal-batch-download-status">{downloadStatus}</em> : null}
    </div>
  );
}
