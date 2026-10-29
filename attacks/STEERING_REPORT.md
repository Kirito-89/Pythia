# Steering Phase Report: Adaptive Memory-Pressure Attacker vs Pythia

Date: 2026-10-19

## Summary

An adaptive memory-pressure attacker slowed five different victim workloads by 6.9% to 27.9% while it pushed, and the victims returned to within about 1% of normal speed when it relaxed (3.2% for soplex). Every result below is a 50M-instruction warmup plus 200M measured victim instructions on a two-core ChampSim with Pythia on the victim.

- **Best victim:** mcf, slowed 27.9% while pushed. The weakest was soplex at 6.9%.
- **Adaptive:** the attacker reacts to the victim. It pushes until the victim's IPC falls, relaxes while it recovers, and resumes, flipping between pushing and relaxing about 90 to 240 times per run.
- **What it is:** a recorded stream trace plus a simulator-side gate that decides how much of that traffic is allowed through. It is an upper-bound attacker, not an unprivileged application.
- **Not policy poisoning:** the frozen-learning victim is slowed as much as the learning victim, so this phase shows contention-based steering. Whether Pythia's learned policy is bent and stays bent is not shown here.
- **Open items from the proposal:** stealth, defenses, and policy persistence.

## 1. Goal and setup

The goal of the steering phase is an attacker that watches a victim and raises or lowers its performance by changing how much shared memory traffic it sends. The attacker's only physical channel is shared memory: it changes the pressure it creates and times its own loads. The oracle shortcut used for the results is described under the honesty label below.

**Why memory pressure matters for Pythia.** Pythia learns prefetch decisions online. Its reward includes a term that depends on how busy DRAM is. A second process that changes DRAM load therefore changes both the victim's raw speed (contention) and, in principle, the rewards Pythia learns from.

**Simulated machine.**

- Two-core ChampSim, binary `bin/perceptron-no-multi-no-ship-2core`, config `config/pythia.ini`.
- **Core 0 is the victim**, running a workload trace with the Pythia prefetcher (`scooby`) at L2 and online learning on.
- **Core 1 is the attacker**, replaying a recorded stream trace, with its own L2 prefetching disabled.
- Both cores share the last-level cache and DRAM. The simulator stops when core 0 finishes its measured instructions.
- Every run uses a 50M-instruction warmup and 200M measured victim instructions.

**Honesty label.** The attacker's controller lives inside the simulator, and in the version used for results it reads the victim's IPC to decide when to flip. That makes these results an upper bound for an attacker with the same goal, not a demonstration against an unprivileged application.

## 2. How the attacker trace is created

The attacker trace is one recorded sweep over a 32 MiB buffer, about 5.05M instructions long, that ChampSim replays in a loop. The same recipe is used for every victim.

**Recording.** `attacks/generate_trace.py` builds the attacker program (`attacks/prefetch_steer.cpp`) and an Intel Pin tracer, runs the program under Pin, and records only the marked region as standard 64-byte ChampSim records. Allocation, page initialization and printing are outside the trace. The recording is compressed to `attacker.trace.gz` (3.2 MB) and a `manifest.json` stores hashes, the exact command and a `complete_roi: true` check.

**The access pattern.** The command used by the steering runner is:

```bash
python3 attacks/generate_trace.py --max-instructions 8388608 -- \
  --mode stream --size-mib 32 --streams 8 --gap-rounds 0 --seed 1 \
  --pre 0 --on 65536 --off 0 --pulses 1 --post 0
```

- The 32 MiB buffer is split into **8 disjoint partitions** (`--streams 8`).
- Each **batch** reads the next 64-byte line from every partition, sequentially. That is 8 loads per batch.
- **65,536 batches** read all 524,288 lines of the buffer exactly once, so the trace is one full sweep with no quiet gap.
- Reads are independent, so the attacker core keeps many requests in flight and uses DRAM bandwidth heavily.

**Why one short trace.** An earlier design recorded a separate long trace with pulses and quiet gaps built in. That needed 300M or more records (about 19 GB raw) per recording and fixed the schedule in advance. The final design records one sweep and lets the simulator decide when the attacker may use memory, so the same file works for any warmup length, victim or schedule. ChampSim is told that replaying this trace is expected, so the attacker simply repeats the sweep.

**Quiet control.** Every attack run has a matching quiet run that replays the identical trace with its memory operations removed, except one load in 64. Attack and control therefore differ only in the attacker's memory traffic.

## 3. How the attacker works

A replayed trace cannot react, so the reacting happens in the simulator. The attacker has three parts: a gate that sets how much traffic gets through, a probe that measures memory latency, and a controller that decides when to push or relax.

**1. The gate.** For each attacker instruction read from the trace, the simulator decides whether its memory operations survive. With the gate closed, the load and store addresses are zeroed, so the instruction still retires but sends nothing to memory. The gate has one setting, **duty**, the percentage of each 1,000-cycle period during which memory operations are allowed. Duty 100 sends the full stream. Duty 0 sends nothing. This is how one fixed trace becomes variable-strength pressure.

**2. The probe.** Even with the gate closed, 1 in every 64 attacker memory operations is let through. The simulator times those loads when they miss the last-level cache. Slow replies mean DRAM is busy. This is the only thing a real co-running process could measure, and it adds only a few DRAM requests per 10,000 cycles.

**3. The controller.** It runs once every 50,000 cycles (an epoch) and follows these steps:

1. **Calibrate.** With the gate closed, average probe latency over 20 valid epochs. This gives the victim-alone latency (the baseline) and, for the cycle mode, the victim's idle IPC.
2. **Measure the victim.** Each epoch it reads the victim's IPC from the core's retired-instruction and cycle counters. This is the oracle part: a real attacker cannot see this.
3. **Choose push or relax** from that IPC (next section).
4. **Set the duty.** While pushing, a proportional rule moves duty toward a target rise in probe latency. The final experiments use a target (setpoint) of 1000%, which cannot be reached, so duty stays at 100% while pushing. While relaxed, duty is 0.

```
 Attacker trace --> Gate --------> Shared DRAM <-------- Victim (core 0)
 (one sweep,        (duty 0-100%,   (both cores'          (Pythia + workload;
  replayed)          strips memory   requests contend)     IPC falls when busy)
                     ops)                 |                     :
                       ^                  | reply latency       : victim IPC
                       | sets duty       v                     : (oracle shortcut,
                  Controller <------- Probe                     : not visible to a
                 (every 50,000        (1 in 64 loads kept,      : real attacker)
                  cycles: push         latency timed)           :
                  or relax) <...................................:
```

The solid arrows are what a real co-runner could do: send traffic and time its own replies. The dotted arrow is the shortcut used in these experiments.

All attacker state changes are written to a log (`intervals.csv.controller.csv`) with the duty, victim IPC and thresholds at each epoch, and each push or relax flip is written to `intervals.csv.phases.csv` with the cycle and the victim's retired-instruction count.

## 4. When it pushes and when it relaxes

The attacker pushes at full duty until the victim's IPC drops below a low line, relaxes until the IPC climbs back above a high line, and repeats. Both lines are fractions of the victim's idle IPC, which the attacker measures itself at the start.

**The rule (cycle mode).**

1. After calibration the attacker starts in **push** (duty 100%).
2. **Push to relax:** the victim's per-epoch IPC stays below **60% of idle IPC** for 2 epochs in a row. The attacker has slowed the victim enough, so it releases.
3. **Relax to push:** the victim's IPC stays above **90% of idle IPC** for 2 epochs in a row. The victim has recovered, so it pushes again.
4. **Stop for good:** when the victim passes the `--ctl-end` instruction count after warmup (199M for mcf, astar and omnetpp; 190M for lbm and soplex), the attacker closes the gate and stays closed.

The gap between the 60% and 90% lines is deliberate (hysteresis). It stops the attacker from flipping on every small wobble. The two-epoch confirmation does the same job against single noisy epochs.

**Thresholds each victim produced.** Idle IPC is the average over the 20 calibration epochs, before any pushing.

| Victim | Idle IPC | Relax below (60%) | Push above (90%) | Push/relax flips in the run |
| --- | ---: | ---: | ---: | ---: |
| lbm | 0.721 | 0.433 | 0.649 | 100 |
| soplex | 0.375 | 0.225 | 0.337 | 166 |
| mcf | 0.450 | 0.270 | 0.405 | 114 |
| astar | 0.219 | 0.131 | 0.197 | 89 |
| omnetpp | 0.343 | 0.206 | 0.309 | 240 |

**What a flip looks like.** In the mcf run the attacker started pushing at cycle 38,950,000, relaxed 100,000 cycles later, pushed again at 39,700,000, and relaxed at 39,850,000.

**One caveat about the trigger.** Because the thresholds are on the victim's raw IPC, some flips happen when the victim's own program phase dips below the low line, not because the attacker pushed it there. The slowdown numbers below are measured against a matched quiet run for exactly this reason.

## 5. Experiment design

Each victim is run four times, in two pairs, and slowdown is always the attack run compared with its own quiet twin over the same victim instructions.

| Run | Learning | Attacker |
| --- | --- | --- |
| learned, attack | Pythia updates its Q-values | adaptive attacker |
| learned, quiet | Pythia updates | same trace, gate closed (probe only) |
| frozen, attack | Q-values frozen after warmup | adaptive attacker |
| frozen, quiet | Q-values frozen | same trace, gate closed |

- **Matched quiet control.** The quiet run replays the attack run's push and relax boundaries by cycle, so the two runs differ only in whether the attacker's memory traffic was real. Before measurement, the victim's Q-table fingerprint is identical in all runs.
- **Learned versus frozen.** If freezing learning changes nothing, the effect is plain memory contention. A gap between them would point at Pythia's learning.
- **Metric: instruction-aligned slowdown.** For each push or relax stretch, compare the cycles the attack run needed against the cycles the quiet run needed for the *same* victim instructions. Positive means the victim was slower under attack. This cancels the victim's own program phases.
- **Why not the other comparisons.** Comparing matched cycles, or comparing push stretches with relax stretches inside one run, mixes the attacker's effect with where the victim happens to be in its program. Both gave misleading answers during this work (see section 8).

A **segment** is one continuous stretch in a single attacker state. The 89 to 240 segments per run in this report are the alternating push and relax stretches plus a final tail after the attacker stops.

## 6. Results across the five victims

mcf is slowed most (+27.9%) and soplex least (+6.9%) while the attacker pushes, and every victim returns to within about 1% of its quiet speed when the attacker relaxes, except soplex.

| Victim | Slowdown while pushing, learning on | Slowdown while pushing, learning frozen | Share of attack window spent pushing | Relaxed windows: slowdown vs quiet | Long pushes (1M+ instr.): range (count) |
| --- | ---: | ---: | ---: | ---: | ---: |
| **mcf** | **+27.9%** | +28.1% | 79% | +0.1% | +15.9% to +32.0% (28) |
| astar | +14.9% | +21.2% | 22% | -0.9% | +10.6% to +19.2% (20) |
| omnetpp | +13.5% | +14.4% | 50% | -0.9% | +9.0% to +22.0% (32) |
| lbm | +12.3% | +12.2% | 94% | -1.0% | +7.2% to +21.7% (48) |
| soplex | +6.9% | +7.4% | 84% | -3.2% | +1.5% to +15.6% (49) |

A negative number in the relaxed column means the victim ran faster than the quiet run. Astar's frozen figure sits well above its learned one only because the learned and frozen runs flipped at different moments, so they pushed over different stretches of the program.

**Timeline over the whole run.** In each panel the top graph is victim IPC with the adaptive attacker (orange) and with the quiet control (blue). Grey bands mark when the attacker was pushing, and the dotted line is where it stops for good. The bottom graph is the victim's speed relative to quiet in 5M-instruction steps, so values below zero mean the attacker slowed it. The curves keep the same shape in both runs because both execute the same victim program; the attacker moves the level, mostly in mcf, omnetpp and lbm.

![Timeline of astar, mcf, omnetpp and lbm under the adaptive attacker](timeline_all.png)

![Timeline of soplex under the adaptive attacker at full duty](timeline_soplex_full.png)

## 7. Victim by victim

All numbers are the learned run measured against its quiet twin, instruction-aligned, with the attacker at 100% duty while pushing.

**mcf: the strongest effect.** Pushed segments of at least 1M victim instructions (28 of them) averaged 26.4% slower and ranged from +15.9% to +32.0%. The attacker pushed for 79% of the victim's instructions and relaxed for 21%. While relaxed, the victim ran at the quiet baseline (+0.06%). The graph shows a clear down, up, down: around 125M to 135M the attacker relaxes and the speed gap closes to about 0% to +3%, then the attacker pushes again and the gap returns to 15% to 25%.

**omnetpp: the most balanced.** The attacker pushed for 98.9M instructions and relaxed for 99.8M, with 240 flips, the most of any victim. Long pushes (32 of them) averaged +13.5% from +9.0% to +22.0%. Relaxed stretches ran 0.9% faster than quiet, which is within noise.

**astar: pushed rarely, but hard when it did.** Its IPC is low (idle 0.219), so the attacker spent only 22% of the run pushing (43M of 199M instructions) and relaxed the rest. Its 20 long pushes averaged +14.7% from +10.6% to +19.2%. Learned (+14.9%) and frozen (+21.2%) differ here only because the two runs flipped at different points, so their push windows covered different parts of the program. That gap is not a learning effect.

**lbm: the steadiest.** A streaming, memory-bound workload. The attacker pushed 94% of the time and the victim was slowed 8% to 14% in nearly every 10M-instruction step. The one exception is 160M to 170M, where lbm's own IPC fell to about 0.39 and the attacker pushed only half the time, leaving the effect at 4%. When the attacker stopped at 190M the victim was back to normal speed immediately (+0.3%).

**soplex: the weakest, and the one that exposed a bug.** At full strength it slowed the victim 6.9%, with 49 long pushes averaging +7.2% from +1.5% to +15.6%. The attacker pushed 84% of the time and relaxed 16%. A first soplex run showed almost no effect because the controller had silently dropped duty to 2% (see section 8).

**bwaves: not re-run with the final attacker.** Earlier fixed-pulse experiments (`attacks/FINDINGS.md`) slowed it only 0.7% to 1.9%, and its no-prefetch rate stayed under 0.25%, so it is a weak performance victim. It was only part of the policy-shift investigation and is not part of the results table.

**Frozen versus learned.** In all five victims freezing Pythia's learning leaves the slowdown essentially unchanged (within 1 point, except astar for the reason above), so the effect measured here is memory contention.

## 8. Problems found and fixed

Several early conclusions were wrong or came from bugs. They are listed so the final numbers can be trusted.

| Problem | What went wrong | Fix |
| --- | --- | --- |
| Quiet run crashed | The tracker wrote two phase rows at the same cycle when tracking started, and the quiet run rejects duplicate cycle boundaries. | Enter the first phase directly, with no placeholder row. |
| Runner rejected `1.35` | The runner parsed the threshold as an integer. | Parse thresholds as numbers. |
| Absolute IPC thresholds | Values like 1.30 and 0.60 fit one program region only. On lbm at 3M warmup the victim could never climb back above 1.30. | Thresholds became fractions of the idle IPC measured at calibration (60% and 90%). |
| Wrong metric | Comparing pushed with relaxed stretches inside one run showed only a 2% gap on lbm, hiding a 12% effect, because the victim's own phases dominate. | Always compare attack against the matched quiet run over the same victim instructions. |
| Duty collapse on soplex | With a 50% latency-rise target, soplex's own memory phases already exceeded the target, so duty fell to 2% and the attacker was nearly silent. That run was wrongly read as "soplex is not steerable". | A target of 1000% keeps duty at 100% while pushing. The soplex conclusion was retracted and re-measured (+6.9%). |
| Unreachable no-prefetch trigger | bwaves and lbm almost never choose "no prefetch" (under 0.25% and about 2%), so thresholds like 30% never fired. | Use the IPC trigger. |
| Report step error | The report tool requires a cooldown phase in every run, and the quiet run finishes before it. | `attacks/summarize_steering.py` computes the same table without a cooldown. |

**Check to run every time:** read the mean duty while pushing in `intervals.csv.controller.csv` before trusting any "no effect" result.

## 9. Limits and what is still missing

**Limits of what was shown.**

- **Oracle trigger.** The push and relax decision reads the victim's IPC directly. A real co-runner cannot. The probe-only controller from earlier work was not evaluated with these final settings, so the results are an upper bound.
- **Simulator-side gate.** The traffic limiter lives in the simulator, not in an unprivileged application.
- **One run per condition.** There are no repeats, seeds or confidence intervals. Run-to-run noise looks like 1% to 2% for a learning victim, so differences smaller than that are not meaningful.
- **Contention, not policy.** Learned and frozen victims are slowed equally, so no change to Pythia's learned policy has been demonstrated.
- **The "up" half is a return to normal.** Relaxing brings the victim back to its quiet baseline. No victim ran meaningfully faster than quiet. Small negative numbers in relaxed windows are partly selection, since the attacker relaxes when the victim's IPC dips.
- **One attacker pattern.** Only the 8-stream sequential-read trace was used.
- **Do not cite the soplex post-attack speedup.** An earlier soplex run showed the learned victim 2.5% faster after the attacker stopped, but in that run the attacker had dropped to about 2% duty. An effect that size from a near-silent attacker points to noise of that size.

**Still missing from the project proposal.**

1. **Stealth:** the smallest attacker footprint that gives a chosen slowdown, and whether it can be told apart from a legitimate memory-heavy co-runner using performance counters.
2. **Defenses:** a per-core bandwidth attribution defense exists in the code but has not been evaluated, and neither has the cost of any defense on benign workloads.
3. **Policy steering and persistence:** inducing a chosen Pythia policy and showing it outlasts the attacker.
4. **Generality:** Micro-Armed Bandit and Athena as victims, and other attacker access patterns.
5. **Repeats:** multiple runs per condition with error bars.
6. **A threat-model write-up** mapping this setting onto the adversarial-RL literature.

## 10. How to reproduce

Run from the repository root. Each victim takes about 70 minutes (four simulations).

```bash
for t in 473.astar-359B 429.mcf-22B 471.omnetpp-188B; do
  python3 attacks/run_gated_experiment.py \
    --controller --track --track-cycle --skip-build --no-plots \
    --victim dpc3_traces/$t.champsimtrace.xz \
    --warmup 50000000 --instructions 200000000 \
    --ctl-end 199000000 \
    --cycle-hi 0.9 --cycle-lo 0.6 --cycle-setpoint 1000 \
    --track-gain 2 --duty-min 1 --track-confirm 2 \
    --output runs/steer-$t-20261019 || true
done
```

Each run ends with a "pressure and cooldown are both required" message. That is expected; the logs are already written. Then summarize and plot:

```bash
python3 attacks/summarize_steering.py runs/steer-429.mcf-22B-20261019 learned frozen
python3 attacks/plot_timeline.py attacks/timeline_all.png \
  "mcf=runs/steer-429.mcf-22B-20261019:199" \
  "lbm=runs/lbm-steer-20261018:190"
```

**Flags that matter.**

| Flag | Meaning |
| --- | --- |
| `--track --track-cycle` | Adaptive push and relax mode |
| `--cycle-hi 0.9 --cycle-lo 0.6` | Push above 90% of idle IPC, relax below 60% |
| `--cycle-setpoint 1000` | Full duty while pushing (a finite target can collapse duty) |
| `--track-confirm 2` | Epochs a threshold must hold before flipping |
| `--ctl-end N` | Victim instructions after warmup at which the attacker stops for good; must be below `--instructions` |

**Where things are.**

| Item | Location |
| --- | --- |
| Controller and gate | `src/steering.cc`, `src/ooo_cpu.cc` |
| Runner | `attacks/run_gated_experiment.py` |
| Per-phase summary | `attacks/summarize_steering.py` |
| Timeline graph | `attacks/plot_timeline.py` |
| Final runs | `runs/lbm-steer-20261018`, `runs/soplex-fullduty-20261018`, `runs/steer-*-20261019` |
| Controller log (duty, victim IPC, thresholds per epoch) | `<run>/stream-learned-attack/intervals.csv.controller.csv` |
| Push and relax flips | `<run>/stream-learned-attack/intervals.csv.phases.csv` |
