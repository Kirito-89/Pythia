#!/bin/bash
# Step 4 distinguishability: receiver=core0(mcf,Pythia) in all runs; suspect=core1.
#   attacker  = lbm duty-cycling a PN bit stream at T=12k (bursty on/off)   x3 seeds
#   benign_lbm    = lbm steady full traffic (duty 100 constant)
#   benign_bwaves = bwaves steady full traffic
# Detector reads core1 per-epoch DRAM-req / LLC-MPKI / IPC time series.
set -u
SP=/tmp/claude-1000/-home-bloodydictator-Pythia/07a36b19-a805-415d-8134-cef38054d270/scratchpad/step4
BIN=/home/bloodydictator/Pythia-steer/bin/perceptron-no-multi-no-ship-2core
MCF=traces/429.mcf-184B.champsimtrace.xz
LBM=traces/470.lbm-1274B.champsimtrace.xz
BWV=traces/410.bwaves-1963B.champsimtrace.xz
cd /home/bloodydictator/Pythia-steer || exit 1
COMMON="--warmup_instructions=5000000 --l2c_prefetcher_types=scooby --config=config/pythia.ini \
  --steering_victim_core=0 --steering_attacker_core=1 --steering_epoch=10000"
JOBS=$SP/jobs.txt; : > $JOBS

for S in 1 2 3; do
  d=$SP/atk_s$S
  echo "$BIN $COMMON --simulation_instructions=15012000 \
    --steering_log=$d/intervals.csv --steering_schedule=$d/sched.csv \
    -traces $MCF $LBM > $d/sim.out 2>&1" >> $JOBS
done
# benign steady (core1 at full continuous traffic — no gating structure)
d=$SP/ben_lbm; mkdir -p $d
echo "$BIN $COMMON --simulation_instructions=15000000 \
  --steering_log=$d/intervals.csv --steering_schedule=$SP/sched_steady.csv \
  -traces $MCF $LBM > $d/sim.out 2>&1" >> $JOBS
d=$SP/ben_bwaves; mkdir -p $d
echo "$BIN $COMMON --simulation_instructions=15000000 \
  --steering_log=$d/intervals.csv --steering_schedule=$SP/sched_steady.csv \
  -traces $MCF $BWV > $d/sim.out 2>&1" >> $JOBS

echo "launching $(wc -l < $JOBS) jobs, pool=5  $(date)"
cat $JOBS | xargs -P 5 -I{} bash -c '{}'
echo "STEP4_RUNS_COMPLETE  $(date)"
