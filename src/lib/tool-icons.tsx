const labels: Record<string, string> = {
  spark: "✦",
  caption: "▣",
  grid: "▦",
  hd: "HD",
  translate: "中A",
  erase: "⌫",
  paint: "◩",
};

export function ToolIcon({ type }: { type: string }) {
  return (
    <span className={`tool-icon tool-icon-${type}`} aria-hidden="true">
      {labels[type] || "AI"}
    </span>
  );
}
