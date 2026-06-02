import { Link, useRouterState } from "@tanstack/react-router";
import { useMutation, useQueryClient, useSuspenseQuery } from "@tanstack/react-query";
import { getBootstrap, recharge } from "../api/client";
import { clearAuthToken } from "../api/client";
import { formatCredits } from "../lib/format";
import type { BootstrapState } from "../types";

function isActive(pathname: string, target: string) {
  if (target === "/tools") return pathname === "/tools" || pathname.startsWith("/tools/");
  return pathname === target;
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  const queryClient = useQueryClient();
  const { data } = useSuspenseQuery({ queryKey: ["bootstrap"], queryFn: getBootstrap });
  const account = data.account;
  const rechargeMutation = useMutation({
    mutationFn: recharge,
    onSuccess: (payload) => queryClient.setQueryData<BootstrapState>(["bootstrap"], payload.state),
  });

  return (
    <>
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">NW</div>
          <div>
            <strong>牛蛙AI工作台</strong>
            <span>AI视频创作必备助手</span>
          </div>
        </div>
        <nav className="nav">
          <button>批处理工作台</button>
          <Link className={isActive(pathname, "/tools") ? "active" : ""} to="/tools">
            AI智能工具
          </Link>
          <Link className={isActive(pathname, "/tasks") ? "active" : ""} to="/tasks">
            任务列表
          </Link>
          <Link className={isActive(pathname, "/admin") ? "active" : ""} to="/admin">
            后台管理
          </Link>
        </nav>
        <div className="side-bottom">
          <Link to="/billing">充值与消耗</Link>
          <button>联系我们</button>
          <div className="login-box">
            <div className="avatar">人</div>
            <div>
              <strong>{account.name}</strong>
              <span>可用 {formatCredits(account.availableCredits)}</span>
            </div>
          </div>
          <button
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
          <div className="crumb">AI智能工具台</div>
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
