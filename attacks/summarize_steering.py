#!/usr/bin/env python3
"""Per-phase steering summary that does not need a cooldown phase.

For each pressure/off segment of an attack run, compares the cycles the attack run needed against the
cycles the matching quiet run needed for the SAME victim instructions (instruction-aligned, so the
victim's own program phases cancel). Positive slowdown = victim slower under attack.

usage: summarize_steering.py RUN_DIR [CONTROL ...]   (CONTROL: learned, frozen, reward-ablation)
"""
import bisect
import csv
import sys
from pathlib import Path


def victim_curve(path):
    """Cumulative (retired, cycle) points for core 0 inside the ROI, from interval boundaries."""
    rows = [r for r in csv.DictReader(open(path)) if r["core"] == "0"]
    xs, ys = [int(rows[0]["retired_begin"])], [int(rows[0]["cycle_begin"])]
    for r in rows:
        xs.append(int(r["retired_end"]))
        ys.append(int(r["cycle_end"]))
    return xs, ys


def cycle_at(curve, retired):
    xs, ys = curve
    if retired <= xs[0]:
        return ys[0]
    if retired >= xs[-1]:
        return ys[-1]
    i = bisect.bisect_right(xs, retired)
    span = xs[i] - xs[i - 1]
    return ys[i - 1] if span == 0 else ys[i - 1] + (ys[i] - ys[i - 1]) * (retired - xs[i - 1]) / span


def segments(run, quiet_name):
    attack = victim_curve(run / "intervals.csv")
    quiet = victim_curve(run.parent / quiet_name / "intervals.csv")
    marks = [r for r in csv.DictReader(open(run / "intervals.csv.phases.csv")) if r["roi"] == "1"]
    bounds = [(int(m["victim_retired"]), int(m["cycle"]), m["phase"], int(m["pulse"])) for m in marks]
    bounds.append((attack[0][-1], attack[1][-1], "end", -1))
    for (v0, c0, phase, pulse), (v1, c1, _, _) in zip(bounds, bounds[1:]):
        if v1 <= v0:
            continue
        a_cycles = cycle_at(attack, v1) - cycle_at(attack, v0)
        q_cycles = cycle_at(quiet, v1) - cycle_at(quiet, v0)
        if q_cycles > 0:
            yield phase, pulse, v1 - v0, a_cycles, q_cycles


def main():
    root = Path(sys.argv[1])
    controls = sys.argv[2:] or ["learned", "frozen"]
    for control in controls:
        run = root / f"stream-{control}-attack"
        if not run.is_dir():
            continue
        segs = list(segments(run, f"stream-{control}-quiet"))
        print(f"\n=== {control}: {len(segs)} segments ===")
        totals = {}
        for phase, _, instr, a, q in segs:
            t = totals.setdefault(phase, [0, 0, 0, 0])
            t[0] += instr; t[1] += a; t[2] += q; t[3] += 1
        print(f"{'phase':10} {'segments':>8} {'victim_instr':>13} {'slowdown%':>10}")
        for phase, (instr, a, q, n) in totals.items():
            print(f"{phase:10} {n:8d} {instr:13d} {100 * (a / q - 1):+10.2f}")
        long_pressure = [(p, i, 100 * (a / q - 1)) for ph, p, i, a, q in segs if ph == "pressure" and i >= 1_000_000]
        long_off = [(p, i, 100 * (a / q - 1)) for ph, p, i, a, q in segs if ph == "off" and i >= 200_000]
        for title, items in (("pressure segments >= 1M victim instr", long_pressure),
                             ("off segments >= 200k victim instr", long_off)):
            if items:
                vals = [s for _, _, s in items]
                print(f"{title}: n={len(vals)}  mean={sum(vals) / len(vals):+.2f}%  min={min(vals):+.2f}%  max={max(vals):+.2f}%")


if __name__ == "__main__":
    main()
