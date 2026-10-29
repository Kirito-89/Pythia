#!/usr/bin/env python3
"""Per-run pressure-window summary: policy shift versus contention cost.

For each gated-experiment directory, compares attack vs quiet inside the
pressure phase for the learned and frozen controls. Positive slowdown means the
victim lost IPC. 'shift' is the attack-minus-quiet change in the share of
decisions that chose no-prefetch, in percentage points.
"""
import csv
import sys
from pathlib import Path


def window(directory, control, role):
    total = {"instr": 0, "cycles": 0, "decisions": 0, "zero": 0, "rewards": 0, "hbw": 0,
             "issued": 0, "attacker_dram": 0}
    for row in csv.DictReader((directory / f"stream-{control}-{role}" / "intervals.csv").open()):
        if row["phase"] != "pressure":
            continue
        if row["core"] == "0":
            total["instr"] += int(row["instructions"])
            total["cycles"] += int(row["cycle_end"]) - int(row["cycle_begin"])
            total["decisions"] += int(row["decisions"])
            total["zero"] += int(row["zero_action"])
            total["rewards"] += int(row["rewards"])
            total["hbw"] += int(row["high_bw_rewards"])
            total["issued"] += int(row["issued_prefetches"])
        else:
            total["attacker_dram"] += int(row["core_dram_requests"])
    return total


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    print(f"{'run':28}{'control':9}{'IPC loss%':>10}{'hbw share':>10}{'no-pf% atk':>11}{'no-pf% qt':>10}"
          f"{'shift pp':>9}{'PFKI atk':>9}{'PFKI qt':>8}{'atk DRAM':>10}")
    for name in sys.argv[1:]:
        directory = Path(name)
        for control in ("learned", "frozen"):
            try:
                a, q = window(directory, control, "attack"), window(directory, control, "quiet")
            except FileNotFoundError:
                continue
            loss = 100 * (1 - (a["instr"] / a["cycles"]) / (q["instr"] / q["cycles"]))
            za, zq = 100 * a["zero"] / a["decisions"], 100 * q["zero"] / q["decisions"]
            print(f"{directory.name:28}{control:9}{loss:10.2f}{a['hbw'] / max(a['rewards'], 1):10.2f}"
                  f"{za:11.1f}{zq:10.1f}{za - zq:9.1f}{1000 * a['issued'] / a['instr']:9.1f}"
                  f"{1000 * q['issued'] / q['instr']:8.1f}{a['attacker_dram']:10d}")


if __name__ == "__main__":
    main()
