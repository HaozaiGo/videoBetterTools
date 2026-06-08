import { useMutation, useQueryClient, useSuspenseQuery } from "@tanstack/react-query";
import { createColumnHelper, flexRender, getCoreRowModel, useReactTable } from "@tanstack/react-table";
import { useForm } from "@tanstack/react-form";
import { createAdminUser, getAdminSummary, getAdminTasks, getAdminUsers, rechargeAdminUser } from "../api/client";
import { formatCredits, formatDate, statusLabel } from "../lib/format";
import type { AdminUser, Task } from "../types";

const userColumns = createColumnHelper<AdminUser>();
const taskColumns = createColumnHelper<Task>();

export function AdminPage() {
  const queryClient = useQueryClient();
  const { data: summary } = useSuspenseQuery({ queryKey: ["admin-summary"], queryFn: getAdminSummary });
  const { data: users } = useSuspenseQuery({ queryKey: ["admin-users"], queryFn: getAdminUsers });
  const { data: tasks } = useSuspenseQuery({ queryKey: ["admin-tasks"], queryFn: getAdminTasks });
  const refreshAdmin = () => {
    queryClient.invalidateQueries({ queryKey: ["admin-summary"] });
    queryClient.invalidateQueries({ queryKey: ["admin-users"] });
    queryClient.invalidateQueries({ queryKey: ["admin-tasks"] });
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

  return (
    <section className="admin-layout">
      <div className="page-head">
        <div>
          <h1>后台管理</h1>
          <p>管理用户、积分充值和全局任务状态。</p>
        </div>
      </div>
      <div className="admin-stats">
        <div className="admin-gpu-status"><span>GPU 状态</span><strong>远端在线</strong></div>
        <div><span>用户</span><strong>{summary.users}</strong></div>
        <div><span>任务</span><strong>{summary.tasks}</strong></div>
        <div><span>资产</span><strong>{summary.assets}</strong></div>
        <div><span>已扣积分</span><strong>{formatCredits(summary.creditsCharged)}</strong></div>
        <div><span>队列中</span><strong>{summary.queuedTasks}</strong></div>
        <div><span>失败</span><strong>{summary.failedTasks}</strong></div>
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
        <div className="panel">
          <div className="section-head">
            <div>
              <h2>任务监控</h2>
              <p>最近任务与状态概览。</p>
            </div>
          </div>
          <SimpleTable table={taskTable} empty="暂无任务" />
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
