#!/usr/bin/env bash
set -euo pipefail

OLD_DIR=${OLD_DIR:-/data1/model-plaza-video-worker}
DOCKER_DIR=${DOCKER_DIR:-/data1/model-plaza-video-worker-docker}
SHARED_DIR=${SHARED_DIR:-/data1/model-plaza-video-worker-shared}
CONDA_ROOT=${CONDA_ROOT:-/data1/conda}
CONDA_PYTHON=${CONDA_PYTHON:-/data1/conda/miniconda3/envs/video-inpaint/bin/python}
HOST_PORT=${HOST_PORT:-18081}
CONTAINER_PORT=${CONTAINER_PORT:-18080}

if [ ! -x "$CONDA_PYTHON" ]; then
  echo "Missing conda python: $CONDA_PYTHON" >&2
  exit 1
fi

mkdir -p \
  "$DOCKER_DIR/app" \
  "$DOCKER_DIR/secrets" \
  "$SHARED_DIR/work" \
  "$SHARED_DIR/logs" \
  "$SHARED_DIR/inputs" \
  "$SHARED_DIR/outputs" \
  "$SHARED_DIR/cache" \
  "$SHARED_DIR/home"

rsync -a --delete "$OLD_DIR/scripts/" "$DOCKER_DIR/app/scripts/"
rsync -a --delete "$OLD_DIR/repos/" "$SHARED_DIR/repos/"
rsync -a --delete "$OLD_DIR/models/" "$SHARED_DIR/models/"

cp "$OLD_DIR/.env.gpu-tos" "$DOCKER_DIR/secrets/gpu-tos.env"
chmod 600 "$DOCKER_DIR/secrets/gpu-tos.env"
cp "$OLD_DIR/README.md" "$DOCKER_DIR/app/README.md"

cat > "$DOCKER_DIR/docker-compose.yml" <<YAML
services:
  gpu-api:
    image: ubuntu:latest
    container_name: model-plaza-gpu-api-docker
    working_dir: /app
    restart: unless-stopped
    ports:
      - "${HOST_PORT}:${CONTAINER_PORT}"
    gpus: all
    environment:
      PYTHONUNBUFFERED: "1"
      NVIDIA_DRIVER_CAPABILITIES: "compute,utility"
      PATH: "/data1/conda/miniconda3/envs/video-inpaint/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
      HOME: "/shared/home"
      HF_HOME: "/shared/cache/huggingface"
      XDG_CACHE_HOME: "/shared/cache"
      MODEL_PLAZA_VIDEO_ROOT: "/app"
      MODEL_PLAZA_GPU_JOBS_ROOT: "/shared/work/api-jobs"
      MODEL_PLAZA_GPU_LOGS_ROOT: "/shared/logs"
      MODEL_PLAZA_PROPAINTER_RUNNER: "/app/scripts/propainter_runner.py"
      MODEL_PLAZA_ENHANCE_RUNNER: "/app/scripts/video_enhance_runner.py"
      MODEL_PLAZA_TRANSLATE_RUNNER: "/app/scripts/video_translate_runner.py"
      PROPAINTER_ROOT: "/app/repos/ProPainter"
      REALESRGAN_ROOT: "/app/repos/Real-ESRGAN"
      PROPAINTER_PYTHON: "$CONDA_PYTHON"
      REALESRGAN_PYTHON: "$CONDA_PYTHON"
      MODEL_PLAZA_GPU_DEVICE_IDS: "2,4,5,6,7"
      CUDA_VISIBLE_DEVICES: "2,4,5,6,7"
      MODEL_PLAZA_GPU_WORKERS_PER_DEVICE: "2"
      MODEL_PLAZA_GPU_MAX_WORKERS: "10"
      MODEL_PLAZA_GPU_UPLOAD_RESULTS: "0"
      MODEL_PLAZA_GPU_PREFLIGHT_ENABLED: "1"
      MODEL_PLAZA_GPU_STALL_TIMEOUT_SECONDS: "1800"
      MODEL_PLAZA_GPU_WATCHDOG_INTERVAL_SECONDS: "30"
      MODEL_PLAZA_GPU_RECOVER_STALE_PROCESSING_TIMEOUT_SECONDS: "1800"
      MODEL_PLAZA_GPU_CANCEL_GRACE_SECONDS: "8"
      MODEL_PLAZA_GPU_CLEANUP_ENABLED: "1"
      MODEL_PLAZA_GPU_CLEANUP_INTERVAL_SECONDS: "3600"
      MODEL_PLAZA_GPU_CLEANUP_SUCCESS_TTL_SECONDS: "86400"
      MODEL_PLAZA_GPU_CLEANUP_FAILED_TTL_SECONDS: "172800"
      MODEL_PLAZA_GPU_CLEANUP_RUNNER_WORK_TTL_SECONDS: "3600"
      MODEL_PLAZA_GPU_CLEANUP_DISK_HIGH_WATERMARK_PERCENT: "80"
      MODEL_PLAZA_GPU_CLEANUP_DISK_LOW_WATERMARK_PERCENT: "70"
      MODEL_PLAZA_TRANSLATE_API_URL: "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
      MODEL_PLAZA_TRANSLATE_MODEL: "qwen-mt-plus"
      MODEL_PLAZA_TRANSLATE_TIMEOUT: "90"
      MODEL_PLAZA_SUBTITLE_FONT_SCALE: "1.42"
      MODEL_PLAZA_SUBTITLE_BOTTOM_MARGIN_RATIO: "0.24"
    volumes:
      - ./app:/app
      - ./secrets/gpu-tos.env:/run/secrets/gpu-tos.env:ro
      - $SHARED_DIR:/shared
      - $SHARED_DIR/repos:/app/repos:ro
      - $SHARED_DIR/models:/app/models:ro
      - $CONDA_ROOT:/data1/conda:ro
    command:
      - bash
      - -lc
      - |
        set -e
        set -a
        [ -f /run/secrets/gpu-tos.env ] && . /run/secrets/gpu-tos.env
        set +a
        exec $CONDA_PYTHON -m uvicorn scripts.propainter_api_server:app --host 0.0.0.0 --port ${CONTAINER_PORT}
YAML

cat > "$DOCKER_DIR/README.md" <<MD
# model-plaza-video-worker Docker deployment

This is the new Dockerized GPU worker deployment.

- Legacy service: \`$OLD_DIR\` on port \`18080\`
- Docker deployment: \`$DOCKER_DIR\`
- Shared runtime data: \`$SHARED_DIR\`
- Test API port: \`$HOST_PORT\`

The compose service mounts the host \`video-inpaint\` conda environment and shared model repos. It does not modify the legacy service.

## Start

\`\`\`bash
cd $DOCKER_DIR
docker compose up -d
curl -H "X-API-Key: model-plaza-dev-gpu-key" http://127.0.0.1:$HOST_PORT/health
\`\`\`

## Sync code and weights from legacy service

\`\`\`bash
$DOCKER_DIR/sync-from-legacy.sh
\`\`\`
MD

cat > "$DOCKER_DIR/sync-from-legacy.sh" <<SH
#!/usr/bin/env bash
set -euo pipefail
OLD_DIR=$OLD_DIR
DOCKER_DIR=$DOCKER_DIR
SHARED_DIR=$SHARED_DIR
rsync -a --delete "\$OLD_DIR/scripts/" "\$DOCKER_DIR/app/scripts/"
rsync -a --delete "\$OLD_DIR/repos/" "\$SHARED_DIR/repos/"
rsync -a --delete "\$OLD_DIR/models/" "\$SHARED_DIR/models/"
cp "\$OLD_DIR/.env.gpu-tos" "\$DOCKER_DIR/secrets/gpu-tos.env"
chmod 600 "\$DOCKER_DIR/secrets/gpu-tos.env"
SH
chmod +x "$DOCKER_DIR/sync-from-legacy.sh"

cat > "$DOCKER_DIR/.docker-wrapper.env" <<ENV
OLD_DIR=$OLD_DIR
DOCKER_DIR=$DOCKER_DIR
SHARED_DIR=$SHARED_DIR
CONDA_ROOT=$CONDA_ROOT
CONDA_PYTHON=$CONDA_PYTHON
HOST_PORT=$HOST_PORT
CONTAINER_PORT=$CONTAINER_PORT
ENV

echo "Docker wrapper files:"
find "$DOCKER_DIR" -maxdepth 2 -type f | sort
echo
echo "Directory sizes:"
du -sh "$DOCKER_DIR" "$SHARED_DIR"
