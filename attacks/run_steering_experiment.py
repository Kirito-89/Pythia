#!/usr/bin/env python3
"""Record stronger pressure traces, run matched controls, and analyze steering.

Each pressure run is followed by a quiet run replaying its measured *cycle*
phase boundaries. This is an open-loop experiment, never an assertion that an
attacker succeeded. Existing experiment directories are never overwritten.
"""
import argparse
import csv
from datetime import datetime
import gzip
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

from analyze_steering import analyze, read_intervals, read_log

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PIN = Path("/home/dheeraj-agarwal/Downloads/pin-external-4.4-99977-g54fbb8814-gcc-linux")


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def dump(path, content):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(content, indent=2) + "\n")
    temporary.replace(path)


def checked_run(command, log, env=None, timeout=None):
    print(f"Running: {shlex.join([str(v) for v in command])}", flush=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    dump(log.with_suffix(".command.json"), [str(v) for v in command])
    started = time.monotonic()
    with log.open("w") as stream:
        result = subprocess.run(command, cwd=ROOT, env=env, stdout=stream,
                                stderr=subprocess.STDOUT, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"command exited {result.returncode}; inspect {log}")
    elapsed = time.monotonic() - started
    print(f"Completed in {elapsed:.1f}s: {log}", flush=True)
    return elapsed


def validate_trace(directory):
    trace = directory / "attacker.trace.gz"
    phases = directory / "phases.csv"
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if not manifest.get("complete_roi"):
        raise ValueError(f"{directory}: trace manifest does not certify a complete ROI")
    if not trace.is_file() or not phases.is_file():
        raise ValueError(f"{directory}: missing trace or phase table")
    digest = sha256(trace)
    if manifest.get("compressed_sha256") != digest:
        raise ValueError(f"{directory}: compressed trace checksum does not match manifest")
    return {"directory": str(directory), "trace": str(trace), "phases": str(phases),
            "compressed_sha256": digest, "phase_sha256": sha256(phases),
            "manifest_sha256": sha256(manifest_path), "trace_records": manifest["trace_records"],
            "recording_command": manifest["command"]}


def normalize_quiet_prefix(attack, quiet, destination, warmup):
    """Copy the same executable prefix into a quiet trace, preserving ROI length.

    Pin gives separate processes different stack addresses. In ChampSim, even
    these tiny address differences can change early Q learning. Both applications
    execute the same binary path until phase dispatch, so their IP streams must
    match throughout the copied prefix; fail instead of silently splicing a
    different control flow.
    """
    with Path(attack["phases"]).open(newline="") as stream:
        a_events = list(csv.DictReader(stream))
    with Path(quiet["phases"]).open(newline="") as stream:
        q_events = list(csv.DictReader(stream))
    a_point = next(int(row["trace_instruction"]) for row in a_events if row["event"] == "phase" and row["phase"] == "1")
    q_point = next(int(row["trace_instruction"]) for row in q_events if row["event"] == "phase" and row["phase"] == "4")
    limit = min(a_point, q_point) - 16  # leave phase-dispatch instructions untouched
    if limit <= warmup + 1000:
        raise ValueError("pre-attack prefix too short to preserve identical learner warmup")
    with gzip.open(attack["trace"], "rb") as a, gzip.open(quiet["trace"], "rb") as q:
        for index in range(limit):
            left, right = a.read(64), q.read(64)
            if len(left) != 64 or len(right) != 64 or left[:8] != right[:8]:
                raise ValueError(f"attack/quiet instruction PCs diverge before copied prefix ends, record {index}")
    destination.mkdir(parents=True, exist_ok=False)
    target = destination / "attacker.trace.gz"
    prefix_bytes = limit * 64
    with gzip.open(attack["trace"], "rb") as a, gzip.open(quiet["trace"], "rb") as q, target.open("wb") as out:
        with gzip.GzipFile(filename="", mode="wb", fileobj=out, mtime=0, compresslevel=1) as compressed:
            remaining = prefix_bytes
            while remaining:
                block = a.read(min(1024 * 1024, remaining))
                if not block:
                    raise ValueError("attack trace ended within common prefix")
                compressed.write(block)
                remaining -= len(block)
            # gzip.seek may decompress the prefix; this never changes the source.
            q.seek(prefix_bytes)
            shutil.copyfileobj(q, compressed, 1024 * 1024)
    result = dict(quiet)
    result.update(trace=str(target), compressed_sha256=sha256(target),
                  normalized_prefix_records=limit,
                  normalized_from_attack_sha256=attack["compressed_sha256"],
                  normalized_from_quiet_sha256=quiet["compressed_sha256"])
    return result


def parser():
    result = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    result.add_argument("--output", type=Path, help="new output directory (default runs/steering-experiment-TIMESTAMP)")
    result.add_argument("--pin-root", type=Path, default=Path(os.environ.get("PIN_ROOT", DEFAULT_PIN)))
    result.add_argument("--binary", type=Path, default=ROOT / "bin/perceptron-no-multi-no-ship-2core")
    result.add_argument("--config", type=Path, default=ROOT / "config/pythia.ini")
    result.add_argument("--victim", type=Path, default=ROOT / "dpc3_traces/603.bwaves_s-2931B.champsimtrace.xz")
    result.add_argument("--skip-build", action="store_true", help="use an already rebuilt instrumented binary")
    result.add_argument("--reuse-traces", type=Path, help="existing directory containing quiet/stream/random generated trace directories; schedule arguments then apply only to metadata, actual commands are recorded")
    result.add_argument("--modes", nargs="+", choices=("stream", "random"), default=["stream", "random"])
    result.add_argument("--reward-ablation", action="store_true", help="disable HBW reward on victim after warmup; keep bandwidth-aware fallback/degree")
    result.add_argument("--generate-only", action="store_true", help="prepare and verify traces without simulation")
    result.add_argument("--warmup", type=int, default=100_000)
    result.add_argument("--instructions", type=int, default=5_000_000, help="victim measurement instruction budget")
    result.add_argument("--epoch", type=int, default=10_000, help="logging interval in global cycles")
    result.add_argument("--pre", type=int, default=250_000, help="initial quiet application rounds")
    result.add_argument("--on", type=int, default=1024, help="pressure batches in each recorded pulse")
    result.add_argument("--on-sequence", help="comma-separated pressure batches for each pulse, overriding --on")
    result.add_argument("--pulse-goals", help="comma-separated pressure goals: speedup or slowdown per pulse")
    result.add_argument("--off", type=int, default=0, help="quiet arithmetic rounds after each pulse")
    result.add_argument("--pulses", type=int, default=1, help="number of pressure/quiet intervals")
    result.add_argument("--post", type=int, default=5_000_000, help="cooldown arithmetic rounds, must prevent EOF")
    result.add_argument("--streams", type=int, choices=(1, 2, 4, 8, 16), default=8)
    result.add_argument("--size-mib", type=int, default=128)
    result.add_argument("--seed", type=int, default=1)
    result.add_argument("--min-slowdown-pct", type=float, default=1.0,
                        help="required positive pressure-window slowdown for interval success")
    result.add_argument("--min-speedup-pct", type=float, default=1.0,
                        help="required speedup above the quiet baseline in a settled off window")
    result.add_argument("--timeout-seconds", type=int, default=7200, help="maximum duration of each build/recording/simulation")
    result.add_argument("--no-plots", action="store_true")
    return result


def main():
    cli = parser()
    args = cli.parse_args()
    if any(getattr(args, key) <= 0 for key in ("instructions", "epoch", "on", "pulses", "post", "size_mib", "seed", "timeout_seconds")) or min(args.pre, args.off, args.warmup) < 0:
        cli.error("instruction counts, epoch, pulse, pulses, cooldown, size, seed and timeout must be positive; pre/off/warmup nonnegative")
    if args.pulses > 1 and not args.off:
        cli.error("multiple pulses require --off > 0 so a recovery interval exists")
    if args.on_sequence is not None:
        try:
            on_sequence = [int(value) for value in args.on_sequence.split(",")]
        except ValueError:
            cli.error("--on-sequence must contain comma-separated positive integers")
        if len(on_sequence) != args.pulses or any(value <= 0 or value > 10**12 for value in on_sequence):
            cli.error("--on-sequence requires one positive batch count per pulse (up to 10^12)")
    else:
        on_sequence = None
    if args.pulse_goals is not None:
        pulse_goals = args.pulse_goals.split(",")
        if len(pulse_goals) != args.pulses or any(goal not in ("speedup", "slowdown") for goal in pulse_goals):
            cli.error("--pulse-goals requires one speedup or slowdown goal per pulse")
    if not all(0 < getattr(args, key) < 100 for key in ("min_slowdown_pct", "min_speedup_pct")):
        cli.error("interval target percentages must be between 0 and 100")
    if len(set(args.modes)) != len(args.modes):
        cli.error("duplicate modes are not allowed")
    for path in (args.victim, args.config):
        if not path.is_file():
            cli.error(f"file not found: {path}")
    if not args.reuse_traces and not (args.pin_root / "pin").is_file():
        cli.error("Pin executable not found; set PIN_ROOT or --pin-root")
    output = args.output or ROOT / "runs" / ("steering-experiment-" + datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y%m%d-%H%M%S"))
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    env = dict(os.environ, PIN_ROOT=str(args.pin_root.resolve()))
    params = {k: str(v.resolve()) if isinstance(v, Path) else v for k, v in vars(args).items()}
    manifest = {"schema_version": 1, "status": "preparing", "victim_core": 0, "attacker_core": 1,
                "parameters": params, "runs": [], "pairs": [], "traces": {},
                "method": "pressure then cycle-matched quiet control, learned and frozen after warmup",
                "created_at": datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(),
                "repo_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                "repo_status": subprocess.check_output(["git", "status", "--short"], cwd=ROOT, text=True),
                "provenance": {"victim": {"path": str(args.victim.resolve()), "sha256": sha256(args.victim)},
                               "config": {"path": str(args.config.resolve()), "sha256": sha256(args.config)}}}
    manifest_file = output / "experiment.json"
    dump(manifest_file, manifest)
    try:
        if not args.skip_build and not args.generate_only:
            checked_run(["bash", str(ROOT / "build_champsim.sh"), "no", "multi", "no", "2"],
                        output / "simulator-build.log", env, args.timeout_seconds)
        if not args.generate_only:
            if not args.binary.is_file():
                raise ValueError(f"simulator binary not found: {args.binary}")
            manifest["provenance"]["binary"] = {"path": str(args.binary.resolve()), "sha256": sha256(args.binary)}
        source_paths = [ROOT / "attacks/run_steering_experiment.py", ROOT / "attacks/analyze_steering.py"]
        source_paths += sorted((ROOT / "src").glob("*steering*")) + sorted((ROOT / "inc").glob("*steering*"))
        source_paths += [ROOT / "src/main.cc", ROOT / "prefetcher/scooby.cc"]
        manifest["source_sha256"] = {str(p.relative_to(ROOT)): sha256(p) for p in source_paths if p.is_file()}
        trace_root = args.reuse_traces.resolve() if args.reuse_traces else output / "traces"
        for mode in ["quiet", *args.modes]:
            directory = trace_root / mode
            if not args.reuse_traces:
                # Keep the same pre-attack instruction stream in all recordings.
                # Quiet replaces only the scheduled memory pulse with arithmetic;
                # the simulator still labels it using the pressure run's cycles.
                schedule = ["--pre", str(args.pre), "--on", str(args.on),
                            "--off", str(args.off), "--pulses", str(args.pulses),
                            "--post", str(args.post)]
                if on_sequence is not None:
                    schedule.extend(["--on-sequence", ",".join(map(str, on_sequence))])
                command = [sys.executable, str(ROOT / "attacks/generate_trace.py"),
                           "--pin-root", str(args.pin_root.resolve()), "--output", str(directory), "--",
                           "--mode", mode, "--size-mib", str(args.size_mib), "--streams", str(args.streams),
                           "--gap-rounds", "0", "--seed", str(args.seed), *schedule]
                checked_run(command, output / f"trace-{mode}.log", env, args.timeout_seconds)
            manifest["traces"][mode] = validate_trace(directory)
            with Path(manifest["traces"][mode]["phases"]).open(newline="") as stream:
                phase_rows = list(csv.DictReader(stream))
            expected_id = "4" if mode == "quiet" else "1"
            pulse_rows = [row for row in phase_rows if row["event"] == "phase" and row["phase"] == expected_id]
            off_rows = [row for row in phase_rows if row["event"] == "phase" and row["phase"] == "2"]
            if len(pulse_rows) != args.pulses or len(off_rows) != (args.pulses if args.off else 0):
                raise ValueError(f"{mode}: recorded schedule has {len(pulse_rows)} pulses and {len(off_rows)} off intervals; expected {args.pulses} pulses and {args.pulses if args.off else 0} off intervals")
            dump(manifest_file, manifest)
        for mode in args.modes:
            derived = normalize_quiet_prefix(
                manifest["traces"][mode], manifest["traces"]["quiet"],
                output / "derived-traces" / f"quiet-{mode}", args.warmup)
            manifest["traces"][f"quiet-{mode}"] = derived
            dump(manifest_file, manifest)
        if args.generate_only:
            manifest["status"] = "traces_ready"
            dump(manifest_file, manifest)
            print(f"Verified trace directory: {trace_root}")
            return
        manifest["status"] = "running"
        dump(manifest_file, manifest)
        controls = ["learned", "frozen"] + (["reward-ablation"] if args.reward_ablation else [])
        common = [str(args.binary.resolve()), f"--config={args.config.resolve()}", "--l2c_prefetcher_types=scooby",
                  f"--warmup_instructions={args.warmup}", f"--simulation_instructions={args.instructions}",
                  f"--steering_epoch={args.epoch}", "--steering_stop_at_victim=true", "--steering_no_repeat=true",
                  "--steering_victim_core=0", "--steering_attacker_core=1", "--steering_disable_attacker_prefetch=true"]
        for mode in args.modes:
            for control in controls:
                pair_name = f"{mode}-{control}"
                attack_name = pair_name + "-attack"
                quiet_name = pair_name + "-quiet"
                replay = output / attack_name / "intervals.csv.phases.csv"
                for role, run_name in (("attack", attack_name), ("quiet", quiet_name)):
                    run_dir = output / run_name
                    run_dir.mkdir()
                    intervals = run_dir / "intervals.csv"
                    log = run_dir / "simulation.log"
                    trace = manifest["traces"][mode if role == "attack" else f"quiet-{mode}"]
                    command = [*common, f"--steering_log={intervals}",
                               f"--steering_freeze_learning={'true' if control == 'frozen' else 'false'}"]
                    if control == "reward-ablation":
                        command.append("--steering_disable_hbw_reward_after_warmup=true")
                    command.append(f"--steering_phases={trace['phases']}" if role == "attack"
                                   else f"--steering_cycle_phases={replay}")
                    command += ["-traces", str(args.victim.resolve()), trace["trace"]]
                    run = {"name": run_name, "mode": mode, "control": control, "role": role,
                           "command": command, "log": str(log.relative_to(output)),
                           "intervals": str(intervals.relative_to(output)), "status": "running"}
                    manifest["runs"].append(run)
                    dump(manifest_file, manifest)
                    run["elapsed_seconds"] = checked_run(command, log, env, args.timeout_seconds)
                    log_info = read_log(log)
                    if role == "quiet":
                        attack_log = output / attack_name / "simulation.log"
                        attack_info = read_log(attack_log)
                        if log_info["q_hash_at_roi"] != attack_info["q_hash_at_roi"]:
                            raise ValueError(f"{pair_name}: victim Q state differs before pressure despite normalized prefix")
                    windows = read_intervals(intervals)
                    phases = {row["phase"] for row in windows}
                    expected = {"pressure", "post"} | ({"off"} if args.off else set())
                    if not expected <= phases:
                        raise ValueError(f"{run_name}: not all scheduled phases were measured. "
                                         "Shorten --pre/--on/--off or lengthen --instructions in a new run.")
                    for pulse in range(args.pulses):
                        for phase in (["pressure", "off"] if args.off else ["pressure"]):
                            if not any(row["phase"] == phase and row["pulse"] == pulse for row in windows):
                                raise ValueError(f"{run_name}: missing measured {phase} interval for pulse {pulse}")
                    run["intervals_sha256"] = sha256(intervals)
                    run["status"] = "complete"
                    dump(manifest_file, manifest)
                    if role == "attack" and not replay.is_file():
                        raise ValueError(f"missing phase replay table: {replay}")
                manifest["pairs"].append({"name": pair_name, "mode": mode, "control": control,
                                          "attack": attack_name, "quiet": quiet_name})
                dump(manifest_file, manifest)
        analyze(output, plots=not args.no_plots)
        manifest["status"] = "complete"
        dump(manifest_file, manifest)
        print(f"Experiment complete: {output / 'report.md'}")
        print("Read the observed results and limitations; no slowdown or persistent steering is assumed.")
    except BaseException as error:
        manifest["status"] = "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
        manifest["error"] = str(error)
        dump(manifest_file, manifest)
        raise


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        sys.exit(str(error))
