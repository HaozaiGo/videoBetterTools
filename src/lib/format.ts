import type { LedgerEntry, TaskStatus } from "../types";

export function formatCredits(value: number | undefined) {
  return `${Number(value || 0).toFixed(0)} 积分`;
}

export function formatDate(value: number | null | undefined) {
  if (!value) return "-";
  return new Date(value).toLocaleString("zh-CN");
}

export function statusLabel(status: TaskStatus) {
  return {
    queued: "排队中",
    processing: "处理中",
    succeeded: "已完成",
    failed: "失败已退还",
    cancelled: "已取消",
  }[status];
}

export function ledgerAmount(entry: LedgerEntry) {
  if (entry.type === "freeze") return "冻结";
  if (entry.type === "refund") return "已释放";
  return `${entry.amount > 0 ? "+" : ""}${formatCredits(entry.amount)}`;
}
