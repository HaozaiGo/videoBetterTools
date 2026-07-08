import { spawn } from "node:child_process";
import { readFileSync } from "node:fs";

const frontendPort = process.env.FRONTEND_PORT ?? "5175";
const workerReplicas = Math.max(1, Number.parseInt(process.env.WORKER_REPLICAS ?? "6", 10) || 6);
const resultWorkerReplicas = Math.max(1, Number.parseInt(process.env.RESULT_WORKER_REPLICAS ?? "4", 10) || 4);

function readBackendDotEnv() {
  try {
    return Object.fromEntries(
      readFileSync(new URL("../backend/.env", import.meta.url), "utf8")
        .split(/\r?\n/)
        .map((line) => line.trim())
        .filter((line) => line && !line.startsWith("#") && line.includes("="))
        .map((line) => {
          const index = line.indexOf("=");
          return [line.slice(0, index), line.slice(index + 1)];
        }),
    );
  } catch {
    return {};
  }
}

const backendDotEnv = readBackendDotEnv();
const envValue = (name) => process.env[name] ?? backendDotEnv[name];

const backendEnv = {
  ...process.env,
  PROPAINTER_COMMAND: process.env.PROPAINTER_COMMAND ?? "python ../scripts/gpu/propainter_api_adapter.py",
  ENHANCE_COMMAND: process.env.ENHANCE_COMMAND ?? "python ../scripts/gpu/video_enhance_api_adapter.py",
  TRANSLATE_COMMAND: process.env.TRANSLATE_COMMAND ?? "python ../scripts/gpu/video_translate_api_adapter.py",
  MODEL_PLAZA_GPU_API_URL: envValue("MODEL_PLAZA_GPU_API_URL") ?? "https://piankexiu.uniphore-ai.com",
  MODEL_PLAZA_GPU_API_KEY: envValue("MODEL_PLAZA_GPU_API_KEY") ?? "",
  MODEL_PLAZA_GPU_API_TUNNEL: envValue("MODEL_PLAZA_GPU_API_TUNNEL") ?? "0",
  MODEL_PLAZA_WORKER_MODE: process.env.MODEL_PLAZA_WORKER_MODE ?? "simple",
  OBJC_DISABLE_INITIALIZE_FORK_SAFETY: process.env.OBJC_DISABLE_INITIALIZE_FORK_SAFETY ?? "YES",
};

const processes = [
  spawn("uv", ["--directory", "backend", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8010", "--reload", "--reload-dir", "app"], { stdio: "inherit", env: backendEnv }),
  ...Array.from({ length: workerReplicas }, () =>
    spawn("uv", ["--directory", "backend", "run", "python", "-m", "app.worker"], { stdio: "inherit", env: backendEnv })
  ),
  ...Array.from({ length: resultWorkerReplicas }, () =>
    spawn("uv", ["--directory", "backend", "run", "python", "-m", "app.worker"], {
      stdio: "inherit",
      env: { ...backendEnv, MODEL_PLAZA_WORKER_QUEUES: "model-plaza-results" },
    })
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
