#!/usr/bin/env python3
"""Step 4 distinguishability. A per-core detector watches the suspect core (core 1) and
must decide: covert attacker, or honest memory-heavy program?

Conditions (receiver = core 0 = mcf/Pythia throughout):
  attacker      = lbm duty-cycling a PN bit stream at T=12k  (bursty on/off)   x3 seeds
  benign_lbm    = lbm steady full traffic
  benign_bwaves = bwaves steady full traffic

We slice each suspect time series into detection windows of W epochs and compute, per window:
  mean_bw   mean per-epoch DRAM requests          (the NAIVE 'average bandwidth' detector)
  cv_bw     sd/mean of per-epoch DRAM requests     (burstiness / temporal structure)
  idle_frac fraction of epochs below 20% of the window's 90th-pct bw (on/off gaps)
  mpki      LLC load MPKI (context)
  ipc       suspect IPC (context)
Then AUC of each feature separating attacker windows from benign windows.

Hypothesis: average bandwidth does NOT separate the attacker from benign heavy programs
(its mean sits inside the benign envelope), but the temporal structure does.

Pure stdlib. Writes step4.json + windows.csv; prints a report.
"""
import csv, json, math, statistics as st
from pathlib import Path

SP = Path("/tmp/claude-1000/-home-bloodydictator-Pythia/07a36b19-a805-415d-8134-cef38054d270/scratchpad/step4")
RUNS = {
    "attacker":      ["atk_s1", "atk_s2", "atk_s3"],
    "benign_lbm":    ["ben_lbm"],
    "benign_bwaves": ["ben_bwaves"],
}
SUSPECT = 1
SKIP_ROI_INSTR = 500_000
W = 120                # epochs per detection window (~2-5 transmitted symbols)

def suspect_series(run):
    """Per-epoch core-1 ROI series after onset skip: (bw, instr, cyc, llc_miss, llc_acc)."""
    path = SP/run/"intervals.csv"
    base = None; out = []
    with open(path) as f:
        for r in csv.DictReader(f):
            if int(r["core"]) != SUSPECT or r["phase"] not in ("pressure", "off"):
                continue
            rb = int(r["retired_begin"])
            if base is None: base = rb
            if rb - base < SKIP_ROI_INSTR: continue
            out.append((int(r["core_dram_requests"]), int(r["instructions"]),
                        int(r["cycle_end"]) - int(r["cycle_begin"]),
                        int(r["llc_load_misses"]), int(r["llc_load_accesses"])))
    return out

def pctl(xs, p):
    if not xs: return 0.0
    s = sorted(xs); i = p*(len(s)-1); lo=int(math.floor(i)); hi=int(math.ceil(i))
    return s[lo] + (s[hi]-s[lo])*(i-lo)

def windows(series):
    """Feature dicts for consecutive W-epoch windows."""
    out = []
    for i in range(0, len(series)-W+1, W):
        chunk = series[i:i+W]
        bw = [c[0] for c in chunk]
        instr = sum(c[1] for c in chunk); cyc = sum(c[2] for c in chunk)
        miss = sum(c[3] for c in chunk)
        m = sum(bw)/len(bw)
        sd = st.pstdev(bw) if len(bw) > 1 else 0.0
        p90 = pctl(bw, 0.90); thr = 0.2*p90
        idle = sum(1 for v in bw if v < thr)/len(bw)
        out.append(dict(mean_bw=m, cv_bw=(sd/m if m else 0.0), idle_frac=idle,
                        mpki=(miss/instr*1000 if instr else 0.0),
                        ipc=(instr/cyc if cyc else 0.0)))
    return out

def auc(pos, neg):
    if not pos or not neg: return float("nan")
    comb = sorted([(v,1) for v in pos] + [(v,0) for v in neg])
    ranks=[0.0]*len(comb); i=0
    while i < len(comb):
        j=i
        while j+1 < len(comb) and comb[j+1][0]==comb[i][0]: j+=1
        a=(i+j)/2.0+1.0
        for k in range(i,j+1): ranks[k]=a
        i=j+1
    R1=sum(ranks[k] for k in range(len(comb)) if comb[k][1]==1)
    n1,n0=len(pos),len(neg)
    return (R1 - n1*(n1+1)/2.0)/(n1*n0)

def auc_sym(pos, neg):
    """Direction-agnostic separability: max(AUC, 1-AUC)."""
    a = auc(pos, neg)
    return a if a != a else max(a, 1-a)

def summ(xs):
    xs=[x for x in xs]
    return dict(n=len(xs), mean=(sum(xs)/len(xs) if xs else 0.0),
                sd=(st.pstdev(xs) if len(xs)>1 else 0.0),
                p10=pctl(xs,0.1), median=pctl(xs,0.5), p90=pctl(xs,0.9),
                mn=(min(xs) if xs else 0.0), mx=(max(xs) if xs else 0.0))

def main():
    feats = {}
    for cls, runs in RUNS.items():
        rows = []
        for run in runs:
            if not (SP/run/"intervals.csv").exists():
                print(f"[warn] missing {run}"); continue
            rows += windows(suspect_series(run))
        feats[cls] = rows
        print(f"{cls:>14}: {len(rows)} detection windows")

    atk = feats["attacker"]
    benign = feats["benign_lbm"] + feats["benign_bwaves"]

    def col(rows, k): return [r[k] for r in rows]
    report = {"W_epochs": W, "counts": {k: len(v) for k,v in feats.items()}}

    print("\n" + "="*84)
    print("STEP 4 DISTINGUISHABILITY  — suspect = core1; detector features per", W, "-epoch window")
    print("="*84)

    print("\n[A] MEAN DRAM REQUESTS PER EPOCH  (the naive 'average bandwidth' detector)")
    for cls in ("attacker","benign_lbm","benign_bwaves"):
        s=summ(col(feats[cls],"mean_bw"))
        print(f"  {cls:>14}: mean={s['mean']:8.1f}  p10..p90=[{s['p10']:7.1f},{s['p90']:7.1f}]  n={s['n']}")
        report.setdefault("mean_bw",{})[cls]=s
    a_mean = auc_sym(col(atk,"mean_bw"), col(benign,"mean_bw"))
    a_mean_lbm = auc_sym(col(atk,"mean_bw"), col(feats["benign_lbm"],"mean_bw"))
    a_mean_bwv = auc_sym(col(atk,"mean_bw"), col(feats["benign_bwaves"],"mean_bw"))
    print(f"  --> mean-bw detector AUC  attacker vs benign(all)={a_mean:.3f}  "
          f"vs lbm={a_mean_lbm:.3f}  vs bwaves={a_mean_bwv:.3f}")

    print("\n[B] TEMPORAL STRUCTURE  (burstiness / on-off gaps the detector can key on)")
    for feat,label in (("cv_bw","CV of per-epoch bw"),("idle_frac","idle-epoch fraction")):
        for cls in ("attacker","benign_lbm","benign_bwaves"):
            s=summ(col(feats[cls],feat))
            print(f"  {feat:>10} {cls:>14}: mean={s['mean']:.3f}  p10..p90=[{s['p10']:.3f},{s['p90']:.3f}]")
            report.setdefault(feat,{})[cls]=s
        a=auc_sym(col(atk,feat), col(benign,feat))
        print(f"      --> {feat} detector AUC attacker vs benign(all)={a:.3f}\n")
        report.setdefault("auc",{})[feat]=a

    report["auc"]["mean_bw"]=a_mean
    report["auc"]["mean_bw_vs_lbm"]=a_mean_lbm
    report["auc"]["mean_bw_vs_bwaves"]=a_mean_bwv

    print("[C] CONTEXT (LLC MPKI, suspect IPC)")
    for cls in ("attacker","benign_lbm","benign_bwaves"):
        print(f"  {cls:>14}: MPKI={summ(col(feats[cls],'mpki'))['mean']:6.2f}  "
              f"IPC={summ(col(feats[cls],'ipc'))['mean']:.3f}")

    # dump windows for the figure
    with (SP/"windows.csv").open("w", newline="") as fh:
        w=csv.writer(fh); w.writerow(["class","mean_bw","cv_bw","idle_frac","mpki","ipc"])
        for cls in ("attacker","benign_lbm","benign_bwaves"):
            for r in feats[cls]:
                w.writerow([cls, f"{r['mean_bw']:.2f}", f"{r['cv_bw']:.4f}",
                            f"{r['idle_frac']:.4f}", f"{r['mpki']:.3f}", f"{r['ipc']:.4f}"])
    (SP/"step4.json").write_text(json.dumps(report, indent=2, default=str))
    print(f"\nwrote {SP/'windows.csv'} and {SP/'step4.json'}")

if __name__ == "__main__":
    main()
