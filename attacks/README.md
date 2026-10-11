# PrefetchSteer

`prefetch_steer.cpp` implements the application's open-loop steering stage from
the project plan: quiet pre-attack work, scheduled memory-pressure bursts, quiet
gaps, and a final cooldown. The original `attacker.cpp` remains the continuous
contention baseline.

The intended first experiment is **MIN-PF**: test whether raising shared bandwidth
changes the victim's reward observations and later reduces its prefetch activity.
The application does not read the victim's actions or edit its Q table. A recorded
trace cannot react live to a victim or guarantee MAX-PF/a particular offset.
The simulator oracle described in `../targeted_steering_attack.md` is a separate
experiment, not implemented by this application.

## Build and generate a trace

Run from the repository root (Linux x86-64, C++17, Python 3, Intel Pin):

```bash
export PIN_ROOT=/home/dheeraj-agarwal/Downloads/pin-external-4.4-99977-g54fbb8814-gcc-linux
make -C attacks
./attacks/prefetch_steer --help

python3 attacks/generate_trace.py --output attacks/generated/stream-pulse -- \
  --mode stream --size-mib 128 --streams 4 --seed 1 \
  --pre 1000000 --on 250000 --off 250000 --pulses 4 --post 1000000
```

The generator builds the application and tracer, records only the marked region,
checks that the complete schedule was captured, and gzip-compresses the trace for
ChampSim. It requires a **new** output directory. Allocation, page initialization,
pointer-chain construction, and printing are outside the trace. Run Pin where
`ptrace` is permitted; it cannot launch inside a sandbox that blocks attachment.

Outputs:

| File | Meaning |
| --- | --- |
| `attacker.trace.gz` | Standard 64-byte, non-CloudSuite ChampSim records |
| `phases.csv` | Phase transitions at zero-based emitted attacker instruction offsets |
| `manifest.json` | Exact command, source/binary/trace hashes, commit/status, host, trace length, completeness |
| `build.log`, `application.log` | Compiler commands/flags and application checksum or failure details |

The default safety cap is 100 million records (up to 6.4 GB uncompressed).
If the cap is reached before cooldown ends, generation fails and preserves the
partial raw trace and log for diagnosis. Increase `--max-instructions` before
`--` and use a new directory. Successful generation deletes its temporary raw
trace unless `--keep-raw` is supplied. Source seeds make access patterns repeatable;
ASLR can still change buffer addresses and trace hashes between recordings.

## Workload knobs

| Option | Meaning |
| --- | --- |
| `--mode stream` | Sequential cache-line reads in independent, disjoint buffer partitions |
| `--mode random` | Independent seeded pseudorandom accesses, no loaded-address dependency |
| `--mode chase` | Shuffled cyclic pointer chains; use `--streams 1` for the low-MLP latency control |
| `--mode quiet` | Replace pressure batches with arithmetic rounds for a quiet co-runner control |
| `--size-mib N` | Total buffer size; power of two, default 128 MiB |
| `--streams N` | Number of partitions/chains: 1, 2, 4, 8, or 16 |
| `--write-every N` | Also store on every Nth pressure batch, starting at batch 0 of each pulse; 0 means read-only |
| `--gap-rounds N` | Register-only arithmetic rounds between pressure batches; default 0, ignored in quiet mode |
| `--seed N` | Nonzero PRNG seed |
| `--pre N`, `--off N`, `--post N` | Register-only arithmetic rounds before attack, after each pulse, and in cooldown |
| `--on N` | Pressure batches per pulse: one 64-byte-line access per stream per batch |
| `--on-sequence A,B,...` | Override `--on` with a separate batch count for each pulse |
| `--pulses N` | Number of attack pulses; zero permits an entirely quiet schedule |

Pointer-chasing is read-only to preserve the chains. Streaming and chain positions
continue across pulses. Each quiet round has four scalar instructions in its hot
loop; calls, markers and phase dispatch add a small amount of overhead. Stack
accesses in pressure kernels are also included in traces. `--write-every 0` means
no **buffer** writes, not zero stack stores.

Examples using the generator above with different application arguments:

```bash
# One attack-on interval followed by cooldown:
./attacks/prefetch_steer --mode stream --pulses 1 --off 0

# Random pressure and dependent-latency control:
./attacks/prefetch_steer --mode random --streams 8 --seed 42
./attacks/prefetch_steer --mode chase --streams 1 --seed 42

# Reduce issue density while retaining the same buffer-access sequence:
./attacks/prefetch_steer --mode stream --streams 4 --gap-rounds 32

# Quiet co-runner (adjust rounds to match measured instruction/cycle intervals):
./attacks/prefetch_steer --mode quiet
```

For a pressure calibration sweep, hold the mode, seed, stream count, and batch
count fixed and record separate traces with `--gap-rounds 0`, `8`, `32`, and
`128`. Pacing adds four scalar loop instructions per round between batches
(plus loop setup/dispatch), with no gap after a pulse's final batch. It preserves
the buffer-access sequence and checksum, but extends the pulse's instruction
length. These settings tune offered request density; their effect on simulated
bandwidth still needs measurement. The zero-gap kernel has no pacing branch.

Counts are application work units, **not simulated cycles or victim retired
instructions**. `on/(on+off)` is not a measured bandwidth duty cycle. Use
`phases.csv` to establish the actual attacker instruction ranges, then correlate
the attacker's retirement with global cycles in the simulator for victim-time
plots. A phase occupies `[its offset, next offset)`; phase IDs are 0=pre,
1=pressure, 2=off, 3=post, 4=quiet-control. The short prefix before the first phase
is marker overhead. The final `roi_end` offset equals the trace's record count.

128 MiB exceeds this checkout's default LLC capacity (2 MiB per simulated core),
but a short burst may touch only part of the buffer. Buffer size alone does not
prove DRAM pressure. Sweep stream count, burst length, and gaps, and calibrate
against actual LLC misses and bandwidth buckets before choosing an attack.
LLC-set and DRAM-bank conflict modes require simulator address-mapping validation
and are not claimed by this implementation.

## Pair with the victim

Use `attacker.trace.gz` as **core 1** in the existing multicore experiment; retain
the victim trace as core 0. This checkout's source build example is:

```bash
./build_champsim.sh no multi no 2
```

With the simulator's existing build dependencies installed, its output is
`bin/perceptron-no-multi-no-ship-2core`. Select Pythia with
`--l2c_prefetcher_types=scooby --config=config/pythia.ini`, and put the two
compressed trace paths after `-traces`, victim first. The current configuration
already enables high-bandwidth rewards with `scooby_high_bw_thresh=2`; legal
buckets are 0–3. Confirm those values in the simulator's resolved knob dump.
The global selection applies to both cores unless the new simulator option
`--steering_disable_attacker_prefetch=true` is set. The campaign runner sets it
so core 0 has Pythia while core 1 has no L2 prefetcher.

**Do not replay the short sample for a long experiment.** This ChampSim reader
reopens a trace at EOF, which repeats the attack after cooldown. Generate enough
instructions to cover the full experiment, including warmup and the period until
the victim finishes. The faster core can consume more than the configured
instruction budget while waiting for the other core. Also distinguish the Pin
ROI markers (recording only) from ChampSim's warmup/measurement boundary: the
simulator does not consume this CSV or reset learning at these markers.

Use the interval logging and causal controls below to compare victim action
distribution, issued prefetches/degree, high-bandwidth reward counts, and cooldown
behavior against quiet, reward-ablated, and frozen-learning runs. The legacy
`CSV_ACTION` print in `Scooby::train()` reports delayed training actions, lacks
core IDs, and shares a static counter across instances; the new CSV uses the
selected action at prediction time and identifies the core. The prior slowdown
graph alone cannot establish persistent policy steering.

## Validation

```bash
make -C attacks test
PIN_ROOT="$PIN_ROOT" PREFETCHSTEER_TEST_PIN=1 python3 attacks/test_prefetch_steer.py

python3 attacks/inspect_trace.py \
  attacks/generated/stream-pulse/attacker.trace.gz \
  attacks/generated/stream-pulse/phases.csv
```

Native tests cover all mode/stream combinations, deterministic checksums, input
validation, writes, and pacing preserving the memory checksum. Pin tests verify
phase ordering, quiet vs pressure memory activity, reduced load density with
pacing, record sizes, truncation detection, and legacy skip/limit behavior.
The inspector scans all records and reports memory-operand counts per phase;
those counts include stack activity and are not LLC misses or DRAM transactions.
Tracer ROI support is opt-in (`-roi 1`); legacy `-s`/`-t` tracing remains available.
The tracer now replaces an existing output rather than appending to it, checks
write failures, and uses a stable output filename string. It remains single-threaded.

## Measure steering with causal controls

The measured `603.bwaves_s` result and contrasting `470.lbm` result are in
[`FINDINGS.md`](FINDINGS.md). Read its limitations before describing this as
reward poisoning or a workload-general attack.

For alternating slowdown/recovery intervals, use `--pulses N --on BATCHES
--off ROUNDS` with the experiment runner and inspect `pulse_comparison.csv` and
`interval_targets.csv`. The [interval-control report](INTERVAL_CONTROL.md)
includes a `603.bwaves_s` trace that produced a short-pulse speedup followed by
a long-pulse slowdown, plus two `470.lbm` quiet-gap calibrations. Use
`--on-sequence 1024,8192 --pulse-goals speedup,slowdown` to declare that
sequence. `pressure_goal_targets.csv` reports each observed direction against a
cycle-matched quiet run. Neither the pulse arguments nor a quiet gap guarantee
an above-baseline speedup at an arbitrary victim cycle.

After the native and Pin checks pass, the full local campaign is:

```bash
export PIN_ROOT=/home/dheeraj-agarwal/Downloads/pin-external-4.4-99977-g54fbb8814-gcc-linux
python3 attacks/run_steering_experiment.py
```

The runner builds the two-core simulator, generates a matched quiet trace and
separate strong stream/random pressure traces (`--streams 8 --gap-rounds 0`),
then runs attack and quiet with learning enabled and frozen: eight simulations.
The runner checks that the pressure and quiet recordings execute the same
pre-attack instruction path. Since separate Pin recordings can have different
stack addresses, it creates a derived quiet trace whose shared prefix is copied
from each pressure trace. The original trace, copied record count, and hashes
remain in the run manifest. It then requires the victim's Q fingerprint to
match at the end of warmup in each pair; otherwise the comparison fails.
It saves `experiment.json`, each simulator log and interval CSV, `comparison.csv`,
`phase_metrics.csv`, `summary.json`, `report.md` and timeline/action plots in a
fresh `runs/steering-experiment-*` directory. Use `--output NEW_DIRECTORY` to
choose the path. A Pin installation that cannot attach under your sandbox needs
the same permission used for the earlier trace generator. `--reuse-traces DIR`
uses complete, verified `DIR/quiet`, `DIR/stream`, `DIR/random` recordings.
`--no-plots` skips Matplotlib if it is unavailable. The default burst is 1024
batches: the longer 8192-batch pilot did not reach cooldown before the victim
finished. The runner rejects a missing
pressure or cooldown interval and stops on any trace EOF. Do not combine the
short smoke trace with a long simulation: it can replay after 12 million records.

The simulator's new flags are opt-in:

| Flag | Meaning |
| --- | --- |
| `--steering_log=PATH` | Emit core-specific interval counters and `PATH.phases.csv` |
| `--steering_epoch=N` | Interval length in global cycles (default 10000) |
| `--steering_phases=PATH` | Read Pin `phases.csv` using attacker retired instructions |
| `--steering_cycle_phases=PATH` | Replay a measured cycle phase table for quiet controls; mutually exclusive with instruction phases |
| `--steering_freeze_learning=true` | Stop victim Q/feature-weight updates after warmup; keep prefetcher decisions, exploration, rewards and traffic active |
| `--steering_stop_at_victim=true` | End the run when core 0 completes its measurement |
| `--steering_no_repeat=true` | Error if either trace reaches EOF before completion |
| `--steering_disable_attacker_prefetch=true` | Disable L2 prefetching on core 1 while retaining Pythia on core 0 |
| `--steering_disable_hbw_reward_after_warmup=true` | Use the low-bandwidth reward table on victim core 0 after warmup; retain bandwidth-aware action fallback and dynamic degree |

The interval log separates each core's decisions, zero-offset choices, chosen
degree, predicted lines, actually issued prefetches, rewards, Q-update attempts,
applied updates, frozen attempts, LLC demand misses, global bandwidth buckets,
DRAM queue mean, and DRAM requests attributed to each core. `q_hash` fingerprints
the stored Q values and feature weights after each interval, so a frozen run
must have zero applied updates and an unchanged fingerprint. Every interval is
split at the recorded phase boundary. Quiet runs use the attack run's measured
cycle transitions for comparison in the same global-cycle windows. The detailed
fields are documented by the CSV header, with per-core DRAM requests in
`core_dram_requests`; global bandwidth and queue columns repeat on both cores.
The report also has a `late_post` comparison beginning 100,000 cycles after
pressure ends and excluding intervals with attacker DRAM enqueues. This tests
behavior after direct request traffic ends; cache state can still differ.

The phase label is determined when the attacker's phase marker retires. Requests
already fetched or queued can drain into the next phase. The first interval
starts at the shared simulator cycle of warmup completion, which can differ by
one tick from a core's printed local cycle; retirement counts reconcile exactly.
Complete-run statistics and exactly matched-cycle comparisons are both shown.
Comparisons across learning-enabled and frozen pairs may still have different
phase times and victim states. The runner reports observed effects and does not
claim that a particular workload slowed down or that policy changes persisted.

Validate this instrumentation without Pin using deterministic tiny traces:

```bash
python3 attacks/test_steering_simulator.py
```
