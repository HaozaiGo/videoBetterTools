import { spawn } from "node:child_process";

const processes = [
  spawn("uv", ["--directory", "backend", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8010", "--reload", "--reload-dir", "app"], { stdio: "inherit" }),
  spawn("uv", ["--directory", "backend", "run", "python", "-m", "app.worker"], { stdio: "inherit" }),
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
