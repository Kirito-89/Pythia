#!/usr/bin/env python3
"""Steering timeline over the whole measured run, one column per victim.

Top row: victim IPC with the attacker (orange) and with the matched quiet control (blue), binned by
victim instructions. Grey bands mark when the attacker was pushing. Bottom row: victim speed under
attack relative to quiet (negative = slowed by the attacker). The dotted line is where the attacker
stopped for good (--ctl-end).

usage: plot_timeline.py [--attack-only [--smooth]] OUT.png LABEL=RUN_DIR[:ATTACK_END_M] ...   (uses the learned control)

--smooth (with --attack-only) draws the top panel in 2M-instruction bins like the quiet-comparison graphs
instead of 50,000-cycle epochs, and omits the threshold lines (they apply to epochs, not to bins).

--attack-only draws the adaptive attacker run alone (no quiet comparison). Top: victim IPC per 50,000-cycle
controller epoch with the relax/push thresholds the controller used. Bottom: attacker DRAM requests per 1,000
cycles, binned by victim instructions, which is the traffic the attacker actually sent.
e.g.   plot_timeline.py attacks/timeline.png soplex=runs/soplex-policy-50m-200m-20261018:100 lbm=runs/lbm-steer-20261018:190
"""
import csv
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

SURFACE, INK, MUTED, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
QUIET, ATTACK, BAND = "#2a78d6", "#eb6834", "#cfcdc6"


def core0(path):
    return [r for r in csv.DictReader(open(path)) if r["core"] == "0"]


def binned(rows, base, size):
    table = {}
    for r in rows:
        k = (int(r["retired_begin"]) - base) // size
        a = table.setdefault(k, [0, 0])
        a[0] += int(r["instructions"])
        a[1] += int(r["cycle_end"]) - int(r["cycle_begin"])
    return table


def pushing_spans(phases_csv, base):
    marks = [r for r in csv.DictReader(open(phases_csv)) if r["roi"] == "1"]
    spans = []
    for cur, nxt in zip(marks, marks[1:]):
        if cur["phase"] == "pressure":
            spans.append(((int(cur["victim_retired"]) - base) / 1e6, (int(nxt["victim_retired"]) - base) / 1e6))
    return spans


def series(attack, quiet, base, size):
    a, q = binned(attack, base, size), binned(quiet, base, size)
    xs, ia, iq = [], [], []
    for k in sorted(a):
        if k in q and a[k][0] and q[k][0]:
            xs.append((k + 0.5) * size / 1e6)
            ia.append(a[k][0] / a[k][1])
            iq.append(q[k][0] / q[k][1])
    return xs, ia, iq


def epoch_ipc(rows, base, cycles=50_000):
    table = {}
    for r in rows:
        k = int(r["cycle_begin"]) // cycles
        a = table.setdefault(k, [0, 0, 0, 0])
        a[0] += int(r["instructions"])
        a[1] += int(r["cycle_end"]) - int(r["cycle_begin"])
        a[2] += int(r["retired_begin"]) - base
        a[3] += 1
    xs = [a[2] / a[3] / 1e6 for k, a in sorted(table.items()) if a[1]]
    ys = [a[0] / a[1] for k, a in sorted(table.items()) if a[1]]
    return xs, ys


def attacker_traffic(victim_rows, base, size=2_000_000):
    """Attacker DRAM requests = all DRAM requests minus the victim's own (both are on every core-0 row, so
    this covers the whole run even after the attacker core's own log rows stop)."""
    table = {}
    for r in victim_rows:
        k = (int(r["retired_begin"]) - base) // size
        a = table.setdefault(k, [0, 0])
        a[0] += int(r["dram_requests"]) - int(r["core_dram_requests"])
        a[1] += int(r["cycle_end"]) - int(r["cycle_begin"])
    xs = [(k + 0.5) * size / 1e6 for k, a in sorted(table.items()) if a[1]]
    ys = [1000 * a[0] / a[1] for k, a in sorted(table.items()) if a[1]]
    return xs, ys


def thresholds(controller_csv):
    for line in open(controller_csv):
        f = line.rstrip("\n").split(",")
        if len(f) > 9 and f[1] == "cycle_baseline":
            return float(f[6]), float(f[8]), float(f[9])  # idle IPC, push-above, relax-below
    return None


def attack_only(out, victims, smooth=False):
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "axes.edgecolor": GRID,
                         "axes.labelcolor": MUTED, "xtick.color": MUTED, "ytick.color": MUTED,
                         "text.color": INK})
    fig, axes = plt.subplots(2, len(victims), figsize=(7.2 * len(victims), 6.4), sharex="col",
                             gridspec_kw={"height_ratios": [3, 2], "hspace": 0.1}, squeeze=False)
    fig.patch.set_facecolor(SURFACE)
    for col, (label, run, end) in enumerate(victims):
        attack_dir = run / "stream-learned-attack"
        all_rows = list(csv.DictReader(open(attack_dir / "intervals.csv")))
        victim = [r for r in all_rows if r["core"] == "0"]
        base = int(victim[0]["retired_begin"])
        spans = pushing_spans(attack_dir / "intervals.csv.phases.csv", base)
        if smooth:
            table = binned(victim, base, 2_000_000)
            xs = [(k + 0.5) * 2 for k in sorted(table) if table[k][1]]
            ys = [table[k][0] / table[k][1] for k in sorted(table) if table[k][1]]
        else:
            xs, ys = epoch_ipc(victim, base)
        tx, ty = attacker_traffic(victim, base)
        th = thresholds(attack_dir / "intervals.csv.controller.csv")
        top, bottom = axes[0][col], axes[1][col]
        for ax in (top, bottom):
            ax.set_facecolor(SURFACE)
            for lo, hi in spans:
                ax.axvspan(lo, hi, color=BAND, alpha=0.45, lw=0)
            if end:
                ax.axvline(end, color=MUTED, lw=1, ls=":")
            ax.grid(axis="y", color=GRID, lw=0.8)
            ax.set_axisbelow(True)
            for side in ("top", "right"):
                ax.spines[side].set_visible(False)
        top.plot(xs, ys, color=ATTACK, lw=2 if smooth else 1.2)
        top.set_title(label, loc="left", fontsize=12, fontweight="bold", color=INK)
        top.set_ylabel("victim IPC (2M-instr bins)" if smooth else "victim IPC (50,000-cycle epochs)")
        top.set_ylim(bottom=0)
        if th and not smooth:
            idle, push_above, relax_below = th
            for level, text in ((push_above, "push again above"), (relax_below, "relax below")):
                top.axhline(level, color=MUTED, lw=1, ls="--")
                top.annotate(f"{text}\n{level:.2f}", xy=(1.0, level), xycoords=("axes fraction", "data"),
                             xytext=(6, 0), textcoords="offset points", color=MUTED, fontsize=8.5,
                             ha="left", va="center", annotation_clip=False)
        if end:
            top.annotate("attacker stops", xy=(end, 1.0), xycoords=("data", "axes fraction"), xytext=(6, -4),
                         textcoords="offset points", color=MUTED, fontsize=9, va="top", annotation_clip=False)
        bottom.step(tx, ty, where="mid", color=ATTACK, lw=1.6)
        bottom.set_ylim(bottom=0)
        bottom.set_ylabel("attacker DRAM requests\nper 1,000 cycles")
        bottom.set_xlabel("victim instructions after warmup (millions)")
        bottom.set_xlim(0, max(xs) + 1)
    fig.legend([plt.Rectangle((0, 0), 1, 1, color=BAND, alpha=0.45)], ["attacker pushing"],
               loc="upper right", frameon=False, bbox_to_anchor=(0.99, 0.99))
    fig.savefig(out, dpi=170, facecolor=SURFACE, bbox_inches="tight")
    print(f"wrote {out}")


def main():
    args = sys.argv[1:]
    only = "--attack-only" in args
    smooth = "--smooth" in args
    if only:
        args.remove("--attack-only")
    if smooth:
        args.remove("--smooth")
    sys.argv = [sys.argv[0]] + args
    if only:
        victims = []
        for spec in sys.argv[2:]:
            label, rest = spec.split("=", 1)
            path, _, end = rest.partition(":")
            victims.append((label, Path(path), float(end) if end else None))
        attack_only(Path(sys.argv[1]), victims, smooth)
        return
    out = Path(sys.argv[1])
    victims = []
    for spec in sys.argv[2:]:
        label, rest = spec.split("=", 1)
        path, _, end = rest.partition(":")
        victims.append((label, Path(path), float(end) if end else None))
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "axes.edgecolor": GRID,
                         "axes.labelcolor": MUTED, "xtick.color": MUTED, "ytick.color": MUTED,
                         "text.color": INK})
    fig, axes = plt.subplots(2, len(victims), figsize=(6.4 * len(victims), 6.4), sharex="col",
                             gridspec_kw={"height_ratios": [3, 2], "hspace": 0.1}, squeeze=False)
    fig.patch.set_facecolor(SURFACE)
    for col, (label, run, end) in enumerate(victims):
        attack = core0(run / "stream-learned-attack" / "intervals.csv")
        quiet = core0(run / "stream-learned-quiet" / "intervals.csv")
        base = int(attack[0]["retired_begin"])
        top, bottom = axes[0][col], axes[1][col]
        spans = pushing_spans(run / "stream-learned-attack" / "intervals.csv.phases.csv", base)
        xs, ia, iq = series(attack, quiet, base, 2_000_000)
        bx, ba, bq = series(attack, quiet, base, 5_000_000)
        delta = [100 * (a / q - 1) for a, q in zip(ba, bq)]
        for ax in (top, bottom):
            ax.set_facecolor(SURFACE)
            for lo, hi in spans:
                ax.axvspan(lo, hi, color=BAND, alpha=0.45, lw=0)
            if end:
                ax.axvline(end, color=MUTED, lw=1, ls=":")
            ax.grid(axis="y", color=GRID, lw=0.8)
            ax.set_axisbelow(True)
            for side in ("top", "right"):
                ax.spines[side].set_visible(False)
        top.plot(xs, iq, color=QUIET, lw=2, label="quiet attacker (control)")
        top.plot(xs, ia, color=ATTACK, lw=2, label="adaptive attacker")
        top.set_title(f"{label}", loc="left", fontsize=12, fontweight="bold", color=INK)
        top.set_ylabel("victim IPC (2M-instr bins)")
        top.set_ylim(bottom=0)
        bottom.axhline(0, color=MUTED, lw=1)
        bottom.plot(bx, delta, color=ATTACK, lw=2, marker="o", ms=4)
        bottom.set_ylabel("speed vs quiet (%)\n(5M-instr bins)")
        bottom.set_xlabel("victim instructions after warmup (millions)")
        bottom.set_xlim(0, max(xs) + 1)
        if end:
            top.text(end + 1, top.get_ylim()[1] * 0.97, "attacker stops", color=MUTED, fontsize=9, va="top")
        if col == 0:
            top.legend(loc="lower left", frameon=False)
            top.text(0.01, 0.02, "", transform=top.transAxes)
    handles = [plt.Rectangle((0, 0), 1, 1, color=BAND, alpha=0.45)]
    fig.legend(handles, ["attacker pushing"], loc="upper right", frameon=False, bbox_to_anchor=(0.99, 0.99))
    fig.savefig(out, dpi=170, facecolor=SURFACE, bbox_inches="tight")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
