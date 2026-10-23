#!/usr/bin/env python3
"""Closed-loop targeted-steering experiment for the Pythia reward-poisoning study.

The victim (Pythia) runs on core 0; a memory-intensive benchmark runs on core 1
as the always-loud attacker. The in-simulator steering oracle suppresses the
attacker's off-chip DRAM reads according to the victim's most recent action
(see src/steering.cc), so no Pin-recorded attacker trace is needed.

It runs a matched matrix that isolates *learned* targeted steering from raw
contention volume:

  treat_learn   conditional gate, target action, learning enabled   (headline)
  treat_frozen  conditional gate, target action, learning FROZEN     (control)
  control_learn budget gate matched to treat_learn's suppressed       (control)
                fraction, action-INDEPENDENT, learning enabled
  control_frozen budget gate matched, learning FROZEN                 (control)
  ablation_learn conditional gate + HBW reward disabled after warmup  (optional)

Headline claim: the victim's share of the target action rises over the run in
treat_learn, but not in treat_frozen (no learning) and much less in
control_learn (same contention volume, not action-conditional). All runs use the
same victim, attacker, config, seed, warmup and measurement budget, and each
stops when the victim completes its measurement. Long runs: hand the printed
command to a machine with time; a 50M/200M run is not interactive.
"""
import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BINARY = ROOT / "bin/perceptron-no-multi-no-ship-2core"
DEFAULT_CONFIG = ROOT / "config/pythia.ini"
DEFAULT_VICTIM = ROOT / "dpc3_traces/603.bwaves_s-2931B.champsimtrace.xz"
DEFAULT_ATTACKER = ROOT / "dpc3_traces/470.lbm-1274B.champsimtrace.xz"
BUDGET_DEN = 1_000_000  # resolution for matching the budget-control suppressed fraction


def metric(text, name):
    match = re.search(rf"^{re.escape(name)}\s+(\S+)\s*$", text, re.MULTILINE)
    return match.group(1) if match else None


def rebuild(args):
    """Rebuild the two-core simulator so a stale binary can never be used by mistake."""
    command = ["bash", str(ROOT / "build_champsim.sh"), "no", "multi", "no", "2"]
    print(f"Rebuilding simulator: {' '.join(command)}", flush=True)
    with (args.output / "build.log").open("w") as out:
        result = subprocess.run(command, cwd=ROOT, stdout=out,
                                stderr=subprocess.STDOUT, timeout=args.timeout)
    text = (args.output / "build.log").read_text()
    if result.returncode != 0 or "successfully built" not in text:
        raise SystemExit(f"build failed (rc={result.returncode}); see {args.output / 'build.log'}")
    if not args.binary.is_file():
        raise SystemExit(f"build reported success but binary missing: {args.binary}")
    print(f"  build ok -> {args.binary}", flush=True)


def run_simulation(args, label, knobs, instructions=None):
    """Run one simulation; return (stdout, interval_csv_path)."""
    log = args.output / f"{label}.steering.csv"
    command = [
        str(args.binary), f"--config={args.config}",
        "--l2c_prefetcher_types=scooby",
        f"--warmup_instructions={args.warmup}",
        f"--simulation_instructions={instructions if instructions is not None else args.instructions}",
        f"--steering_log={log}", f"--steering_epoch={args.epoch}",
        f"--steering_victim_core={args.victim_core}",
        f"--steering_attacker_core={args.attacker_core}",
        "--steering_stop_at_victim=true",
        "--steering_disable_attacker_prefetch=true",
        *([f"--scooby_epsilon={args.epsilon}"] if args.epsilon is not None else []),
        *knobs,
        "-traces", str(args.victim), str(args.attacker),
    ]
    (args.output / f"{label}.cmd").write_text(" ".join(command) + "\n")
    started = time.time()
    with (args.output / f"{label}.log").open("w") as out:
        result = subprocess.run(command, cwd=ROOT, stdout=out,
                                stderr=subprocess.STDOUT, timeout=args.timeout)
    text = (args.output / f"{label}.log").read_text()
    if result.returncode != 0 or "ChampSim completed" not in text:
        raise SystemExit(f"[{label}] simulation failed (rc={result.returncode}); see {args.output / (label + '.log')}")
    print(f"  [{label}] done in {time.time() - started:.0f}s", flush=True)
    return text, log


def target_share_trajectory(csv_path, victim_core, target_action):
    """Per-interval victim share of the target action, in selection order."""
    shares = []
    with csv_path.open() as stream:
        for row in csv.DictReader(stream):
            if int(row["core"]) != victim_core:
                continue
            decisions = int(row["decisions"])
            if decisions == 0:
                continue
            count = 0
            for item in row["action_histogram"].split(";"):
                if item:
                    action, n = item.split(":")
                    if int(action) == target_action:
                        count += int(n)
            shares.append(count / decisions)
    return shares


def drift(shares):
    """Last-third minus first-third mean target-action share (the steering signal)."""
    if len(shares) < 3:
        return None
    third = max(1, len(shares) // 3)
    first = sum(shares[:third]) / third
    last = sum(shares[-third:]) / third
    return {"first_third": first, "last_third": last, "drift": last - first,
            "intervals": len(shares)}


def summarize(text, csv_path, args):
    shares = target_share_trajectory(csv_path, args.victim_core, args.target_action)
    return {
        "target_action_drift": drift(shares),
        "oracle_loud_fraction": float(metric(text, "steering_oracle_attacker_loud_fraction") or "nan"),
        "oracle_requests": int(metric(text, "steering_oracle_attacker_requests") or 0),
        "oracle_suppressed": int(metric(text, "steering_oracle_attacker_suppressed") or 0),
        "victim_ipc": float(metric(text, f"Core_{args.victim_core}_IPC") or "nan"),
        "q_hash_at_roi": metric(text, f"Steering_core_{args.victim_core}_q_hash_at_roi"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, required=True, help="new output directory")
    parser.add_argument("--binary", type=Path, default=DEFAULT_BINARY)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--victim", type=Path, default=DEFAULT_VICTIM)
    parser.add_argument("--attacker", type=Path, default=DEFAULT_ATTACKER,
                        help="always-loud co-runner trace on core 1")
    parser.add_argument("--target-action", type=int, default=0,
                        help="action value to steer the victim toward (default 0 = no-prefetch)")
    parser.add_argument("--warmup", type=int, default=50_000_000)
    parser.add_argument("--instructions", type=int, default=200_000_000)
    parser.add_argument("--epoch", type=int, default=100_000, help="logging interval in global cycles")
    parser.add_argument("--victim-core", type=int, default=0)
    parser.add_argument("--attacker-core", type=int, default=1)
    parser.add_argument("--reward-ablation", action="store_true",
                        help="also run a conditional+HBW-reward-disabled learning condition")
    parser.add_argument("--epsilon", type=float, default=None,
                        help="override scooby_epsilon (exploration); omit to use the config value")
    parser.add_argument("--diagnose", action="store_true",
                        help="run ONE no-oracle sim and report the victim's action distribution, then exit; "
                             "use this to choose a steering target before the full matrix")
    parser.add_argument("--diagnose-instructions", type=int, default=20_000_000,
                        help="measured instructions for --diagnose (default 20M; full matrix uses --instructions)")
    parser.add_argument("--skip-build", action="store_true",
                        help="do NOT rebuild the simulator first (default is to rebuild, so a stale binary is never used)")
    parser.add_argument("--timeout", type=int, default=86_400)
    args = parser.parse_args()

    args.output.mkdir(parents=True, exist_ok=False)
    if args.skip_build:
        if not args.binary.is_file():
            raise SystemExit(f"binary missing: {args.binary} (build with: bash build_champsim.sh no multi no 2, or drop --skip-build)")
    else:
        rebuild(args)

    if args.diagnose:
        print(f"Diagnostic: {args.warmup:,}-instruction warmup, then {args.diagnose_instructions:,} measured, "
              "no oracle, loud attacker present.", flush=True)
        text, csv_path = run_simulation(args, "diagnose", [], instructions=args.diagnose_instructions)
        counts = {}
        total = 0
        with csv_path.open() as stream:
            for row in csv.DictReader(stream):
                if int(row["core"]) != args.victim_core:
                    continue
                for item in row["action_histogram"].split(";"):
                    if item:
                        action, n = item.split(":")
                        counts[int(action)] = counts.get(int(action), 0) + int(n)
                        total += int(n)
        dist = sorted(({"action": a, "count": c, "share": c / total} for a, c in counts.items()),
                      key=lambda d: d["count"], reverse=True)
        (args.output / "action_distribution.json").write_text(
            json.dumps({"victim": str(args.victim), "attacker": str(args.attacker),
                        "warmup": args.warmup, "measured": args.diagnose_instructions,
                        "total_decisions": total, "distribution": dist}, indent=2))
        print(f"\nVictim action distribution over {total:,} decisions:")
        print(f"  {'action':>7} {'share':>9} {'count':>12}")
        for d in dist:
            print(f"  {d['action']:>7} {d['share']:>9.4f} {d['count']:>12,}")
        minority = [d for d in dist if 0.0005 <= d["share"] <= 0.25]
        print("\nCandidate steering targets (low-but-nonzero share, growable):",
              ", ".join(str(d["action"]) for d in minority) or "(none in range)")
        print(f"\nWrote {args.output / 'action_distribution.json'}")
        return

    cond = [f"--steering_oracle_enable=true", "--steering_oracle_mode=conditional",
            f"--steering_oracle_target_action={args.target_action}"]
    results = {}

    print("Treatment (conditional gate):", flush=True)
    t_learn, t_learn_csv = run_simulation(args, "treat_learn", cond + ["--steering_freeze_learning=false"])
    t_frozen, t_frozen_csv = run_simulation(args, "treat_frozen", cond + ["--steering_freeze_learning=true"])
    results["treat_learn"] = summarize(t_learn, t_learn_csv, args)
    results["treat_frozen"] = summarize(t_frozen, t_frozen_csv, args)

    # Budget-matched control: same suppressed fraction as treat_learn, but
    # action-independent, to separate learned steering from contention volume.
    suppressed_fraction = 1.0 - results["treat_learn"]["oracle_loud_fraction"]
    budget_num = max(0, min(BUDGET_DEN, round(suppressed_fraction * BUDGET_DEN)))
    budget = ["--steering_oracle_enable=true", "--steering_oracle_mode=budget",
              f"--steering_oracle_budget_num={budget_num}", f"--steering_oracle_budget_den={BUDGET_DEN}"]
    print(f"Budget control (matched suppressed fraction {suppressed_fraction:.4f}):", flush=True)
    c_learn, c_learn_csv = run_simulation(args, "control_learn", budget + ["--steering_freeze_learning=false"])
    c_frozen, c_frozen_csv = run_simulation(args, "control_frozen", budget + ["--steering_freeze_learning=true"])
    results["control_learn"] = summarize(c_learn, c_learn_csv, args)
    results["control_frozen"] = summarize(c_frozen, c_frozen_csv, args)

    if args.reward_ablation:
        print("Reward-ablation (conditional, HBW reward disabled after warmup):", flush=True)
        a_learn, a_learn_csv = run_simulation(
            args, "ablation_learn",
            cond + ["--steering_freeze_learning=false", "--steering_disable_hbw_reward_after_warmup=true"])
        results["ablation_learn"] = summarize(a_learn, a_learn_csv, args)

    summary = {
        "target_action": args.target_action,
        "victim": str(args.victim), "attacker": str(args.attacker),
        "warmup": args.warmup, "instructions": args.instructions,
        "budget_matched_suppressed_fraction": suppressed_fraction,
        "conditions": results,
    }
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2))

    # Human-readable headline table.
    lines = [f"# Oracle steering result (target action {args.target_action})", "",
             f"victim: `{args.victim.name}`  attacker: `{args.attacker.name}`  "
             f"warmup {args.warmup:,} / measured {args.instructions:,}", "",
             "| condition | target-action share: first third | last third | drift | victim IPC |",
             "| --- | ---: | ---: | ---: | ---: |"]
    for name, r in results.items():
        d = r["target_action_drift"]
        if d:
            lines.append(f"| {name} | {d['first_third']:.3f} | {d['last_third']:.3f} | "
                         f"{d['drift']:+.3f} | {r['victim_ipc']:.3f} |")
        else:
            lines.append(f"| {name} | (too few intervals) | | | {r['victim_ipc']:.3f} |")
    lines += ["", "Headline: a large positive drift in `treat_learn` with ~0 drift in "
              "`treat_frozen` and a much smaller drift in `control_learn` is learned, "
              "action-conditional steering rather than a contention-volume artifact.",
              "Q fingerprints must match at end of warmup across conditions; frozen runs "
              "apply zero Q updates. Confirm both in the per-condition logs before claiming a result."]
    (args.output / "report.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\nWrote {args.output / 'summary.json'} and {args.output / 'report.md'}")


if __name__ == "__main__":
    main()
