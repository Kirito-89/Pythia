#!/usr/bin/env python3
"""One PNG per trace for the single-core (no attacker) Pythia runs.

usage: plot_singlecore_each.py LOG_DIR OUT_DIR
e.g.   plot_singlecore_each.py runs/single-core graphs/singlecore

For every *.log in LOG_DIR, writes OUT_DIR/<trace-stem>.png: four headline stat
tiles (mean IPC, LLC MPKI, prefetch accuracy, DRAM busiest-quarter share) plus
the IPC timeline over the measured (post-warmup) region. Reads the heartbeat
lines and the [ROI Statistics] block of each log.
"""
import re
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

SURFACE, INK, MUTED, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
ACCENT = "#2a78d6"
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


def stat_tile(ax, value, label):
    ax.set_facecolor(SURFACE)
    ax.axis("off")
    ax.text(0.0, 0.62, value, fontsize=22, fontweight="bold", color=INK, ha="left", va="baseline")
    ax.text(0.0, 0.18, label, fontsize=9.5, color=MUTED, ha="left", va="baseline")


def metrics(stats):
    ipc = stats.get("Core_0_IPC", 0.0)
    instr = stats.get("Core_0_instructions", 1.0)
    mpki = 1000 * stats.get("Core_0_LLC_load_miss", 0.0) / instr
    u, x = stats.get("Core_0_L2C_prefetch_useful", 0.0), stats.get("Core_0_L2C_prefetch_useless", 0.0)
    accuracy = 100 * u / (u + x) if (u + x) else 0.0
    levels = ["DRAM_bw_level_0", "DRAM_bw_level_1", "DRAM_bw_level_2", "DRAM_bw_level_3"]
    tot = sum(stats.get(k, 0.0) for k in levels) or 1.0
    busy = 100 * stats.get("DRAM_bw_level_3", 0.0) / tot
    return ipc, mpki, accuracy, busy


def plot_one(path, out_dir):
    beats, warm, stats = parse(path)
    if not stats.get("Core_0_IPC"):
        print(f"skip {path.name}: no [ROI Statistics] block found (run may not have finished)")
        return
    ipc, mpki, accuracy, busy = metrics(stats)
    pts = [(i - warm, v) for i, v in beats if i > warm]

    fig = plt.figure(figsize=(9, 5.6))
    fig.patch.set_facecolor(SURFACE)
    gs = fig.add_gridspec(2, 4, height_ratios=[1, 2.6], hspace=0.45, wspace=0.3)

    tiles = [
        (f"{ipc:.2f}", "mean IPC"),
        (f"{mpki:.1f}", "LLC load-miss MPKI"),
        (f"{accuracy:.0f}%", "L2 prefetch accuracy"),
        (f"{busy:.0f}%", "epochs in busiest DRAM quarter"),
    ]
    for col, (value, label) in enumerate(tiles):
        stat_tile(fig.add_subplot(gs[0, col]), value, label)

    ax = fig.add_subplot(gs[1, :])
    style(ax)
    ax.plot([x / 1e6 for x, _ in pts], [v for _, v in pts], color=ACCENT, lw=1.6)
    ax.axhline(ipc, color=MUTED, lw=1, ls="--")
    ax.text(198, ipc, f" mean {ipc:.2f}", color=MUTED, fontsize=9, va="center", ha="right")
    ax.set_ylim(0, max((v for _, v in pts), default=1) * 1.15)
    ax.set_xlim(0, 200)
    ax.set_xlabel("instructions after warmup (millions)", color=MUTED)
    ax.set_ylabel("IPC per 1M instructions")
    ax.set_title("IPC over the measured 200M instructions", loc="left", fontsize=11, fontweight="bold", color=INK)

    fig.suptitle(f"{path.stem}  —  single-core, Pythia prefetcher", x=0.01, ha="left",
                 fontsize=13, fontweight="bold", color=INK)
    out_path = out_dir / f"{path.stem}.png"
    fig.savefig(out_path, dpi=170, facecolor=SURFACE, bbox_inches="tight")
    plt.close(fig)
    print(f"wrote {out_path}")


def main():
    log_dir = Path(sys.argv[1])
    out_dir = Path(sys.argv[2])
    out_dir.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "axes.labelcolor": MUTED,
                         "xtick.color": MUTED, "ytick.color": MUTED, "text.color": INK})
    logs = sorted(log_dir.glob("*.log"))
    if not logs:
        sys.exit(f"no *.log files found in {log_dir}")
    for p in logs:
        plot_one(p, out_dir)


if __name__ == "__main__":
    main()
