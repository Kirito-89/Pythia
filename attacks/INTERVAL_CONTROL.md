# Scheduled slowdown and speedup experiment

The attacker application can record alternating memory-pressure pulses and quiet
gaps with `--pulses N --on BATCHES --off ROUNDS`. `--on-sequence 1024,8192`
lets each pulse have a different strength. These are attacker work units,
not victim instructions or simulator cycles. Pin phase markers and ChampSim's
`intervals.csv.phases.csv` show when each interval actually reached the victim.

The experiment runner now makes a cycle-matched quiet comparison **for every
pulse**. In `pulse_comparison.csv`, positive `slowdown_pct` means the attack run
had lower victim IPC than quiet. Negative values mean the attack run was faster
than quiet. `settled_off` starts 100,000 cycles into a quiet gap and includes
only windows with zero observed attacker DRAM enqueues. `interval_targets.csv`
checks both a pressure slowdown and an above-quiet speedup, each at least 1%
unless `--min-slowdown-pct` or `--min-speedup-pct` changes the threshold.
For pressure intervals meant to have different effects, set
`--pulse-goals speedup,slowdown` before tracing; the runner writes
`pressure_goal_targets.csv`.

## Same-victim 603.bwaves_s result

The requested speedup-then-slowdown sequence was observed on `603.bwaves_s` in
two separately recorded traces. The fresh run declared both pressure goals
before recording; its [report](../runs/steering-targeted-bwaves-20261005/report.md)
gives these matched-cycle measurements:

| Pressure pulse | Attacker batches | Requested effect | Measured IPC effect (learned) | Measured IPC effect (frozen) |
|---:|---:|---|---:|---:|
| 0 | 1,024 | Speedup | +0.60% above quiet | +0.59% above quiet |
| 1 | 8,192 | Slowdown | -1.76% below quiet | -1.52% below quiet |

The [exploratory run](../runs/steering-mixed-bwaves-20261005/report.md) had
the same signs: +0.48% speedup, then -1.93% slowdown with learning enabled.
The fresh run used a 0.4% speedup threshold and a 1.0% slowdown threshold;
both were chosen after the exploratory run, so this is calibrated replication,
not a blind discovery. The 0.60% speedup is small, and neither quiet gap was
faster than its own quiet control. Frozen learning preserved the opposite
pressure effects, so these results do not show that Pythia learning caused
either IPC change.

To make a fresh recording and simulation with the same planned sequence:

```bash
export PIN_ROOT=/home/dheeraj-agarwal/Downloads/pin-external-4.4-99977-g54fbb8814-gcc-linux
python3 attacks/run_steering_experiment.py \
  --output runs/steering-targeted-bwaves-replication \
  --victim dpc3_traces/603.bwaves_s-2931B.champsimtrace.xz \
  --modes stream --pulses 2 --on-sequence 1024,8192 \
  --pulse-goals speedup,slowdown \
  --min-speedup-pct 0.4 --min-slowdown-pct 1.0 \
  --off 250000 --pre 250000 --post 5000000 \
  --instructions 15000000 --warmup 100000
```

The measured pressure windows in the fresh learned run were about 0.99–1.46
million and 2.49–7.72 million global simulator cycles. Their positions are
observed outcomes of the trace, not exact-cycle commands.

## Quiet-gap calibration on 470.lbm

To reproduce the two-pulse 470.lbm experiment in a new output directory:

```bash
export PIN_ROOT=/home/dheeraj-agarwal/Downloads/pin-external-4.4-99977-g54fbb8814-gcc-linux
python3 attacks/run_steering_experiment.py \
  --output runs/steering-onoff-replication \
  --victim dpc3_traces/470.lbm-1274B.champsimtrace.xz \
  --modes stream --pulses 2 --on 1024 --off 500000 \
  --pre 250000 --post 5000000 --instructions 10000000
```

The local [two-pulse report](../runs/steering-onoff-lbm-20261005-approved/report.md)
records these learned-control results:

| Pulse | Pressure slowdown | Speedup in settled off interval | Both 1% targets met? |
|---:|---:|---:|---|
| 0 | 29.91% | 3.25% | Yes |
| 1 | 30.18% | -15.42% (a slowdown) | No |

The [longer-gap report](../runs/steering-onoff-lbm-longgap-20261005/report.md)
uses `--off 1000000 --instructions 15000000`. It slowed the victim in both
pressure intervals but neither settled gap exceeded the quiet baseline. The
post phase did exceed it by 7.22% in the learned pair. Frozen-learning controls
showed similar behavior in both experiments, so these IPC changes are not
attributable to Pythia learning from the data shown here.

The application schedules its own memory work ahead of time. A recorded trace
cannot observe the victim and switch at an arbitrary requested victim cycle,
and quieting the attacker cannot guarantee IPC above the quiet baseline.
In-flight requests and cache state persist after a phase transition. Use the
per-pulse matched measurements, not the application `on/off` counts, to decide
whether a requested performance interval was achieved. For exact simulator-cycle
gating or action-responsive control, a separate simulator-level controller is
required; this application is an open-loop actuator.
