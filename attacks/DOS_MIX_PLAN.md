# Pythia DoS study — proposed 4-core mixes (for adviser review)

**Goal.** Show that a crafted attacker trace, running on *vanilla* Pythia with **no
per-core advantage**, degrades co-running victims' IPC more than any benign
memory hog does — i.e. the degradation is an *engineered* prefetcher-steering
attack, not generic DRAM contention.

All mixes are wired into `attacks/run_dos_weighted_speedup.py` (block comments
`Block B`, `Block S`). Runner is resumable and single-machine
(`results.json` is the cache), so simulations can be split across server nodes
but each individual 4-core run stays on one machine.

---

## 1. "No advantage" guarantees (fairness)

These make the result defensible; please confirm you agree:

1. **One config for all cores.** Every core (attacker included) runs the stock
   `config/pythia.ini` + `scooby` on L2. There is no per-core prefetcher,
   degree, or reward tuning. The attacker is just another trace.
2. **Pythia's bandwidth defense stays ON.** `scooby_enable_hbw_reward = true`
   and `scooby_high_bw_thresh = 2` are left at default. We deliberately do **not**
   switch the high-bandwidth reward off for the attacker — that would be the
   "advantage" to avoid. The attack has to beat Pythia *with* its bandwidth-aware
   throttling active.
3. **Self-sacrificing attacker.** The attacker gains nothing: its own IPC is the
   lowest in the mix and is *lower* under Pythia than under no-prefetcher
   (prior data). So it cannot be accused of exploiting Pythia for its own speed.
4. **Paired no-prefetcher control.** Every mix is run under `pythia` and `nopf`.
   The headline number is `degr% = 100*(1 - WS_pythia/WS_nopf)`: how much worse
   the system does *because Pythia is on*. That isolates Pythia as the vector.

---

## 2. The attack mechanism (sawtooth) and the burst knob

`prefetch_steer --mode sawtooth --burst N`: each stream reads **N sequential
cache lines** (which Pythia learns and ramps its dynamic degree on), then takes a
**random jump**, stranding the degree-4 look-ahead lines it just issued →
wasted DRAM bandwidth that victims needed.

Pythia ramps degree `1,2,4,4` gated by confidence thresholds `1,3,8`
(`scooby_dyn_degrees`, `scooby_last_pref_offset_conf_thresholds` in pythia.ini).
So the sequential run must be long enough (~8 lines) to reach top degree before
jumping. **Prediction: the degradation-vs-burst curve is ~flat/low at b1 (pure
random, Pythia can't train), climbs to a knee around b8–b16, then plateaus.**

---

## 3. Block B — burst-length sweep (TA request #1)

Victim fixed = **3× lbm** (most bandwidth-sensitive, most prefetch-friendly →
most for Pythia to waste; matches the existing A2–A5 series). Core 0 varies in
**only** the burst length, bracketed by two benign controls.

| mix label | core 0 (attacker slot) | cores 1–3 (victims) |
|---|---|---|
| `B_ctl_light` | exchange2 (benign, light) | lbm × 3 |
| `B_ctl_mcf`   | **mcf** (benign, heavy mem) | lbm × 3 |
| `B_sb1`  | sawtooth burst=1 (≈ pure random) | lbm × 3 |
| `B_sb2`  | sawtooth burst=2  | lbm × 3 |
| `B_sb4`  | sawtooth burst=4  | lbm × 3 |
| `B_sb8`  | sawtooth burst=8  | lbm × 3 |
| `B_sb16` | sawtooth burst=16 | lbm × 3 |
| `B_sb32` | sawtooth burst=32 | lbm × 3 |
| `B_sb64` | sawtooth burst=64 | lbm × 3 |

Deliverable: a **degradation-vs-burst plot** with the two benign controls as
horizontal reference lines. The winning burst feeds Block S.

**Note on apples-to-apples:** the sweep uses a fresh, fully-controlled trace
family (`attacker_saw_bN.trace.gz`) where *only* `--burst` changes. The older
`attacker_sawtooth_b{2,4,16}` traces were generated with different `--on` lengths
and are kept only for continuity with the earlier A-series.

---

## 4. Block S — specificity / false-positive matrix (TA request #2)

The core of the "we really got the degradation" argument. For each victim class,
run core 0 as **three conditions**, everything else identical:

- **light** = exchange2 (benign, near-zero memory traffic) — lower bound.
- **mcf**   = 605.mcf (benign but *heavy, irregular* memory hog) — **the
  false-positive control**: "does a real memory-intensive app degrade the victim
  just as much?"
- **atk**   = the attacker (currently `pollute_w`; swap to Block-B knee winner).

| victim class | light ctl | mcf ctl (FP) | attacker |
|---|---|---|---|
| lbm × 3 (BW streaming) | `S_lbm_light` | `S_lbm_mcf` | `S_lbm_atk` |
| bwaves × 3 (BW streaming) | `S_bwaves_light` | `S_bwaves_mcf` | `S_bwaves_atk` |
| libquantum × 3 (pure sequential) | `S_libq_light` | `S_libq_mcf` | `S_libq_atk` |
| mcf × 3 (latency / irregular) | `S_mcfvic_light` | `S_mcfvic_mcf` | `S_mcfvic_atk` |
| **gcc × 3 (compute-bound, NEGATIVE control)** | `S_gcc_light` | `S_gcc_mcf` | `S_gcc_atk` |

**The attack is proven when, per victim class:**
`degr(atk) > degr(mcf) ≥ degr(light)` — i.e. the crafted trace is strictly worse
than even the worst *natural* memory hog — **and** the attacker is
self-sacrificing (lowest own IPC). The **gcc** row must stay ≈0 across all three
columns, proving the DoS is *targeted* (hits prefetch-friendly/BW-bound victims),
not a blunt hog that slows everything.

---

## 4b. Why homogeneous victims — and Block H for heterogeneous

Blocks B and S use **3× the same victim on purpose**: they are controlled
microbenchmarks. Block B *must* hold the victim constant or the burst-length
effect is confounded; Block S's per-class rows let us state
`degr(atk) > degr(mcf) ≥ degr(light)` cleanly for each class, and they line up
with the existing 3×lbm A-series data.

Homogeneous is, however, unrealistic. **Block H** adds heterogeneous victims:

| mix label | core 0 | cores 1–3 (victims) | purpose |
|---|---|---|---|
| `H_stream_{light,mcf,atk}` | exchange2 / mcf / atk | bwaves + libquantum + GemsFDTD | three *different* prefetch-friendly streamers → attack isn't an artifact of one app ×3 |
| `H_sel_{light,mcf,atk}` | exchange2 / mcf / atk | bwaves + omnetpp + gcc | one victim per class (BW / irregular / compute) → **selectivity visible in one run** |

For `H_sel_atk`, the per-core IPC should show the attacker clobber **bwaves**,
dent **omnetpp**, and leave **gcc** nearly untouched — selectivity demonstrated
in a single realistic mix. **Report per-core victim IPC for Block H** (the
weighted-speedup aggregate hides which victim actually moved).

## 5. Suggested headline metric (for discussion)

The runner currently reports whole-mix weighted speedup, which includes the
attacker core in the sum. For a DoS claim the cleaner headline is
**victim-only aggregate slowdown**: mean of cores 1–3 IPC normalized to the
`*_light` baseline's victim IPCs. Recommend adding a victim-only column; the
raw per-core IPCs are already in `results.json`, so this is post-processing only.

---

## 6. Running on the server (single machine per run)

Generate the controlled burst-sweep traces once (needs `PIN_ROOT`):

```bash
for N in 1 2 4 8 16 32 64; do
  python3 attacks/generate_trace.py --output runs/saw_b$N -- \
    --mode sawtooth --burst $N --size-mib 256 --streams 8 --seed 1 \
    --pre 0 --pulses 1 --on 400000 --off 0 --post 0
  cp runs/saw_b$N/attacker.trace.gz dpc3_traces/attacker_saw_b$N.trace.gz
done
```

Then run the two blocks (resumable; 50M warmup + 200M measured per core):

```bash
# Block B (burst sweep)
python3 attacks/run_dos_weighted_speedup.py --output runs/dos-burst-sweep \
  --mixes B_ctl_light B_ctl_mcf B_sb1 B_sb2 B_sb4 B_sb8 B_sb16 B_sb32 B_sb64

# Block S (specificity / false-positive matrix)
python3 attacks/run_dos_weighted_speedup.py --output runs/dos-specificity \
  --mixes S_lbm_light S_lbm_mcf S_lbm_atk \
          S_bwaves_light S_bwaves_mcf S_bwaves_atk \
          S_libq_light S_libq_mcf S_libq_atk \
          S_mcfvic_light S_mcfvic_mcf S_mcfvic_atk \
          S_gcc_light S_gcc_mcf S_gcc_atk
```

**Cost:** Block B = 9 mixes, Block S = 15 mixes, ×2 configs = 48 mix-runs, plus
~a dozen cached single-core baselines. Split the `--mixes` list across nodes;
`results.json` dedupes so nothing is run twice.
