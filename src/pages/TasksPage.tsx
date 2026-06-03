import { useMutation, useQueryClient, useQuery, useSuspenseQuery } from "@tanstack/react-query";
import { createColumnHelper, flexRender, getCoreRowModel, useReactTable } from "@tanstack/react-table";
import { failProviderJob, getBootstrap } from "../api/client";
import { formatCredits, formatDate, statusLabel } from "../lib/format";
import type { BootstrapState, Task } from "../types";

const columnHelper = createColumnHelper<Task>();

export function TasksPage() {
  const queryClient = useQueryClient();
  const { data } = useSuspenseQuery({ queryKey: ["bootstrap"], queryFn: getBootstrap });
  useQuery({
    queryKey: ["bootstrap"],
    queryFn: getBootstrap,
    refetchInterval: (query) => {
      const state = query.state.data;
      return state?.tasks.some((task) => ["queued", "processing"].includes(task.status)) ? 1600 : false;
    },
  });

  const failMutation = useMutation({
    mutationFn: failProviderJob,
    onSuccess: (payload) => queryClient.setQueryData<BootstrapState>(["bootstrap"], payload.state),
  });

  const columns = [
    columnHelper.accessor("toolSlug", {
      header: "工具 / 供应商任务",
      cell: ({ row }) => {
        const tool = data.tools.find((item) => item.slug === row.original.toolSlug);
        return (
          <>
            <strong>{tool?.name || row.original.toolSlug}</strong>
            <span className="subtle">{row.original.providerJobId}</span>
          </>
        );
      },
    }),
    columnHelper.accessor("status", {
      header: "状态",
      cell: (info) => <span className={`status ${info.getValue()}`}>{statusLabel(info.getValue())}</span>,
    }),
    columnHelper.accessor("estimatedCredits", {
      header: "预估",
      cell: (info) => formatCredits(info.getValue()),
    }),
    columnHelper.accessor("createdAt", {
      header: "创建时间",
      cell: (info) => formatDate(info.getValue()),
    }),
    columnHelper.display({
      id: "result",
      header: "结果",
      cell: ({ row }) => {
        const task = row.original;
        const isFinal = ["succeeded", "failed", "cancelled"].includes(task.status);
        return (
          <div className="task-actions">
            {task.outputUrl ? (
              <a href={task.outputUrl} target="_blank" rel="noreferrer">
                查看结果
              </a>
            ) : (
              "等待结果"
            )}
            {!isFinal ? (
              <button className="link-button" onClick={() => failMutation.mutate(task.providerJobId)} disabled={failMutation.isPending}>
                模拟失败回调
              </button>
            ) : null}
          </div>
        );
      },
    }),
  ];

  const table = useReactTable({
    data: data.tasks,
    columns,
    getCoreRowModel: getCoreRowModel(),
  });

  return (
    <section className="panel">
      <div className="panel-head">
        <div>
          <h1>任务列表</h1>
        </div>
        <button className="ghost" onClick={() => queryClient.invalidateQueries({ queryKey: ["bootstrap"] })}>
          刷新
        </button>
      </div>
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
            table.getRowModel().rows.map((row) => (
              <tr key={row.id}>
                {row.getVisibleCells().map((cell) => (
                  <td key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</td>
                ))}
              </tr>
            ))
          ) : (
            <tr>
              <td colSpan={columns.length} className="empty">
                暂无任务，从工具广场创建第一个任务。
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </section>
  );
}
