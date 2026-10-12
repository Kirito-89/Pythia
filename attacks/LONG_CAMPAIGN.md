# Two-core steering campaign: 50M warmup + 200M measured victim instructions

Run these commands from the repository root. The victim is always **core 0** and
the recorded attacker or quiet control is **core 1**. `--warmup 50000000` is a
per-core instruction warmup; `--instructions 200000000` is the victim's measured
instruction budget **after** warmup. The simulator stops when core 0 completes
that budget. These numbers do not specify 200M cycles or a 200M-instruction
length for core 1.

The existing targeted `603.bwaves_s` stream recording has **23,756,134**
64-byte records (the quiet recording has 23,037,024). Neither reaches a 50M
attacker warmup. Do not use those files for this campaign: ChampSim can replay
short traces, making later "pulses" artificial. The runner below explicitly
forbids replay and errors on early EOF. The supplied `603.bwaves_s` victim has
2,000,000,000 records, so it covers its 250M instruction warmup plus measure.
Check every other victim trace separately.

## 1. Set paths and check available space

```bash
cd /home/dheeraj-agarwal/semester_7/ACA/Pythia
export PIN_ROOT=/home/dheeraj-agarwal/Downloads/pin-external-4.4-99977-g54fbb8814-gcc-linux
export VICTIM_TRACE=dpc3_traces/603.bwaves_s-2931B.champsimtrace.xz
export TRACE_ROOT="runs/steering-long-traces-${USER}-$(date +%Y%m%d-%H%M%S)"

test -x "$PIN_ROOT/pin"
test -f "$VICTIM_TRACE"
df -h .
xz --list --verbose "$VICTIM_TRACE"
```

The `UncompSize` for a standard non-CloudSuite ChampSim victim trace divided
by 64 is its record count. `603.bwaves_s` reports 128,000,000,000 bytes = 2B
records. The example below makes roughly 303M attacker records, or about 19.4GB
of temporary raw data **per recording**. It records stream and quiet in turn,
then compresses them; leave room for the raw file, compressed files, derived
quiet trace, and six simulation logs. The example schedule is a sizing starting
point, not a universal guarantee: core 1's consumption depends on both cores'
IPCs and the victim trace.

## 2. Build and validate once per code/configuration version

```bash
bash build_champsim.sh no multi no 2
test -x bin/perceptron-no-multi-no-ship-2core
make -C attacks test
python3 attacks/test_steering_simulator.py
PIN_ROOT="$PIN_ROOT" PREFETCHSTEER_TEST_PIN=1 python3 attacks/test_prefetch_steer.py
```

This builds the two-core Pythia simulator and checks the attacker and steering
instrumentation. `generate_trace.py` below rebuilds the attacker application
and Pin tracer and records their hashes. Pin needs permission to attach to its
child. A failed validation should be resolved before launching long runs.

## 3. Record long attacker and quiet traces

The first 15M quiet application rounds emit about 60M attacker instructions,
putting the first pulse after the nominal 50M attacker warmup. `--on-sequence`
selects a short and long pulse; `--off` adds a quiet gap after **each** pulse;
`--post` supplies a long final cooldown and protects against EOF. Rounds and
batches are attacker application work units, not victim instructions or cycles.

```bash
python3 attacks/generate_trace.py \
  --pin-root "$PIN_ROOT" --output "$TRACE_ROOT/stream" \
  --max-instructions 350000000 -- \
  --mode stream --size-mib 128 --streams 8 --seed 1 --gap-rounds 0 \
  --pre 15000000 --on-sequence 1024,8192 --off 250000 \
  --pulses 2 --post 60000000

python3 attacks/generate_trace.py \
  --pin-root "$PIN_ROOT" --output "$TRACE_ROOT/quiet" \
  --max-instructions 350000000 -- \
  --mode quiet --size-mib 128 --streams 8 --seed 1 --gap-rounds 0 \
  --pre 15000000 --on-sequence 1024,8192 --off 250000 \
  --pulses 2 --post 60000000
```

The Pin cap is a **maximum**, not a request for that many instructions; the
recording stops at the application's `roi_end`. A complete trace is required.
If a cap is hit, inspect `application.log`, increase the cap and use a fresh
`TRACE_ROOT`. Successful recordings have `complete_roi: true` and a final
`roi_end` in `phases.csv`. Quickly inspect the generated metadata:

```bash
python3 - "$TRACE_ROOT" <<'PY'
import csv, json, pathlib, sys
root = pathlib.Path(sys.argv[1])
for mode in ('stream', 'quiet'):
    folder = root / mode
    manifest = json.loads((folder / 'manifest.json').read_text())
    events = list(csv.DictReader((folder / 'phases.csv').open()))
    print(mode, 'complete=', manifest['complete_roi'],
          'records=', manifest['trace_records'],
          'raw_GB=', round(manifest['trace_records'] * 64 / 1e9, 2))
    print([(e['event'], e['phase'], e['pulse'], e['trace_instruction'])
           for e in events])
    assert manifest['complete_roi'] and manifest['trace_records'] > 250_000_000
    assert events[-1]['event'] == 'roi_end'
PY
```

This 250M-record assertion is a useful minimum, **not** a proof of sufficient
core-1 length. The long simulator run performs the definitive no-replay check.
`phases.csv` lists zero-based *attacker* instruction offsets. The victim phase
timing is established only when those markers retire in the two-core simulation.

## 4. Run the matched two-core experiment

Use a fresh `RUN_OUT` for each victim/recording combination. Keep the schedule
arguments identical to the recording; with `--reuse-traces`, they are metadata
and validation inputs and do not regenerate or alter a trace. The runner makes
six simulations: learned/attack, learned/quiet, frozen/attack, frozen/quiet,
reward-ablation/attack, reward-ablation/quiet. Each uses core 0 victim first,
core 1 attacker/quiet second, the same config, 50M warmup and 200M measurement.
The quiet run replays the attack run's measured **cycle** boundaries. It also
normalizes the quiet trace's pre-attack prefix to the attack trace, then checks
that both victim Q fingerprints match at the end of warmup.

```bash
export RUN_OUT="runs/steering-50m-200m-bwaves-${USER}-$(date +%Y%m%d-%H%M%S)"
python3 attacks/run_steering_experiment.py \
  --output "$RUN_OUT" \
  --skip-build --reuse-traces "$TRACE_ROOT" \
  --binary bin/perceptron-no-multi-no-ship-2core \
  --config config/pythia.ini --victim "$VICTIM_TRACE" \
  --modes stream --warmup 50000000 --instructions 200000000 \
  --pre 15000000 --on-sequence 1024,8192 --off 250000 \
  --pulses 2 --post 60000000 --size-mib 128 --streams 8 \
  --seed 1 --gap-rounds 0 --pulse-goals speedup,slowdown \
  --min-speedup-pct 0.4 --min-slowdown-pct 1.0 \
  --reward-ablation --timeout-seconds 86400
```

The runner chooses `--l2c_prefetcher_types=scooby`, keeps the attacker core's
L2 prefetcher disabled, enables interval logging, stops at victim completion,
and uses `--steering_no_repeat=true`. A failed run is **not** a data point.
If EOF occurs, record a longer stream **and** quiet trace, using more `--post`
rounds and a larger Pin cap, then start a new run directory. If some pulse or
post phase is absent from measured data, adjust `--pre`, `--off`, or pulse
lengths and record both traces again. Source changes require rebuilding and a
new campaign group.

To let the runner perform trace generation too, omit `--reuse-traces` and
`--skip-build` from the above command and add `--trace-max-instructions
350000000`; it will create `$RUN_OUT/traces/stream` and `quiet` itself. Use the
manual two-step route when several friends will reuse the same recording.

## 5. Inspect one completed run

```bash
python3 - "$RUN_OUT" <<'PY'
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
data = json.loads((root / 'experiment.json').read_text())
print('overall status:', data['status'])
print('warmup:', data['parameters']['warmup'],
      'victim measurement:', data['parameters']['instructions'])
for run in data['runs']:
    print(run['name'], run['status'], 'elapsed seconds:',
          round(run.get('elapsed_seconds', 0), 1))
assert data['status'] == 'complete'
assert len(data['runs']) == 6
assert all(run['status'] == 'complete' for run in data['runs'])
PY

cat "$RUN_OUT/report.md"
rg 'ChampSim completed victim CPU|Core_0_IPC|Core_1_IPC|Steering_core_0_q_hash_at_roi' \
  "$RUN_OUT"/*/simulation.log
```

The runner also writes `comparison.csv` (cycle-matched attack vs quiet),
`pulse_comparison.csv` (each short/long pulse), `phase_metrics.csv` (full phase
totals), `interval_targets.csv`/`pressure_goal_targets.csv` (requested speedup
and slowdown checks), each run's `intervals.csv` and `simulation.log`, plots,
and `experiment.json` with paths, commands, hashes and status. Positive
`slowdown_pct` means the victim lost IPC compared with its quiet counterpart;
negative means it exceeded quiet-run IPC in that window. A pressure-phase IPC
change alone can be direct contention. Compare `learned`, `frozen`, and
`reward-ablation` in `late_post` for evidence of a lasting reward-mediated
policy change. A speedup or slowdown goal is an observation to verify, not an
effect guaranteed by the pulse length.

## 6. Share work across friends and aggregate results

For a different victim, change `VICTIM_TRACE` and choose a new `RUN_OUT`.
Check that the victim has more than 250M records and that **all measured phases**
occur for it. The same complete stream/quiet recording can be reused across
victims to compare workload sensitivity. For additional recorded attacker
traces, create a new `TRACE_ROOT`; if testing `random`, changing `--seed` changes
its address sequence. For `stream`, changing `--seed` changes
initialized data but **not** its sequential address pattern. Separate Pin
recordings may have different ASLR address placement, but that alone is not a
new traffic pattern. Treat new victim workloads and meaningfully different
attacker patterns as the main expansion of the sample space. Multiple runs
reusing the exact same attacker trace on one victim are useful repeat checks,
but they are not new attacker recordings. Share complete run directories and
their source trace directories; retain `experiment.json` and all CSV/log files.

Once at least one full run has completed, list its directory and teammates'
directories explicitly. The collector rejects incomplete runs and checks the
matching warmup Q state, interval CSV hashes, victim completion, and replay.

```bash
export AGG_OUT="runs/steering-50m-200m-aggregate-$(date +%Y%m%d-%H%M%S)"
python3 attacks/aggregate_steering.py \
  --output "$AGG_OUT" \
  --expected-warmup 50000000 --expected-instructions 200000000 \
  "$RUN_OUT" \
  runs/FRIEND_2_COMPLETED_RUN_DIRECTORY \
  runs/FRIEND_3_COMPLETED_RUN_DIRECTORY

cat "$AGG_OUT/report.md"
```

Replace the `FRIEND_*` examples with real paths, or remove those lines if only
one run is complete. The collector writes `campaign_rows.csv` (each paired
effect), `campaign_pulses.csv`, `campaign_summary.csv` (means/medians/ranges and
standard deviations across distinct attacker trace files), `causal_controls.csv`
(learned vs frozen/reward-ablation), and `inputs.json`. It keeps different
victims, simulator binaries, configs, schedules, controls, and phases in
separate groups. Do not average 10,000-cycle windows as independent samples.
For a claim about reward steering, inspect the learned-minus-control shifts in
no-prefetch choice and prefetches per 1,000 victim instructions after pressure,
alongside bandwidth/reward observations and victim IPC. Report raw per-run
effects and sample counts, including runs with no steering.
