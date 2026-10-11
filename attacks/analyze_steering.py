#!/usr/bin/env python3
"""Analyze victim-only steering windows and matched quiet controls (no trace replay)."""
import argparse
import csv
import json
import math
import os
from pathlib import Path
import re
import sys

COUNTS = (
    "instructions", "decisions", "zero_action", "degree_sum", "predicted_lines",
    "issued_prefetches", "useful_prefetches", "late_prefetches", "useless_prefetches",
    "llc_load_misses", "llc_load_accesses", "train_attempts", "updates", "frozen_updates",
    "rewards", "reward_sum", "high_bw_rewards", "bw_level_0", "bw_level_1",
    "bw_level_2", "bw_level_3", "dram_requests", "core_dram_requests",
)
PHASES = ("pre", "pressure", "off", "post")
METRICS = ("ipc", "zero_action_fraction", "mean_degree", "prefetches_per_ki",
           "high_bw_fraction", "llc_load_mpki", "high_bw_reward_fraction")


def ratio(a, b):
    return a / b if b else None


def read_intervals(path, core=0):
    """Validate the measured CSV; counters are per interval, never cumulative."""
    rows = []
    with Path(path).open(newline="") as stream:
        reader = csv.DictReader(stream)
        required = {"core", "cycle_begin", "cycle_end", "phase", "pulse", "q_hash",
                    "action_histogram", *COUNTS}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path}: missing columns: {', '.join(sorted(missing))}")
        for line, raw in enumerate(reader, 2):
            if int(raw["core"]) != core:
                continue
            row = dict(raw)
            for field in (*COUNTS, "core", "cycle_begin", "cycle_end", "pulse"):
                row[field] = int(raw[field])
            row["cycles"] = row["cycle_end"] - row["cycle_begin"]
            if row["cycles"] <= 0:
                raise ValueError(f"{path}:{line}: nonpositive interval")
            if rows and row["cycle_begin"] != rows[-1]["cycle_end"]:
                raise ValueError(f"{path}:{line}: victim intervals overlap or have a gap")
            if any(row[field] < 0 for field in COUNTS if field != "reward_sum"):
                raise ValueError(f"{path}:{line}: negative event counter")
            actions = {}
            for item in raw["action_histogram"].split(";"):
                if item:
                    offset, count = item.split(":")
                    actions[int(offset)] = int(count)
            if sum(actions.values()) != row["decisions"]:
                raise ValueError(f"{path}:{line}: action counts do not equal decisions")
            if actions.get(0, 0) != row["zero_action"]:
                raise ValueError(f"{path}:{line}: zero action count disagrees with histogram")
            row["actions"] = actions
            row["dram_rq_mean"] = float(raw.get("dram_rq_mean", 0))
            rows.append(row)
    if not rows:
        raise ValueError(f"{path}: no victim intervals")
    return rows


def summarize(rows):
    total = {name: sum(row[name] for row in rows) for name in COUNTS}
    total["cycles"] = sum(row["cycles"] for row in rows)
    total["windows"] = len(rows)
    total["ipc"] = ratio(total["instructions"], total["cycles"])
    total["zero_action_fraction"] = ratio(total["zero_action"], total["decisions"])
    total["mean_degree"] = ratio(total["degree_sum"], total["decisions"])
    total["prefetches_per_ki"] = ratio(1000 * total["issued_prefetches"], total["instructions"])
    total["llc_load_mpki"] = ratio(1000 * total["llc_load_misses"], total["instructions"])
    total["llc_load_miss_fraction"] = ratio(total["llc_load_misses"], total["llc_load_accesses"])
    total["high_bw_fraction"] = ratio(total["bw_level_2"] + total["bw_level_3"],
                                      sum(total[f"bw_level_{i}"] for i in range(4)))
    total["high_bw_reward_fraction"] = ratio(total["high_bw_rewards"], total["rewards"])
    total["dram_rq_mean"] = ratio(sum(row["dram_rq_mean"] * row["cycles"] for row in rows),
                                   total["cycles"])
    actions = {}
    for row in rows:
        for offset, count in row["actions"].items():
            actions[offset] = actions.get(offset, 0) + count
    total["actions"] = actions
    return total


def window_key(row):
    return row["cycle_begin"], row["cycle_end"], row["phase"], row["pulse"]


def matched_windows(attack, quiet):
    """Keep exact common intervals; never interpolate indivisible event counts."""
    left = {window_key(row): row for row in attack}
    right = {window_key(row): row for row in quiet}
    keys = sorted(left.keys() & right.keys())
    return [left[k] for k in keys], [right[k] for k in keys]


def late_cooldown(attack, quiet):
    """Exclude the pressure drain and any interval with attacker DRAM requests."""
    post = [row for row in attack if row["phase"] == "post"]
    if not post:
        return [], []
    start = post[0]["cycle_begin"] + 100_000
    keep = [i for i, row in enumerate(attack)
            if row["phase"] == "post" and row["cycle_begin"] >= start
            and row["dram_requests"] == row["core_dram_requests"]]
    return [attack[i] for i in keep], [quiet[i] for i in keep]


def settled_off(attack, quiet, pulse):
    """Compare a quiet gap after request drain in identical cycle windows."""
    gap = [row for row in attack if row["phase"] == "off" and row["pulse"] == pulse]
    if not gap:
        return [], []
    start = gap[0]["cycle_begin"] + 100_000
    keep = [i for i, row in enumerate(attack)
            if row["phase"] == "off" and row["pulse"] == pulse
            and row["cycle_begin"] >= start
            and row["dram_requests"] == row["core_dram_requests"]]
    return [attack[i] for i in keep], [quiet[i] for i in keep]


def metric_changes(attack, quiet):
    change = {}
    for metric in METRICS:
        a, q = attack[metric], quiet[metric]
        change[metric + "_delta"] = a - q if a is not None and q is not None else None
    change["slowdown_pct"] = ((quiet["ipc"] / attack["ipc"] - 1) * 100
                              if attack["ipc"] and quiet["ipc"] else None)
    return change


def read_log(path):
    content = Path(path).read_text(errors="replace")
    if "ChampSim completed all CPUs" not in content and "ChampSim completed victim CPU 0" not in content:
        raise ValueError(f"{path}: completion marker missing")
    if "Repeating trace" in content or "STEERING_TRACE_EOF" in content:
        raise ValueError(f"{path}: trace replay/EOF invalidates this experiment")
    matches = re.findall(r"^Core_0_IPC\s+([0-9.eE+-]+)", content, re.MULTILINE)
    hashes = re.findall(r"^Steering_core_0_q_hash_at_roi\s+(\S+)", content, re.MULTILINE)
    return {"reported_victim_ipc": float(matches[-1]) if matches else None,
            "q_hash_at_roi": hashes[-1] if hashes else None}


def number(value, digits=4):
    return "n/a" if value is None else f"{value:.{digits}f}"


def write_csv(path, rows):
    if not rows:
        return
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def make_plots(directory, pairs, data):
    # MPLCONFIGDIR must be writable in restricted/shared execution environments.
    os.environ.setdefault("MPLCONFIGDIR", str(directory / "matplotlib-cache"))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    figures = []
    for pair in pairs:
        name = pair["name"]
        fig, axes = plt.subplots(4, 1, figsize=(11, 10), sharex=True, constrained_layout=True)
        for run_id, label, color in ((pair["attack"], "Pressure application", "#bd4035"),
                                     (pair["quiet"], "Quiet control", "#2469a0")):
            rows = data[run_id]
            points = [summarize([row]) for row in rows]
            x = [row["cycle_end"] / 1e6 for row in rows]
            for ax, metric in zip(axes, ("ipc", "prefetches_per_ki", "zero_action_fraction", "high_bw_fraction")):
                y = [point[metric] if point[metric] is not None else math.nan for point in points]
                ax.plot(x, y, color=color, label=label, linewidth=0.9, alpha=0.9)
        attack_rows = data[pair["attack"]]
        pressure = [r for r in attack_rows if r["phase"] == "pressure"]
        pressure_pulses = sorted({r["pulse"] for r in pressure})
        for ax, ylabel in zip(axes, ("Victim IPC", "Issued prefetches / 1k instr.", "Zero-action fraction", "High-BW sample fraction")):
            for pulse in pressure_pulses:
                windows = [r for r in pressure if r["pulse"] == pulse]
                ax.axvspan(windows[0]["cycle_begin"] / 1e6, windows[-1]["cycle_end"] / 1e6,
                           color="#f0c33c", alpha=0.18,
                           label="Pressure phase" if ax is axes[0] and pulse == pressure_pulses[0] else None)
            ax.set_ylabel(ylabel)
            ax.grid(alpha=0.2)
        axes[0].legend(loc="best", fontsize=9)
        axes[0].set_title(name + " — victim-only measured windows")
        axes[-1].set_xlabel("Global simulator cycles (millions)")
        path = directory / f"{name}-timeline.png"
        fig.savefig(path, dpi=160)
        plt.close(fig)
        figures.append(path.name)

        left, right = matched_windows(data[pair["attack"]], data[pair["quiet"]])
        fig, axes = plt.subplots(1, 3, figsize=(14, 3.8), constrained_layout=True)
        for ax, phase in zip(axes, ("pre", "pressure", "post")):
            a = summarize([r for r in left if r["phase"] == phase])
            q = summarize([r for r in right if r["phase"] == phase])
            offsets = sorted(set(a["actions"]) | set(q["actions"]))
            x = list(range(len(offsets)))
            ax.bar([v - .2 for v in x], [a["actions"].get(k, 0) / a["decisions"] if a["decisions"] else 0 for k in offsets],
                   width=.4, label="Pressure app", color="#bd4035")
            ax.bar([v + .2 for v in x], [q["actions"].get(k, 0) / q["decisions"] if q["decisions"] else 0 for k in offsets],
                   width=.4, label="Quiet control", color="#2469a0")
            ax.set_xticks(x, [str(k) for k in offsets], rotation=65)
            ax.set_title(phase + " (matched cycles)")
            ax.set_xlabel("Selected prefetch offset (0 = no prefetch)")
            ax.set_ylabel("Fraction of victim decisions")
            ax.grid(axis="y", alpha=.2)
        axes[0].legend(fontsize=8)
        path = directory / f"{name}-actions.png"
        fig.savefig(path, dpi=160)
        plt.close(fig)
        figures.append(path.name)
    return figures


def analyze(experiment, plots=True):
    experiment = Path(experiment).resolve()
    manifest = json.loads((experiment / "experiment.json").read_text())
    pairs = manifest["pairs"]
    if not pairs:
        raise ValueError("experiment has no completed pairs")
    data, run_totals, phase_totals = {}, {}, []
    for run in manifest["runs"]:
        run_id = run["name"]
        rows = read_intervals(experiment / run["intervals"], manifest.get("victim_core", 0))
        log = read_log(experiment / run["log"])
        if not any(r["phase"] == "pressure" for r in rows) or not any(r["phase"] == "post" for r in rows):
            raise ValueError(f"{run_id}: measured pressure and cooldown are both required; adjust trace schedule/run length")
        if run["control"] == "frozen":
            if sum(r["updates"] for r in rows):
                raise ValueError(f"{run_id}: frozen control applied learning updates")
            hashes = {r["q_hash"] for r in rows}
            if log["q_hash_at_roi"] is not None:
                hashes.add(log["q_hash_at_roi"])
            if len(hashes) > 1:
                raise ValueError(f"{run_id}: frozen Q-table checksum changed")
        data[run_id] = rows
        run_totals[run_id] = {**summarize(rows), **log, "frozen_hash_constant":
                              len({r["q_hash"] for r in rows}) == 1 if run["control"] == "frozen" else None}
        for phase in dict.fromkeys(r["phase"] for r in rows):
            values = summarize([r for r in rows if r["phase"] == phase])
            phase_totals.append({"run": run_id, "phase": phase,
                                 **{k: v for k, v in values.items() if k != "actions"}})
    comparisons = []
    pulse_comparisons = []
    interval_targets = []
    pressure_goal_targets = []
    params = manifest.get("parameters", {})
    min_slowdown = float(params.get("min_slowdown_pct", 1.0))
    min_speedup = float(params.get("min_speedup_pct", 1.0))
    pulse_goals = params.get("pulse_goals")
    pulse_goals = pulse_goals.split(",") if pulse_goals else None
    for pair in pairs:
        attack, quiet = matched_windows(data[pair["attack"]], data[pair["quiet"]])
        for phase in ("all", "pre", "pressure", "post", "late_post"):
            if phase == "late_post":
                a_rows, q_rows = late_cooldown(attack, quiet)
            else:
                a_rows = [r for r in attack if phase == "all" or r["phase"] == phase]
                q_rows = [r for r in quiet if phase == "all" or r["phase"] == phase]
            if phase in ("pressure", "post", "late_post") and not a_rows:
                raise ValueError(f"{pair['name']}: no matched {phase} windows; lengthen this phase")
            a, q = summarize(a_rows), summarize(q_rows)
            comparisons.append({"pair": pair["name"], "mode": pair["mode"], "control": pair["control"],
                                "phase": phase, "matched_cycles": a["cycles"], "matched_windows": a["windows"],
                                "attack": a, "quiet": q, **metric_changes(a, q)})
        pulses = sorted({row["pulse"] for row in attack if row["phase"] == "pressure"})
        for pulse in pulses:
            windows = {}
            for phase in ("pressure", "off", "settled_off"):
                if phase == "settled_off":
                    a_rows, q_rows = settled_off(attack, quiet, pulse)
                else:
                    a_rows = [row for row in attack if row["phase"] == phase and row["pulse"] == pulse]
                    q_rows = [row for row in quiet if row["phase"] == phase and row["pulse"] == pulse]
                if not a_rows:
                    continue
                a, q = summarize(a_rows), summarize(q_rows)
                row = {"pair": pair["name"], "mode": pair["mode"], "control": pair["control"],
                       "pulse": pulse, "phase": phase, "matched_cycles": a["cycles"],
                       "matched_windows": a["windows"], "attack_ipc": a["ipc"], "quiet_ipc": q["ipc"],
                       "attacker_dram_requests": a["dram_requests"] - a["core_dram_requests"],
                       **metric_changes(a, q)}
                pulse_comparisons.append(row)
                windows[phase] = row
            if "pressure" not in windows:
                raise ValueError(f"{pair['name']}: missing matched pressure windows for pulse {pulse}")
            pressure_slowdown = windows["pressure"]["slowdown_pct"]
            speedup = -windows["settled_off"]["slowdown_pct"] if "settled_off" in windows else None
            if pulse_goals is not None:
                if pulse >= len(pulse_goals):
                    raise ValueError(f"{pair['name']}: pulse {pulse} has no declared performance goal")
                goal = pulse_goals[pulse]
                if goal not in ("speedup", "slowdown"):
                    raise ValueError(f"unknown pressure goal: {goal}")
                achieved = -pressure_slowdown if goal == "speedup" else pressure_slowdown
                threshold = min_speedup if goal == "speedup" else min_slowdown
                pressure_goal_targets.append({"pair": pair["name"], "pulse": pulse,
                                              "goal": goal, "achieved_pct": achieved,
                                              "required_pct": threshold,
                                              "goal_met": achieved >= threshold})
            interval_targets.append({"pair": pair["name"], "pulse": pulse,
                                     "pressure_slowdown_pct": pressure_slowdown,
                                     "settled_off_speedup_pct": speedup,
                                     "pressure_target_met": pressure_slowdown >= min_slowdown,
                                     "speedup_target_met": speedup is not None and speedup >= min_speedup,
                                     "both_targets_met": pressure_slowdown >= min_slowdown and
                                     speedup is not None and speedup >= min_speedup})
    interactions = []
    for mode in dict.fromkeys(p["mode"] for p in pairs):
        for phase in ("pressure", "post", "late_post"):
            choices = {c["control"]: c for c in comparisons if c["mode"] == mode and c["phase"] == phase}
            if "learned" in choices and "frozen" in choices:
                values = {"mode": mode, "phase": phase}
                for metric in METRICS:
                    learned = choices["learned"][metric + "_delta"]
                    frozen = choices["frozen"][metric + "_delta"]
                    values[metric + "_interaction"] = learned - frozen if learned is not None and frozen is not None else None
                interactions.append(values)
    summary = {"runs": run_totals, "comparisons": comparisons,
               "pulse_comparisons": pulse_comparisons, "interval_targets": interval_targets,
               "pressure_goal_targets": pressure_goal_targets,
               "target_thresholds": {"slowdown_pct": min_slowdown, "speedup_pct": min_speedup},
               "learning_interactions": interactions,
               "limitations": [
                   "One deterministic run per condition; these are observed differences, not significance tests.",
                   "Retirement phase markers lag fetched and in-flight requests; early cooldown can retain contention/cache effects.",
                   "Frozen Q values leave action selection, dynamic degree, histories, caches and outstanding requests active.",
                   "Each attack/quiet pair uses matched global-cycle windows; learned/frozen pairs can have different phase timings and victim state.",
                   "Cooldown policy differences and learned/frozen interactions are evidence to investigate, not proof of persistent poisoning.",
                   "Late cooldown begins 100,000 cycles after pressure ends and excludes intervals with attacker DRAM enqueues; residual cache effects can remain.",
                   "Settled off windows begin 100,000 cycles after each quiet gap starts and exclude observed attacker DRAM enqueues; caches and learned policy are not reset.",
                   "Negative slowdown means IPC above the cycle-matched quiet baseline; a recovered IPC versus the prior pressure window alone does not count as speedup.",
                   "High-bandwidth fraction uses levels 2+3 (threshold 2). Reward-only ablation, if enabled, retains bandwidth-aware fallback/degree.",
               ]}
    summary["figures"] = make_plots(experiment, pairs, data) if plots else []
    (experiment / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    write_csv(experiment / "phase_metrics.csv", phase_totals)
    flat = []
    for c in comparisons:
        row = {k: v for k, v in c.items() if k not in ("attack", "quiet")}
        for role in ("attack", "quiet"):
            row.update({role + "_" + metric: c[role][metric] for metric in METRICS})
        flat.append(row)
    write_csv(experiment / "comparison.csv", flat)
    write_csv(experiment / "pulse_comparison.csv", pulse_comparisons)
    write_csv(experiment / "interval_targets.csv", interval_targets)
    write_csv(experiment / "pressure_goal_targets.csv", pressure_goal_targets)
    lines = ["# Steering experiment results", "", "Victim IPC and policy metrics below cover measured core 0 intervals only.", "",
             "## Complete victim measurement", "", "| Run | IPC | Issued PF / 1k instr. | Zero-action fraction | Updates |", "|---|---:|---:|---:|---:|"]
    for name, total in run_totals.items():
        lines.append(f"| {name} | {number(total['ipc'])} | {number(total['prefetches_per_ki'])} | {number(total['zero_action_fraction'])} | {total['updates']} |")
    lines += ["", "## Exact matched-cycle comparisons", "", "Positive slowdown means lower victim IPC. Policy changes are attack minus quiet.", "",
              "| Pair | Phase | Matched cycles | Slowdown % | Δ PF / 1k instr. | Δ zero-action fraction | Δ high-BW fraction |",
              "|---|---|---:|---:|---:|---:|---:|"]
    for c in comparisons:
        if c["phase"] != "all":
            lines.append(f"| {c['pair']} | {c['phase']} | {c['matched_cycles']} | {number(c['slowdown_pct'], 2)} | {number(c['prefetches_per_ki_delta'])} | {number(c['zero_action_fraction_delta'])} | {number(c['high_bw_fraction_delta'])} |")
    lines += ["", "First/last partial windows without an identical counterpart are excluded from this table. Full-run and phase totals remain in `phase_metrics.csv`.", "",
              "## Scheduled intervals", "",
              "Each pulse is compared with its own cycle-matched quiet run. Negative slowdown is a speedup **above the quiet baseline**. Settled off excludes the first 100,000 cycles and intervals with attacker DRAM enqueues.", "",
              "| Pair | Pulse | Interval | Matched cycles | Victim IPC attack | Victim IPC quiet | Slowdown % | Attacker DRAM requests |",
              "|---|---:|---|---:|---:|---:|---:|---:|"]
    for c in pulse_comparisons:
        lines.append(f"| {c['pair']} | {c['pulse']} | {c['phase']} | {c['matched_cycles']} | {number(c['attack_ipc'])} | {number(c['quiet_ipc'])} | {number(c['slowdown_pct'], 2)} | {c['attacker_dram_requests']} |")
    if not pressure_goal_targets:
        lines += ["", f"Interval targets: pressure slowdown ≥ {min_slowdown:g}% and settled-off speedup ≥ {min_speedup:g}% above quiet.", "",
                  "| Pair | Pulse | Pressure slowdown % | Settled-off speedup % | Both targets met |",
                  "|---|---:|---:|---:|---|"]
        for item in interval_targets:
            lines.append(f"| {item['pair']} | {item['pulse']} | {number(item['pressure_slowdown_pct'], 2)} | {number(item['settled_off_speedup_pct'], 2)} | {'yes' if item['both_targets_met'] else 'no'} |")
    else:
        lines += ["", "## Requested pressure directions", "",
                  "These goals were declared in the experiment command before recording. Speedup and slowdown are both measured against the cycle-matched quiet control.", "",
                  "| Pair | Pulse | Requested direction | Observed effect % | Required % | Goal met |",
                  "|---|---:|---|---:|---:|---|"]
        for item in pressure_goal_targets:
            lines.append(f"| {item['pair']} | {item['pulse']} | {item['goal']} | {number(item['achieved_pct'], 2)} | {number(item['required_pct'], 2)} | {'yes' if item['goal_met'] else 'no'} |")
        lines += ["", "The quiet-gap criterion is still available in `interval_targets.csv`; it is separate from the requested pressure directions."]
    lines += ["",
              "## Learning interaction", "", "Values below subtract the frozen-control attack effect from the learned-control attack effect. Timings/state can differ across these pairs, so these are descriptive interactions.", "",
              "| Mode | Phase | ΔΔ PF / 1k instr. | ΔΔ zero-action fraction | ΔΔ mean degree |", "|---|---|---:|---:|---:|"]
    for row in interactions:
        lines.append(f"| {row['mode']} | {row['phase']} | {number(row['prefetches_per_ki_interaction'])} | {number(row['zero_action_fraction_interaction'])} | {number(row['mean_degree_interaction'])} |")
    lines += ["", "## Interpretation limits", "", *["- " + item for item in summary["limitations"]], "", "## Figures", ""]
    lines.extend(f"![{name}]({name})" for name in summary["figures"])
    (experiment / "report.md").write_text("\n".join(lines) + "\n")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiment", type=Path, help="directory containing experiment.json")
    parser.add_argument("--no-plots", action="store_true")
    args = parser.parse_args()
    result = analyze(args.experiment, not args.no_plots)
    print(f"Analyzed {len(result['runs'])} runs. Report: {args.experiment / 'report.md'}")
    for c in result["comparisons"]:
        if c["phase"] in ("pressure", "post", "late_post"):
            print(f"{c['pair']} {c['phase']}: slowdown {number(c['slowdown_pct'], 2)}%; "
                  f"issued PF/ki delta {number(c['prefetches_per_ki_delta'])}; "
                  f"zero-action delta {number(c['zero_action_fraction_delta'])}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError) as error:
        sys.exit(str(error))
