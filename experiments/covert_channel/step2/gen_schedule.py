#!/usr/bin/env python3
"""Generate a symbol-clocked steering schedule + ground-truth bit file for Step 2.

Sender transmits a bit sequence: each symbol period T (receiver instructions) is one
schedule row, phase 'pressure', duty=100 for a '1' and duty=0 for a '0'.
A short alternating PREAMBLE (10101...) leads the payload so the decoder can calibrate
the on/off IPC levels. Payload bits come from a seeded xorshift PN generator.

Usage: gen_schedule.py --T 200000 --payload 128 --preamble 16 --seed 1 --out DIR
Writes DIR/sched.csv and DIR/truth.csv ; prints the measured-instruction count needed.
"""
import argparse, csv
from pathlib import Path

def pn_bits(n, seed):
    x = (seed * 2654435761) & 0xFFFFFFFFFFFFFFFF or 1
    out = []
    for _ in range(n):
        x ^= (x << 13) & 0xFFFFFFFFFFFFFFFF
        x ^= (x >> 7)
        x ^= (x << 17) & 0xFFFFFFFFFFFFFFFF
        out.append(x & 1)
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--T", type=int, required=True, help="symbol period in receiver instructions")
    ap.add_argument("--payload", type=int, default=128)
    ap.add_argument("--preamble", type=int, default=16, help="alternating 1010.. calibration bits")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)

    pre = [1 if i % 2 == 0 else 0 for i in range(a.preamble)]
    pay = pn_bits(a.payload, a.seed)
    bits = pre + pay
    kinds = ["preamble"] * len(pre) + ["payload"] * len(pay)

    # schedule rows: pre marker far in warmup, then one pressure row per symbol
    rows = [(-10**12, "pre", 0, 0)]
    for i, b in enumerate(bits):
        rows.append((i * a.T, "pressure", i, 100 if b else 0))
    with (a.out / "sched.csv").open("w", newline="") as f:
        w = csv.writer(f); w.writerow(["victim_instr", "phase", "pulse", "duty"]); w.writerows(rows)
    with (a.out / "truth.csv").open("w", newline="") as f:
        w = csv.writer(f); w.writerow(["symbol", "roi_instr_start", "bit", "kind"])
        for i, (b, k) in enumerate(zip(bits, kinds)):
            w.writerow([i, i * a.T, b, k])

    measured = len(bits) * a.T + a.T  # +1 symbol slack so the last symbol fully elapses
    (a.out / "params.txt").write_text(
        f"T={a.T}\npayload={a.payload}\npreamble={a.preamble}\nseed={a.seed}\n"
        f"n_symbols={len(bits)}\nmeasured_instructions={measured}\n")
    print(measured)

if __name__ == "__main__":
    main()
