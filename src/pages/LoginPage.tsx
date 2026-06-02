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
      email: "demo@modelplaza.local",
      password: "demo123456",
      name: "",
    },
    onSubmit: async ({ value }) => mutation.mutateAsync(value),
  });

  return (
    <section className="login-page">
      <div className="panel login-panel">
        <h1>{mode === "login" ? "登录" : "注册账号"}</h1>
        <p>{mode === "login" ? "使用账号进入工具工作台。" : "创建新用户后会自动登录。"}</p>
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
            {mutation.isPending ? "处理中..." : mode === "login" ? "登录" : "注册"}
          </button>
        </form>
        <button className="link-button login-switch" onClick={() => setMode(mode === "login" ? "register" : "login")}>
          {mode === "login" ? "没有账号？创建一个" : "已有账号？去登录"}
        </button>
      </div>
    </section>
  );
}
