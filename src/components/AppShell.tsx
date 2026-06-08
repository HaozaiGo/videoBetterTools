import { Link, useRouterState } from "@tanstack/react-router";
import { useMutation, useQueryClient, useSuspenseQuery } from "@tanstack/react-query";
import { getBootstrap, recharge } from "../api/client";
import { clearAuthToken } from "../api/client";
import { formatCredits } from "../lib/format";
import type { BootstrapState } from "../types";

function isActive(pathname: string, target: string) {
  if (target === "/tools") return pathname === "/tools" || pathname.startsWith("/tools/");
  if (target === "/billing") return pathname === "/billing";
  return pathname === target;
}

const navItems = [
  { to: "/tools", label: "工具广场", icon: "⌕" },
  { to: "/tasks", label: "任务列表", icon: "☑" },
  { to: "/billing", label: "充值消耗", icon: "¥" },
  { to: "/admin", label: "后台管理", icon: "⚙", adminOnly: true },
] as const;

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  const queryClient = useQueryClient();
  const { data } = useSuspenseQuery({ queryKey: ["bootstrap"], queryFn: getBootstrap });
  const account = data.account;
  const rechargeMutation = useMutation({
    mutationFn: recharge,
    onSuccess: (payload) => queryClient.setQueryData<BootstrapState>(["bootstrap"], payload.state),
  });

  if (pathname === "/login") {
    return <div className="auth-root">{children}</div>;
  }

  return (
    <>
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">片</div>
          <div>
            <strong>片刻修AI</strong>
            <span>AI视频创作必备</span>
          </div>
        </div>
        <nav className="nav">
          {navItems
            .filter((item) => !("adminOnly" in item) || account.role === "admin")
            .map((item) => (
              <Link className={isActive(pathname, item.to) ? "active" : ""} key={item.to} to={item.to}>
                <span aria-hidden="true">{item.icon}</span>
                {item.label}
              </Link>
            ))}
        </nav>
        <div className="side-bottom">
          <div className="login-box">
            <div className="avatar">人</div>
            <div>
              <strong>{account.name}</strong>
              <span>{account.role || "user"} · 可用 {formatCredits(account.availableCredits)}</span>
            </div>
          </div>
          <button
            className="logout-button"
            onClick={() => {
              clearAuthToken();
              location.assign("/login");
            }}
          >
            退出登录
          </button>
        </div>
      </aside>
      <main className="workspace">
        <header className="topbar">
          <div className="crumb">
            <span>片刻修AI</span>
            <strong>AI视频工具生产台</strong>
          </div>
          <div className="account-pill">
            <span>余额 {formatCredits(account.credits)}</span>
            <span>冻结 {formatCredits(account.frozenCredits)}</span>
            <button onClick={() => rechargeMutation.mutate(100)} disabled={rechargeMutation.isPending}>
              优惠充值
            </button>
          </div>
        </header>
        {children}
      </main>
    </>
  );
}
