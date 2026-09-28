#!/usr/bin/env bash
# Process-local environment only: never rewrite user API settings or shell files.
set -euo pipefail
task_repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export GPU_ANALYTICS_PYTHON="${GPU_ANALYTICS_PYTHON:-${HOME}/miniforge3/envs/rapids-cudf/bin/python}"
export GPU_ANALYTICS_SCRIPT="${task_repo}/skills/cudf-analytics/scripts/gpu_analytics.py"
export CUDA_PATH="${CUDA_PATH:-/usr/local/cuda-13.0/targets/sbsa-linux}"
task_profile="${HOME}/gpu-analysis-release-6e287a2/acceptance/agent-profile.json"
if [[ -z "${GPU_ANALYSIS_HYBRID_PROFILE:-}" && -f "${task_profile}" ]]; then
    export GPU_ANALYSIS_HYBRID_PROFILE="${task_profile}"
fi
task_mode="${1:-agent}"
if [[ $# -gt 0 ]]; then shift; fi
case "${task_mode}" in
    agent) task_entry="agent/agent_main.py" ;;
    queue) task_entry="agent/batch_queue.py" ;;
    service) task_entry="agent/report_service.py" ;;
    *) printf 'Usage: bash scripts/run_gb10_core.sh [agent|queue|service] [arguments...]\n' >&2; exit 2 ;;
esac
if [[ ! -x "${GPU_ANALYTICS_PYTHON}" ]]; then
    printf 'RAPIDS Python not found. Set GPU_ANALYTICS_PYTHON to the installed interpreter.\n' >&2
    exit 1
fi
cd -- "${task_repo}"
exec "${GPU_ANALYTICS_PYTHON}" "${task_repo}/${task_entry}" "$@"
