#!/usr/bin/env python3
"""Upload a local backend task to the GPU server and run ProPainter."""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import uuid
from pathlib import Path


DEFAULT_REMOTE_ROOT = "/data1/model-plaza-video-worker"


def _run(command: list[str]) -> None:
    print("+ " + " ".join(command), flush=True)
    subprocess.run(command, check=True)


def _ssh_base(identity: str) -> list[str]:
    return [
        "ssh",
        "-i",
        str(Path(identity).expanduser()),
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "BatchMode=yes",
        "-o",
        "ServerAliveInterval=30",
    ]


def _scp_base(identity: str) -> list[str]:
    return [
        "scp",
        "-i",
        str(Path(identity).expanduser()),
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "BatchMode=yes",
    ]


def _remote_quote(value: str | Path) -> str:
    return shlex.quote(str(value))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get("MODEL_PLAZA_GPU_HOST", "ubuntu@32.196.46.122"))
    parser.add_argument("--identity", default=os.environ.get("MODEL_PLAZA_GPU_IDENTITY", "~/.ssh/moda-gpu-new-prod01.pem"))
    parser.add_argument("--remote-root", default=os.environ.get("MODEL_PLAZA_GPU_ROOT", DEFAULT_REMOTE_ROOT))
    args = parser.parse_args()

    input_path = Path(os.environ["MODEL_PLAZA_INPUT"]).expanduser().resolve()
    output_path = Path(os.environ["MODEL_PLAZA_OUTPUT"]).expanduser().resolve()
    regions_path = Path(os.environ["MODEL_PLAZA_REGIONS"]).expanduser().resolve()
    params_path = Path(os.environ.get("MODEL_PLAZA_PARAMS", regions_path)).expanduser().resolve()
    local_runner = Path(__file__).with_name("propainter_runner.py").resolve()

    job_id = uuid.uuid4().hex
    remote_root = args.remote_root.rstrip("/")
    remote_job = f"{remote_root}/work/jobs/{job_id}"
    remote_input = f"{remote_job}/input{input_path.suffix or '.mp4'}"
    remote_output = f"{remote_job}/output.mp4"
    remote_regions = f"{remote_job}/regions.json"
    remote_params = f"{remote_job}/params.json"
    remote_runner = f"{remote_root}/scripts/propainter_runner.py"

    ssh = _ssh_base(args.identity)
    scp = _scp_base(args.identity)

    _run(ssh + [args.host, f"mkdir -p {_remote_quote(remote_job)} {_remote_quote(remote_root + '/scripts')}"])
    _run(scp + [str(local_runner), f"{args.host}:{remote_runner}"])
    _run(scp + [str(input_path), f"{args.host}:{remote_input}"])
    _run(scp + [str(regions_path), f"{args.host}:{remote_regions}"])
    _run(scp + [str(params_path), f"{args.host}:{remote_params}"])

    remote_command = " ".join(
        [
            "source",
            _remote_quote(f"{remote_root}/scripts/env.sh"),
            "&&",
            _remote_quote("/data1/conda/miniconda3/envs/video-inpaint/bin/python"),
            _remote_quote(remote_runner),
            "--input",
            _remote_quote(remote_input),
            "--output",
            _remote_quote(remote_output),
            "--regions",
            _remote_quote(remote_regions),
            "--params",
            _remote_quote(remote_params),
            "--workdir",
            _remote_quote(f"{remote_job}/runner-work"),
        ]
    )
    _run(ssh + [args.host, remote_command])

    output_path.parent.mkdir(parents=True, exist_ok=True)
    _run(scp + [f"{args.host}:{remote_output}", str(output_path)])
    print(f"Downloaded ProPainter result to {output_path}", flush=True)


if __name__ == "__main__":
    main()
