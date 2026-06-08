import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "@tanstack/react-router";
import { useForm } from "@tanstack/react-form";
import { useState } from "react";
import { login, register, setAuthToken } from "../api/client";

type LoginValues = {
  email: string;
  password: string;
  name: string;
};

export function LoginPage() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [error, setError] = useState("");
  const mutation = useMutation({
    mutationFn: async (values: LoginValues) => {
      if (mode === "login") return login({ email: values.email, password: values.password });
      return register({ email: values.email, password: values.password, name: values.name || values.email, role: "user", initialCredits: 0 });
    },
    onSuccess: async (payload) => {
      setAuthToken(payload.token);
      await queryClient.invalidateQueries({ queryKey: ["bootstrap"] });
      router.navigate({ to: "/tools" });
    },
    onError: (err) => setError(err.message),
  });
  const form = useForm({
    defaultValues: {
      email: "",
      password: "",
      name: "",
    },
    onSubmit: async ({ value }) => mutation.mutateAsync(value),
  });

  return (
    <section className="login-page">
      <div className="login-hero">
        <div className="brand login-brand">
          <div className="brand-mark">片</div>
          <div>
            <strong>片刻修AI</strong>
            <span>视频去水印与画质修复工作台</span>
          </div>
        </div>
        <h1>上传视频，一站式完成去字幕、去水印与高清修复</h1>
        <p>素材上传处理、积分计费、任务回调和结果下载，都在片刻修AI高效完成。</p>
        <div className="login-chips">
          <span>视频去字幕</span>
          <span>视频去水印</span>
          <span>高清增强</span>
          <span>任务进度追踪</span>
        </div>
      </div>
      <div className="login-panel">
        <div className="login-panel-head">
          <span>AI VIDEO WORKBENCH</span>
          <h2>{mode === "login" ? "登录片刻修AI" : "创建片刻修账号"}</h2>
          <p>{mode === "login" ? "继续查看处理进度、积分余额和历史结果。" : "创建账号后即可提交视频处理任务。"}</p>
        </div>
        {error ? <div className="notice">{error}</div> : null}
        <form
          className="login-form"
          onSubmit={(event) => {
            event.preventDefault();
            form.handleSubmit();
          }}
        >
          {mode === "register" ? (
            <form.Field name="name">
              {(field) => (
                <label>
                  昵称
                  <input value={field.state.value} onChange={(event) => field.handleChange(event.target.value)} placeholder="请输入昵称" />
                </label>
              )}
            </form.Field>
          ) : null}
          <form.Field name="email">
            {(field) => (
              <label>
                邮箱
                <input type="email" value={field.state.value} onChange={(event) => field.handleChange(event.target.value)} />
              </label>
            )}
          </form.Field>
          <form.Field name="password">
            {(field) => (
              <label>
                密码
                <input type="password" value={field.state.value} onChange={(event) => field.handleChange(event.target.value)} />
              </label>
            )}
          </form.Field>
          <button className="primary wide" type="submit" disabled={mutation.isPending}>
            {mutation.isPending ? "处理中..." : mode === "login" ? "进入工作台" : "创建账号"}
          </button>
        </form>
        <button className="link-button login-switch" onClick={() => setMode(mode === "login" ? "register" : "login")}>
          {mode === "login" ? "还没有账号？立即创建" : "已有账号？去登录"}
        </button>
      </div>
    </section>
  );
}
