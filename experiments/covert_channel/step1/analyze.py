#!/usr/bin/env python3
"""Step 1 existence test: does a fixed sender (duty=100 'always-1' vs duty=0 'always-0')
separate the receiver's observable? Receiver = core 0 (mcf, Pythia).
Headline observable: per-epoch IPC. Corroborating observable: action-0 rate (zero/decisions).
Transmitted-symbol check: DRAM bandwidth bucket mix + sender (core 1) DRAM requests.

Pure stdlib. Reads the two intervals.csv, prints stats + writes binned CSVs + a small JSON.
"""
import csv, json, math, statistics as st
from pathlib import Path

SP = Path("/tmp/claude-1000/-home-bloodydictator-Pythia/07a36b19-a805-415d-8134-cef38054d270/scratchpad/step1")
RUNS = {"class1_on": SP/"class1_on"/"intervals.csv",   # bit=1, duty=100
        "class0_off": SP/"class0_off"/"intervals.csv"}  # bit=0, duty=0
WARMUP = 5_000_000
BIN = 200_000           # receiver-instruction bin for the paired comparison
SKIP_ROI_INSTR = 500_000  # drop the first 0.5M ROI instructions (onset transient)

def load_core(path, core):
    rows = []
    with open(path) as f:
        for r in csv.DictReader(f):
            if int(r["core"]) != core:
                continue
            rows.append(r)
    return rows

def per_epoch(rows):
    """Per-epoch receiver observables, ROI only, after the onset-transient skip."""
    base = None
    ipc, a0, rel = [], [], []
    for r in rows:
        if r["phase"] not in ("pressure", "off"):  # ROI phases only (pre = warmup)
            continue
        rb = int(r["retired_begin"])
        if base is None:
            base = rb
        roi_instr = rb - base
        if roi_instr < SKIP_ROI_INSTR:
            continue
        instr = int(r["instructions"]); cyc = int(r["cycle_end"]) - int(r["cycle_begin"])
        dec = int(r["decisions"]); zero = int(r["zero_action"])
        if cyc <= 0 or instr <= 0:
            continue
        ipc.append(instr / cyc)
        rel.append(roi_instr)
        if dec > 0:
            a0.append(zero / dec)
    return ipc, a0, rel

def bin_by_instr(rows, base_ref=None):
    """Aggregate into fixed receiver-instruction bins: bin -> [instr, cyc, dec, zero, bw0..3]."""
    base = None
    table = {}
    for r in rows:
        if r["phase"] not in ("pressure", "off"):
            continue
        rb = int(r["retired_begin"])
        if base is None:
            base = rb
        roi_instr = rb - base
        if roi_instr < SKIP_ROI_INSTR:
            continue
        k = roi_instr // BIN
        a = table.setdefault(k, [0,0,0,0,0,0,0,0])
        a[0] += int(r["instructions"]); a[1] += int(r["cycle_end"]) - int(r["cycle_begin"])
        a[2] += int(r["decisions"]);    a[3] += int(r["zero_action"])
        for i in range(4):
            a[4+i] += int(r[f"bw_level_{i}"])
    return table

def summary(xs):
    xs = sorted(xs)
    n = len(xs)
    if n == 0:
        return dict(n=0)
    mean = sum(xs)/n
    sd = st.pstdev(xs) if n > 1 else 0.0
    def pct(p):
        if n == 1: return xs[0]
        i = p*(n-1); lo = int(math.floor(i)); hi = int(math.ceil(i))
        return xs[lo] + (xs[hi]-xs[lo])*(i-lo)
    return dict(n=n, mean=mean, sd=sd, median=pct(0.5), p10=pct(0.10), p90=pct(0.90),
                mn=xs[0], mx=xs[-1])

def auc(pos, neg):
    """AUC that a threshold on the observable separates pos(class1) from neg(class0).
    Mann-Whitney U with tie handling. Returns prob(pos>neg)+0.5*prob(tie)."""
    if not pos or not neg:
        return float("nan")
    comb = sorted([(v,1) for v in pos] + [(v,0) for v in neg])
    # assign average ranks
    ranks = [0.0]*len(comb); i = 0
    while i < len(comb):
        j = i
        while j+1 < len(comb) and comb[j+1][0] == comb[i][0]:
            j += 1
        avg = (i + j)/2.0 + 1.0
        for k in range(i, j+1):
            ranks[k] = avg
        i = j+1
    R1 = sum(ranks[k] for k in range(len(comb)) if comb[k][1]==1)
    n1 = len(pos); n0 = len(neg)
    U1 = R1 - n1*(n1+1)/2.0
    return U1/(n1*n0)

def dprime(pos, neg):
    if len(pos)<2 or len(neg)<2: return float("nan")
    m1,m0 = sum(pos)/len(pos), sum(neg)/len(neg)
    v = 0.5*(st.pvariance(pos)+st.pvariance(neg))
    return (m1-m0)/math.sqrt(v) if v>0 else float("inf")

def ascii_hist(xs, lo, hi, nbins=30, width=50, label=""):
    if not xs: return f"{label}: (no data)"
    step = (hi-lo)/nbins if hi>lo else 1
    counts = [0]*nbins
    for x in xs:
        b = min(nbins-1, max(0, int((x-lo)/step)))
        counts[b]+=1
    mx = max(counts) or 1
    out=[f"  {label}  (n={len(xs)}, range [{lo:.4g},{hi:.4g}])"]
    for b in range(nbins):
        bar="#"*int(width*counts[b]/mx)
        out.append(f"   {lo+b*step:8.4f} | {bar} {counts[b]}")
    return "\n".join(out)

def main():
    res = {}
    c0 = {name: load_core(p, 0) for name,p in RUNS.items()}
    c1 = {name: load_core(p, 1) for name,p in RUNS.items()}

    pe = {name: per_epoch(rows) for name,rows in c0.items()}
    ipc1, a01, _ = pe["class1_on"]
    ipc0, a00, _ = pe["class0_off"]

    print("="*70)
    print("STEP 1 EXISTENCE TEST  — receiver = core0 (mcf, Pythia); sender = core1 (lbm)")
    print("  class1 = duty 100 ('always 1'),  class0 = duty 0 ('always 0')")
    print("="*70)

    print("\n[A] HEADLINE OBSERVABLE: per-epoch receiver IPC")
    s1, s0 = summary(ipc1), summary(ipc0)
    print(f"  class1(on) : mean={s1['mean']:.5f}  median={s1['median']:.5f}  sd={s1['sd']:.5f}  n={s1['n']}")
    print(f"  class0(off): mean={s0['mean']:.5f}  median={s0['median']:.5f}  sd={s0['sd']:.5f}  n={s0['n']}")
    gap = (s0['mean']-s1['mean'])/s0['mean']*100 if s0['mean'] else float('nan')
    A = auc(ipc0, ipc1)  # class0 IPC should be HIGHER -> use class0 as "positive" so AUC>0.5 means separable
    print(f"  mean IPC gap (off vs on): {gap:.1f}%   d'={dprime(ipc0,ipc1):.3f}   AUC(sep)={A:.3f}")

    print("\n[B] CORROBORATING OBSERVABLE: per-epoch action-0 rate (zero/decisions)")
    s1a, s0a = summary(a01), summary(a00)
    print(f"  class1(on) : mean={s1a.get('mean',0):.4f}  median={s1a.get('median',0):.4f}  n={s1a['n']}")
    print(f"  class0(off): mean={s0a.get('mean',0):.4f}  median={s0a.get('median',0):.4f}  n={s0a['n']}")
    Aa = auc(a01, a00)  # action-0 should be HIGHER under pressure(on) -> class1 positive
    print(f"  d'={dprime(a01,a00):.3f}   AUC(sep)={Aa:.3f}")

    # Transmitted-symbol check: sender DRAM requests + global bw bucket mix
    def sender_reqs(rows):
        tot=0
        for r in rows:
            if r["phase"] in ("pressure","off"):
                tot += int(r["core_dram_requests"])
        return tot
    print("\n[C] TRANSMITTED-SYMBOL CHECK (did the sender actually move the channel?)")
    print(f"  sender(core1) DRAM requests in ROI:  on={sender_reqs(c1['class1_on']):,}   off={sender_reqs(c1['class0_off']):,}")
    def bwmix(rows):
        agg=[0,0,0,0]
        for r in rows:
            if r["phase"] in ("pressure","off"):
                for i in range(4): agg[i]+=int(r[f"bw_level_{i}"])
        tot=sum(agg) or 1
        return [a/tot for a in agg]
    print(f"  receiver-seen bw bucket mix on : {[f'{x:.2f}' for x in bwmix(c0['class1_on'])]}")
    print(f"  receiver-seen bw bucket mix off: {[f'{x:.2f}' for x in bwmix(c0['class0_off'])]}")

    # Paired instruction-binned comparison (removes mcf program-phase confound)
    b1 = bin_by_instr(c0["class1_on"]); b0 = bin_by_instr(c0["class0_off"])
    common = sorted(set(b1) & set(b0))
    paired=[]
    with (SP/"paired_bins.csv").open("w", newline="") as fh:
        w=csv.writer(fh); w.writerow(["roi_instr_bin_start","ipc_on","ipc_off","ipc_drop_pct","a0_on","a0_off"])
        for k in common:
            x,y=b1[k],b0[k]
            if x[0]<=0 or y[0]<=0 or x[1]<=0 or y[1]<=0: continue
            ion=x[0]/x[1]; ioff=y[0]/y[1]
            a0on = x[3]/x[2] if x[2] else 0; a0off=y[3]/y[2] if y[2] else 0
            drop=(ioff-ion)/ioff*100 if ioff else 0
            paired.append(drop)
            w.writerow([k*BIN, f"{ion:.6f}", f"{ioff:.6f}", f"{drop:.2f}", f"{a0on:.4f}", f"{a0off:.4f}"])
    if paired:
        pos=sum(1 for d in paired if d>0)
        print("\n[D] PAIRED instruction-binned IPC (same receiver instructions, on vs off):")
        print(f"  bins={len(paired)}  median IPC drop under pressure={st.median(paired):.1f}%  "
              f"bins slowed={pos}/{len(paired)} ({100*pos/len(paired):.0f}%)")

    # ASCII histograms
    lo=min(min(ipc1),min(ipc0)); hi=max(max(ipc1),max(ipc0))
    print("\n[E] HISTOGRAMS — per-epoch receiver IPC")
    print(ascii_hist(ipc1, lo, hi, label="class1 (duty 100, bit=1)"))
    print(ascii_hist(ipc0, lo, hi, label="class0 (duty 0,  bit=0)"))

    res=dict(ipc=dict(on=s1,off=s0,gap_pct=gap,auc=A,dprime=dprime(ipc0,ipc1)),
             a0=dict(on=s1a,off=s0a,auc=Aa),
             paired_median_drop_pct=(st.median(paired) if paired else None),
             n_bins=len(paired))
    (SP/"summary.json").write_text(json.dumps(res, indent=2, default=str))
    print(f"\nWrote {SP/'summary.json'} and {SP/'paired_bins.csv'}")

if __name__=="__main__":
    main()
