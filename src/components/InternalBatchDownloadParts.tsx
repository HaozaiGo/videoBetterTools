import { downloadInternalBatchZipPart, type InternalBatchDownloadManifest, type InternalBatchDownloadPart } from "../api/client";

type Props = {
  batchName: string;
  manifest: InternalBatchDownloadManifest | null;
  onStarted?: (part: InternalBatchDownloadPart) => void;
};

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

export function InternalBatchDownloadParts({ batchName, manifest, onStarted }: Props) {
  if (!manifest?.parts.length) return null;

  return (
    <div className="internal-batch-parts">
      {manifest.parts.map((part) => {
        const size = formatBytes(part.sizeBytes || part.estimatedSizeBytes);
        return (
          <button
            className="ghost compact internal-batch-part"
            key={part.index}
            type="button"
            onClick={() => {
              downloadInternalBatchZipPart(part, batchName);
              onStarted?.(part);
            }}
          >
            <span>{`part${String(part.index).padStart(2, "0")}`}</span>
            {size ? <small>{part.sizeBytes > 0 ? size : `约 ${size}`}</small> : null}
          </button>
        );
      })}
    </div>
  );
}
