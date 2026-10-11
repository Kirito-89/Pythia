# Steering experiment: measured result

The working result is a **specific policy shift** on `603.bwaves_s`, not a
general claim that every victim slows because of reward poisoning. The
`470.lbm` control shows why these claims must be separated.

## Setup and reproducibility

- Two-core ChampSim, victim/Pythia on core 0, application trace on core 1;
  attacker L2 prefetching disabled.
- High-bandwidth reward threshold 2; all conditions use the same victim,
  configuration, simulator seed, 100,000-instruction warmup and matched attack
  trace/quiet-control prefix. The runner verifies identical victim Q fingerprints
  at the end of warmup for every attack/quiet pair.
- One 128 MiB, eight-stream sequential-read pulse of 8,192 batches after
  250,000 quiet rounds; 5,000,000 cooldown rounds. The victim is measured for
  10,000,000 instructions. Trace replay is an error, and each run ends at the
  victim's measurement boundary.
- Phase comparisons use the same global-cycle windows. `late_post` begins
  100,000 cycles after pressure ends and excludes intervals with attacker DRAM
  enqueues.

Run the full set in a new directory:

```bash
export PIN_ROOT=/home/dheeraj-agarwal/Downloads/pin-external-4.4-99977-g54fbb8814-gcc-linux
python3 attacks/run_steering_experiment.py \
  --output runs/steering-replication --modes stream --on 8192 \
  --instructions 10000000 --reward-ablation
python3 attacks/test_steering_simulator.py
```

The completed local result, including exact commands, hashes, raw CSVs,
resolved simulator output, and plots, is in
`runs/steering-reward-causal-20261004/`. A second workload control is in
`runs/steering-lbm-20261004/`. The short, 1,024-batch stream/random calibration
is in `runs/steering-full-20261004-d/`.

## Victim policy and performance

Each number below is attack minus quiet for the same control and phase. A
positive slowdown means the victim's IPC fell. Percentage-point changes refer
to the fraction of victim decisions choosing action 0 (no prefetch).

| `603.bwaves_s`, 8,192-batch pulse | Pressure slowdown | Late-cooldown slowdown | No-prefetch change in pressure | No-prefetch change in late cooldown |
| --- | ---: | ---: | ---: | ---: |
| Learning enabled | +0.67% | +0.32% | +4.00 points | +5.20 points |
| Victim Q updates frozen after warmup | +0.86% | +0.56% | ~0 | ~0 |
| High-bandwidth reward disabled after warmup | +0.77% | +0.11% | ~0 | ~0 |

In the ordinary learning run, high-bandwidth sample share rose by 45.67
percentage points during pressure. The victim's issued prefetches fell by
0.093 per thousand retired instructions during pressure and 0.165 in late
cooldown relative to the matched quiet run. Pre-attack action and IPC metrics
match closely. The Q fingerprint remains constant and Q-update count is zero
in frozen runs, while decisions and reward tracking continue. In the reward
control, Q values were initialized through the *same warmup* before the reward
rule was disabled; the no-prefetch change disappears even though other
bandwidth-aware degree/fallback paths remain active.

The victim slows with learning frozen, including in cooldown. Thus the IPC
slowdown is not established as a consequence of the learned policy change.
Residual cache state and in-flight memory requests can affect early cooldown;
late cooldown excludes observed attacker DRAM enqueues but does not reset
caches.

For `470.lbm`, the 1,024-batch pulse caused about 29.55% victim slowdown during
pressure with learning enabled and 29.94% with learning frozen, while
no-prefetch action selection stayed at zero. The victim recovered and ran
faster than the quiet control afterward. This is a contention result, not an
observed policy-steering result for that victim.

Two-pulse `470.lbm` schedules were also measured to test requested slowdown
and above-baseline speedup in separate intervals. With 500,000 quiet rounds
between pulses, the first pressure interval slowed the victim 29.91% and its
settled quiet gap ran 3.25% faster than the cycle-matched quiet control. The
second pressure interval slowed it 30.18%, but its settled gap remained 15.42%
slower. Doubling the quiet gap to 1,000,000 rounds did not produce an
above-baseline speedup in either gap. Frozen-learning controls behaved similarly.
The detailed commands, per-pulse CSVs, and interpretation are in
[`INTERVAL_CONTROL.md`](INTERVAL_CONTROL.md). These results do not establish
on-demand speedup or a learned cause of the IPC changes.

On the same `603.bwaves_s` victim, a planned mixed-strength trace produced
opposite pressure-window effects within one run. A fresh recording, with
`speedup,slowdown` goals declared before tracing, measured victim IPC 0.60%
above its matched quiet control during the 1,024-batch first pulse and 1.76%
below quiet during the 8,192-batch second pulse. An earlier exploratory
recording showed the same signs (+0.48%, then -1.93%). Frozen learning showed
similar effects (+0.59%, then -1.52%) in the fresh run, so the IPC direction
change is not evidence that reward poisoning learned a better or worse policy.
The speedup is small, and no arbitrary-cycle control is demonstrated. See the
[`603.bwaves_s` interval report](../runs/steering-targeted-bwaves-20261005/report.md)
and the exact reproduction command in [`INTERVAL_CONTROL.md`](INTERVAL_CONTROL.md).

These runs use one deterministic victim trace and one recorded attacker trace
per condition. They demonstrate a controlled proof of concept on `603.bwaves_s`;
they do not establish statistical significance, workload-general policy
steering, or a learned cause for the victim's IPC slowdown. Additional victim
classes, seeds/recordings, and a saved-policy replay would strengthen those
claims.
