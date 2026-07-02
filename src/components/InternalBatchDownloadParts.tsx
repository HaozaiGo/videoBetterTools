import { useEffect, useRef, useState } from "react";
import { downloadInternalBatchZipPart, getInternalBatchDownloadManifest, prepareInternalBatchZipPart, type InternalBatchDownloadManifest, type InternalBatchDownloadPart } from "../api/client";

type Props = {
  batchId: string;
  batchName: string;
  manifest: InternalBatchDownloadManifest | null;
  onManifestChange?: (manifest: InternalBatchDownloadManifest) => void;
  onStarted?: (part: InternalBatchDownloadPart) => void;
};

const partReadyPollMs = 5000;

function manifestStorageKey(batchId: string) {
  return `model-plaza:internal-batch:${batchId}:download-manifest`;
}

function preparingStorageKey(batchId: string) {
  return `model-plaza:internal-batch:${batchId}:preparing-part`;
}

function readStoredManifest(batchId: string): InternalBatchDownloadManifest | null {
  try {
    const raw = window.sessionStorage.getItem(manifestStorageKey(batchId));
    return raw ? (JSON.parse(raw) as InternalBatchDownloadManifest) : null;
  } catch {
    return null;
  }
}

function writeStoredManifest(batchId: string, nextManifest: InternalBatchDownloadManifest) {
  try {
    window.sessionStorage.setItem(manifestStorageKey(batchId), JSON.stringify(nextManifest));
  } catch {
    // Best-effort UI continuity only.
  }
}

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
  const [localManifest, setLocalManifest] = useState<InternalBatchDownloadManifest | null>(() => readStoredManifest(batchId));
  const [preparingPartIndex, setPreparingPartIndex] = useState<number | null>(() => {
    const stored = window.sessionStorage.getItem(preparingStorageKey(batchId));
    return stored ? Number(stored) || null : null;
  });
  const [readyPartIndexes, setReadyPartIndexes] = useState<Set<number>>(() => new Set());
  const [downloadStatus, setDownloadStatus] = useState("");
  const pollTimerRef = useRef<number | null>(null);
  const autoDownloadedPartsRef = useRef<Set<number>>(new Set());

  useEffect(() => {
    return () => {
      if (pollTimerRef.current !== null) {
        window.clearInterval(pollTimerRef.current);
      }
    };
  }, []);

  const stopPolling = () => {
    if (pollTimerRef.current !== null) {
      window.clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  };

  const rememberManifest = (nextManifest: InternalBatchDownloadManifest) => {
    setLocalManifest(nextManifest);
    writeStoredManifest(batchId, nextManifest);
    onManifestChange?.(nextManifest);
  };

  const rememberPreparingPart = (partIndex: number | null) => {
    setPreparingPartIndex(partIndex);
    try {
      if (partIndex === null) {
        window.sessionStorage.removeItem(preparingStorageKey(batchId));
      } else {
        window.sessionStorage.setItem(preparingStorageKey(batchId), String(partIndex));
      }
    } catch {
      // Best-effort UI continuity only.
    }
  };

  const pollPartReady = (partIndex: number) => {
    stopPolling();
    pollTimerRef.current = window.setInterval(async () => {
      try {
        const latestManifest = await getInternalBatchDownloadManifest(batchId);
        rememberManifest(latestManifest);
        const latestPart = latestManifest.parts.find((item) => item.index === partIndex);
        if (latestPart && latestPart.sizeBytes > 0) {
          setReadyPartIndexes((current) => new Set(current).add(partIndex));
          rememberPreparingPart(null);
          if (!autoDownloadedPartsRef.current.has(partIndex)) {
            autoDownloadedPartsRef.current.add(partIndex);
            downloadInternalBatchZipPart(latestPart, batchName);
          }
          setDownloadStatus(`${latestPart.filename} 已生成，已开始下载；如未开始，可再次点击该分包。`);
          stopPolling();
        }
      } catch (error) {
        rememberPreparingPart(null);
        setDownloadStatus(error instanceof Error ? error.message : "分包状态刷新失败");
        stopPolling();
      }
    }, partReadyPollMs);
  };

  useEffect(() => {
    if (manifest) {
      setLocalManifest(manifest);
      writeStoredManifest(batchId, manifest);
    } else {
      setLocalManifest(readStoredManifest(batchId));
    }
  }, [batchId, manifest]);

  useEffect(() => {
    if (preparingPartIndex !== null) {
      pollPartReady(preparingPartIndex);
    }
    return stopPolling;
  }, [batchId, preparingPartIndex]);

  const visibleManifest = manifest ?? localManifest;
  if (!visibleManifest?.parts.length) return null;

  const handlePartClick = async (part: InternalBatchDownloadPart, isReady: boolean) => {
    if (isReady) {
      setDownloadStatus(`已开始下载 ${part.filename}`);
      rememberPreparingPart(null);
      downloadInternalBatchZipPart(part, batchName);
      onStarted?.(part);
      return;
    }

    rememberPreparingPart(part.index);
    setDownloadStatus(`正在生成 ${part.filename}，生成完成后会自动开始下载。`);
    onStarted?.(part);
    try {
      const prepared = await prepareInternalBatchZipPart(batchId, part.index);
      const mergedParts = visibleManifest.parts.map((item) => (item.index === prepared.part.index ? prepared.part : item));
      rememberManifest({ partCount: prepared.partCount, parts: mergedParts });
      if (prepared.status === "ready" || prepared.part.sizeBytes > 0) {
        setReadyPartIndexes((current) => new Set(current).add(prepared.part.index));
        rememberPreparingPart(null);
        downloadInternalBatchZipPart(prepared.part, batchName);
        setDownloadStatus(`已开始下载 ${prepared.part.filename}`);
      } else {
        pollPartReady(part.index);
      }
    } catch (error) {
      rememberPreparingPart(null);
      setDownloadStatus(error instanceof Error ? error.message : "分包生成失败");
    }
  };

  return (
    <div className="internal-batch-parts-wrap">
      <div className="internal-batch-parts">
        {visibleManifest.parts.map((part) => {
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
              onClick={() => handlePartClick(part, isReady)}
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
