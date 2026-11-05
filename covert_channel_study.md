# A Covert Channel Through Pythia's Shared Bandwidth Signal

A microarchitectural covert-channel study against **Pythia** (the RL/Scooby L2
prefetcher) in ChampSim. This document records the threat model, the experimental
harness, and the results, observations, and open questions for each step completed
so far.

**Status:** Steps 1–4 complete. Step 5 out of scope.

| Step | Question | Status |
|---|---|---|
| 1 | Does a covert channel exist at all? | ✅ Done — channel exists (AUC ≈ 0.995) |
| 2 | How many bits/sec can it carry, at what error rate? | ✅ Done — ~2.3 b/Mcycle peak |
| 3 | How little attacker effort does it need (stealth knee)? | ✅ Done — usable to 6% duty |
| 4 | Is the attacker distinguishable from benign co-runners? | ✅ Done — only by temporal structure, not mean |
| 5 | Side channel against an unwitting victim | ⛔ Out of scope |

Figures (private Claude artifacts):
- Step 1 existence: https://claude.ai/artifact/LnC1FYHsLBjEwSbFAEJ5RS
- Step 2 capacity/BER: https://claude.ai/artifact/HFjAkEaSkrqzoH7nurSeYt
- Step 3 stealth sweep: https://claude.ai/artifact/7xDrdP25EkUbETdEvYJGXp
- Step 4 distinguishability: https://claude.ai/artifact/Ahu9RPNSwPfFPBY3RY35bq

---

## 1. Threat model and mechanism

Two programs run on two cores of one simulated chip. They do **not** share a
prefetcher Q-table — each core has its own Pythia/Scooby learner. The only thing
they share is the uncore: the LLC, the DRAM controller, and in particular a
**global 4-level DRAM-bandwidth bucket**.

- The DRAM bandwidth bucket is recomputed every `measure_dram_bw_epoch = 256`
  cycles, quantized into 4 quartile levels, and **broadcast to every core's Scooby**
  (`src/main.cc`, `uncore.LLC.broadcast_bw`).
- When the shared bandwidth reads high, `is_high_bw()` (threshold
  `scooby_high_bw_thresh = 2`) flips each core's Scooby onto its *high-bandwidth
  reward table* (`*_hbw`). That changes how the victim's prefetcher behaves.

So the **sender** modulates its own DRAM traffic; this moves the global bandwidth
bucket and the LLC/DRAM contention; the **receiver** observes the effect on its own
execution. No shared memory, no shared learner, no explicit IPC — just the shared
bandwidth signal plus contention.

**Receiver observables (both logged, no code change needed):**
- **IPC** — per-epoch retired-instructions / cycles. The crude, universal timing signal.
- **action-0 rate** — how often the receiver's Pythia *chooses not to prefetch*
  (`zero_action / decisions`). This is the Pythia-specific "the learned policy
  actually reacted" observable, and it turns out to be the more sensitive one.

**Sender control (the "gate"):** on branch `origin/steering`, `suppress_memory()`
zeroes the attacker core's load/store operands at the trace-read level. A **duty**
knob sets the fraction of each 1000-cycle window the sender's memory ops pass
(`src/steering.cc`: `(uncore.cycle % 1000) >= duty*10` ⇒ transmit for `duty`% of the
window). `duty=100` is full traffic; `duty=0` is fully stripped (matched quiet
control). The schedule is **open-loop** — the sender follows a fixed CSV with no IPC
oracle — which is the correct model for a cooperating covert sender.

---

## 2. Harness and build

Everything is run from an isolated git worktree so the master tree stays clean:

```
git worktree add -b steer-exp /home/bloodydictator/Pythia-steer origin/steering
ln -sfn /home/bloodydictator/Pythia/traces /home/bloodydictator/Pythia-steer/traces
```

The stock build is broken out of the box (no `make`; `libbf` absent, used only by the
sandbox prefetcher). Fix: comment out `#include "sandbox.h"` and delete the `sandbox`
else-if branch in `prefetcher/l2c_prefetcher.cc`, then compile directly:

```
g++ -O3 -std=c++17 -D_DEFAULT_SOURCE -I./inc -w \
  $(find src branch replacement prefetcher -name '*.cc' ! -name 'sandbox.cc') \
  -o bin/perceptron-no-multi-no-ship-2core
```

`NUM_CPUS=2`, `DRAM_CHANNELS=1`, `cpu_freq = 4000` (4 GHz). Throughput ≈ 24k instr/s;
429.mcf is the slow core (~14 min for a 5M+15M run).

**Run recipe** (receiver = core 0 = mcf/Pythia; sender = core 1 = lbm, gated):

```
BIN --warmup_instructions=N --simulation_instructions=N \
    --l2c_prefetcher_types=scooby --config=config/pythia.ini \
    --steering_log=OUT/intervals.csv --steering_schedule=SCHED.csv \
    --steering_victim_core=0 --steering_attacker_core=1 --steering_epoch=10000 \
    -traces traces/429.mcf-184B.champsimtrace.xz traces/470.lbm-1274B.champsimtrace.xz
```

`intervals.csv` logs, per epoch and per core: `ipc`, `zero_action`/`decisions`
(action-0 rate), `bw_level_0..3` (bandwidth-bucket mix), `core_dram_requests`,
`retired_begin/end`, `cycle_begin/end`. All observables come straight from this log.

Separability throughout is reported as **AUC** (area under the ROC curve, via the
Mann–Whitney U statistic over per-epoch observables: 0.5 = indistinguishable,
1.0 = perfect) and **d′** (standardized mean difference).

---

## 3. Step 1 — Does the channel exist?

**Design.** Fix the sender to always-1 (`duty=100`) vs always-0 (`duty=0`), run each
for 5M warmup + 15M instructions, and ask whether the receiver's observable separates
the two classes. (**Class 1 = sender transmits / accesses memory; Class 0 = sender
silent.** Mnemonic: the bit value is how much the sender transmits.)

**Results (receiver = mcf):**

| Observable | Class 1 (on) | Class 0 (off) | AUC | d′ |
|---|---|---|---|---|
| Per-epoch IPC | 0.020 | 0.133 | 0.995 | 2.99 |
| action-0 rate | 0.64 | 0.06 | 0.996 | — |

A paired, instruction-binned comparison (same receiver instructions, on vs off, which
removes mcf's own program-phase confound) showed **73/73 bins slowed** under pressure.

**Observation.** The channel is real and strong: when the sender floods, the
receiver's Pythia is pushed into its high-bandwidth regime, its no-prefetch rate
jumps 10×, and its IPC collapses to ~15% of baseline. Both observables separate the
two classes almost perfectly.

---

## 4. Step 2 — Capacity and bit error rate

**Design.** Send real 256-bit sequences (16-bit `1010…` preamble for threshold
calibration + a pseudo-random payload), one symbol per period, `duty=100` for a 1 and
`duty=0` for a 0. Decode by binning the receiver's own IPC per symbol against a
preamble-calibrated threshold. Sweep the symbol period `T`; N = 3 seeds at the
reported points; 50M warmup. Capacity of the resulting binary symmetric channel is
`rate × (1 − H(BER))`, reported in bits/Mcycle and, at the stated 4 GHz, bits/sec.

**Results (receiver = mcf):**

| Symbol period T | BER (mean ± sd) | Capacity (bits/Mcycle) | ≈ @ 4 GHz |
|---:|---:|---:|---:|
| 25,000 instr | 0.017 ± 0.016 | 2.23 ± 0.51 | ~8.9 kbps |
| **12,000 (knee)** | 0.137 ± 0.003 | **2.32 ± 0.06** | ~9.3 kbps |
| 9,000 | 0.223 ± 0.031 | 1.75 ± 0.48 | ~7.0 kbps |
| 7,000 | 0.326 ± 0.023 | 0.86 ± 0.26 | ~3.4 kbps |
| 6,000 | 0.404 ± 0.014 | 0.30 ± 0.09 | ~1.2 kbps |

**Observations.**
- Peak usable capacity ≈ **2.3 bits/Mcycle (~9 kbps @ 4 GHz)** at T ≈ 12k, error-free
  down to T ≈ 25k, collapsing to noise by T ≈ 6k. This is a genuinely low-capacity
  channel — the honest property of the real, unmodified mechanism.
- **The knee is intersymbol interference, not sampling.** The bandwidth bucket is
  recomputed every 256 cycles — ~700 samples per symbol at the knee, far more than
  enough. The channel breaks when a symbol gets shorter than the receiver's settling
  time (queue fill + Pythia adaptation), so adjacent bits bleed. That settling time is
  the physical rate limit.
- **A smarter decoder roughly doubles it.** Short coarse transmissions with a fresh
  threshold were error-free down to T = 12k and reached ~5.9 b/Mcyc. The reported
  decoder uses one fixed preamble threshold that drifts across the long 256-bit
  payload as mcf's own phases move the baseline. So 2.3 b/Mcyc is a conservative floor
  set by decoder quality, not by the channel.

---

## 5. Contrasting victim — is it just mcf?

To check that the channel is not an artifact of one cherry-picked memory-bound
receiver, Step 1's existence test was repeated with a **compute-bound receiver
(403.gcc, IPC ≈ 1.4, ~70× faster than mcf)**:

| Receiver | IPC AUC | d′ | IPC gap |
|---|---|---|---|
| 429.mcf (memory-bound) | 0.995 | 2.99 | 84.8% |
| 403.gcc (compute-bound) | **0.526** | 0.14 | 4.9% |

**Observation.** Against gcc the channel is essentially **absent** — AUC ≈ chance. The
channel's strength tracks how memory-bound the receiver is. This cleanly bounds the
claim: the channel exists and is measurable on memory-bound victims, not universally.

---

## 6. Step 3 — Stealth effort sweep

**Design.** Fix the sender to always-1, but sweep its **duty** (effort — the fraction
of time it floods) from 100% down to 3%. At each level, measure separability against
the shared duty-0 baseline, and the sender's **footprint** = injected DRAM requests
per Mcycle. The *knee* is the lowest duty whose IPC channel stays above AUC 0.90.

**Results (receiver = mcf):**

| Duty | IPC AUC | action-0 AUC | Injected bw (req/Mcyc) | Footprint |
|---:|---:|---:|---:|---:|
| 100% | 0.995 | 0.996 | 17,603 | 1.00× |
| 60% | 0.993 | 0.996 | 17,049 | 0.97× |
| 40% | 0.989 | 0.996 | 16,243 | 0.92× |
| 25% | 0.970 | 0.994 | 12,781 | 0.73× |
| 15% | 0.965 | 0.994 | 12,945 | 0.74× |
| 10% | 0.960 | 0.995 | 12,363 | 0.70× |
| **6% (knee)** | **0.909** | **0.988** | 9,872 | 0.56× |
| 3% | 0.804 | 0.949 | 5,936 | 0.34× |

**Observations.**
- **The channel is cheap to run.** Raw-IPC separability stays above AUC 0.96 down to
  10% duty, with the knee at **6%**. The sender need not flood continuously.
- **Pythia's learned policy is the more sensitive antenna.** A receiver reading the
  action-0 rate keeps **AUC ≥ 0.99 down to 6% duty**, and still 0.95 at 3% — where raw
  IPC has already collapsed to 0.80. The prefetcher's adaptation carries the signal far
  below where the crude timing channel dies. This is the Pythia-specific result.
- **Honest caveat — footprint is sublinear in duty.** Injected bandwidth falls much
  slower than effort: at 60% duty the sender still emits 97% of full bandwidth, and even
  at the 6% knee it emits 56%. So the stealth win is strong on the *time-active* axis
  but modest on the *bandwidth* axis.
- **Consistency check:** the duty-100 point reproduces Step 1's on-run exactly (AUC
  0.995 / action-0 0.996).

---

## 7. Step 4 — Distinguishability

**Design.** A per-core detector watches the suspect core (core 1) and must decide
whether it is the covert sender or an honest memory-heavy program. The receiver (core
0 = mcf/Pythia) is identical in every run. On core 1:

- **attacker** — lbm duty-cycling a PN bit stream at T = 12k (the Step 2 operating
  point), so its traffic is bursty on/off; 3 seeds.
- **benign lbm** — lbm at steady full traffic (duty 100 constant).
- **benign bwaves** — bwaves at steady full traffic.

Each suspect time series is sliced into 120-epoch detection windows (27 attacker / 18
lbm / 10 bwaves). Per window we compute the mean DRAM-request rate (the naive "average
bandwidth" feature), the coefficient of variation of per-epoch bandwidth (burstiness),
and the idle-epoch fraction, then take the AUC of each feature separating attacker
windows from benign windows.

**Results:**

| Feature | Attacker | Benign lbm | Benign bwaves | Detector AUC (atk vs benign) |
|---|---:|---:|---:|---:|
| Mean bw (req/epoch) | 129 | 176 | 211 | 0.91 *(wrong direction)* |
| Burstiness (CV) | 0.77 | 0.14 | 0.14 | **1.00** |
| Idle-epoch fraction | 0.335 | 0.000 | 0.000 | **1.00** |

**Observations.**
- **Average bandwidth is a poor, backwards tell.** Duty-cycling leaves the attacker
  idle about half the time, so its *mean* bandwidth (129 req/epoch) is **lower** than
  either steady benign program (176, 211). A detector that flags heavy memory users
  therefore reads the attacker as a *light, innocent* tenant and misses it. And the
  mean is the attacker's to tune — denser coding or higher duty slides it anywhere
  inside the benign envelope.
- **Temporal structure is a perfect and unavoidable tell.** The attacker's on/off
  modulation gives it a burstiness ≈ 5–6× the benign programs' and genuine idle gaps
  (34% of epochs) that steady heavy workloads **never** exhibit (benign idle-fraction
  is exactly 0). Both structure features separate the classes perfectly (AUC 1.00).
  The burst signature *is* the channel; the attacker cannot remove it without ceasing
  to transmit.
- **Implication for defense.** Mean-rate bandwidth accounting is the wrong tool. A
  monitor should watch the *shape* of a core's traffic — variance, idle gaps,
  periodicity — not its average. This also bounds the attacker: evading a
  structure-aware detector means spreading transmission so thin that the channel is
  lost (Step 2's rate limit), so detectability and capacity trade off directly.

> Caveat: window counts are small, but the attacker and benign distributions do not
> overlap on the structure features, so the separation is not a sampling artifact. One
> receiver workload; a mean detector can of course be tuned for a different attacker
> transmit profile — the robust point is that *structure* separates regardless of mean.

---

## 8. Overall observations

1. **A real but low-capacity covert channel exists** through Pythia's shared
   bandwidth signal, with no shared learner and no shared memory — contention plus the
   broadcast bandwidth bucket are enough.
2. **The Pythia-specific observable (action-0 rate) consistently beats raw IPC** — it
   is as separable in Step 1 and far more robust at low sender effort in Step 3.
   A receiver aware of the prefetcher's policy extracts more than a naive timing
   receiver.
3. **The channel is workload-dependent.** It is strong on memory-bound receivers
   (mcf) and absent on compute-bound ones (gcc). Capacity and existence both track the
   receiver's memory-boundedness.
4. **Detectability is temporal, not volumetric.** The attacker is not hidden — but it
   is invisible to a detector that watches *average* load (which even flags it as a
   light user). It is caught perfectly by one that watches the *shape* of its traffic.
   A mean-rate defense is the wrong defense.

---

## 9. Limitations

- Single sender pattern per experiment; one receiver workload (mcf) for the headline
  numbers, with gcc as the contrast.
- The symbol clock is a shared instruction count (fine for a cooperating upper bound;
  a real channel needs a wall clock).
- bits/sec figures assume the stated 4 GHz; the simulator is frequency-agnostic.
- Step 3 is N = 1 per duty level (a sweep to locate the knee, not a confidence
  estimate); Step 2's reported points are N = 3; Step 4 uses 3 attacker seeds and 1 run
  per benign program, sliced into detection windows (small window counts, but
  non-overlapping class distributions on the structure features).
- No defense is actually implemented. A per-core bandwidth-accounting defense knob
  (`steering_per_core_bw_DEFENSE`) exists and is currently off; Step 4 argues such a
  *mean-rate* defense would not work anyway.

---

## 10. Future steps

**Structure-aware detector / defense.** Step 4 shows a mean-rate monitor fails and a
burstiness monitor succeeds. The natural next step is to implement a temporal detector
(variance / idle-gap / periodicity on a per-core traffic window) as an actual defense
and measure how far it drives the attacker down the capacity curve — and to sweep
`steering_per_core_bw_DEFENSE` to confirm a mean-rate defense does not close the
channel.

**Evasion vs capacity.** Quantify the trade-off Step 4 implies: how much must the
attacker spread/randomize its transmission to fall under a structure detector's
threshold, and what capacity (Step 2) survives that constraint.

**Drift-tracking decoder.** Realize the ~5.9 b/Mcyc that Step 2's coarse runs
indicated, by replacing the fixed preamble threshold with one that tracks the
receiver's baseline drift over a long payload.

**Step 5 — Side channel (out of scope).** Extending from a cooperating covert sender
to leaking from an *unwitting* victim is a stretch goal, blocked/hypothetical, and not
part of the current study.

---

## 11. Reproducing

Scripts, schedules, and summary outputs for each step are in this repo under
[experiments/covert_channel/](experiments/covert_channel/):

- **Step 1** — [step1/](experiments/covert_channel/step1/): `analyze.py`,
  `sched_on.csv` / `sched_off.csv`, `summary.json`, `paired_bins.csv`, `channel.html`.
- **Step 2** — [step2/](experiments/covert_channel/step2/): `gen_schedule.py`
  (symbol-clocked schedule + PN payload), `decode.py` (per-symbol IPC decode, BER,
  BSC capacity), `run_coarse.sh`, `run_reported.sh`, `aggregate.json`,
  `raw_decode.txt`, `capacity.html`.
- **Step 3** — [step3/](experiments/covert_channel/step3/): `sched_d{D}.csv`,
  `run_step3.sh` (8 duties, pooled, reuses the Step 1 duty-0 run as baseline),
  `analyze_step3.py` (per-duty AUC/d′ + injected-bandwidth footprint + knee),
  `sweep.csv`, `step3.json`, `stealth.html`.
- **Step 4** — [step4/](experiments/covert_channel/step4/): `run_step4.sh` (attacker
  3 seeds + benign lbm/bwaves, pooled), `sched_steady.csv`, `atk_s*/sched.csv`,
  `analyze_step4.py` (per-window detector features + AUC), `windows.csv`,
  `waveform.json`, `step4.json`, `detect.html`.

All runs use the libbf-free 2-core binary described in §2. The large raw per-run logs
(`intervals.csv`, `sim.out`) are not committed — regenerate them with the `run_*.sh`
scripts.

> **Note:** the `run_*.sh` and `analyze*.py` scripts hardcode absolute paths to the
> session scratchpad (`SP=/tmp/claude-.../scratchpad/...`) and to the worktree binary
> (`/home/bloodydictator/Pythia-steer/bin/...`). Edit the `SP`, `BIN`, and trace paths
> at the top of each script to point at wherever you place the outputs before re-running.
