#!/bin/bash
#SBATCH --job-name=parquet-baseline
#SBATCH --nodes=2
#SBATCH --ntasks-per-node=1
#SBATCH --gpus-per-node=8
#SBATCH --cpus-per-task=40
#SBATCH --output=baseline-%j.out

set -euo pipefail
cd "$SLURM_SUBMIT_DIR"

export GPUS_PER_NODE="${GPUS_PER_NODE:-8}"
export PYTHON="$SLURM_SUBMIT_DIR/venv/bin/python"
export IMAGE="${IMAGE:-}"
export TRAIN_SCRIPT="$SLURM_SUBMIT_DIR/training/baseline_multinode.py"
export MASTER_ADDR="$(scontrol show hostnames "$SLURM_JOB_NODELIST" | head -n 1)"
export MASTER_PORT="${MASTER_PORT:-29500}"
export OMP_NUM_THREADS=1
export BATCH_SIZE="${BATCH_SIZE:-64}"
export NUM_WORKERS="${NUM_WORKERS:-4}"
export PREFETCH_FACTOR="${PREFETCH_FACTOR:-2}"
export WARMUP_STEPS="${WARMUP_STEPS:-5}"
export MEASURE_STEPS="${MEASURE_STEPS:-200}"

srun --ntasks="$SLURM_NNODES" --ntasks-per-node=1 \
  --gpus-per-task="$GPUS_PER_NODE" --cpu-bind=none --kill-on-bad-exit=1 \
  bash -c '
    set -euo pipefail
    export TZ=UTC
    GPU_LOG_DIR="$SLURM_SUBMIT_DIR/gpu_logs/$SLURM_JOB_ID"
    mkdir -p "$GPU_LOG_DIR"

    nvidia-smi \
      --query-gpu=timestamp,uuid,index,utilization.gpu,memory.used \
      --format=csv,nounits \
      --loop=1 \
      > "$GPU_LOG_DIR/$(hostname).csv" \
      2> "$GPU_LOG_DIR/$(hostname).err" &

    MONITOR_PID=$!

    cleanup() {
      kill "$MONITOR_PID" 2>/dev/null || true
      wait "$MONITOR_PID" 2>/dev/null || true
    }
    trap cleanup EXIT
    launcher=("$PYTHON" -m torch.distributed.run
      --nnodes="$SLURM_NNODES"
      --nproc-per-node="$GPUS_PER_NODE"
      --node-rank="$SLURM_PROCID"
      --master-addr="$MASTER_ADDR"
      --master-port="$MASTER_PORT"
      "$TRAIN_SCRIPT")
    if [[ -n "$IMAGE" ]]; then
      apptainer exec --nv --bind /home "$IMAGE" "${launcher[@]}" 
    else
      "${launcher[@]}"
    fi
  '