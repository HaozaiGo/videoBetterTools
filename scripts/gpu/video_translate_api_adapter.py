#!/usr/bin/env python3
"""Submit a video translation job to the GPU worker API."""

from __future__ import annotations

import os

from video_enhance_api_adapter import main


if __name__ == "__main__":
    os.environ.setdefault("MODEL_PLAZA_GPU_JOB_TYPE", "translate")
    os.environ.setdefault("MODEL_PLAZA_GPU_JOB_LABEL", "translate")
    main()
