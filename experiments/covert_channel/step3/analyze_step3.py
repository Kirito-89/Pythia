#!/usr/bin/env python3
"""Step 3 stealth effort sweep. Sender is fixed 'on' but at varying duty (effort).
For each duty level D we measure, against the shared duty-0 baseline (reused from Step 1):
  - channel separability of the receiver observable: IPC AUC & d', action-0 AUC
  - the attacker's FOOTPRINT: injected DRAM bandwidth (core1 requests per Mcycle of ROI),
    both absolute and as a fraction of the duty-100 footprint.
Then we locate the knee: the lowest duty whose channel is still usable (IPC AUC >= 0.90).
Pure stdlib. Writes sweep.csv + step3.json; prints a table.
"""
import csv, json, math, statistics as st
from pathlib import Path

SP = Path("/tmp/claude-1000/-home-bloodydictator-Pythia/07a36b19-a805-415d-8134-cef38054d270/scratchpad")
OFF = SP/"step1"/"class0_off"/"intervals.csv"          # duty-0 baseline (shared)
DUTIES = [100, 60, 40, 25, 15, 10, 6, 3]
WARMUP = 5_000_000
SKIP_ROI_INSTR = 500_000        # drop onset transient (same as Step 1)
USABLE_AUC = 0.90               # knee criterion on IPC separability

def load_core(path, core):
    with open(path) as f:
        return [r for r in csv.DictReader(f) if int(r["core"]) == core]

def per_epoch(rows):
    """Per-epoch receiver observables, ROI only, after onset-transient skip."""
    base = None; ipc = []; a0 = []
    for r in rows:
        if r["phase"] not in ("pressure", "off"):
            continue
        rb = int(r["retired_begin"])
        if base is None: base = rb
        if rb - base < SKIP_ROI_INSTR: continue
        instr = int(r["instructions"]); cyc = int(r["cycle_end"]) - int(r["cycle_begin"])
        if cyc <= 0 or instr <= 0: continue
        ipc.append(instr / cyc)
        dec = int(r["decisions"]); zero = int(r["zero_action"])
        if dec > 0: a0.append(zero / dec)
    return ipc, a0

def injected_bw(rows):
    """Attacker (core1) footprint: DRAM requests per Mcycle of ROI, ROI cycles too."""
    base = None; reqs = 0; cyc = 0
    for r in rows:
        if r["phase"] not in ("pressure", "off"):
            continue
        rb = int(r["retired_begin"])
        if base is None: base = rb
        if rb - base < SKIP_ROI_INSTR: continue
        reqs += int(r["core_dram_requests"])
        cyc += int(r["cycle_end"]) - int(r["cycle_begin"])
    rpm = reqs / (cyc/1e6) if cyc else float("nan")
    return reqs, cyc, rpm

def auc(pos, neg):
    if not pos or not neg: return float("nan")
    comb = sorted([(v,1) for v in pos] + [(v,0) for v in neg])
    ranks = [0.0]*len(comb); i = 0
    while i < len(comb):
        j = i
        while j+1 < len(comb) and comb[j+1][0] == comb[i][0]: j += 1
        avg = (i+j)/2.0 + 1.0
        for k in range(i, j+1): ranks[k] = avg
        i = j+1
    R1 = sum(ranks[k] for k in range(len(comb)) if comb[k][1]==1)
    n1, n0 = len(pos), len(neg)
    return (R1 - n1*(n1+1)/2.0)/(n1*n0)

def dprime(pos, neg):
    if len(pos)<2 or len(neg)<2: return float("nan")
    m1, m0 = sum(pos)/len(pos), sum(neg)/len(neg)
    v = 0.5*(st.pvariance(pos)+st.pvariance(neg))
    return (m1-m0)/math.sqrt(v) if v>0 else float("inf")

def main():
    off0 = load_core(OFF, 0)
    ipc_off, a0_off = per_epoch(off0)
    off_mean_ipc = sum(ipc_off)/len(ipc_off)

    rows_out = []
    for D in DUTIES:
        run = SP/"step3"/f"d{D}"/"intervals.csv"
        if not run.exists():
            print(f"[warn] missing {run}; skipping D={D}"); continue
        c0 = load_core(run, 0); c1 = load_core(run, 1)
        ipc_on, a0_on = per_epoch(c0)
        reqs, cyc, rpm = injected_bw(c1)
        # IPC: off is HIGHER than on -> treat off as positive so AUC>0.5 == separable
        A_ipc = auc(ipc_off, ipc_on); d_ipc = dprime(ipc_off, ipc_on)
        # action-0: on is HIGHER than off -> on positive
        A_a0 = auc(a0_on, a0_off)
        mean_on = sum(ipc_on)/len(ipc_on)
        gap = (off_mean_ipc - mean_on)/off_mean_ipc*100
        rows_out.append(dict(duty=D, ipc_auc=A_ipc, ipc_dprime=d_ipc, a0_auc=A_a0,
                             ipc_gap_pct=gap, mean_ipc_on=mean_on,
                             inj_reqs=reqs, roi_cycles=cyc, inj_rpm=rpm, n_on=len(ipc_on)))

    # footprint as fraction of duty-100
    base_rpm = next((r["inj_rpm"] for r in rows_out if r["duty"] == 100), float("nan"))
    for r in rows_out:
        r["footprint_frac"] = r["inj_rpm"]/base_rpm if base_rpm else float("nan")

    # knee: lowest duty still usable
    usable = [r for r in rows_out if r["ipc_auc"] >= USABLE_AUC]
    knee = min(usable, key=lambda r: r["duty"]) if usable else None

    print("="*96)
    print("STEP 3 STEALTH EFFORT SWEEP  — receiver=core0(mcf), sender=core1(lbm), vs duty-0 baseline")
    print(f"  off baseline: mean IPC={off_mean_ipc:.5f} over {len(ipc_off)} epochs")
    print("="*96)
    dpr = "IPC d'"
    hdr = f"{'duty%':>5} {'inj req/Mcyc':>13} {'footprint':>9} {'IPC gap%':>9} {'IPC AUC':>8} {dpr:>7} {'a0 AUC':>7} {'epochs':>7}"
    print(hdr); print("-"*len(hdr))
    for r in rows_out:
        print(f"{r['duty']:>5} {r['inj_rpm']:>13,.0f} {r['footprint_frac']:>8.2f}x "
              f"{r['ipc_gap_pct']:>8.1f}% {r['ipc_auc']:>8.3f} {r['ipc_dprime']:>7.2f} "
              f"{r['a0_auc']:>7.3f} {r['n_on']:>7}")
    print("-"*len(hdr))
    if knee:
        print(f"KNEE (lowest usable, IPC AUC>={USABLE_AUC}): duty={knee['duty']}%  "
              f"footprint={knee['footprint_frac']:.2f}x of full  "
              f"inj={knee['inj_rpm']:,.0f} req/Mcyc  IPC AUC={knee['ipc_auc']:.3f}")
    else:
        print(f"no duty reached IPC AUC >= {USABLE_AUC}")

    with (SP/"step3"/"sweep.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows_out[0].keys())); w.writeheader()
        for r in rows_out: w.writerow(r)
    (SP/"step3"/"step3.json").write_text(json.dumps(
        dict(off_mean_ipc=off_mean_ipc, off_epochs=len(ipc_off),
             usable_auc=USABLE_AUC, knee=knee, sweep=rows_out), indent=2, default=str))
    print(f"\nwrote {SP/'step3'/'sweep.csv'} and {SP/'step3'/'step3.json'}")

if __name__ == "__main__":
    main()
