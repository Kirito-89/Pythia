#!/usr/bin/env python3
"""Decode one Step 2 run: map receiver epochs -> symbols, decode IPC against a
preamble-calibrated threshold, compute BER, raw symbol rate, and channel capacity.

Usage: decode.py --run RUNDIR --truth TRUTHCSV --T 200000 [--json OUT.json]
RUNDIR must contain intervals.csv (steering_log). Receiver = core 0.
"""
import argparse, csv, json, math, statistics as st
from pathlib import Path

def binary_entropy(p):
    if p <= 0 or p >= 1: return 0.0
    return -p*math.log2(p) - (1-p)*math.log2(1-p)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=Path, required=True)
    ap.add_argument("--truth", type=Path, required=True)
    ap.add_argument("--T", type=int, required=True)
    ap.add_argument("--json", type=Path)
    ap.add_argument("--clock-ghz", type=float, default=4.0, help="assumed core clock for a bits/sec figure")
    a = ap.parse_args()

    truth = {int(r["symbol"]): (int(r["bit"]), r["kind"]) for r in csv.DictReader(a.truth.open())}

    # aggregate receiver (core 0) epochs into symbols
    base = None
    sym = {}  # idx -> [instr, cyc]
    for r in csv.DictReader((a.run / "intervals.csv").open()):
        if int(r["core"]) != 0 or r["phase"] not in ("pressure", "off"):
            continue
        rb = int(r["retired_begin"])
        if base is None: base = rb
        k = (rb - base) // a.T
        s = sym.setdefault(k, [0, 0])
        s[0] += int(r["instructions"]); s[1] += int(r["cycle_end"]) - int(r["cycle_begin"])

    ipc = {k: v[0]/v[1] for k, v in sym.items() if v[1] > 0 and k in truth}

    # calibrate threshold from preamble symbols
    pre1 = [ipc[k] for k in ipc if truth[k][1] == "preamble" and truth[k][0] == 1]
    pre0 = [ipc[k] for k in ipc if truth[k][1] == "preamble" and truth[k][0] == 0]
    if pre1 and pre0:
        thr = (st.mean(pre1) + st.mean(pre0)) / 2
        lvl1, lvl0 = st.mean(pre1), st.mean(pre0)
    else:  # fall back to payload-wide midpoint (ideal observer) if no preamble
        allp = [ipc[k] for k in ipc if truth[k][1] == "payload"]
        thr = st.median(allp) if allp else 0
        lvl1 = lvl0 = thr
    # IPC is LOW when bit=1 (sender floods) -> decode 1 when ipc < threshold
    payload = sorted(k for k in ipc if truth[k][1] == "payload")
    errors = 0; decoded = []
    for k in payload:
        bit = 1 if ipc[k] < thr else 0
        decoded.append(bit)
        if bit != truth[k][0]: errors += 1
    n = len(payload)
    ber = errors / n if n else float("nan")

    # raw rate: median receiver cycles per symbol over payload
    cps = st.median([sym[k][1] for k in payload]) if payload else float("nan")
    bits_per_mcycle = 1e6 / cps if cps else float("nan")
    raw_bps = a.clock_ghz * 1e9 / cps if cps else float("nan")
    cap_frac = 1 - binary_entropy(ber)            # per-use capacity of a BSC(ber)
    cap_bps = raw_bps * cap_frac
    cap_per_mcycle = bits_per_mcycle * cap_frac

    res = dict(T=a.T, n_payload=n, errors=errors, ber=ber,
               cycles_per_symbol=cps, bits_per_mcycle=bits_per_mcycle,
               raw_bps_at_clk=raw_bps, clock_ghz=a.clock_ghz,
               capacity_frac=cap_frac, capacity_bps=cap_bps,
               capacity_per_mcycle=cap_per_mcycle,
               level1_ipc=lvl1, level0_ipc=lvl0, threshold=thr)
    print(f"T={a.T:>8}  BER={ber:6.3f}  cyc/sym={cps:,.0f}  "
          f"raw={bits_per_mcycle:6.3f} b/Mcyc  cap={cap_per_mcycle:6.3f} b/Mcyc  "
          f"(@{a.clock_ghz}GHz: {cap_bps:,.0f} bps)")
    if a.json:
        a.json.write_text(json.dumps(res, indent=2, default=str))

if __name__ == "__main__":
    main()
