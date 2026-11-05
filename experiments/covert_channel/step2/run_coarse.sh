#!/bin/bash
# Step 2 coarse sweep (single run per symbol period) + gcc contrasting-victim existence check.
# Parallel pool capped to keep the laptop usable. Writes per-run dirs; decodes at the end.
set -u
SP=/tmp/claude-1000/-home-bloodydictator-Pythia/07a36b19-a805-415d-8134-cef38054d270/scratchpad/step2
S1=/tmp/claude-1000/-home-bloodydictator-Pythia/07a36b19-a805-415d-8134-cef38054d270/scratchpad/step1
BIN=/home/bloodydictator/Pythia-steer/bin/perceptron-no-multi-no-ship-2core
MCF=traces/429.mcf-184B.champsimtrace.xz
LBM=traces/470.lbm-1274B.champsimtrace.xz
GCC=/home/bloodydictator/Downloads/403.gcc-16B.champsimtrace.xz
WARM=20000000
cd /home/bloodydictator/Pythia-steer || exit 1

PERIODS="200000 100000 50000 25000 12000 6000"
JOBS=$SP/jobs_coarse.txt; : > $JOBS

# --- symbol-clocked sweep (mcf receiver) ---
for T in $PERIODS; do
  d=$SP/sweep/T$T; mkdir -p $d
  M=$(python3 $SP/gen_schedule.py --T $T --payload 128 --preamble 16 --seed 1 --out $d)
  echo "$BIN --warmup_instructions=$WARM --simulation_instructions=$M \
    --l2c_prefetcher_types=scooby --config=config/pythia.ini \
    --steering_log=$d/intervals.csv --steering_schedule=$d/sched.csv \
    --steering_victim_core=0 --steering_attacker_core=1 --steering_epoch=5000 \
    -traces $MCF $LBM > $d/sim.out 2>&1" >> $JOBS
done

# --- contrasting-victim existence check (gcc receiver, always-on vs always-off) ---
for cls in on off; do
  d=$SP/gcc_$cls; mkdir -p $d
  echo "$BIN --warmup_instructions=$WARM --simulation_instructions=15000000 \
    --l2c_prefetcher_types=scooby --config=config/pythia.ini \
    --steering_log=$d/intervals.csv --steering_schedule=$S1/sched_$cls.csv \
    --steering_victim_core=0 --steering_attacker_core=1 --steering_epoch=10000 \
    -traces $GCC $LBM > $d/sim.out 2>&1" >> $JOBS
done

echo "launching $(wc -l < $JOBS) jobs, pool=6  $(date)"
cat $JOBS | xargs -P 6 -I{} bash -c '{}'
echo "all coarse jobs done  $(date)"

# --- decode sweep ---
echo "=== COARSE SWEEP RESULTS (mcf) ==="
for T in $PERIODS; do
  d=$SP/sweep/T$T
  python3 $SP/decode.py --run $d --truth $d/truth.csv --T $T --json $d/decode.json
done
echo "=== GCC EXISTENCE (receiver IPC mean, on vs off) ==="
for cls in on off; do
  awk -F, '$1==0 && ($4=="pressure"||$4=="off"){s+=$9;n++} END{if(n)printf "gcc %s: mean IPC %.5f over %d epochs\n","'$cls'",s/n,n}' $SP/gcc_$cls/intervals.csv
done
echo "COARSE_COMPLETE"
