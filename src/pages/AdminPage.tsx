import { useMutation, useQuery, useQueryClient, useSuspenseQuery } from "@tanstack/react-query";
import { createColumnHelper, flexRender, getCoreRowModel, useReactTable } from "@tanstack/react-table";
import { useForm } from "@tanstack/react-form";
import { createAdminUser, getAdminGpuMetrics, getAdminSummary, getAdminTasks, getAdminUsers, rechargeAdminUser } from "../api/client";
import { formatCredits, formatDate, statusLabel } from "../lib/format";
import type { AdminUser, Task } from "../types";

const userColumns = createColumnHelper<AdminUser>();
const taskColumns = createColumnHelper<Task>();

function formatGpuMemory(usedMiB: number, totalMiB: number) {
  return `${(usedMiB / 1024).toFixed(1)} / ${(totalMiB / 1024).toFixed(1)} GB`;
}

function formatRunningTime(seconds: number) {
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const restSeconds = seconds % 60;
  if (minutes < 60) return `${minutes}m ${restSeconds}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

function formatGpuTimestamp(timestamp?: number) {
  if (!timestamp) return "尚未同步";
  return new Date(timestamp * 1000).toLocaleString("zh-CN");
}

export function AdminPage() {
  const queryClient = useQueryClient();
  const { data: summary } = useSuspenseQuery({ queryKey: ["admin-summary"], queryFn: getAdminSummary });
  const { data: users } = useSuspenseQuery({ queryKey: ["admin-users"], queryFn: getAdminUsers });
  const { data: tasks } = useSuspenseQuery({ queryKey: ["admin-tasks"], queryFn: getAdminTasks });
  const { data: gpuMetrics, isFetching: isGpuFetching } = useQuery({
    queryKey: ["admin-gpu"],
    queryFn: getAdminGpuMetrics,
    refetchInterval: 5000,
  });
  const refreshAdmin = () => {
    queryClient.invalidateQueries({ queryKey: ["admin-summary"] });
    queryClient.invalidateQueries({ queryKey: ["admin-users"] });
    queryClient.invalidateQueries({ queryKey: ["admin-tasks"] });
    queryClient.invalidateQueries({ queryKey: ["admin-gpu"] });
  };
  const createUserMutation = useMutation({
    mutationFn: createAdminUser,
    onSuccess: refreshAdmin,
  });
  const rechargeMutation = useMutation({
    mutationFn: ({ userId, credits }: { userId: string; credits: number }) => rechargeAdminUser(userId, credits),
    onSuccess: refreshAdmin,
  });
  const createUserForm = useForm({
    defaultValues: {
      email: "",
      password: "12345678",
      name: "",
      role: "user",
      initialCredits: 100,
    },
    onSubmit: async ({ value }) => createUserMutation.mutateAsync(value),
  });

  const userTable = useReactTable({
    data: users,
    columns: [
      userColumns.accessor("email", { header: "用户" }),
      userColumns.accessor("role", { header: "角色" }),
      userColumns.accessor("credits", { header: "余额", cell: (info) => formatCredits(info.getValue()) }),
      userColumns.accessor("frozenCredits", { header: "冻结", cell: (info) => formatCredits(info.getValue()) }),
      userColumns.accessor("createdAt", { header: "创建时间", cell: (info) => formatDate(info.getValue()) }),
      userColumns.display({
        id: "actions",
        header: "操作",
        cell: ({ row }) => (
          <button className="link-button" onClick={() => rechargeMutation.mutate({ userId: row.original.id, credits: 100 })}>
            +100 积分
          </button>
        ),
      }),
    ],
    getCoreRowModel: getCoreRowModel(),
  });

  const taskTable = useReactTable({
    data: tasks,
    columns: [
      taskColumns.accessor("toolSlug", { header: "工具" }),
      taskColumns.accessor("status", { header: "状态", cell: (info) => statusLabel(info.getValue()) }),
      taskColumns.accessor("estimatedCredits", { header: "预估", cell: (info) => formatCredits(info.getValue()) }),
      taskColumns.accessor("createdAt", { header: "创建时间", cell: (info) => formatDate(info.getValue()) }),
    ],
    getCoreRowModel: getCoreRowModel(),
  });
  const gpuStatusText = !gpuMetrics ? "检查中" : gpuMetrics.ok ? "远端在线" : "远端异常";
  const gpus = gpuMetrics?.gpus || [];
  const runningJobs = gpuMetrics?.runningJobs || [];
  const busyGpuCount = gpus.filter((gpu) => gpu.utilizationGpuPercent > 5 || gpu.workerSlotsUsed > 0).length;
  const freeGpuCount = gpus.filter((gpu) => gpu.utilizationGpuPercent <= 5 && gpu.memoryUsedMiB < 1024 && gpu.workerSlotsUsed === 0).length;

  return (
    <section className="admin-layout">
      <div className="page-head">
        <div>
          <h1>后台管理</h1>
          <p>管理用户、积分充值和全局任务状态。</p>
        </div>
      </div>
      <div className="admin-stats">
        <div className="admin-gpu-status"><span>GPU 状态</span><strong>{gpuStatusText}</strong></div>
        <div><span>用户</span><strong>{summary.users}</strong></div>
        <div><span>任务</span><strong>{summary.tasks}</strong></div>
        <div><span>资产</span><strong>{summary.assets}</strong></div>
        <div><span>已扣积分</span><strong>{formatCredits(summary.creditsCharged)}</strong></div>
        <div><span>队列中</span><strong>{summary.queuedTasks}</strong></div>
        <div><span>失败</span><strong>{summary.failedTasks}</strong></div>
      </div>
      <div className="panel gpu-monitor">
        <div className="section-head">
          <div>
            <h2>线上 GPU 监控</h2>
            <p>
              {gpuMetrics?.ok
                ? `最近同步：${formatGpuTimestamp(gpuMetrics.timestamp)}，忙碌 ${busyGpuCount} 张，空闲 ${freeGpuCount} 张。`
                : gpuMetrics?.error || "正在获取远端 GPU 指标。"}
            </p>
          </div>
          <button className="ghost compact" onClick={() => queryClient.invalidateQueries({ queryKey: ["admin-gpu"] })}>
            {isGpuFetching ? "刷新中" : "刷新"}
          </button>
        </div>
        {gpus.length ? (
          <div className="gpu-monitor-grid">
            {gpus.map((gpu) => (
              <div className="gpu-monitor-row" key={gpu.index}>
                <div>
                  <strong>GPU {gpu.index}</strong>
                  <span>{gpu.name}</span>
                </div>
                <div>
                  <span>利用率</span>
                  <strong>{gpu.utilizationGpuPercent}%</strong>
                  <div className="gpu-meter"><span style={{ width: `${gpu.utilizationGpuPercent}%` }} /></div>
                </div>
                <div>
                  <span>显存</span>
                  <strong>{formatGpuMemory(gpu.memoryUsedMiB, gpu.memoryTotalMiB)}</strong>
                  <div className="gpu-meter memory">
                    <span style={{ width: `${gpu.memoryTotalMiB ? Math.round((gpu.memoryUsedMiB / gpu.memoryTotalMiB) * 100) : 0}%` }} />
                  </div>
                </div>
                <div>
                  <span>worker</span>
                  <strong>
                    {gpu.workerSlotsUsed} / {gpu.workerSlotsTotal}
                  </strong>
                </div>
                <div>
                  <span>温度 / 功耗</span>
                  <strong>{gpu.temperatureGpu}°C / {gpu.powerDrawW.toFixed(0)}W</strong>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div className="empty">暂无 GPU 指标。</div>
        )}
        <div className="gpu-job-list">
          <h3>运行中的远端任务</h3>
          {runningJobs.length ? (
            runningJobs.map((job) => (
              <div className="gpu-job-row" key={job.id}>
                <div>
                  <strong>{job.jobType || "video"}</strong>
                  <span>{job.id}</span>
                </div>
                <span>GPU {job.assignedGpu || "-"}</span>
                <span>{job.progressPercent}%</span>
                <span>{formatRunningTime(job.runningSeconds)}</span>
                <em>{job.progressStage || job.status}</em>
              </div>
            ))
          ) : (
            <div className="empty">当前没有运行中的远端任务。</div>
          )}
        </div>
      </div>
      <div className="admin-columns">
        <div className="panel">
          <div className="section-head">
            <div>
              <h2>用户管理</h2>
              <p>添加用户、查看余额，并为用户模拟充值。</p>
            </div>
          </div>
          <form
            className="admin-create-user"
            onSubmit={(event) => {
              event.preventDefault();
              createUserForm.handleSubmit();
            }}
          >
            <createUserForm.Field name="email">
              {(field) => (
                <label>
                  邮箱
                  <input type="email" value={field.state.value} onChange={(event) => field.handleChange(event.target.value)} placeholder="user@example.com" />
                </label>
              )}
            </createUserForm.Field>
            <createUserForm.Field name="name">
              {(field) => (
                <label>
                  昵称
                  <input value={field.state.value} onChange={(event) => field.handleChange(event.target.value)} placeholder="用户昵称" />
                </label>
              )}
            </createUserForm.Field>
            <createUserForm.Field name="password">
              {(field) => (
                <label>
                  初始密码
                  <input value={field.state.value} onChange={(event) => field.handleChange(event.target.value)} />
                </label>
              )}
            </createUserForm.Field>
            <createUserForm.Field name="initialCredits">
              {(field) => (
                <label>
                  初始积分
                  <input type="number" value={field.state.value} onChange={(event) => field.handleChange(Number(event.target.value))} />
                </label>
              )}
            </createUserForm.Field>
            <button className="primary" type="submit" disabled={createUserMutation.isPending}>
              创建用户
            </button>
          </form>
          <SimpleTable table={userTable} empty="暂无用户" />
        </div>
        <div className="panel admin-task-monitor">
          <div className="section-head">
            <div>
              <h2>任务监控</h2>
              <p>最近任务与状态概览。</p>
            </div>
          </div>
          <div className="admin-task-monitor-table">
            <SimpleTable table={taskTable} empty="暂无任务" />
          </div>
        </div>
      </div>
    </section>
  );
}

function SimpleTable({ table, empty }: { table: ReturnType<typeof useReactTable<any>>; empty: string }) {
  const columnCount = table.getAllColumns().length;
  return (
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
          <tr><td className="empty" colSpan={columnCount}>{empty}</td></tr>
        )}
      </tbody>
    </table>
  );
}
