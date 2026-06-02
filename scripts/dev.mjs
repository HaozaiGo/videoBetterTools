import { spawn } from "node:child_process";

const backendEnv = {
  ...process.env,
  PROPAINTER_COMMAND: process.env.PROPAINTER_COMMAND ?? "python ../scripts/gpu/propainter_api_adapter.py",
  MODEL_PLAZA_GPU_API_URL: process.env.MODEL_PLAZA_GPU_API_URL ?? "http://32.196.46.122:18080",
  MODEL_PLAZA_GPU_API_KEY: process.env.MODEL_PLAZA_GPU_API_KEY ?? "model-plaza-dev-gpu-key",
  MODEL_PLAZA_GPU_API_TUNNEL: process.env.MODEL_PLAZA_GPU_API_TUNNEL ?? "0",
};

const processes = [
  spawn("uv", ["--directory", "backend", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8010", "--reload", "--reload-dir", "app"], { stdio: "inherit", env: backendEnv }),
  spawn("uv", ["--directory", "backend", "run", "python", "-m", "app.worker"], { stdio: "inherit", env: backendEnv }),
  spawn("npx", ["vite", "--host", "0.0.0.0"], { stdio: "inherit" }),
];

function stop() {
  for (const child of processes) child.kill("SIGTERM");
}

process.on("SIGINT", () => {
  stop();
  process.exit(0);
});

process.on("SIGTERM", () => {
  stop();
  process.exit(0);
});

for (const child of processes) {
  child.on("exit", (code) => {
    if (code && code !== 0) {
      stop();
      process.exit(code);
    }
  });
}
