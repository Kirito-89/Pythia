#!/usr/bin/env python3
"""One PNG per trace: victim IPC per 1M instructions, two-core vs single-core.

usage: plot_twocore_each.py SINGLE_LOG_DIR OUT_DIR RUN_DIR [RUN_DIR ...]
       plot_twocore_each.py --twocore-only OUT_DIR RUN_DIR [RUN_DIR ...]
e.g.   plot_twocore_each.py runs/single-core graphs/twocore \
           "runs/twocore-0.5:0.8" "runs/twocore-0.6:0.9"

Each RUN_DIR holds <trace>/stream-learned-attack/simulation.log; its label is the
directory name after "twocore-". The victim is core 0. Settings whose directory or
trace log is missing are skipped. The x axis is victim instructions after warmup.
"""
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from plot_singlecore_each import parse, style, SURFACE, INK, MUTED

COLORS = ["#2a78d6", "#d6572a", "#2a9d5c", "#8a4fd6"]
RUN = "stream-learned-attack"


def series(path):
    beats, warm, _ = parse(path)
    pts = [((i - warm) / 1e6, v) for i, v in beats if i >= warm + 1_000_000]
    return [x for x, _ in pts], [v for _, v in pts]


def main():
    twocore_only = sys.argv[1] == "--twocore-only"
    single_dir, out_dir, run_dirs = Path(sys.argv[1]), Path(sys.argv[2]), [Path(p) for p in sys.argv[3:]]
    if not run_dirs:
        sys.exit(__doc__)
    out_dir.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "axes.labelcolor": MUTED,
                         "xtick.color": MUTED, "ytick.color": MUTED, "text.color": INK})
    for run_dir in run_dirs:
        if not run_dir.is_dir():
            print(f"skip {run_dir}: not found")
    traces = sorted({p.name for d in run_dirs if d.is_dir() for p in d.iterdir() if p.is_dir()})
    for trace in traces:
        fig, ax = plt.subplots(figsize=(9, 4.2))
        fig.patch.set_facecolor(SURFACE)
        style(ax)
        top = 0.0
        single = single_dir / f"{trace}.log"
        if single.exists() and not twocore_only:
            xs, ys = series(single)
            ax.plot(xs, ys, color=MUTED, lw=1.4, label="single-core")
            top = max(top, max(ys, default=0))
        for color, run_dir in zip(COLORS, run_dirs):
            log = run_dir / trace / RUN / "simulation.log"
            if not log.exists():
                continue
            xs, ys = series(log)
            ax.plot(xs, ys, color=color, lw=1.4, label=f"two-core {run_dir.name.removeprefix('twocore-')}")
            top = max(top, max(ys, default=0))
        ax.set_ylim(0, top * 1.15 or 1)
        ax.set_xlim(0, 200)
        ax.set_xlabel("victim instructions after warmup (millions)", color=MUTED)
        ax.set_ylabel("IPC per 1M instructions")
        ax.legend(frameon=False, loc="upper right", ncol=3)
        ax.set_title(f"{trace}  —  victim IPC, two-core{'' if twocore_only else ' vs single-core'}", loc="left",
                     fontsize=11, fontweight="bold", color=INK)
        out = out_dir / (f"{trace}_twocore-only.png" if twocore_only else f"{trace}.png")
        fig.savefig(out, dpi=170, facecolor=SURFACE, bbox_inches="tight")
        plt.close(fig)
        print(f"wrote {out}")


if __name__ == "__main__":
    main()
