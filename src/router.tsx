import { Outlet, Router, createRootRouteWithContext, createRoute, redirect } from "@tanstack/react-router";
import type { QueryClient } from "@tanstack/react-query";
import { getBootstrap } from "./api/client";
import { AppShell } from "./components/AppShell";
import { BillingPage } from "./pages/BillingPage";
import { AdminPage } from "./pages/AdminPage";
import { TasksPage } from "./pages/TasksPage";
import { ToolPage } from "./pages/ToolPage";
import { ToolsPage } from "./pages/ToolsPage";
import { LoginPage } from "./pages/LoginPage";

type RouterContext = {
  queryClient: QueryClient;
};

export const rootRoute = createRootRouteWithContext<RouterContext>()({
  component: () => (
    <AppShell>
      <Outlet />
    </AppShell>
  ),
  loader: ({ context }) => context.queryClient.ensureQueryData({ queryKey: ["bootstrap"], queryFn: getBootstrap }),
});

const indexRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  beforeLoad: () => {
    throw redirect({ to: "/tools" });
  },
});

const toolsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/tools",
  component: ToolsPage,
});

const loginRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/login",
  component: LoginPage,
});

const videoToolRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/tools/video/$toolSlug",
  component: ToolPage,
});

const imageToolRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/tools/image/$toolSlug",
  component: ToolPage,
});

const tasksRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/tasks",
  component: TasksPage,
});

const billingRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/billing",
  component: BillingPage,
});

const adminRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/admin",
  component: AdminPage,
});

const routeTree = rootRoute.addChildren([indexRoute, loginRoute, toolsRoute, videoToolRoute, imageToolRoute, tasksRoute, billingRoute, adminRoute]);

export const router = new Router({
  routeTree,
  context: {
    queryClient: undefined!,
  },
});

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
