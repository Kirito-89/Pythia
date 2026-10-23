#!/usr/bin/env python3
"""Directional-specificity sweep: does the oracle steer Pythia to a CHOSEN action,
or does the victim just adapt to high bandwidth regardless of the attacker's target?

For each victim it (1) diagnoses the post-warmup action distribution, (2) picks the
two most growable minority actions T and D, (3) runs the conditional gate targeting
each (one or more seeds), and (4) builds the 2x2 specificity matrix: the drift of
action T and action D in selection share, under "target T" vs "target D".

If targeting is real and direction-specific, the diagonal dominates: targeting T
grows T more than targeting D grows T, and vice versa. If targeting is a myth and
the drift is just contention adaptation, the COLUMNS are flat: an action grows the
same no matter what we target. A frozen-learning run per victim confirms the drift
is learning-caused (reward poisoning) rather than a mechanical contention artifact.

Long runs: hand the printed command to a machine with time. See --help for scope.
"""
import argparse
import csv
import json
import statistics as st
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BINARY = ROOT / "bin/perceptron-no-multi-no-ship-2core"
DEFAULT_CONFIG = ROOT / "config/pythia.ini"
DEFAULT_ATTACKER = ROOT / "dpc3_traces/470.lbm-1274B.champsimtrace.xz"


def short(path):
    return Path(path).name.split(".champsimtrace")[0]


def build(binary, timeout, log):
    command = ["bash", str(ROOT / "build_champsim.sh"), "no", "multi", "no", "2"]
    print(f"Rebuilding: {' '.join(command)}", flush=True)
    with log.open("w") as out:
        r = subprocess.run(command, cwd=ROOT, stdout=out, stderr=subprocess.STDOUT, timeout=timeout)
    if r.returncode != 0 or "successfully built" not in log.read_text():
        raise SystemExit(f"build failed; see {log}")
    if not Path(binary).is_file():
        raise SystemExit(f"build ok but binary missing: {binary}")
    print(f"  build ok -> {binary}", flush=True)


def run_sim(cfg, label, extra_knobs):
    """Run one simulation; return (stdout, interval_csv_path)."""
    log_csv = cfg["dir"] / f"{label}.steering.csv"
    command = [
        str(cfg["binary"]), f"--config={cfg['config']}", "--l2c_prefetcher_types=scooby",
        f"--warmup_instructions={cfg['warmup']}", f"--simulation_instructions={cfg['instructions']}",
        f"--steering_log={log_csv}", f"--steering_epoch={cfg['epoch']}",
        "--steering_victim_core=0", "--steering_attacker_core=1",
        "--steering_stop_at_victim=true", "--steering_disable_attacker_prefetch=true",
        f"--scooby_epsilon={cfg['epsilon']}", f"--scooby_seed={cfg['seed']}",
        *extra_knobs,
        "-traces", str(cfg["victim"]), str(cfg["attacker"]),
    ]
    (cfg["dir"] / f"{label}.cmd").write_text(" ".join(command) + "\n")
    started = time.time()
    with (cfg["dir"] / f"{label}.log").open("w") as out:
        r = subprocess.run(command, cwd=ROOT, stdout=out, stderr=subprocess.STDOUT, timeout=cfg["timeout"])
    text = (cfg["dir"] / f"{label}.log").read_text()
    if r.returncode != 0 or "ChampSim completed" not in text:
        raise SystemExit(f"[{label}] simulation failed (rc={r.returncode}); see {cfg['dir'] / (label + '.log')}")
    print(f"    [{label}] {time.time() - started:.0f}s", flush=True)
    return text, log_csv


def metric(text, name):
    import re
    m = re.search(rf"^{re.escape(name)}\s+(\S+)\s*$", text, re.MULTILINE)
    return m.group(1) if m else None


def action_counts(csv_path):
    """Per-interval (count_by_action, decisions) for the victim core, in order."""
    out = []
    with csv_path.open() as stream:
        for row in csv.DictReader(stream):
            if int(row["core"]) != 0:
                continue
            dec = int(row["decisions"])
            if dec == 0:
                continue
            counts = {}
            for item in row["action_histogram"].split(";"):
                if item:
                    a, n = item.split(":")
                    counts[int(a)] = counts.get(int(a), 0) + int(n)
            out.append((counts, dec))
    return out


def share_drift(csv_path, action):
    """Last-third minus first-third mean selection share of `action` (the steering signal)."""
    intervals = action_counts(csv_path)
    if len(intervals) < 3:
        return None
    shares = [c.get(action, 0) / d for c, d in intervals]
    third = max(1, len(shares) // 3)
    return sum(shares[-third:]) / third - sum(shares[:third]) / third


def diagnose_targets(cfg):
    """Run one no-oracle sim and return the sorted action distribution + picked (T, D)."""
    text, csv_path = run_sim(
        {**cfg, "instructions": cfg["diagnose_instructions"]}, "diagnose", [])
    totals, grand = {}, 0
    for counts, _ in action_counts(csv_path):
        for a, n in counts.items():
            totals[a] = totals.get(a, 0) + n
            grand += n
    dist = sorted(({"action": a, "count": c, "share": c / grand} for a, c in totals.items()),
                  key=lambda d: d["count"], reverse=True)
    # growable = selected sometimes but not dominant; prefer the most-selected such actions
    growable = [d["action"] for d in dist if 0.0005 <= d["share"] <= 0.25]
    return dist, grand, growable


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--output", type=Path, required=True, help="new output directory")
    p.add_argument("--victims", nargs="+", required=True, help="victim trace paths (core 0)")
    p.add_argument("--attacker", type=Path, default=DEFAULT_ATTACKER, help="fixed loud co-runner (core 1)")
    p.add_argument("--seeds", nargs="+", type=int, default=[1], help="scooby_seed values (Stage A: one)")
    p.add_argument("--warmup", type=int, default=50_000_000)
    p.add_argument("--instructions", type=int, default=100_000_000)
    p.add_argument("--diagnose-instructions", type=int, default=20_000_000)
    p.add_argument("--epsilon", type=float, default=0.02)
    p.add_argument("--epoch", type=int, default=100_000)
    p.add_argument("--targets", nargs=2, type=int, default=None,
                   help="override auto-picked T D (applied to every victim)")
    p.add_argument("--binary", type=Path, default=DEFAULT_BINARY)
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--skip-build", action="store_true")
    p.add_argument("--timeout", type=int, default=86_400)
    args = p.parse_args()

    args.output.mkdir(parents=True, exist_ok=False)
    if not args.skip_build:
        build(args.binary, args.timeout, args.output / "build.log")
    elif not args.binary.is_file():
        raise SystemExit(f"binary missing: {args.binary} (drop --skip-build)")

    sweep = {"attacker": str(args.attacker), "epsilon": args.epsilon,
             "warmup": args.warmup, "instructions": args.instructions,
             "seeds": args.seeds, "victims": {}}

    for victim in args.victims:
        name = short(victim)
        vdir = args.output / name
        vdir.mkdir()
        print(f"\n=== victim {name} ===", flush=True)
        base = {"binary": args.binary, "config": args.config, "victim": Path(victim),
                "attacker": args.attacker, "warmup": args.warmup, "instructions": args.instructions,
                "diagnose_instructions": args.diagnose_instructions, "epsilon": args.epsilon,
                "epoch": args.epoch, "timeout": args.timeout, "dir": vdir, "seed": args.seeds[0]}

        dist, grand, growable = diagnose_targets(base)
        if args.targets:
            T, D = args.targets
        elif len(growable) >= 2:
            T, D = growable[0], growable[1]
        elif len(growable) == 1:
            # only one growable action; use the rarest observed action as the decoy
            T = growable[0]
            D = min((d["action"] for d in dist if d["action"] != T), key=lambda a: a, default=0)
        else:
            print(f"  no growable minority action for {name}; recording distribution only", flush=True)
            (vdir / "victim_summary.json").write_text(json.dumps(
                {"victim": victim, "total_decisions": grand, "distribution": dist,
                 "note": "no growable minority target; victim policy too concentrated"}, indent=2))
            sweep["victims"][name] = {"target_T": None, "target_D": None, "distribution": dist[:6]}
            continue
        print(f"  targets: T={T}, D={D}  (from {grand:,} diagnostic decisions)", flush=True)

        # specificity runs: conditional gate targeting T and D, each seed; one frozen for causation
        cross = {("T", "T"): [], ("T", "D"): [], ("D", "T"): [], ("D", "D"): []}
        q_by_seed = {}
        for seed in args.seeds:
            cfg = {**base, "seed": seed}
            for tag, target in (("T", T), ("D", D)):
                text, csvp = run_sim(cfg, f"seed{seed}_target{tag}_learn",
                                     ["--steering_oracle_enable=true", "--steering_oracle_mode=conditional",
                                      f"--steering_oracle_target_action={target}",
                                      "--steering_freeze_learning=false"])
                q_by_seed.setdefault(seed, {})[tag] = metric(text, "Steering_core_0_q_hash_at_roi")
                cross[(tag, "T")].append(share_drift(csvp, T))
                cross[(tag, "D")].append(share_drift(csvp, D))
        # learning-causation control: frozen gate targeting T at the first seed
        ftext, fcsv = run_sim({**base, "seed": args.seeds[0]}, f"seed{args.seeds[0]}_targetT_frozen",
                              ["--steering_oracle_enable=true", "--steering_oracle_mode=conditional",
                               f"--steering_oracle_target_action={T}", "--steering_freeze_learning=true"])
        frozen_T_drift = share_drift(fcsv, T)

        def agg(key):
            vals = [v for v in cross[key] if v is not None]
            if not vals:
                return {"mean": None, "std": None, "n": 0}
            return {"mean": st.mean(vals), "std": (st.pstdev(vals) if len(vals) > 1 else 0.0), "n": len(vals)}

        victim_result = {
            "victim": victim, "target_T": T, "target_D": D,
            "total_diagnostic_decisions": grand, "distribution": dist[:8],
            "specificity_matrix": {f"target_{r}_measure_{c}": agg((r, c))
                                   for r in ("T", "D") for c in ("T", "D")},
            "frozen_targetT_drift_T": frozen_T_drift,
            "q_hash_at_roi_matches_within_seed": {
                str(s): (len(set(d.values())) == 1) for s, d in q_by_seed.items()},
        }
        (vdir / "victim_summary.json").write_text(json.dumps(victim_result, indent=2))
        sweep["victims"][name] = victim_result

        # per-victim readable table
        m = victim_result["specificity_matrix"]
        def cell(r, c):
            a = m[f"target_{r}_measure_{c}"]
            return "   n/a" if a["mean"] is None else f"{a['mean']:+.4f}±{a['std']:.4f}"
        lines = [f"# Directional specificity — {name}", "",
                 f"attacker `{short(args.attacker)}`, eps {args.epsilon}, "
                 f"warmup {args.warmup:,} / measured {args.instructions:,}, seeds {args.seeds}",
                 f"T = {T}, D = {D} (the two most-selected minority actions)", "",
                 "treat_learn drift (last-third minus first-third selection share), mean±std over seeds:", "",
                 f"| | measure T={T} | measure D={D} |",
                 "| --- | ---: | ---: |",
                 f"| **target T={T}** | {cell('T','T')} | {cell('T','D')} |",
                 f"| **target D={D}** | {cell('D','T')} | {cell('D','D')} |", "",
                 f"frozen (target T, no learning) drift of T: {frozen_T_drift:+.4f}  "
                 "(≈0 expected → drift is learning-caused)", "",
                 "Read: diagonal >> off-diagonal (and column-varying) ⇒ real targeting. "
                 "Flat columns (an action grows the same whatever is targeted) ⇒ contention "
                 "adaptation, not targeting."]
        (vdir / "victim_report.md").write_text("\n".join(lines) + "\n")
        print("\n".join(lines), flush=True)

    (args.output / "sweep_summary.json").write_text(json.dumps(sweep, indent=2))
    print(f"\nWrote {args.output / 'sweep_summary.json'} and per-victim reports.", flush=True)


if __name__ == "__main__":
    main()
