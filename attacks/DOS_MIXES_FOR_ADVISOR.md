# Pythia DoS study — complete 4-core mix list (for adviser review)

**Author:** Adamya Verma · **Simulator:** Pythia (ChampSim-based), 4-core, shared 8 MB LLC
**Per run:** 50 M warm-up + 200 M measured instructions per core, every mix run twice
(with Pythia prefetcher `scooby` on L2, and with no L2 prefetcher).

---

## 1. What we are trying to show

A deliberately crafted **attacker** program, running on **core 0** of a 4-core chip under
the *stock, unmodified* Pythia prefetcher, degrades the IPC (instructions-per-cycle) of
the three co-running victim programs — and does so **more than any genuine
memory-intensive program would**. If we can show that, the slowdown is an *engineered*
attack that weaponises the prefetcher, not just ordinary memory-bandwidth contention.

### Fairness — the attacker gets no special treatment
1. All four cores run the **same** `config/pythia.ini`. There is no per-core tuning.
2. Pythia's own bandwidth-aware defence stays **ON** (`scooby_enable_hbw_reward = true`).
   We do **not** weaken Pythia to make the attack work — it must beat Pythia *with* its
   throttling active.
3. The attacker is **self-sacrificing**: it gains no speed for itself (its own IPC is the
   lowest in the mix and is *lower* with Pythia on than off).
4. Every mix is also run with **no prefetcher**, so we can report
   `degradation% = 100 × (1 − WS_pythia / WS_noprefetch)` — i.e. how much worse the system
   does *because Pythia is enabled*. That isolates the prefetcher as the attack vector.

### LLC consistency fix
The last-level cache is a fixed **8 MB** whether 1 or 4 cores run, so the single-core
"alone" baseline (the denominator of weighted speedup) sees the same cache as the 4-core
run. (Previously the 1-core build got only 2 MB, which biased the baseline.)

---

## 2. The attacker (core 0) — the "sawtooth"

The attacker is a single-threaded memory actuator recorded into a ChampSim trace. In
**sawtooth** mode each memory stream reads **N sequential cache lines** — which Pythia
learns and ramps its prefetch *degree* up on (1 → 2 → 4) — and then takes a **random
jump**. The jump strands the degree-4 look-ahead lines Pythia just fetched, so they are
pure **wasted DRAM bandwidth** that the victims needed. **N is the "burst length."**

- **Block B** (below) runs the attacker at **seven different burst lengths**
  (1, 2, 4, 8, 16, 32, 64) against one fixed victim, to find the burst that does the most
  damage — the "knee." Prediction: ~0 damage at burst 1 (pure random, Pythia can't train),
  rising to a knee around burst 8–16, then flat.
- The other blocks then use the **winning burst** as the attacker
  (currently set to **burst = sb16**; one line in the script changes it).

In every "false-positive" row below, core 0 is instead **605.mcf_s** — a real, benign but
heavily memory-intensive SPEC2017 program — with the three victims unchanged. If the
attacker degrades the victims *more* than mcf does, the attack is real.

---

## 3. Benchmark glossary (full names)

| short | full name / suite / class |
|---|---|
| `GemsFDTD` | 459.GemsFDTD — computational electromagnetics [SPEC2006, MEMORY/bandwidth] |
| `bfs` | bfs-14 — Breadth-First Search [GAP graph suite, MEMORY/irregular, very high MPKI] |
| `bwaves` | 603.bwaves_s-2931B — blast-wave hydrodynamics [SPEC2017, MEMORY/bandwidth, prefetch-friendly] |
| `bwaves17` | 603.bwaves_s-2931B — blast-wave hydrodynamics [SPEC2017, MEMORY/bandwidth, prefetch-friendly] |
| `cactu17` | 607.cactuBSSN_s-4004B — numerical relativity [SPEC2017, MEMORY/bandwidth] |
| `cc` | cc-14 — Connected Components [GAP graph suite, MEMORY/irregular, very high MPKI] |
| `exch` | 648.exchange2_s — recursive Sudoku solver [SPEC2017, COMPUTE, ~no memory traffic] |
| `exchange2` | 648.exchange2_s — recursive Sudoku solver [SPEC2017, COMPUTE, ~no memory traffic] |
| `foton17` | 649.fotonik3d_s-10881B — photonics FDTD [SPEC2017, MEMORY/bandwidth, prefetch-friendly] |
| `fp_mcf` | 605.mcf_s-1644B — combinatorial optimization [SPEC2017, MEMORY/irregular] (benign heavy-hog control) |
| `gcc` | 403.gcc — GNU C compiler [SPEC2006, COMPUTE, moderate memory] |
| `gcc17` | 602.gcc_s — GNU C compiler [SPEC2017, COMPUTE, moderate memory] |
| `lbm` | 470.lbm — Lattice-Boltzmann fluid flow [SPEC2006, MEMORY/bandwidth, prefetch-friendly] |
| `lbm17` | 619.lbm_s-4268B — Lattice-Boltzmann fluid flow [SPEC2017, MEMORY/bandwidth, prefetch-friendly] |
| `libquantum` | 462.libquantum — quantum-computing sim [SPEC2006, MEMORY/bandwidth, pure sequential] |
| `mcf1554` | 605.mcf_s-1554B — combinatorial optimization [SPEC2017, MEMORY/irregular, pointer-chasing, high MPKI] |
| `mcfv` | 605.mcf_s-1554B — combinatorial optimization [SPEC2017, MEMORY/irregular, pointer-chasing, high MPKI] |
| `omnet17` | 620.omnetpp_s-874B — discrete-event network sim [SPEC2017, MEMORY/irregular] |
| `omnetpp` | 620.omnetpp_s-141B — discrete-event network sim [SPEC2017, MEMORY/irregular] |
| `pr` | pr-14 — PageRank [GAP graph suite, MEMORY/irregular, very high MPKI] |
| `roms17` | 654.roms_s-1613B — regional ocean model [SPEC2017, MEMORY/bandwidth] |
| `sssp` | sssp-14 — Single-Source Shortest Paths [GAP graph suite, MEMORY/irregular, very high MPKI] |
| `wrf17` | 621.wrf_s-8065B — weather forecasting [SPEC2017, MEMORY/bandwidth] |
| `xalan17` | 623.xalancbmk_s-592B — XML-to-HTML XSLT transform [SPEC2017, MEMORY/irregular] |

Attacker traces: `attacker_saw_bN.trace.gz` (sawtooth, burst N) and
`attacker_pollute_w.trace.gz` (heavy-pollution sawtooth, 1 GB / 16 streams + writes).

---

## 4. Block B — burst-length sweep (find the best attacker)

Victim fixed = three copies of **619.lbm_s** (the most bandwidth-sensitive,
most prefetch-friendly victim). Core 0 varies **only** in burst length, bracketed by two
benign controls. Output: a degradation-vs-burst curve; the peak feeds the other blocks.

| mix name | core 0 | core 1 | core 2 | core 3 |
|---|---|---|---|---|
| `B_ctl_light` | 648.exchange2_s — recursive Sudoku solver [SPEC2017, COMPUTE, ~no memory traffic] | 470.lbm — Lattice-Boltzmann fluid flow [SPEC2006, MEMORY/bandwidth, prefetch-friendly] | 470.lbm — Lattice-Boltzmann fluid flow [SPEC2006, MEMORY/bandwidth, prefetch-friendly] | 470.lbm — Lattice-Boltzmann fluid flow [SPEC2006, MEMORY/bandwidth, prefetch-friendly] |
| `B_ctl_mcf` | 605.mcf_s-1554B — combinatorial optimization [SPEC2017, MEMORY/irregular, pointer-chasing, high MPKI] | 470.lbm — Lattice-Boltzmann fluid flow [SPEC2006, MEMORY/bandwidth, prefetch-friendly] | 470.lbm — Lattice-Boltzmann fluid flow [SPEC2006, MEMORY/bandwidth, prefetch-friendly] | 470.lbm — Lattice-Boltzmann fluid flow [SPEC2006, MEMORY/bandwidth, prefetch-friendly] |
| `B_sb1` | **ATTACKER** — sawtooth, burst=1 (attacker_saw_b1.trace.gz) | 470.lbm — Lattice-Boltzmann fluid flow [SPEC2006, MEMORY/bandwidth, prefetch-friendly] | 470.lbm — Lattice-Boltzmann fluid flow [SPEC2006, MEMORY/bandwidth, prefetch-friendly] | 470.lbm — Lattice-Boltzmann fluid flow [SPEC2006, MEMORY/bandwidth, prefetch-friendly] |
| `B_sb2` | **ATTACKER** — sawtooth, burst=2 (attacker_saw_b2.trace.gz) | 470.lbm — Lattice-Boltzmann fluid flow [SPEC2006, MEMORY/bandwidth, prefetch-friendly] | 470.lbm — Lattice-Boltzmann fluid flow [SPEC2006, MEMORY/bandwidth, prefetch-friendly] | 470.lbm — Lattice-Boltzmann fluid flow [SPEC2006, MEMORY/bandwidth, prefetch-friendly] |
| `B_sb4` | **ATTACKER** — sawtooth, burst=4 (attacker_saw_b4.trace.gz) | 470.lbm — Lattice-Boltzmann fluid flow [SPEC2006, MEMORY/bandwidth, prefetch-friendly] | 470.lbm — Lattice-Boltzmann fluid flow [SPEC2006, MEMORY/bandwidth, prefetch-friendly] | 470.lbm — Lattice-Boltzmann fluid flow [SPEC2006, MEMORY/bandwidth, prefetch-friendly] |
| `B_sb8` | **ATTACKER** — sawtooth, burst=8 (attacker_saw_b8.trace.gz) | 470.lbm — Lattice-Boltzmann fluid flow [SPEC2006, MEMORY/bandwidth, prefetch-friendly] | 470.lbm — Lattice-Boltzmann fluid flow [SPEC2006, MEMORY/bandwidth, prefetch-friendly] | 470.lbm — Lattice-Boltzmann fluid flow [SPEC2006, MEMORY/bandwidth, prefetch-friendly] |
| `B_sb16` | **ATTACKER** — sawtooth, burst=16 (attacker_saw_b16.trace.gz) | 470.lbm — Lattice-Boltzmann fluid flow [SPEC2006, MEMORY/bandwidth, prefetch-friendly] | 470.lbm — Lattice-Boltzmann fluid flow [SPEC2006, MEMORY/bandwidth, prefetch-friendly] | 470.lbm — Lattice-Boltzmann fluid flow [SPEC2006, MEMORY/bandwidth, prefetch-friendly] |
| `B_sb32` | **ATTACKER** — sawtooth, burst=32 (attacker_saw_b32.trace.gz) | 470.lbm — Lattice-Boltzmann fluid flow [SPEC2006, MEMORY/bandwidth, prefetch-friendly] | 470.lbm — Lattice-Boltzmann fluid flow [SPEC2006, MEMORY/bandwidth, prefetch-friendly] | 470.lbm — Lattice-Boltzmann fluid flow [SPEC2006, MEMORY/bandwidth, prefetch-friendly] |
| `B_sb64` | **ATTACKER** — sawtooth, burst=64 (attacker_saw_b64.trace.gz) | 470.lbm — Lattice-Boltzmann fluid flow [SPEC2006, MEMORY/bandwidth, prefetch-friendly] | 470.lbm — Lattice-Boltzmann fluid flow [SPEC2006, MEMORY/bandwidth, prefetch-friendly] | 470.lbm — Lattice-Boltzmann fluid flow [SPEC2006, MEMORY/bandwidth, prefetch-friendly] |

---

## 5. Block S — specificity / false-positive matrix

For each victim class, core 0 is run as **three** conditions — benign-light (exchange2),
benign-heavy (mcf, the false-positive control), and the attacker — with the three victims
held identical. **Proof:** `degradation(attacker) > degradation(mcf) ≥ degradation(light)`.
The **gcc** rows are a NEGATIVE control: a compute-bound victim should barely move,
proving the attack is targeted, not a blunt hog.

| mix name | core 0 | core 1 | core 2 | core 3 |
|---|---|---|---|---|
| `S_lbm_light` | 648.exchange2_s — recursive Sudoku solver [SPEC2017, COMPUTE, ~no memory traffic] | 470.lbm — Lattice-Boltzmann fluid flow [SPEC2006, MEMORY/bandwidth, prefetch-friendly] | 470.lbm — Lattice-Boltzmann fluid flow [SPEC2006, MEMORY/bandwidth, prefetch-friendly] | 470.lbm — Lattice-Boltzmann fluid flow [SPEC2006, MEMORY/bandwidth, prefetch-friendly] |
| `S_lbm_mcf` | 605.mcf_s-1554B — combinatorial optimization [SPEC2017, MEMORY/irregular, pointer-chasing, high MPKI] | 470.lbm — Lattice-Boltzmann fluid flow [SPEC2006, MEMORY/bandwidth, prefetch-friendly] | 470.lbm — Lattice-Boltzmann fluid flow [SPEC2006, MEMORY/bandwidth, prefetch-friendly] | 470.lbm — Lattice-Boltzmann fluid flow [SPEC2006, MEMORY/bandwidth, prefetch-friendly] |
| `S_lbm_atk` | **ATTACKER** — sawtooth heavy-pollution variant (attacker_pollute_w.trace.gz) | 470.lbm — Lattice-Boltzmann fluid flow [SPEC2006, MEMORY/bandwidth, prefetch-friendly] | 470.lbm — Lattice-Boltzmann fluid flow [SPEC2006, MEMORY/bandwidth, prefetch-friendly] | 470.lbm — Lattice-Boltzmann fluid flow [SPEC2006, MEMORY/bandwidth, prefetch-friendly] |
| `S_bwaves_light` | 648.exchange2_s — recursive Sudoku solver [SPEC2017, COMPUTE, ~no memory traffic] | 603.bwaves_s-2931B — blast-wave hydrodynamics [SPEC2017, MEMORY/bandwidth, prefetch-friendly] | 603.bwaves_s-2931B — blast-wave hydrodynamics [SPEC2017, MEMORY/bandwidth, prefetch-friendly] | 603.bwaves_s-2931B — blast-wave hydrodynamics [SPEC2017, MEMORY/bandwidth, prefetch-friendly] |
| `S_bwaves_mcf` | 605.mcf_s-1554B — combinatorial optimization [SPEC2017, MEMORY/irregular, pointer-chasing, high MPKI] | 603.bwaves_s-2931B — blast-wave hydrodynamics [SPEC2017, MEMORY/bandwidth, prefetch-friendly] | 603.bwaves_s-2931B — blast-wave hydrodynamics [SPEC2017, MEMORY/bandwidth, prefetch-friendly] | 603.bwaves_s-2931B — blast-wave hydrodynamics [SPEC2017, MEMORY/bandwidth, prefetch-friendly] |
| `S_bwaves_atk` | **ATTACKER** — sawtooth heavy-pollution variant (attacker_pollute_w.trace.gz) | 603.bwaves_s-2931B — blast-wave hydrodynamics [SPEC2017, MEMORY/bandwidth, prefetch-friendly] | 603.bwaves_s-2931B — blast-wave hydrodynamics [SPEC2017, MEMORY/bandwidth, prefetch-friendly] | 603.bwaves_s-2931B — blast-wave hydrodynamics [SPEC2017, MEMORY/bandwidth, prefetch-friendly] |
| `S_libq_light` | 648.exchange2_s — recursive Sudoku solver [SPEC2017, COMPUTE, ~no memory traffic] | 462.libquantum — quantum-computing sim [SPEC2006, MEMORY/bandwidth, pure sequential] | 462.libquantum — quantum-computing sim [SPEC2006, MEMORY/bandwidth, pure sequential] | 462.libquantum — quantum-computing sim [SPEC2006, MEMORY/bandwidth, pure sequential] |
| `S_libq_mcf` | 605.mcf_s-1554B — combinatorial optimization [SPEC2017, MEMORY/irregular, pointer-chasing, high MPKI] | 462.libquantum — quantum-computing sim [SPEC2006, MEMORY/bandwidth, pure sequential] | 462.libquantum — quantum-computing sim [SPEC2006, MEMORY/bandwidth, pure sequential] | 462.libquantum — quantum-computing sim [SPEC2006, MEMORY/bandwidth, pure sequential] |
| `S_libq_atk` | **ATTACKER** — sawtooth heavy-pollution variant (attacker_pollute_w.trace.gz) | 462.libquantum — quantum-computing sim [SPEC2006, MEMORY/bandwidth, pure sequential] | 462.libquantum — quantum-computing sim [SPEC2006, MEMORY/bandwidth, pure sequential] | 462.libquantum — quantum-computing sim [SPEC2006, MEMORY/bandwidth, pure sequential] |
| `S_mcfvic_light` | 648.exchange2_s — recursive Sudoku solver [SPEC2017, COMPUTE, ~no memory traffic] | 605.mcf_s-1554B — combinatorial optimization [SPEC2017, MEMORY/irregular, pointer-chasing, high MPKI] | 605.mcf_s-1554B — combinatorial optimization [SPEC2017, MEMORY/irregular, pointer-chasing, high MPKI] | 605.mcf_s-1554B — combinatorial optimization [SPEC2017, MEMORY/irregular, pointer-chasing, high MPKI] |
| `S_mcfvic_mcf` | 605.mcf_s-1554B — combinatorial optimization [SPEC2017, MEMORY/irregular, pointer-chasing, high MPKI] | 605.mcf_s-1554B — combinatorial optimization [SPEC2017, MEMORY/irregular, pointer-chasing, high MPKI] | 605.mcf_s-1554B — combinatorial optimization [SPEC2017, MEMORY/irregular, pointer-chasing, high MPKI] | 605.mcf_s-1554B — combinatorial optimization [SPEC2017, MEMORY/irregular, pointer-chasing, high MPKI] |
| `S_mcfvic_atk` | **ATTACKER** — sawtooth heavy-pollution variant (attacker_pollute_w.trace.gz) | 605.mcf_s-1554B — combinatorial optimization [SPEC2017, MEMORY/irregular, pointer-chasing, high MPKI] | 605.mcf_s-1554B — combinatorial optimization [SPEC2017, MEMORY/irregular, pointer-chasing, high MPKI] | 605.mcf_s-1554B — combinatorial optimization [SPEC2017, MEMORY/irregular, pointer-chasing, high MPKI] |
| `S_gcc_light` | 648.exchange2_s — recursive Sudoku solver [SPEC2017, COMPUTE, ~no memory traffic] | 403.gcc — GNU C compiler [SPEC2006, COMPUTE, moderate memory] | 403.gcc — GNU C compiler [SPEC2006, COMPUTE, moderate memory] | 403.gcc — GNU C compiler [SPEC2006, COMPUTE, moderate memory] |
| `S_gcc_mcf` | 605.mcf_s-1554B — combinatorial optimization [SPEC2017, MEMORY/irregular, pointer-chasing, high MPKI] | 403.gcc — GNU C compiler [SPEC2006, COMPUTE, moderate memory] | 403.gcc — GNU C compiler [SPEC2006, COMPUTE, moderate memory] | 403.gcc — GNU C compiler [SPEC2006, COMPUTE, moderate memory] |
| `S_gcc_atk` | **ATTACKER** — sawtooth heavy-pollution variant (attacker_pollute_w.trace.gz) | 403.gcc — GNU C compiler [SPEC2006, COMPUTE, moderate memory] | 403.gcc — GNU C compiler [SPEC2006, COMPUTE, moderate memory] | 403.gcc — GNU C compiler [SPEC2006, COMPUTE, moderate memory] |

---

## 6. Block H — heterogeneous victims (realism + per-core selectivity)

Mixed victim types in one run. `H_stream_*` uses three *different* streamers (so the
result is not an artefact of one program ×3); `H_sel_*` puts one victim of each class
together so a single run shows the attacker hit the bandwidth victim hard and leave the
compute victim (gcc) almost untouched. **Report per-core victim IPC for these.**

| mix name | core 0 | core 1 | core 2 | core 3 |
|---|---|---|---|---|
| `H_stream_light` | 648.exchange2_s — recursive Sudoku solver [SPEC2017, COMPUTE, ~no memory traffic] | 603.bwaves_s-2931B — blast-wave hydrodynamics [SPEC2017, MEMORY/bandwidth, prefetch-friendly] | 462.libquantum — quantum-computing sim [SPEC2006, MEMORY/bandwidth, pure sequential] | 459.GemsFDTD — computational electromagnetics [SPEC2006, MEMORY/bandwidth] |
| `H_stream_mcf` | 605.mcf_s-1554B — combinatorial optimization [SPEC2017, MEMORY/irregular, pointer-chasing, high MPKI] | 603.bwaves_s-2931B — blast-wave hydrodynamics [SPEC2017, MEMORY/bandwidth, prefetch-friendly] | 462.libquantum — quantum-computing sim [SPEC2006, MEMORY/bandwidth, pure sequential] | 459.GemsFDTD — computational electromagnetics [SPEC2006, MEMORY/bandwidth] |
| `H_stream_atk` | **ATTACKER** — sawtooth heavy-pollution variant (attacker_pollute_w.trace.gz) | 603.bwaves_s-2931B — blast-wave hydrodynamics [SPEC2017, MEMORY/bandwidth, prefetch-friendly] | 462.libquantum — quantum-computing sim [SPEC2006, MEMORY/bandwidth, pure sequential] | 459.GemsFDTD — computational electromagnetics [SPEC2006, MEMORY/bandwidth] |
| `H_sel_light` | 648.exchange2_s — recursive Sudoku solver [SPEC2017, COMPUTE, ~no memory traffic] | 603.bwaves_s-2931B — blast-wave hydrodynamics [SPEC2017, MEMORY/bandwidth, prefetch-friendly] | 620.omnetpp_s-141B — discrete-event network sim [SPEC2017, MEMORY/irregular] | 403.gcc — GNU C compiler [SPEC2006, COMPUTE, moderate memory] |
| `H_sel_mcf` | 605.mcf_s-1554B — combinatorial optimization [SPEC2017, MEMORY/irregular, pointer-chasing, high MPKI] | 603.bwaves_s-2931B — blast-wave hydrodynamics [SPEC2017, MEMORY/bandwidth, prefetch-friendly] | 620.omnetpp_s-141B — discrete-event network sim [SPEC2017, MEMORY/irregular] | 403.gcc — GNU C compiler [SPEC2006, COMPUTE, moderate memory] |
| `H_sel_atk` | **ATTACKER** — sawtooth heavy-pollution variant (attacker_pollute_w.trace.gz) | 603.bwaves_s-2931B — blast-wave hydrodynamics [SPEC2017, MEMORY/bandwidth, prefetch-friendly] | 620.omnetpp_s-141B — discrete-event network sim [SPEC2017, MEMORY/irregular] | 403.gcc — GNU C compiler [SPEC2006, COMPUTE, moderate memory] |

---

## 7. Grand study (G) — attacker vs memory-intensive false-positive, many workloads

18 victim combinations spanning bandwidth-streaming, irregular/graph, mixed, and
compute-bound classes. Each is run in two versions: `_atk` (core 0 = attacker) and
`_fp` (core 0 = benign heavy mcf). Everything but core 0 is identical within a pair.

| mix name | core 0 | core 1 | core 2 | core 3 |
|---|---|---|---|---|
| `G1_lbm3_atk` | **ATTACKER** — sawtooth, burst=16 (attacker_saw_b16.trace.gz) | 619.lbm_s-4268B — Lattice-Boltzmann fluid flow [SPEC2017, MEMORY/bandwidth, prefetch-friendly] | 619.lbm_s-4268B — Lattice-Boltzmann fluid flow [SPEC2017, MEMORY/bandwidth, prefetch-friendly] | 619.lbm_s-4268B — Lattice-Boltzmann fluid flow [SPEC2017, MEMORY/bandwidth, prefetch-friendly] |
| `G1_lbm3_fp` | 605.mcf_s-1644B — combinatorial optimization [SPEC2017, MEMORY/irregular] (benign heavy-hog control) | 619.lbm_s-4268B — Lattice-Boltzmann fluid flow [SPEC2017, MEMORY/bandwidth, prefetch-friendly] | 619.lbm_s-4268B — Lattice-Boltzmann fluid flow [SPEC2017, MEMORY/bandwidth, prefetch-friendly] | 619.lbm_s-4268B — Lattice-Boltzmann fluid flow [SPEC2017, MEMORY/bandwidth, prefetch-friendly] |
| `G2_bwaves3_atk` | **ATTACKER** — sawtooth, burst=16 (attacker_saw_b16.trace.gz) | 603.bwaves_s-2931B — blast-wave hydrodynamics [SPEC2017, MEMORY/bandwidth, prefetch-friendly] | 603.bwaves_s-2931B — blast-wave hydrodynamics [SPEC2017, MEMORY/bandwidth, prefetch-friendly] | 603.bwaves_s-2931B — blast-wave hydrodynamics [SPEC2017, MEMORY/bandwidth, prefetch-friendly] |
| `G2_bwaves3_fp` | 605.mcf_s-1644B — combinatorial optimization [SPEC2017, MEMORY/irregular] (benign heavy-hog control) | 603.bwaves_s-2931B — blast-wave hydrodynamics [SPEC2017, MEMORY/bandwidth, prefetch-friendly] | 603.bwaves_s-2931B — blast-wave hydrodynamics [SPEC2017, MEMORY/bandwidth, prefetch-friendly] | 603.bwaves_s-2931B — blast-wave hydrodynamics [SPEC2017, MEMORY/bandwidth, prefetch-friendly] |
| `G3_foton3_atk` | **ATTACKER** — sawtooth, burst=16 (attacker_saw_b16.trace.gz) | 649.fotonik3d_s-10881B — photonics FDTD [SPEC2017, MEMORY/bandwidth, prefetch-friendly] | 649.fotonik3d_s-10881B — photonics FDTD [SPEC2017, MEMORY/bandwidth, prefetch-friendly] | 649.fotonik3d_s-10881B — photonics FDTD [SPEC2017, MEMORY/bandwidth, prefetch-friendly] |
| `G3_foton3_fp` | 605.mcf_s-1644B — combinatorial optimization [SPEC2017, MEMORY/irregular] (benign heavy-hog control) | 649.fotonik3d_s-10881B — photonics FDTD [SPEC2017, MEMORY/bandwidth, prefetch-friendly] | 649.fotonik3d_s-10881B — photonics FDTD [SPEC2017, MEMORY/bandwidth, prefetch-friendly] | 649.fotonik3d_s-10881B — photonics FDTD [SPEC2017, MEMORY/bandwidth, prefetch-friendly] |
| `G4_stream3_atk` | **ATTACKER** — sawtooth, burst=16 (attacker_saw_b16.trace.gz) | 619.lbm_s-4268B — Lattice-Boltzmann fluid flow [SPEC2017, MEMORY/bandwidth, prefetch-friendly] | 603.bwaves_s-2931B — blast-wave hydrodynamics [SPEC2017, MEMORY/bandwidth, prefetch-friendly] | 649.fotonik3d_s-10881B — photonics FDTD [SPEC2017, MEMORY/bandwidth, prefetch-friendly] |
| `G4_stream3_fp` | 605.mcf_s-1644B — combinatorial optimization [SPEC2017, MEMORY/irregular] (benign heavy-hog control) | 619.lbm_s-4268B — Lattice-Boltzmann fluid flow [SPEC2017, MEMORY/bandwidth, prefetch-friendly] | 603.bwaves_s-2931B — blast-wave hydrodynamics [SPEC2017, MEMORY/bandwidth, prefetch-friendly] | 649.fotonik3d_s-10881B — photonics FDTD [SPEC2017, MEMORY/bandwidth, prefetch-friendly] |
| `G5_stream3b_atk` | **ATTACKER** — sawtooth, burst=16 (attacker_saw_b16.trace.gz) | 607.cactuBSSN_s-4004B — numerical relativity [SPEC2017, MEMORY/bandwidth] | 654.roms_s-1613B — regional ocean model [SPEC2017, MEMORY/bandwidth] | 621.wrf_s-8065B — weather forecasting [SPEC2017, MEMORY/bandwidth] |
| `G5_stream3b_fp` | 605.mcf_s-1644B — combinatorial optimization [SPEC2017, MEMORY/irregular] (benign heavy-hog control) | 607.cactuBSSN_s-4004B — numerical relativity [SPEC2017, MEMORY/bandwidth] | 654.roms_s-1613B — regional ocean model [SPEC2017, MEMORY/bandwidth] | 621.wrf_s-8065B — weather forecasting [SPEC2017, MEMORY/bandwidth] |
| `G6_mcf3_atk` | **ATTACKER** — sawtooth, burst=16 (attacker_saw_b16.trace.gz) | 605.mcf_s-1554B — combinatorial optimization [SPEC2017, MEMORY/irregular, pointer-chasing, high MPKI] | 605.mcf_s-1554B — combinatorial optimization [SPEC2017, MEMORY/irregular, pointer-chasing, high MPKI] | 605.mcf_s-1554B — combinatorial optimization [SPEC2017, MEMORY/irregular, pointer-chasing, high MPKI] |
| `G6_mcf3_fp` | 605.mcf_s-1644B — combinatorial optimization [SPEC2017, MEMORY/irregular] (benign heavy-hog control) | 605.mcf_s-1554B — combinatorial optimization [SPEC2017, MEMORY/irregular, pointer-chasing, high MPKI] | 605.mcf_s-1554B — combinatorial optimization [SPEC2017, MEMORY/irregular, pointer-chasing, high MPKI] | 605.mcf_s-1554B — combinatorial optimization [SPEC2017, MEMORY/irregular, pointer-chasing, high MPKI] |
| `G7_omnet3_atk` | **ATTACKER** — sawtooth, burst=16 (attacker_saw_b16.trace.gz) | 620.omnetpp_s-874B — discrete-event network sim [SPEC2017, MEMORY/irregular] | 620.omnetpp_s-874B — discrete-event network sim [SPEC2017, MEMORY/irregular] | 620.omnetpp_s-874B — discrete-event network sim [SPEC2017, MEMORY/irregular] |
| `G7_omnet3_fp` | 605.mcf_s-1644B — combinatorial optimization [SPEC2017, MEMORY/irregular] (benign heavy-hog control) | 620.omnetpp_s-874B — discrete-event network sim [SPEC2017, MEMORY/irregular] | 620.omnetpp_s-874B — discrete-event network sim [SPEC2017, MEMORY/irregular] | 620.omnetpp_s-874B — discrete-event network sim [SPEC2017, MEMORY/irregular] |
| `G8_graph3_atk` | **ATTACKER** — sawtooth, burst=16 (attacker_saw_b16.trace.gz) | bfs-14 — Breadth-First Search [GAP graph suite, MEMORY/irregular, very high MPKI] | cc-14 — Connected Components [GAP graph suite, MEMORY/irregular, very high MPKI] | pr-14 — PageRank [GAP graph suite, MEMORY/irregular, very high MPKI] |
| `G8_graph3_fp` | 605.mcf_s-1644B — combinatorial optimization [SPEC2017, MEMORY/irregular] (benign heavy-hog control) | bfs-14 — Breadth-First Search [GAP graph suite, MEMORY/irregular, very high MPKI] | cc-14 — Connected Components [GAP graph suite, MEMORY/irregular, very high MPKI] | pr-14 — PageRank [GAP graph suite, MEMORY/irregular, very high MPKI] |
| `G9_irr3_atk` | **ATTACKER** — sawtooth, burst=16 (attacker_saw_b16.trace.gz) | 605.mcf_s-1554B — combinatorial optimization [SPEC2017, MEMORY/irregular, pointer-chasing, high MPKI] | 620.omnetpp_s-874B — discrete-event network sim [SPEC2017, MEMORY/irregular] | bfs-14 — Breadth-First Search [GAP graph suite, MEMORY/irregular, very high MPKI] |
| `G9_irr3_fp` | 605.mcf_s-1644B — combinatorial optimization [SPEC2017, MEMORY/irregular] (benign heavy-hog control) | 605.mcf_s-1554B — combinatorial optimization [SPEC2017, MEMORY/irregular, pointer-chasing, high MPKI] | 620.omnetpp_s-874B — discrete-event network sim [SPEC2017, MEMORY/irregular] | bfs-14 — Breadth-First Search [GAP graph suite, MEMORY/irregular, very high MPKI] |
| `G10_irr3b_atk` | **ATTACKER** — sawtooth, burst=16 (attacker_saw_b16.trace.gz) | 623.xalancbmk_s-592B — XML-to-HTML XSLT transform [SPEC2017, MEMORY/irregular] | sssp-14 — Single-Source Shortest Paths [GAP graph suite, MEMORY/irregular, very high MPKI] | cc-14 — Connected Components [GAP graph suite, MEMORY/irregular, very high MPKI] |
| `G10_irr3b_fp` | 605.mcf_s-1644B — combinatorial optimization [SPEC2017, MEMORY/irregular] (benign heavy-hog control) | 623.xalancbmk_s-592B — XML-to-HTML XSLT transform [SPEC2017, MEMORY/irregular] | sssp-14 — Single-Source Shortest Paths [GAP graph suite, MEMORY/irregular, very high MPKI] | cc-14 — Connected Components [GAP graph suite, MEMORY/irregular, very high MPKI] |
| `G11_bic_atk` | **ATTACKER** — sawtooth, burst=16 (attacker_saw_b16.trace.gz) | 619.lbm_s-4268B — Lattice-Boltzmann fluid flow [SPEC2017, MEMORY/bandwidth, prefetch-friendly] | 605.mcf_s-1554B — combinatorial optimization [SPEC2017, MEMORY/irregular, pointer-chasing, high MPKI] | 602.gcc_s — GNU C compiler [SPEC2017, COMPUTE, moderate memory] |
| `G11_bic_fp` | 605.mcf_s-1644B — combinatorial optimization [SPEC2017, MEMORY/irregular] (benign heavy-hog control) | 619.lbm_s-4268B — Lattice-Boltzmann fluid flow [SPEC2017, MEMORY/bandwidth, prefetch-friendly] | 605.mcf_s-1554B — combinatorial optimization [SPEC2017, MEMORY/irregular, pointer-chasing, high MPKI] | 602.gcc_s — GNU C compiler [SPEC2017, COMPUTE, moderate memory] |
| `G12_bgc_atk` | **ATTACKER** — sawtooth, burst=16 (attacker_saw_b16.trace.gz) | 603.bwaves_s-2931B — blast-wave hydrodynamics [SPEC2017, MEMORY/bandwidth, prefetch-friendly] | bfs-14 — Breadth-First Search [GAP graph suite, MEMORY/irregular, very high MPKI] | 602.gcc_s — GNU C compiler [SPEC2017, COMPUTE, moderate memory] |
| `G12_bgc_fp` | 605.mcf_s-1644B — combinatorial optimization [SPEC2017, MEMORY/irregular] (benign heavy-hog control) | 603.bwaves_s-2931B — blast-wave hydrodynamics [SPEC2017, MEMORY/bandwidth, prefetch-friendly] | bfs-14 — Breadth-First Search [GAP graph suite, MEMORY/irregular, very high MPKI] | 602.gcc_s — GNU C compiler [SPEC2017, COMPUTE, moderate memory] |
| `G13_bbi_atk` | **ATTACKER** — sawtooth, burst=16 (attacker_saw_b16.trace.gz) | 619.lbm_s-4268B — Lattice-Boltzmann fluid flow [SPEC2017, MEMORY/bandwidth, prefetch-friendly] | 603.bwaves_s-2931B — blast-wave hydrodynamics [SPEC2017, MEMORY/bandwidth, prefetch-friendly] | 605.mcf_s-1554B — combinatorial optimization [SPEC2017, MEMORY/irregular, pointer-chasing, high MPKI] |
| `G13_bbi_fp` | 605.mcf_s-1644B — combinatorial optimization [SPEC2017, MEMORY/irregular] (benign heavy-hog control) | 619.lbm_s-4268B — Lattice-Boltzmann fluid flow [SPEC2017, MEMORY/bandwidth, prefetch-friendly] | 603.bwaves_s-2931B — blast-wave hydrodynamics [SPEC2017, MEMORY/bandwidth, prefetch-friendly] | 605.mcf_s-1554B — combinatorial optimization [SPEC2017, MEMORY/irregular, pointer-chasing, high MPKI] |
| `G14_fgx_atk` | **ATTACKER** — sawtooth, burst=16 (attacker_saw_b16.trace.gz) | 649.fotonik3d_s-10881B — photonics FDTD [SPEC2017, MEMORY/bandwidth, prefetch-friendly] | pr-14 — PageRank [GAP graph suite, MEMORY/irregular, very high MPKI] | 623.xalancbmk_s-592B — XML-to-HTML XSLT transform [SPEC2017, MEMORY/irregular] |
| `G14_fgx_fp` | 605.mcf_s-1644B — combinatorial optimization [SPEC2017, MEMORY/irregular] (benign heavy-hog control) | 649.fotonik3d_s-10881B — photonics FDTD [SPEC2017, MEMORY/bandwidth, prefetch-friendly] | pr-14 — PageRank [GAP graph suite, MEMORY/irregular, very high MPKI] | 623.xalancbmk_s-592B — XML-to-HTML XSLT transform [SPEC2017, MEMORY/irregular] |
| `G15_rox_atk` | **ATTACKER** — sawtooth, burst=16 (attacker_saw_b16.trace.gz) | 654.roms_s-1613B — regional ocean model [SPEC2017, MEMORY/bandwidth] | 620.omnetpp_s-874B — discrete-event network sim [SPEC2017, MEMORY/irregular] | 603.bwaves_s-2931B — blast-wave hydrodynamics [SPEC2017, MEMORY/bandwidth, prefetch-friendly] |
| `G15_rox_fp` | 605.mcf_s-1644B — combinatorial optimization [SPEC2017, MEMORY/irregular] (benign heavy-hog control) | 654.roms_s-1613B — regional ocean model [SPEC2017, MEMORY/bandwidth] | 620.omnetpp_s-874B — discrete-event network sim [SPEC2017, MEMORY/irregular] | 603.bwaves_s-2931B — blast-wave hydrodynamics [SPEC2017, MEMORY/bandwidth, prefetch-friendly] |
| `G16_gcc3_atk` | **ATTACKER** — sawtooth, burst=16 (attacker_saw_b16.trace.gz) | 602.gcc_s — GNU C compiler [SPEC2017, COMPUTE, moderate memory] | 602.gcc_s — GNU C compiler [SPEC2017, COMPUTE, moderate memory] | 602.gcc_s — GNU C compiler [SPEC2017, COMPUTE, moderate memory] |
| `G16_gcc3_fp` | 605.mcf_s-1644B — combinatorial optimization [SPEC2017, MEMORY/irregular] (benign heavy-hog control) | 602.gcc_s — GNU C compiler [SPEC2017, COMPUTE, moderate memory] | 602.gcc_s — GNU C compiler [SPEC2017, COMPUTE, moderate memory] | 602.gcc_s — GNU C compiler [SPEC2017, COMPUTE, moderate memory] |
| `G17_comp3_atk` | **ATTACKER** — sawtooth, burst=16 (attacker_saw_b16.trace.gz) | 602.gcc_s — GNU C compiler [SPEC2017, COMPUTE, moderate memory] | 648.exchange2_s — recursive Sudoku solver [SPEC2017, COMPUTE, ~no memory traffic] | 648.exchange2_s — recursive Sudoku solver [SPEC2017, COMPUTE, ~no memory traffic] |
| `G17_comp3_fp` | 605.mcf_s-1644B — combinatorial optimization [SPEC2017, MEMORY/irregular] (benign heavy-hog control) | 602.gcc_s — GNU C compiler [SPEC2017, COMPUTE, moderate memory] | 648.exchange2_s — recursive Sudoku solver [SPEC2017, COMPUTE, ~no memory traffic] | 648.exchange2_s — recursive Sudoku solver [SPEC2017, COMPUTE, ~no memory traffic] |
| `G18_cbg_atk` | **ATTACKER** — sawtooth, burst=16 (attacker_saw_b16.trace.gz) | 602.gcc_s — GNU C compiler [SPEC2017, COMPUTE, moderate memory] | 648.exchange2_s — recursive Sudoku solver [SPEC2017, COMPUTE, ~no memory traffic] | bfs-14 — Breadth-First Search [GAP graph suite, MEMORY/irregular, very high MPKI] |
| `G18_cbg_fp` | 605.mcf_s-1644B — combinatorial optimization [SPEC2017, MEMORY/irregular] (benign heavy-hog control) | 602.gcc_s — GNU C compiler [SPEC2017, COMPUTE, moderate memory] | 648.exchange2_s — recursive Sudoku solver [SPEC2017, COMPUTE, ~no memory traffic] | bfs-14 — Breadth-First Search [GAP graph suite, MEMORY/irregular, very high MPKI] |

---

## 8. Group BAL — deliberate compute + memory blends

Victim triples that mix compute-bound and memory-intensive programs in a set ratio
(2 memory + 1 compute, or 1 memory + 2 compute), the realistic in-between workloads.
In one run the per-core IPC should show the memory cores slow while the compute cores
hold steady. **Report per-core victim IPC.**

| mix name | core 0 | core 1 | core 2 | core 3 |
|---|---|---|---|---|
| `BAL1_mmc_atk` | **ATTACKER** — sawtooth, burst=16 (attacker_saw_b16.trace.gz) | 619.lbm_s-4268B — Lattice-Boltzmann fluid flow [SPEC2017, MEMORY/bandwidth, prefetch-friendly] | 603.bwaves_s-2931B — blast-wave hydrodynamics [SPEC2017, MEMORY/bandwidth, prefetch-friendly] | 602.gcc_s — GNU C compiler [SPEC2017, COMPUTE, moderate memory] |
| `BAL1_mmc_fp` | 605.mcf_s-1644B — combinatorial optimization [SPEC2017, MEMORY/irregular] (benign heavy-hog control) | 619.lbm_s-4268B — Lattice-Boltzmann fluid flow [SPEC2017, MEMORY/bandwidth, prefetch-friendly] | 603.bwaves_s-2931B — blast-wave hydrodynamics [SPEC2017, MEMORY/bandwidth, prefetch-friendly] | 602.gcc_s — GNU C compiler [SPEC2017, COMPUTE, moderate memory] |
| `BAL2_mmc_atk` | **ATTACKER** — sawtooth, burst=16 (attacker_saw_b16.trace.gz) | 605.mcf_s-1554B — combinatorial optimization [SPEC2017, MEMORY/irregular, pointer-chasing, high MPKI] | 654.roms_s-1613B — regional ocean model [SPEC2017, MEMORY/bandwidth] | 648.exchange2_s — recursive Sudoku solver [SPEC2017, COMPUTE, ~no memory traffic] |
| `BAL2_mmc_fp` | 605.mcf_s-1644B — combinatorial optimization [SPEC2017, MEMORY/irregular] (benign heavy-hog control) | 605.mcf_s-1554B — combinatorial optimization [SPEC2017, MEMORY/irregular, pointer-chasing, high MPKI] | 654.roms_s-1613B — regional ocean model [SPEC2017, MEMORY/bandwidth] | 648.exchange2_s — recursive Sudoku solver [SPEC2017, COMPUTE, ~no memory traffic] |
| `BAL3_mmc_atk` | **ATTACKER** — sawtooth, burst=16 (attacker_saw_b16.trace.gz) | bfs-14 — Breadth-First Search [GAP graph suite, MEMORY/irregular, very high MPKI] | 620.omnetpp_s-874B — discrete-event network sim [SPEC2017, MEMORY/irregular] | 602.gcc_s — GNU C compiler [SPEC2017, COMPUTE, moderate memory] |
| `BAL3_mmc_fp` | 605.mcf_s-1644B — combinatorial optimization [SPEC2017, MEMORY/irregular] (benign heavy-hog control) | bfs-14 — Breadth-First Search [GAP graph suite, MEMORY/irregular, very high MPKI] | 620.omnetpp_s-874B — discrete-event network sim [SPEC2017, MEMORY/irregular] | 602.gcc_s — GNU C compiler [SPEC2017, COMPUTE, moderate memory] |
| `BAL4_mcc_atk` | **ATTACKER** — sawtooth, burst=16 (attacker_saw_b16.trace.gz) | 649.fotonik3d_s-10881B — photonics FDTD [SPEC2017, MEMORY/bandwidth, prefetch-friendly] | 602.gcc_s — GNU C compiler [SPEC2017, COMPUTE, moderate memory] | 648.exchange2_s — recursive Sudoku solver [SPEC2017, COMPUTE, ~no memory traffic] |
| `BAL4_mcc_fp` | 605.mcf_s-1644B — combinatorial optimization [SPEC2017, MEMORY/irregular] (benign heavy-hog control) | 649.fotonik3d_s-10881B — photonics FDTD [SPEC2017, MEMORY/bandwidth, prefetch-friendly] | 602.gcc_s — GNU C compiler [SPEC2017, COMPUTE, moderate memory] | 648.exchange2_s — recursive Sudoku solver [SPEC2017, COMPUTE, ~no memory traffic] |
| `BAL5_mcc_atk` | **ATTACKER** — sawtooth, burst=16 (attacker_saw_b16.trace.gz) | 605.mcf_s-1554B — combinatorial optimization [SPEC2017, MEMORY/irregular, pointer-chasing, high MPKI] | 602.gcc_s — GNU C compiler [SPEC2017, COMPUTE, moderate memory] | 648.exchange2_s — recursive Sudoku solver [SPEC2017, COMPUTE, ~no memory traffic] |
| `BAL5_mcc_fp` | 605.mcf_s-1644B — combinatorial optimization [SPEC2017, MEMORY/irregular] (benign heavy-hog control) | 605.mcf_s-1554B — combinatorial optimization [SPEC2017, MEMORY/irregular, pointer-chasing, high MPKI] | 602.gcc_s — GNU C compiler [SPEC2017, COMPUTE, moderate memory] | 648.exchange2_s — recursive Sudoku solver [SPEC2017, COMPUTE, ~no memory traffic] |
| `BAL6_mcc_atk` | **ATTACKER** — sawtooth, burst=16 (attacker_saw_b16.trace.gz) | sssp-14 — Single-Source Shortest Paths [GAP graph suite, MEMORY/irregular, very high MPKI] | 602.gcc_s — GNU C compiler [SPEC2017, COMPUTE, moderate memory] | 648.exchange2_s — recursive Sudoku solver [SPEC2017, COMPUTE, ~no memory traffic] |
| `BAL6_mcc_fp` | 605.mcf_s-1644B — combinatorial optimization [SPEC2017, MEMORY/irregular] (benign heavy-hog control) | sssp-14 — Single-Source Shortest Paths [GAP graph suite, MEMORY/irregular, very high MPKI] | 602.gcc_s — GNU C compiler [SPEC2017, COMPUTE, moderate memory] | 648.exchange2_s — recursive Sudoku solver [SPEC2017, COMPUTE, ~no memory traffic] |

---

## 9. Totals & how it runs

- Block B: 9 mixes · Block S: 15 · Block H: 6 · Grand G: 36 · BAL: 12
  → **78 distinct 4-core mixes**, each run under Pythia and
  no-prefetcher (156 simulations) plus cached single-core baselines.
- Runs on one machine at a time; the runner caches finished results, so the list can be
  split across server nodes and resumed freely.
- Traces are SPEC CPU2017 (6xx) and GAP graph kernels, largest available checkpoints.
  Eight still need downloading on the server (cactuBSSN, roms, wrf, omnetpp-874B,
  xalancbmk, bfs, cc, pr, sssp); the rest are already in place.

*Please suggest additions or changes to the victim combinations, the burst-length set,
or the false-positive control.*
