#!/usr/bin/env python3
"""Graphs from the single-core (no attacker) Pythia runs.

usage: plot_single_core.py LOG_DIR OUT_PREFIX
e.g.   plot_single_core.py runs/single-core attacks/single_core

Writes OUT_PREFIX_ipc.png (IPC per victim), OUT_PREFIX_timeline.png (IPC every 1M instructions, all victims),
OUT_PREFIX_timeline_1..3.png (the same, two victims per image) and
OUT_PREFIX_memory.png (memory behaviour). Reads the heartbeat lines and the [ROI Statistics] block of each
*.log; only the measured region (after warmup) is plotted.
"""
import re
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

SURFACE, INK, MUTED, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
ACCENT, GREY = "#2a78d6", "#c3c2b7"
HEART = re.compile(r"^Heartbeat CPU\s+0 instructions:\s+(\d+) cycles:\s+(\d+) heartbeat IPC: ([\d.]+)")
WARM = re.compile(r"^Warmup complete CPU\s+0 instructions:\s+(\d+)")


def parse(path):
    beats, warm, stats, in_roi = [], 0, {}, False
    for line in open(path, errors="replace"):
        if line.startswith("Heartbeat"):
            m = HEART.match(line)
            if m:
                beats.append((int(m.group(1)), float(m.group(3))))
        elif line.startswith("Warmup complete"):
            m = WARM.match(line)
            warm = int(m.group(1)) if m else warm
        elif line.startswith("[ROI Statistics]"):
            in_roi = True
        elif in_roi and line.startswith(("Core_0_", "DRAM_", "scooby_")):
            parts = line.split()
            if len(parts) == 2:
                try:
                    stats[parts[0]] = float(parts[1])
                except ValueError:
                    pass
    return beats, warm, stats


def style(ax):
    ax.set_facecolor(SURFACE)
    ax.grid(axis="y", color=GRID, lw=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.spines["left"].set_color(GRID)
    ax.spines["bottom"].set_color(GRID)


def short(path):
    return path.stem.split(".", 1)[1].split("-")[0]  # 429.mcf-22B -> mcf


def hbar(ax, names, values, fmt, xlabel, title):
    style(ax)
    ax.grid(axis="y", visible=False)
    ax.grid(axis="x", color=GRID, lw=0.8)
    ys = range(len(names))
    ax.barh(list(ys), values, color=GREY, height=0.62)
    for y, v in zip(ys, values):
        ax.text(v + max(values) * 0.012, y, fmt(v), va="center", fontsize=9.5, color=INK)
    ax.set_yticks(list(ys))
    ax.set_yticklabels(names, color=INK)
    ax.invert_yaxis()
    ax.set_xlim(0, max(values) * 1.18)
    ax.set_xlabel(xlabel, color=MUTED)
    ax.set_title(title, loc="left", fontsize=11.5, fontweight="bold", color=INK)


def main():
    logs = sorted(Path(sys.argv[1]).glob("*.log"))
    prefix = sys.argv[2]
    data = {}
    for p in logs:
        beats, warm, stats = parse(p)
        if stats.get("Core_0_IPC"):
            data[short(p)] = (beats, warm, stats)
    if not data:
        sys.exit("no finished logs found")
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "axes.labelcolor": MUTED,
                         "xtick.color": MUTED, "ytick.color": MUTED, "text.color": INK})
    order = sorted(data, key=lambda n: -data[n][2]["Core_0_IPC"])

    # 1. IPC per victim
    fig, ax = plt.subplots(figsize=(8, 4.2))
    fig.patch.set_facecolor(SURFACE)
    ipc = [data[n][2]["Core_0_IPC"] for n in order]
    hbar(ax, order, ipc, lambda v: f"{v:.2f}", "IPC (instructions per cycle)",
         "Single-core IPC with Pythia: bwaves runs 10x faster than astar")
    for i, n in enumerate(order):
        if n == order[0]:
            ax.patches[i].set_color(ACCENT)
    fig.savefig(f"{prefix}_ipc.png", dpi=170, facecolor=SURFACE, bbox_inches="tight")
    print(f"wrote {prefix}_ipc.png")

    # 2. IPC over the measured region, one panel per victim (own scale: IPC differs 10x)
    fig, axes = plt.subplots(2, 3, figsize=(13, 6.4), sharex=True)
    fig.patch.set_facecolor(SURFACE)
    for ax, n in zip(axes.flat, order):
        beats, warm, stats = data[n]
        pts = [(i - warm, v) for i, v in beats if i > warm]
        style(ax)
        ax.plot([x / 1e6 for x, _ in pts], [v for _, v in pts], color=ACCENT, lw=1.5)
        ax.axhline(stats["Core_0_IPC"], color=MUTED, lw=1, ls="--")
        ax.set_ylim(0, max(v for _, v in pts) * 1.15)
        ax.set_title(f"{n}   (mean IPC {stats['Core_0_IPC']:.2f})", loc="left", fontsize=11, fontweight="bold")
        ax.set_xlim(0, 200)
    for ax in axes[1]:
        ax.set_xlabel("victim instructions after warmup (millions)")
    for ax in axes[:, 0]:
        ax.set_ylabel("IPC per 1M instructions")
    fig.text(0.01, 0.995, "Victim IPC over the measured 200M instructions (dashed line = whole-run mean)",
             fontsize=12.5, fontweight="bold", va="top")
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.savefig(f"{prefix}_timeline.png", dpi=170, facecolor=SURFACE, bbox_inches="tight")
    print(f"wrote {prefix}_timeline.png")

    # 2b. The same timelines, two graphs per image (stacked, shared x-axis)
    for part, start in enumerate(range(0, len(order), 2), 1):
        pair = order[start:start + 2]
        fig, axes = plt.subplots(len(pair), 1, figsize=(10, 3.4 * len(pair)), sharex=True, squeeze=False)
        fig.patch.set_facecolor(SURFACE)
        for ax, n in zip(axes[:, 0], pair):
            beats, warm, stats = data[n]
            pts = [(i - warm, v) for i, v in beats if i > warm]
            style(ax)
            ax.plot([x / 1e6 for x, _ in pts], [v for _, v in pts], color=ACCENT, lw=1.6)
            ax.axhline(stats["Core_0_IPC"], color=MUTED, lw=1, ls="--")
            ax.set_ylim(0, max(v for _, v in pts) * 1.15)
            ax.set_xlim(0, 200)
            ax.set_ylabel("IPC per 1M instructions")
            ax.set_title(f"{n}   (mean IPC {stats['Core_0_IPC']:.2f}, dashed line)", loc="left",
                         fontsize=11.5, fontweight="bold")
        axes[-1][0].set_xlabel("victim instructions after warmup (millions)")
        fig.tight_layout()
        fig.savefig(f"{prefix}_timeline_{part}.png", dpi=170, facecolor=SURFACE, bbox_inches="tight")
        print(f"wrote {prefix}_timeline_{part}.png  ({', '.join(pair)})")

    # 3. Memory behaviour
    def mpki(n):
        s = data[n][2]
        return 1000 * s["Core_0_LLC_load_miss"] / s["Core_0_instructions"]

    def pf_per_ki(n):
        s = data[n][2]
        return 1000 * s["Core_0_L2C_prefetch_issued"] / s["Core_0_instructions"]

    def accuracy(n):
        s = data[n][2]
        u, x = s["Core_0_L2C_prefetch_useful"], s["Core_0_L2C_prefetch_useless"]
        return 100 * u / (u + x) if u + x else 0.0

    fig, axes = plt.subplots(2, 2, figsize=(12.5, 8))
    fig.patch.set_facecolor(SURFACE)
    panels = [(axes[0][0], mpki, lambda v: f"{v:.1f}", "LLC demand-load misses per 1,000 instructions (after prefetching)",
               "How often demand loads still go to DRAM"),
              (axes[0][1], pf_per_ki, lambda v: f"{v:.0f}", "L2 prefetches issued per 1,000 instructions",
               "How much Pythia prefetches"),
              (axes[1][0], accuracy, lambda v: f"{v:.1f}%", "useful / (useful + useless) L2 prefetches, %",
               "How accurate the prefetches are")]
    for ax, fn, fmt, label, title in panels:
        vals = [fn(n) for n in order]
        hbar(ax, order, vals, fmt, label, title)
    # DRAM busy: share of epochs in each bandwidth level
    ax = axes[1][1]
    style(ax)
    ax.grid(axis="y", visible=False)
    ax.grid(axis="x", color=GRID, lw=0.8)
    levels = ["DRAM_bw_level_0", "DRAM_bw_level_1", "DRAM_bw_level_2", "DRAM_bw_level_3"]
    names = ["lowest quarter", "second", "third", "busiest quarter"]
    alphas = [0.22, 0.45, 0.72, 1.0]
    left = [0.0] * len(order)
    for lvl, nm, al in zip(levels, names, alphas):
        shares = []
        for n in order:
            s = data[n][2]
            tot = sum(s.get(k, 0) for k in levels) or 1
            shares.append(100 * s.get(lvl, 0) / tot)
        ax.barh(range(len(order)), shares, left=left, color=ACCENT, alpha=al, height=0.62, label=nm,
                edgecolor=SURFACE, linewidth=1)
        left = [a + b for a, b in zip(left, shares)]
    ax.set_yticks(range(len(order)))
    ax.set_yticklabels(order, color=INK)
    ax.invert_yaxis()
    ax.set_xlim(0, 100)
    ax.set_xlabel("share of measurement epochs (%)", color=MUTED)
    ax.set_title("How busy DRAM is (the level Pythia's reward uses)", loc="left", fontsize=11.5, fontweight="bold")
    ax.legend(title="DRAM bandwidth level", frameon=False, fontsize=8.5, title_fontsize=9, loc="upper center",
              bbox_to_anchor=(0.5, -0.17), ncol=4)
    fig.suptitle("Memory behaviour of each victim running alone", x=0.01, ha="left", fontsize=13, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    fig.savefig(f"{prefix}_memory.png", dpi=170, facecolor=SURFACE, bbox_inches="tight")
    print(f"wrote {prefix}_memory.png")


if __name__ == "__main__":
    main()
