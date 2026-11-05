#!/bin/bash
# Step 3 stealth effort sweep: fix sender "on", vary its duty (effort), measure channel
# separability vs the duty-0 baseline AND the attacker's injected-bandwidth footprint.
# Receiver=core0 (mcf, Pythia); sender=core1 (lbm). Same config as Step 1 (5M/15M/epoch1e4).
set -u
SP=/tmp/claude-1000/-home-bloodydictator-Pythia/07a36b19-a805-415d-8134-cef38054d270/scratchpad/step3
BIN=/home/bloodydictator/Pythia-steer/bin/perceptron-no-multi-no-ship-2core
MCF=traces/429.mcf-184B.champsimtrace.xz
LBM=traces/470.lbm-1274B.champsimtrace.xz
cd /home/bloodydictator/Pythia-steer || exit 1

DUTIES="100 60 40 25 15 10 6 3"
JOBS=$SP/jobs.txt; : > $JOBS
for D in $DUTIES; do
  d=$SP/d${D}; mkdir -p $d
  echo "$BIN --warmup_instructions=5000000 --simulation_instructions=15000000 \
    --l2c_prefetcher_types=scooby --config=config/pythia.ini \
    --steering_log=$d/intervals.csv --steering_schedule=$SP/sched_d${D}.csv \
    --steering_victim_core=0 --steering_attacker_core=1 --steering_epoch=10000 \
    -traces $MCF $LBM > $d/sim.out 2>&1" >> $JOBS
done
echo "launching $(wc -l < $JOBS) jobs, pool=6  $(date)"
cat $JOBS | xargs -P 6 -I{} bash -c '{}'
echo "STEP3_RUNS_COMPLETE  $(date)"
