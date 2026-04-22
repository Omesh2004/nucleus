#!/bin/bash
# ──────────────────────────────────────────────────────────────
# Dynamic vLLM Model Selector
# Detects available GPU VRAM and selects the optimal model tier.
# ──────────────────────────────────────────────────────────────

set -e

# Query free VRAM in MiB from nvidia-smi
VRAM_FREE_MIB=$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits | head -n1 | tr -d ' ')
VRAM_TOTAL_MIB=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -n1 | tr -d ' ')

echo "═══════════════════════════════════════════════════════"
echo "  vLLM Dynamic Model Selector"
echo "  GPU VRAM: ${VRAM_FREE_MIB} MiB free / ${VRAM_TOTAL_MIB} MiB total"
echo "═══════════════════════════════════════════════════════"

# Tier thresholds (in MiB)
TIER_7B=6500    # 7B AWQ needs ~5GB, safe margin at 6.5GB free
TIER_3B=2800    # 3B AWQ needs ~2.2GB, safe margin at 2.8GB free

if [ "$VRAM_FREE_MIB" -ge "$TIER_7B" ]; then
    MODEL="Qwen/Qwen2.5-7B-Instruct-AWQ"
    GPU_UTIL=0.85
    MAX_LEN=4096
    EXTRA_FLAGS=""
    echo "  ▸ Selected: 7B model (high-VRAM tier)"
elif [ "$VRAM_FREE_MIB" -ge "$TIER_3B" ]; then
    MODEL="Qwen/Qwen2.5-3B-Instruct-AWQ"
    GPU_UTIL=0.75
    MAX_LEN=2048
    EXTRA_FLAGS="--enforce-eager"
    echo "  ▸ Selected: 3B model (mid-VRAM tier)"
else
    MODEL="Qwen/Qwen2.5-1.5B-Instruct-AWQ"
    GPU_UTIL=0.65
    MAX_LEN=1024
    EXTRA_FLAGS="--enforce-eager"
    echo "  ▸ Selected: 1.5B model (low-VRAM tier)"
fi

echo "  ▸ Model:    $MODEL"
echo "  ▸ GPU Util: $GPU_UTIL"
echo "  ▸ Max Len:  $MAX_LEN"
echo "═══════════════════════════════════════════════════════"

exec vllm serve \
    --model "$MODEL" \
    --quantization awq \
    --gpu-memory-utilization "$GPU_UTIL" \
    --max-model-len "$MAX_LEN" \
    $EXTRA_FLAGS
