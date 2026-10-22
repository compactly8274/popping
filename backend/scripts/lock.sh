#!/bin/sh
# Regenerate the pinned dependency files from pyproject.toml.
# torch and its CUDA-only dependencies are left out: the Dockerfile and CI
# install the CPU build of torch (TORCH_VERSION) from the PyTorch index.
set -eu
cd "$(dirname "$0")/.."
EXCLUDE="--no-emit-package torch --no-emit-package cuda-bindings --no-emit-package cuda-pathfinder --no-emit-package cuda-toolkit --no-emit-package nvidia-cublas --no-emit-package nvidia-cuda-cupti --no-emit-package nvidia-cuda-nvrtc --no-emit-package nvidia-cuda-runtime --no-emit-package nvidia-cudnn-cu13 --no-emit-package nvidia-cufft --no-emit-package nvidia-cufile --no-emit-package nvidia-curand --no-emit-package nvidia-cusolver --no-emit-package nvidia-cusparse --no-emit-package nvidia-cusparselt-cu13 --no-emit-package nvidia-nccl-cu13 --no-emit-package nvidia-nvjitlink --no-emit-package nvidia-nvshmem-cu13 --no-emit-package nvidia-nvtx --no-emit-package triton "
COMMON="--python-version 3.12 --python-platform x86_64-manylinux_2_28 --no-header $EXCLUDE"
uv pip compile pyproject.toml $COMMON -o requirements.lock
uv pip compile pyproject.toml --extra test $COMMON -o requirements-dev.lock
