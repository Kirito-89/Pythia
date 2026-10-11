#!/usr/bin/env python3
"""Count memory operations by recorded phase; does not infer simulated bandwidth."""
import argparse
import csv
import gzip
import json
from pathlib import Path
import struct

RECORD = struct.Struct("<QBB2B4B2Q4Q")
PHASES = {0: "pre", 1: "pressure", 2: "off", 3: "post", 4: "quiet-control"}


def inspect(trace, phases):
    with Path(phases).open() as stream:
        events = list(csv.DictReader(stream))
    if not events or events[0]["event"] != "roi_begin" or events[-1]["event"] != "roi_end":
        raise ValueError("expected a complete ROI, not a truncated trace")
    boundaries = [int(event["trace_instruction"]) for event in events]
    if boundaries[0] != 0 or boundaries != sorted(boundaries):
        raise ValueError("invalid phase boundaries")
    intervals = []
    for i, event in enumerate(events[:-1]):
        intervals.append(dict(
            phase=PHASES[int(event["phase"])] if event["event"] == "phase" else "marker-overhead",
            pulse=int(event["pulse"]), start=boundaries[i], stop=boundaries[i + 1],
            instructions=0, loads=0, stores=0))
    open_trace = gzip.open if str(trace).endswith(".gz") else open
    count = 0
    interval = 0
    with open_trace(trace, "rb") as stream:
        while True:
            block = stream.read(RECORD.size * 16384)
            if not block:
                break
            if len(block) % RECORD.size:
                raise ValueError("partial trace record")
            for instruction in RECORD.iter_unpack(block):
                while interval < len(intervals) and count >= intervals[interval]["stop"]:
                    interval += 1
                if interval == len(intervals):
                    raise ValueError("trace extends beyond ROI end")
                row = intervals[interval]
                row["instructions"] += 1
                row["stores"] += sum(address != 0 for address in instruction[9:11])
                row["loads"] += sum(address != 0 for address in instruction[11:15])
                count += 1
    if count != boundaries[-1]:
        raise ValueError("trace length differs from phase log")
    return intervals


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    parser.add_argument("phases", type=Path)
    args = parser.parse_args()
    print(json.dumps(inspect(args.trace, args.phases), indent=2))


if __name__ == "__main__":
    main()
