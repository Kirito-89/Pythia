#!/bin/bash
# Step 2 reported pass: N=3 seeds at periods bracketing the BER knee, 50M warmup, 256-bit payload.
set -u
SP=/tmp/claude-1000/-home-bloodydictator-Pythia/07a36b19-a805-415d-8134-cef38054d270/scratchpad/step2
BIN=/home/bloodydictator/Pythia-steer/bin/perceptron-no-multi-no-ship-2core
MCF=traces/429.mcf-184B.champsimtrace.xz
LBM=traces/470.lbm-1274B.champsimtrace.xz
WARM=50000000
cd /home/bloodydictator/Pythia-steer || exit 1

PERIODS="25000 12000 9000 7000 6000"
SEEDS="1 2 3"
JOBS=$SP/jobs_reported.txt; : > $JOBS
for T in $PERIODS; do
  for S in $SEEDS; do
    d=$SP/rep/T${T}_s${S}; mkdir -p $d
    M=$(python3 $SP/gen_schedule.py --T $T --payload 256 --preamble 16 --seed $S --out $d)
    echo "$BIN --warmup_instructions=$WARM --simulation_instructions=$M \
      --l2c_prefetcher_types=scooby --config=config/pythia.ini \
      --steering_log=$d/intervals.csv --steering_schedule=$d/sched.csv \
      --steering_victim_core=0 --steering_attacker_core=1 --steering_epoch=5000 \
      -traces $MCF $LBM > $d/sim.out 2>&1" >> $JOBS
  done
done
echo "launching $(wc -l < $JOBS) jobs, pool=6  $(date)"
cat $JOBS | xargs -P 6 -I{} bash -c '{}'
echo "all reported jobs done  $(date)"

# decode every run
for T in $PERIODS; do for S in $SEEDS; do
  d=$SP/rep/T${T}_s${S}
  python3 $SP/decode.py --run $d --truth $d/truth.csv --T $T --json $d/decode.json >> $SP/rep/raw_decode.txt
done; done
echo "REPORTED_COMPLETE"
