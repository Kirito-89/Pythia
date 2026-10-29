#!/usr/bin/env python3
"""Victim-clocked steering experiment: the attacker trace is warmup/length independent.

One short, periodic STREAM pressure trace (a single sweep of the buffer) is
recorded once and replayed by ChampSim for as long as needed. When the attacker
may use memory is decided by the *simulator* from the victim's retired
instruction count, using a schedule file. Outside a pressure window the
attacker still retires its instructions but its loads/stores are removed, so
no attacker memory traffic exists. The quiet control replays the same attacker
trace with the gate permanently closed, so attack and control differ only in
attacker memory traffic. Windows are in victim instructions measured from the
start of the measured region (ROI), unless --warmup-pulse is used.

This is an oracle actuator (simulator-side gate): label results "upper bound",
not "unprivileged application".

--controller replaces the fixed windows with a closed-loop latency-probe controller.
The attacker keeps a sparse probe (1 in --probe-period memory operations always
runs) and times its own LLC-missing loads. It calibrates the victim-only latency
while silent, then switches pressure on. While pressure is on, a fall in probe
latency below its post-onset value means the victim now sends less traffic (it has
backed off), so pressure is released; when probe latency climbs back to the
victim-only baseline the victim has recovered and pressure resumes. The controller
sees no victim state. Its per-epoch trace is intervals.csv.controller.csv.
"""
import argparse
import csv
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
from zoneinfo import ZoneInfo

from analyze_steering import analyze, read_intervals, read_log
from run_steering_experiment import (DEFAULT_PIN, ROOT, checked_run, dump, sha256,
                                     validate_trace)


def windows(text):
    result = []
    for item in text.split(","):
        begin, end = (int(v.replace("_", "")) for v in item.split("-"))
        if not 0 <= begin < end:
            raise ValueError(f"bad window {item}")
        if result and begin <= result[-1][1]:
            raise ValueError("windows must be ordered and separated by a quiet gap")
        result.append((begin, end))
    return result


def write_schedule(path, warmup, pulses, duty, warmup_pulse):
    """Rows: victim_instr (signed, relative to ROI start), phase, pulse, duty."""
    rows = [(-10**12, "pre", 0, 0)]
    if warmup_pulse:  # preconditioning: pressure during warmup, quiet at ROI entry
        rows += [(-(warmup * 3) // 4, "pressure", 0, duty), (-1, "pre", 0, 0)]  # negative = before ROI
    for index, (begin, end) in enumerate(pulses):
        last = index == len(pulses) - 1
        rows.append((begin, "pressure", index, duty))
        rows.append((end, "post" if last else "off", len(pulses) if last else index, 0))
    with path.open("w", newline="") as stream:
        out = csv.writer(stream)
        out.writerow(["victim_instr", "phase", "pulse", "duty"])
        out.writerows(rows)
    return rows


def persistence_table(output, pairs, budget, bin_size=2_000_000):
    """Attack vs quiet, binned by ROI-relative victim instructions (the attacker is silent in the ROI)."""
    def bins(path):
        rows = [r for r in csv.DictReader(Path(path).open()) if r["core"] == "0"]
        start, table = int(rows[0]["retired_begin"]), {}
        for r in rows:
            k = (int(r["retired_begin"]) - start) // bin_size
            a = table.setdefault(k, [0] * 6)
            a[0] += int(r["instructions"]); a[1] += int(r["cycle_end"]) - int(r["cycle_begin"])
            a[2] += int(r["issued_prefetches"]); a[3] += int(r["decisions"])
            a[4] += int(r["zero_action"]); a[5] += int(r["degree_sum"])
        return table
    with (output / "persistence.csv").open("w", newline="") as stream:
        out = csv.writer(stream)
        out.writerow(["control", "roi_bin_start_instr", "ipc_attack", "ipc_quiet", "ipc_delta_pct",
                      "pfki_attack", "pfki_quiet", "avg_degree_attack", "avg_degree_quiet",
                      "zero_attack", "zero_quiet"])
        for pair in pairs:
            a, q = bins(output / pair["attack"] / "intervals.csv"), bins(output / pair["quiet"] / "intervals.csv")
            for k in sorted(set(a) & set(q)):
                x, y = a[k], q[k]
                if not (x[0] and y[0] and x[3] and y[3]): continue
                out.writerow([pair["control"], k * bin_size, x[0] / x[1], y[0] / y[1],
                              100 * ((x[0] / x[1]) / (y[0] / y[1]) - 1), 1000 * x[2] / x[0], 1000 * y[2] / y[0],
                              x[5] / x[3], y[5] / y[3], x[4], y[4]])


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--output", type=Path)
    p.add_argument("--pin-root", type=Path, default=Path(os.environ.get("PIN_ROOT", DEFAULT_PIN)))
    p.add_argument("--binary", type=Path, default=ROOT / "bin/perceptron-no-multi-no-ship-2core")
    p.add_argument("--config", type=Path, default=ROOT / "config/pythia.ini")
    p.add_argument("--victim", type=Path, default=ROOT / "dpc3_traces/603.bwaves_s-2931B.champsimtrace.xz")
    p.add_argument("--reuse-trace", type=Path, help="existing directory from generate_trace.py (periodic pressure trace)")
    p.add_argument("--warmup", type=int, default=50_000_000)
    p.add_argument("--instructions", type=int, default=200_000_000)
    p.add_argument("--epoch", type=int, default=10_000)
    p.add_argument("--windows", default="40000000-60000000,120000000-160000000",
                   help="pressure windows as ROI-relative victim instructions: begin-end,begin-end")
    p.add_argument("--pulse-goals", default="speedup,slowdown")
    p.add_argument("--controller", action="store_true",
                   help="closed-loop latency-probe controller instead of fixed --windows")
    p.add_argument("--probe-tolerance", type=int, default=20,
                   help="attacker DRAM requests per interval still treated as silent when scoring off/post windows")
    p.add_argument("--probe-period", type=int, default=64, help="controller probe: 1 in N attacker memory ops always issues")
    p.add_argument("--ctl-epoch", type=int, default=50_000, help="controller decision period, cycles")
    p.add_argument("--ctl-calibrate", type=int, default=20, help="valid silent epochs used for the baseline latency")
    p.add_argument("--ctl-settle", type=int, default=10, help="epochs after pressure onset ignored (queues filling)")
    p.add_argument("--ctl-window", type=int, default=10, help="epochs averaged as the reference latency after the settle period")
    p.add_argument("--ctl-min-on", type=int, default=30)
    p.add_argument("--ctl-max-on", type=int, default=200)
    p.add_argument("--ctl-min-off", type=int, default=10)
    p.add_argument("--ctl-max-off", type=int, default=200)
    p.add_argument("--ctl-drop-pct", type=float, default=5.0, help="latency fall (percent) taken to mean the victim backed off")
    p.add_argument("--ctl-recover-pct", type=float, default=3.0, help="release is over when latency is within this percent of baseline")
    p.add_argument("--track", action="store_true",
                   help="with --controller: continuous duty tracking of --targets instead of on/relax pulses")
    p.add_argument("--targets", default="0:10,70000000:40,130000000:0",
                   help="--track setpoints 'victim_instr:pct,...' (ROI-relative; pct = probe-latency rise over the "
                        "victim-only baseline the attacker holds; 0 = silent)")
    p.add_argument("--track-gain", type=float, default=2.0, help="duty points changed per percentage point of setpoint error")
    p.add_argument("--track-trigger", choices=("instr", "ipc", "noprefetch"), default="instr",
                   help="what advances setpoint segments: victim instruction count (default), or ORACLE victim state "
                        "(ipc below / noprefetch-rate above the next segment's threshold). ipc/noprefetch read victim state -> upper bound.")
    p.add_argument("--track-confirm", type=int, default=2, help="consecutive epochs the victim threshold must hold before advancing")
    p.add_argument("--track-cycle", action="store_true",
                   help="ORACLE repeated down/up control: pressure until victim IPC < --cycle-lo, release until IPC > --cycle-hi, repeat")
    p.add_argument("--cycle-hi", type=float, help="resume pressure when victim IPC rises above this (recovered)")
    p.add_argument("--cycle-lo", type=float, help="release pressure when victim IPC falls below this (driven down)")
    p.add_argument("--cycle-setpoint", type=float, default=50.0, help="latency-rise setpoint (percent) held while pressuring")
    p.add_argument("--cycle-abs", action="store_true",
                   help="treat --cycle-hi/--cycle-lo as absolute victim IPC values; default is fractions of measured idle IPC")
    p.add_argument("--duty-min", type=int, default=1, help="lowest duty while a setpoint is active (percent)")
    p.add_argument("--ctl-end", type=int, help="ROI-relative victim instruction where attacking stops (default: 75%% of --instructions)")
    p.add_argument("--duty", type=int, default=100, help="percent of cycles the attacker may use memory in a window")
    p.add_argument("--warmup-pulse", action="store_true", help="also pressure the last 3/4 of warmup (persistence test)")
    p.add_argument("--size-mib", type=int, default=32, help="stream buffer; the trace is exactly one sweep")
    p.add_argument("--streams", type=int, choices=(1, 2, 4, 8, 16), default=8)
    p.add_argument("--min-slowdown-pct", type=float, default=1.0)
    p.add_argument("--min-speedup-pct", type=float, default=1.0)
    p.add_argument("--reward-ablation", action="store_true")
    p.add_argument("--oracle-bw", type=int, choices=(0, 1, 2, 3), help="ORACLE upper bound: no attacker traffic; force this bandwidth bucket in pressure windows")
    p.add_argument("--defense-per-core-bw", action="store_true", help="DEFENSE: prefetchers see only their own core's DRAM request rate")
    p.add_argument("--skip-build", action="store_true")
    p.add_argument("--timeout-seconds", type=int, default=86400)
    p.add_argument("--no-plots", action="store_true")
    return p


def main():
    cli = parser()
    args = cli.parse_args()
    if args.controller:
        if args.warmup_pulse or args.oracle_bw is not None:
            cli.error("--controller cannot be combined with --warmup-pulse or --oracle-bw")
        if args.probe_period < 1:
            cli.error("--probe-period must be positive")
        if args.ctl_window < 1 or args.ctl_min_on <= args.ctl_settle + args.ctl_window:
            cli.error("--ctl-min-on must exceed --ctl-settle + --ctl-window")
        if args.track_cycle:
            args.track = True
            args.track_trigger = "ipc"
            if args.cycle_hi is None or args.cycle_lo is None:
                cli.error("--track-cycle needs --cycle-hi and --cycle-lo")
            if not (args.cycle_hi > args.cycle_lo > 0):
                cli.error("--track-cycle needs cycle-hi > cycle-lo > 0")
            if not args.cycle_abs and args.cycle_hi > 2:
                cli.error("relative --cycle-hi is a fraction of idle IPC (<=2); pass --cycle-abs for absolute IPC values")
            if args.cycle_setpoint <= 0:
                cli.error("--cycle-setpoint must be positive")
        if args.track and not args.track_cycle:
            # instr trigger: first field is a victim instruction count; ipc/noprefetch: a victim-state threshold.
            try:
                tp = [(float(a), float(b)) for a, b in (i.split(":") for i in args.targets.split(","))]
            except ValueError:
                cli.error("--targets must look like 0:10,70000000:40 (instr) or 0:10,1.35:40 (ipc)")
            if args.track_trigger == "instr" and (tp[0][0] != 0 or any(y[0] <= x[0] for x, y in zip(tp, tp[1:]))):
                cli.error("--targets must start at 0 and strictly increase for the instr trigger")
        if args.ctl_end is None:
            args.ctl_end = args.instructions * 3 // 4
        if not 0 < args.ctl_end < args.instructions:
            cli.error("--ctl-end must fall inside the measured region, leaving a cooldown")
        pulses = []
    else:
        pulses = windows(args.windows)
        goals = args.pulse_goals.split(",")
        if len(goals) != len(pulses):
            cli.error("--pulse-goals needs one goal per window")
        if pulses[-1][1] >= args.instructions:
            cli.error("last window must end before the measured region does, leaving a cooldown")
    if not 0 < args.duty <= 100:
        cli.error("--duty must be in 1..100")
    output = (args.output or ROOT / "runs" / ("steering-gated-" + datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y%m%d-%H%M%S"))).resolve()
    output.mkdir(parents=True, exist_ok=False)
    env = dict(os.environ, PIN_ROOT=str(args.pin_root.resolve()))
    manifest_file = output / "experiment.json"
    params = {k: str(v.resolve()) if isinstance(v, Path) else v for k, v in vars(args).items()}
    params["pulse_goals"] = None if args.controller else args.pulse_goals
    # the controller's probe keeps ~8 attacker DRAM requests per 10k-cycle interval; do not call that "pressure"
    params["attacker_dram_tolerance"] = args.probe_tolerance if args.controller else 0
    manifest = {"schema_version": 1, "status": "preparing", "victim_core": 0, "attacker_core": 1,
                "parameters": params, "runs": [], "pairs": [], "traces": {},
                "method": "simulator-side attacker gate (victim-clocked windows, or closed-loop latency probe with --controller); same periodic trace, gate closed for quiet control",
                "created_at": datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(),
                "repo_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                "repo_status": subprocess.check_output(["git", "status", "--short"], cwd=ROOT, text=True)}
    dump(manifest_file, manifest)
    try:
        if not args.skip_build:
            checked_run(["bash", str(ROOT / "build_champsim.sh"), "no", "multi", "no", "2"],
                        output / "simulator-build.log", env, args.timeout_seconds)
        if not args.binary.is_file():
            raise ValueError(f"simulator binary not found: {args.binary}")
        manifest["provenance"] = {"binary": sha256(args.binary), "config": sha256(args.config),
                                  "victim": sha256(args.victim)}
        # One full sweep: each batch reads one line per stream.
        batches = args.size_mib * 1024 * 1024 // 64 // args.streams
        directory = args.reuse_trace.resolve() if args.reuse_trace else output / "trace"
        if not args.reuse_trace:
            checked_run([sys.executable, str(ROOT / "attacks/generate_trace.py"),
                         "--pin-root", str(args.pin_root.resolve()), "--output", str(directory),
                         "--max-instructions", str(batches * 128), "--",
                         "--mode", "stream", "--size-mib", str(args.size_mib), "--streams", str(args.streams),
                         "--gap-rounds", "0", "--seed", "1", "--pre", "0", "--on", str(batches),
                         "--off", "0", "--pulses", "1", "--post", "0"],
                        output / "trace.log", env, args.timeout_seconds)
        manifest["traces"]["periodic"] = validate_trace(directory)
        schedule = output / "schedule.csv"
        if not args.controller:
            rows = write_schedule(schedule, args.warmup, pulses, args.duty, args.warmup_pulse)
            manifest["schedule"] = {"path": str(schedule), "sha256": sha256(schedule), "rows": rows}
        manifest["status"] = "running"
        dump(manifest_file, manifest)
        controls = ["learned", "frozen"] + (["reward-ablation"] if args.reward_ablation else [])
        common = [str(args.binary.resolve()), f"--config={args.config.resolve()}", "--l2c_prefetcher_types=scooby",
                  f"--warmup_instructions={args.warmup}", f"--simulation_instructions={args.instructions}",
                  f"--steering_epoch={args.epoch}", "--steering_stop_at_victim=true",
                  "--steering_victim_core=0", "--steering_attacker_core=1",
                  "--steering_disable_attacker_prefetch=true"]
        trace = manifest["traces"]["periodic"]["trace"]
        for control in controls:
            pair = f"stream-{control}"
            for role in ("attack", "quiet"):
                name = f"{pair}-{role}"
                run_dir = output / name
                run_dir.mkdir()
                intervals, log = run_dir / "intervals.csv", run_dir / "simulation.log"
                command = [*common, f"--steering_log={intervals}",
                           f"--steering_freeze_learning={'true' if control == 'frozen' else 'false'}"]
                if args.defense_per_core_bw:
                    command.append("--steering_per_core_bw=true")
                if control == "reward-ablation":
                    command.append("--steering_disable_hbw_reward_after_warmup=true")
                if args.controller:
                    command.append(f"--steering_probe_period={args.probe_period}")  # identical probe in both roles
                if role == "attack" and args.controller:
                    command += ["--steering_probe_controller=true", f"--steering_ctl_epoch={args.ctl_epoch}",
                                f"--steering_ctl_calibrate={args.ctl_calibrate}", f"--steering_ctl_settle={args.ctl_settle}",
                                f"--steering_ctl_window={args.ctl_window}",
                                f"--steering_ctl_min_on={args.ctl_min_on}", f"--steering_ctl_max_on={args.ctl_max_on}",
                                f"--steering_ctl_min_off={args.ctl_min_off}", f"--steering_ctl_max_off={args.ctl_max_off}",
                                f"--steering_ctl_drop_pct={args.ctl_drop_pct}", f"--steering_ctl_recover_pct={args.ctl_recover_pct}",
                                f"--steering_ctl_end={args.ctl_end}", f"--steering_ctl_duty={args.duty}"]
                    if args.track:
                        command += ["--steering_ctl_track=true", f"--steering_ctl_targets={args.targets}",
                                    f"--steering_ctl_gain={args.track_gain}", f"--steering_ctl_duty_min={args.duty_min}",
                                    f"--steering_ctl_trigger={args.track_trigger}", f"--steering_ctl_confirm={args.track_confirm}"]
                        if args.track_cycle:
                            command += ["--steering_ctl_cycle=true", f"--steering_ctl_cycle_hi={args.cycle_hi}",
                                        f"--steering_ctl_cycle_lo={args.cycle_lo}", f"--steering_ctl_cycle_setpoint={args.cycle_setpoint}",
                                        f"--steering_ctl_cycle_rel={'false' if args.cycle_abs else 'true'}"]
                elif role == "attack":
                    command.append(f"--steering_schedule={schedule}")
                    if args.oracle_bw is not None:  # attacker memory gated off; bucket forced by the simulator
                        command += ["--steering_schedule_quiet=true", f"--steering_force_bw={args.oracle_bw}"]
                elif args.warmup_pulse:  # warmup lengths differ, so align by victim instructions, not cycles
                    command += [f"--steering_schedule={schedule}", "--steering_schedule_quiet=true"]
                else:  # same cycle boundaries as the attack run, gate permanently closed
                    command += ["--steering_schedule_quiet=true",
                                f"--steering_cycle_phases={output / (pair + '-attack') / 'intervals.csv.phases.csv'}"]
                command += ["-traces", str(args.victim.resolve()), trace]
                run = {"name": name, "mode": "stream", "control": control, "role": role, "command": command,
                       "log": str(log.relative_to(output)), "intervals": str(intervals.relative_to(output)),
                       "status": "running"}
                manifest["runs"].append(run)
                dump(manifest_file, manifest)
                run["elapsed_seconds"] = checked_run(command, log, env, args.timeout_seconds)
                info = read_log(log)
                if role == "quiet" and not args.warmup_pulse:
                    if info["q_hash_at_roi"] != read_log(output / f"{pair}-attack" / "simulation.log")["q_hash_at_roi"]:
                        raise ValueError(f"{pair}: victim Q state differs at ROI although warmup had no pressure")
                read_intervals(intervals)
                run["intervals_sha256"] = sha256(intervals)
                run["status"] = "complete"
                dump(manifest_file, manifest)
            manifest["pairs"].append({"name": pair, "mode": "stream", "control": control,
                                      "attack": pair + "-attack", "quiet": pair + "-quiet"})
            dump(manifest_file, manifest)
        if args.warmup_pulse:
            persistence_table(output, manifest["pairs"], args.instructions)
            manifest["status"] = "complete"
            dump(manifest_file, manifest)
            print(f"Persistence table: {output / 'persistence.csv'}")
            return
        analyze(output, plots=not args.no_plots)
        manifest["status"] = "complete"
        dump(manifest_file, manifest)
        print(f"Experiment complete: {output / 'report.md'}")
    except BaseException as error:
        manifest["status"] = "failed"
        manifest["error"] = str(error)
        dump(manifest_file, manifest)
        raise


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        sys.exit(str(error))
