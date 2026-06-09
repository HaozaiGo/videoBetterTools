import { spawn } from "node:child_process";

const frontendPort = process.env.FRONTEND_PORT ?? "5175";
const workerReplicas = Math.max(1, Number.parseInt(process.env.WORKER_REPLICAS ?? "8", 10) || 8);

const backendEnv = {
  ...process.env,
  PROPAINTER_COMMAND: process.env.PROPAINTER_COMMAND ?? "python ../scripts/gpu/propainter_api_adapter.py",
  ENHANCE_COMMAND: process.env.ENHANCE_COMMAND ?? "python ../scripts/gpu/video_enhance_api_adapter.py",
  TRANSLATE_COMMAND: process.env.TRANSLATE_COMMAND ?? "python ../scripts/gpu/video_translate_api_adapter.py",
  MODEL_PLAZA_GPU_API_URL: process.env.MODEL_PLAZA_GPU_API_URL ?? "http://32.196.46.122:18080",
  MODEL_PLAZA_GPU_API_KEY: process.env.MODEL_PLAZA_GPU_API_KEY ?? "model-plaza-dev-gpu-key",
  MODEL_PLAZA_GPU_API_TUNNEL: process.env.MODEL_PLAZA_GPU_API_TUNNEL ?? "0",
  MODEL_PLAZA_WORKER_MODE: process.env.MODEL_PLAZA_WORKER_MODE ?? "simple",
};

const processes = [
  spawn("uv", ["--directory", "backend", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8010", "--reload", "--reload-dir", "app"], { stdio: "inherit", env: backendEnv }),
  ...Array.from({ length: workerReplicas }, () =>
    spawn("uv", ["--directory", "backend", "run", "python", "-m", "app.worker"], { stdio: "inherit", env: backendEnv })
  ),
  spawn("npx", ["vite", "--host", "0.0.0.0", "--port", frontendPort], { stdio: "inherit" }),
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
