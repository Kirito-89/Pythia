#!/usr/bin/env python3
"""4-core DoS / contention study: Pythia vs no-prefetcher weighted speedup.

For each of five trace mixes (ordered from most to least expected degradation) this
runs the mix on both configurations and reports weighted speedup, so the TA can see
Pythia's benefit collapse as mixes become bandwidth-bound.

Weighted speedup for a mix under config C:
    WS_C = sum_i ( IPC_i^{shared, C} / IPC_i^{alone, C} )
so it needs single-core "alone" IPC per benchmark per config. Those alone baselines
are computed once and cached (results.json), then reused across every mix that uses
the benchmark. The direct throughput ratio  sum(IPC_pythia) / sum(IPC_nopf)  in each
mix is the most intuitive "does Pythia help or hurt here" number.

Configs: Pythia = scooby on L2 (bin/perceptron-no-multi-no-ship-*core, pythia.ini);
no-pf = no L2 prefetcher (bin/perceptron-no-no-no-ship-*core). All runs use 50M
warmup + 200M measured per core. The run is resumable: anything already in
results.json is skipped, so you can stop/restart or split across machines.

Long runs: hand the printed command to a machine with time (~32 simulations).
"""
import argparse
import json
import re
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRACES = ROOT / "dpc3_traces"
CONFIG = ROOT / "config/pythia.ini"

# short label -> trace filename in dpc3_traces/
TRACE_FILE = {
    "lbm":        "470.lbm-1274B.champsimtrace.xz",
    "libquantum": "462.libquantum-1343B.champsimtrace.xz",
    "GemsFDTD":   "459.GemsFDTD-1491B.champsimtrace.xz",
    "bwaves":     "603.bwaves_s-2931B.champsimtrace.xz",
    "mcf":        "605.mcf_s-1152B.champsimtrace.xz",
    "omnetpp":    "620.omnetpp_s-141B.champsimtrace.xz",
    "bfs":        "bfs-14.trace.gz",
    "exchange2":  "648.exchange2_s-1712B.champsimtrace.xz",
    "leela":      "641.leela_s-1083B.champsimtrace.xz",
    "sjeng":      "458.sjeng-767B.champsimtrace.xz",
    "x264":       "625.x264_s-18B.champsimtrace.xz",
    # heavier FP streamers and extra mcf checkpoints (degradation-max set)
    "milc":       "433.milc-337B.champsimtrace.xz",
    "leslie3d":   "437.leslie3d-273B.champsimtrace.xz",
    "fotonik3d":  "649.fotonik3d_s-10881B.champsimtrace.xz",
    "cactuBSSN":  "607.cactuBSSN_s-4248B.champsimtrace.xz",
    "mcf1554":    "605.mcf_s-1554B.champsimtrace.xz",
    "mcf1644":    "605.mcf_s-1644B.champsimtrace.xz",
    "mcf472":     "605.mcf_s-472B.champsimtrace.xz",
    # our crafted prefetch-adversarial attacker (generate with prefetch_steer
    # --mode sawtooth, Pin-record, then drop the .gz into dpc3_traces/)
    "sawb2":      "attacker_sawtooth_b2.trace.gz",
    "sawb3":      "attacker_sawtooth_b3.trace.gz",
    "sawb4":      "attacker_sawtooth_b4.trace.gz",
    # focused DoS batch: burst-knee (degree-4 trigger) + LLC-pollution + writes
    "sawb16":     "attacker_sawtooth_b16.trace.gz",
    "pollute":    "attacker_pollute.trace.gz",
    "pollutew":   "attacker_pollute_w.trace.gz",
    # controlled burst-length sweep (identical params, ONLY --burst varies:
    #   --mode sawtooth --size-mib 256 --streams 8 --seed 1 --pre 0 --pulses 1
    #   --on 400000 --off 0 --post 0) so the burst-knee curve is apples-to-apples.
    "sb1":        "attacker_saw_b1.trace.gz",
    "sb2":        "attacker_saw_b2.trace.gz",
    "sb4":        "attacker_saw_b4.trace.gz",
    "sb8":        "attacker_saw_b8.trace.gz",
    "sb16":       "attacker_saw_b16.trace.gz",
    "sb32":       "attacker_saw_b32.trace.gz",
    "sb64":       "attacker_saw_b64.trace.gz",
    # pollution footprint (1 GB / 16 streams) but SHORT bursts: keep max victim
    # pollution while the attacker jumps before consuming its own prefetches, so
    # it stays self-sacrificing (low atkNorm) -> should push WS degr% past 17%.
    "polb2":      "attacker_pollute_b2.trace.gz",
    "polb3":      "attacker_pollute_b3.trace.gz",
    "polb8":      "attacker_pollute_b8.trace.gz",
    # victim-variety panel: compute-bound control (gcc) + extra latency victim
    "gcc":        "403.gcc-16B.champsimtrace.xz",
    "sphinx3":    "482.sphinx3-1297B.champsimtrace.xz",
    # ---- SPEC CPU2017 (6xx) + GAP roster for the grand DoS mix study ----
    # Heavy / large checkpoints preferred. [P]=present in dpc3_traces,
    # [D]=download on the server first (scripts/download_traces.pl / GAP source).
    # BW-bound streaming, prefetch-friendly (Pythia helps most -> most to lose):
    "lbm17":      "619.lbm_s-4268B.champsimtrace.xz",        # [P]
    "bwaves17":   "603.bwaves_s-2931B.champsimtrace.xz",     # [P]
    "foton17":    "649.fotonik3d_s-10881B.champsimtrace.xz", # [P]
    "cactu17":    "607.cactuBSSN_s-4004B.champsimtrace.xz",  # [D]
    "roms17":     "654.roms_s-1613B.champsimtrace.xz",       # [D]
    "wrf17":      "621.wrf_s-8065B.champsimtrace.xz",        # [D]
    # Irregular / latency-bound, high MPKI (memory-intensive, Pythia helps less):
    "mcfv":       "605.mcf_s-1554B.champsimtrace.xz",        # [P] victim mcf
    "omnet17":    "620.omnetpp_s-874B.champsimtrace.xz",     # [D]
    "xalan17":    "623.xalancbmk_s-592B.champsimtrace.xz",   # [D]
    # GAP graph workloads (irregular, very memory-intensive):
    "bfs":        "bfs-14.trace.gz",                         # [D]
    "cc":         "cc-14.trace.gz",                          # [D]
    "pr":         "pr-14.trace.gz",                          # [D]
    "sssp":       "sssp-14.trace.gz",                        # [D]
    "bc":         "bc-12.trace.gz",                          # [D]
    # Compute-bound (low memory -> NEGATIVE-control victims, should not move):
    "gcc17":      "602.gcc_s-1850B.champsimtrace.xz",        # [P]
    "exch":       "648.exchange2_s-1712B.champsimtrace.xz",  # [P] also benign-light
    # Core-0 false-positive control: heavy benign memory hog (distinct mcf ckpt
    # from the victim mcf so core0 != victim even in the all-mcf mix):
    "fp_mcf":     "605.mcf_s-1644B.champsimtrace.xz",        # [P]
}


def nice_name(label):
    """Full trace identifier for the report (filename minus the format suffix)."""
    return TRACE_FILE[label].replace(".champsimtrace.xz", "").replace(".trace.gz", "")

# Grand-study + balanced-group attacker (core 0). It is always the SAWTOOTH
# attacker; set this to the burst that wins Block B (sb1/sb2/sb4/sb8/sb16/sb32/
# sb64), or to "pollutew" for the heavy 1 GB/16-stream+writes sawtooth variant.
# Changing this one line re-runs the whole grand/balanced study at that burst.
ATK = "sb16"

# mix name -> four benchmark labels (ordered most -> least expected degradation)
MIXES = {
    "M1_bw_saturated":     ["lbm", "libquantum", "GemsFDTD", "bwaves"],
    "M2_irregular_mem":    ["mcf", "omnetpp", "bfs", "lbm"],
    "M3_mixed":            ["lbm", "GemsFDTD", "exchange2", "leela"],
    "M4_victim_surrounded":["bwaves", "lbm", "mcf", "libquantum"],
    "M5_light_control":    ["exchange2", "leela", "sjeng", "x264"],
    # Degradation-max mixes: homogeneous / doubled streaming hogs to saturate DRAM
    # and maximize prefetch-waste, pushing the Pythia/no-pf ratio toward 1 or below.
    "D1_libquantum_x4":    ["libquantum", "libquantum", "libquantum", "libquantum"],
    "D2_lbm_x4":           ["lbm", "lbm", "lbm", "lbm"],
    "D3_quant2_gems2":     ["libquantum", "libquantum", "GemsFDTD", "GemsFDTD"],
    "D4_fp_streamers":     ["milc", "leslie3d", "fotonik3d", "cactuBSSN"],
    "D6_mcf_x4":           ["mcf", "mcf1554", "mcf1644", "mcf472"],
    # Homogeneous heavy-streamer x4 mixes: maximize same-bank contention to push
    # system degradation (1 - WS/4) into/above the 40-50% band.
    "E1_milc_x4":          ["milc", "milc", "milc", "milc"],
    "E2_leslie3d_x4":      ["leslie3d", "leslie3d", "leslie3d", "leslie3d"],
    "E3_bwaves_x4":        ["bwaves", "bwaves", "bwaves", "bwaves"],
    "E4_GemsFDTD_x4":      ["GemsFDTD", "GemsFDTD", "GemsFDTD", "GemsFDTD"],
    "E5_fotonik3d_x4":     ["fotonik3d", "fotonik3d", "fotonik3d", "fotonik3d"],
    # 8-core mixes: double the bandwidth pressure so even Pythia's throttled
    # prefetches steal bandwidth demands need -> widen the WS_pythia vs WS_nopf gap.
    "X1_lbm_x8":           ["lbm"] * 8,
    "X2_libquantum_x8":    ["libquantum"] * 8,
    "X3_M1_doubled_x8":    ["lbm", "libquantum", "GemsFDTD", "bwaves"] * 2,
    "X4_hogs8_distinct":   ["lbm", "libquantum", "GemsFDTD", "bwaves",
                            "milc", "leslie3d", "fotonik3d", "cactuBSSN"],
    # Crafted-attacker DoS: core 0 = our prefetch-adversarial attacker (vanilla
    # Pythia, no advantage), cores 1-3 = bandwidth-sensitive victims (lbm). A0 is
    # the benign-co-runner control; run under both configs for the amplification
    # result. The sawbN alone baselines also show Pythia hurts the attacker.
    "A0_benign_vs_lbm":    ["exchange2", "lbm", "lbm", "lbm"],
    "A2_sawb2_vs_lbm":     ["sawb2", "lbm", "lbm", "lbm"],
    "A3_sawb3_vs_lbm":     ["sawb3", "lbm", "lbm", "lbm"],
    "A4_sawb4_vs_lbm":     ["sawb4", "lbm", "lbm", "lbm"],
    # focused batch (same vanilla Pythia, attacker still gets no advantage):
    # A5 = longer sequential runs so Pythia trains to degree 4 before each jump;
    # A6 = 1 GB footprint + 16 streams, sustained, to pollute the 8 MB LLC;
    # A7 = A6 plus writebacks/RFO to roughly double the wasted DRAM traffic.
    "A5_sawb16_vs_lbm":    ["sawb16", "lbm", "lbm", "lbm"],
    "A6_pollute_vs_lbm":   ["pollute", "lbm", "lbm", "lbm"],
    "A7_pollutew_vs_lbm":  ["pollutew", "lbm", "lbm", "lbm"],
    # pollution footprint + short-burst sweep (find WS-maximizing sweet spot):
    "A8_polb2_vs_lbm":     ["polb2", "lbm", "lbm", "lbm"],
    "A9_polb3_vs_lbm":     ["polb3", "lbm", "lbm", "lbm"],
    "A10_polb8_vs_lbm":    ["polb8", "lbm", "lbm", "lbm"],
    # Victim-variety matrix: each victim set run twice -- benign neighbor
    # (exchange2, the matched control) vs attacker (pollutew), everything else
    # identical, so any victim slowdown is attributable to the attacker. Spans
    # bandwidth / latency / compute classes; gcc is the compute-bound control
    # that should barely move (proves the DoS is targeted, not a generic hog).
    "Pbwaves_ctl": ["exchange2", "bwaves", "bwaves", "bwaves"],
    "Pbwaves_atk": ["pollutew",  "bwaves", "bwaves", "bwaves"],
    "Pmilc_ctl":   ["exchange2", "milc", "milc", "milc"],
    "Pmilc_atk":   ["pollutew",  "milc", "milc", "milc"],
    "Plibq_ctl":   ["exchange2", "libquantum", "libquantum", "libquantum"],
    "Plibq_atk":   ["pollutew",  "libquantum", "libquantum", "libquantum"],
    "Pmcf_ctl":    ["exchange2", "mcf1554", "mcf1554", "mcf1554"],
    "Pmcf_atk":    ["pollutew",  "mcf1554", "mcf1554", "mcf1554"],
    "Pomnet_ctl":  ["exchange2", "omnetpp", "omnetpp", "omnetpp"],
    "Pomnet_atk":  ["pollutew",  "omnetpp", "omnetpp", "omnetpp"],
    "Psphinx_ctl": ["exchange2", "sphinx3", "sphinx3", "sphinx3"],
    "Psphinx_atk": ["pollutew",  "sphinx3", "sphinx3", "sphinx3"],
    "Pgcc_ctl":    ["exchange2", "gcc", "gcc", "gcc"],
    "Pgcc_atk":    ["pollutew",  "gcc", "gcc", "gcc"],
    "Phet_ctl":    ["exchange2", "bwaves", "mcf1554", "milc"],
    "Phet_atk":    ["pollutew",  "bwaves", "mcf1554", "milc"],

    # ================================================================
    # Block B -- BURST-LENGTH SWEEP (TA request #1).
    # Victim fixed = 3x lbm (most bandwidth-sensitive + prefetch-friendly
    # victim, matches the A2-A5 series). Core 0 varies ONLY in burst length,
    # bracketed by two benign controls: exchange2 (light) and mcf (heavy).
    # Hypothesis: degradation is ~0 at b1 (pure random -> Pythia can't train),
    # rises as the sequential run grows long enough to ramp Pythia to its top
    # dynamic degree (scooby_dyn_degrees 1,2,4,4 gated by conf 1,3,8, so the
    # knee should appear around b8-b16), then plateaus. The jump after the
    # burst strands the degree-4 look-ahead lines -> wasted DRAM bandwidth.
    "B_ctl_light":  ["exchange2", "lbm", "lbm", "lbm"],
    "B_ctl_mcf":    ["mcf1554",   "lbm", "lbm", "lbm"],
    "B_sb1":        ["sb1",  "lbm", "lbm", "lbm"],
    "B_sb2":        ["sb2",  "lbm", "lbm", "lbm"],
    "B_sb4":        ["sb4",  "lbm", "lbm", "lbm"],
    "B_sb8":        ["sb8",  "lbm", "lbm", "lbm"],
    "B_sb16":       ["sb16", "lbm", "lbm", "lbm"],
    "B_sb32":       ["sb32", "lbm", "lbm", "lbm"],
    "B_sb64":       ["sb64", "lbm", "lbm", "lbm"],

    # ================================================================
    # Block S -- SPECIFICITY / FALSE-POSITIVE MATRIX (TA request #2).
    # For each victim class, core 0 is run as three conditions, everything
    # else identical: exchange2 (benign light), mcf (benign HEAVY memory hog
    # = the false-positive control) and the attacker. Proof of a real,
    # engineered attack: degr(atk) > degr(mcf) >= degr(light), AND the attacker
    # is self-sacrificing (its own IPC is the lowest, and lower under Pythia).
    # gcc victim = NEGATIVE control (compute-bound): should barely move under
    # any core 0, proving the DoS is targeted, not a generic hog.
    # NOTE: swap "pollutew" below for the Block-B burst-knee winner once known.
    "S_lbm_light":  ["exchange2", "lbm", "lbm", "lbm"],
    "S_lbm_mcf":    ["mcf1554",   "lbm", "lbm", "lbm"],
    "S_lbm_atk":    ["pollutew",  "lbm", "lbm", "lbm"],
    "S_bwaves_light": ["exchange2", "bwaves", "bwaves", "bwaves"],
    "S_bwaves_mcf":   ["mcf1554",   "bwaves", "bwaves", "bwaves"],
    "S_bwaves_atk":   ["pollutew",  "bwaves", "bwaves", "bwaves"],
    "S_libq_light": ["exchange2", "libquantum", "libquantum", "libquantum"],
    "S_libq_mcf":   ["mcf1554",   "libquantum", "libquantum", "libquantum"],
    "S_libq_atk":   ["pollutew",  "libquantum", "libquantum", "libquantum"],
    "S_mcfvic_light": ["exchange2", "mcf1554", "mcf1554", "mcf1554"],
    "S_mcfvic_mcf":   ["mcf1554",   "mcf1554", "mcf1554", "mcf1554"],
    "S_mcfvic_atk":   ["pollutew",  "mcf1554", "mcf1554", "mcf1554"],
    "S_gcc_light":  ["exchange2", "gcc", "gcc", "gcc"],
    "S_gcc_mcf":    ["mcf1554",   "gcc", "gcc", "gcc"],
    "S_gcc_atk":    ["pollutew",  "gcc", "gcc", "gcc"],

    # ================================================================
    # Block H -- HETEROGENEOUS victims (realism + in-mix selectivity).
    # Homogeneous triples (Blocks B/S) are the controlled microbenchmark;
    # these answer "does it work on a mixed, realistic workload?". REPORT
    # PER-CORE victim IPC for these (the aggregate hides which victim moved).
    #
    # H_stream_* : three DIFFERENT prefetch-friendly streamers -> shows the
    #   attack is not an artifact of running one app x3.
    # H_sel_*    : one victim per class (bwaves=BW, omnetpp=irregular,
    #   gcc=compute). In a SINGLE run the per-core IPC should show the attacker
    #   clobber bwaves, dent omnetpp, and leave gcc ~untouched = selectivity.
    "H_stream_light": ["exchange2", "bwaves", "libquantum", "GemsFDTD"],
    "H_stream_mcf":   ["mcf1554",   "bwaves", "libquantum", "GemsFDTD"],
    "H_stream_atk":   ["pollutew",  "bwaves", "libquantum", "GemsFDTD"],
    "H_sel_light":    ["exchange2", "bwaves", "omnetpp", "gcc"],
    "H_sel_mcf":      ["mcf1554",   "bwaves", "omnetpp", "gcc"],
    "H_sel_atk":      ["pollutew",  "bwaves", "omnetpp", "gcc"],

    # ================================================================
    # GRAND DoS MIX STUDY (SPEC CPU2017 6xx + GAP, heavy checkpoints).
    # Core 0 = attacker; cores 1-3 = victim combination (compute / memory /
    # graph / mixed). Each victim triple is run in TWO versions, everything but
    # core 0 identical:
    #   _atk = core 0 is our crafted attacker (pollutew; swap for burst winner)
    #   _fp  = core 0 is a heavy benign memory hog (605.mcf) -- the FALSE-
    #          POSITIVE control. "does a real memory-intensive app do the same?"
    # Proof per triple: victim slowdown(_atk) > victim slowdown(_fp), with the
    # attacker self-sacrificing. REPORT PER-CORE victim IPC for heterogeneous
    # triples. 18 triples x 2 = 36 mixes; run under pythia + nopf.
    #
    # --- BW-streaming victims (prefetch-friendly: maximum expected damage) ---
    "G1_lbm3_atk":    [ATK,      "lbm17", "lbm17", "lbm17"],
    "G1_lbm3_fp":     ["fp_mcf", "lbm17", "lbm17", "lbm17"],
    "G2_bwaves3_atk": [ATK,      "bwaves17", "bwaves17", "bwaves17"],
    "G2_bwaves3_fp":  ["fp_mcf", "bwaves17", "bwaves17", "bwaves17"],
    "G3_foton3_atk":  [ATK,      "foton17", "foton17", "foton17"],
    "G3_foton3_fp":   ["fp_mcf", "foton17", "foton17", "foton17"],
    "G4_stream3_atk": [ATK,      "lbm17", "bwaves17", "foton17"],
    "G4_stream3_fp":  ["fp_mcf", "lbm17", "bwaves17", "foton17"],
    "G5_stream3b_atk":[ATK,      "cactu17", "roms17", "wrf17"],
    "G5_stream3b_fp": ["fp_mcf", "cactu17", "roms17", "wrf17"],
    # --- Irregular / graph victims (memory-intensive, Pythia helps less) ---
    "G6_mcf3_atk":    [ATK,      "mcfv", "mcfv", "mcfv"],
    "G6_mcf3_fp":     ["fp_mcf", "mcfv", "mcfv", "mcfv"],
    "G7_omnet3_atk":  [ATK,      "omnet17", "omnet17", "omnet17"],
    "G7_omnet3_fp":   ["fp_mcf", "omnet17", "omnet17", "omnet17"],
    "G8_graph3_atk":  [ATK,      "bfs", "cc", "pr"],
    "G8_graph3_fp":   ["fp_mcf", "bfs", "cc", "pr"],
    "G9_irr3_atk":    [ATK,      "mcfv", "omnet17", "bfs"],
    "G9_irr3_fp":     ["fp_mcf", "mcfv", "omnet17", "bfs"],
    "G10_irr3b_atk":  [ATK,      "xalan17", "sssp", "cc"],
    "G10_irr3b_fp":   ["fp_mcf", "xalan17", "sssp", "cc"],
    # --- Mixed-class victims (realistic consolidation) ---
    "G11_bic_atk":    [ATK,      "lbm17", "mcfv", "gcc17"],
    "G11_bic_fp":     ["fp_mcf", "lbm17", "mcfv", "gcc17"],
    "G12_bgc_atk":    [ATK,      "bwaves17", "bfs", "gcc17"],
    "G12_bgc_fp":     ["fp_mcf", "bwaves17", "bfs", "gcc17"],
    "G13_bbi_atk":    [ATK,      "lbm17", "bwaves17", "mcfv"],
    "G13_bbi_fp":     ["fp_mcf", "lbm17", "bwaves17", "mcfv"],
    "G14_fgx_atk":    [ATK,      "foton17", "pr", "xalan17"],
    "G14_fgx_fp":     ["fp_mcf", "foton17", "pr", "xalan17"],
    "G15_rox_atk":    [ATK,      "roms17", "omnet17", "bwaves17"],
    "G15_rox_fp":     ["fp_mcf", "roms17", "omnet17", "bwaves17"],
    # --- Compute-bound victims (NEGATIVE control: should barely move) ---
    "G16_gcc3_atk":   [ATK,      "gcc17", "gcc17", "gcc17"],
    "G16_gcc3_fp":    ["fp_mcf", "gcc17", "gcc17", "gcc17"],
    "G17_comp3_atk":  [ATK,      "gcc17", "exch", "exch"],
    "G17_comp3_fp":   ["fp_mcf", "gcc17", "exch", "exch"],
    "G18_cbg_atk":    [ATK,      "gcc17", "exch", "bfs"],
    "G18_cbg_fp":     ["fp_mcf", "gcc17", "exch", "bfs"],

    # --- Group BAL: deliberate COMPUTE + MEMORY-INTENSIVE blends ------------
    # Each victim triple mixes compute-bound and memory-intensive cores in a
    # set ratio. Tests the attacker on balanced workloads (not the homogeneous
    # extremes): in one run the per-core IPC should show memory cores slow and
    # compute cores nearly steady. Report PER-CORE victim IPC.
    #   2 memory + 1 compute:
    "BAL1_mmc_atk":   [ATK,      "lbm17", "bwaves17", "gcc17"],
    "BAL1_mmc_fp":    ["fp_mcf", "lbm17", "bwaves17", "gcc17"],
    "BAL2_mmc_atk":   [ATK,      "mcfv", "roms17", "exch"],
    "BAL2_mmc_fp":    ["fp_mcf", "mcfv", "roms17", "exch"],
    "BAL3_mmc_atk":   [ATK,      "bfs", "omnet17", "gcc17"],
    "BAL3_mmc_fp":    ["fp_mcf", "bfs", "omnet17", "gcc17"],
    #   1 memory + 2 compute:
    "BAL4_mcc_atk":   [ATK,      "foton17", "gcc17", "exch"],
    "BAL4_mcc_fp":    ["fp_mcf", "foton17", "gcc17", "exch"],
    "BAL5_mcc_atk":   [ATK,      "mcfv", "gcc17", "exch"],
    "BAL5_mcc_fp":    ["fp_mcf", "mcfv", "gcc17", "exch"],
    "BAL6_mcc_atk":   [ATK,      "sssp", "gcc17", "exch"],
    "BAL6_mcc_fp":    ["fp_mcf", "sssp", "gcc17", "exch"],
}

# config name -> (build l1d, l2c, llc, extra run args)
CONFIGS = {
    "pythia": ("no", "multi", "no", ["--l2c_prefetcher_types=scooby", f"--config={CONFIG}"]),
    "nopf":   ("no", "no",    "no", [f"--config={CONFIG}"]),
}


def binary(cfg, cores):
    l1d, l2c, llc, _ = CONFIGS[cfg]
    return ROOT / f"bin/perceptron-{l1d}-{l2c}-{llc}-ship-{cores}core"


def build_one(cfg, cores, log):
    l1d, l2c, llc, _ = CONFIGS[cfg]
    command = ["bash", str(ROOT / "build_champsim.sh"), l1d, l2c, llc, str(cores)]
    print(f"  build {cfg} {cores}-core: {' '.join(command)}", flush=True)
    with log.open("a") as out:
        out.write(f"\n### {cfg} {cores}-core\n")
        r = subprocess.run(command, cwd=ROOT, stdout=out, stderr=subprocess.STDOUT, timeout=3600)
    if r.returncode != 0 or not binary(cfg, cores).is_file():
        raise SystemExit(f"build failed for {cfg} {cores}-core; see {log}")


def metric(text, name):
    m = re.search(rf"^{re.escape(name)}\s+(\S+)\s*$", text, re.MULTILINE)
    return float(m.group(1)) if m else None


def run(cfg, cores, traces, label, args):
    """Run one simulation; return list of per-core IPCs (len == cores)."""
    _, _, _, extra = CONFIGS[cfg]
    trace_paths = [str(TRACES / TRACE_FILE[t]) for t in traces]
    for p in trace_paths:
        if not Path(p).is_file():
            raise SystemExit(f"missing trace: {p}")
    command = [str(binary(cfg, cores)),
               f"--warmup_instructions={args.warmup}",
               f"--simulation_instructions={args.instructions}",
               *extra, "-traces", *trace_paths]
    (args.output / f"{label}.cmd").write_text(" ".join(command) + "\n")
    started = time.time()
    with (args.output / f"{label}.log").open("w") as out:
        r = subprocess.run(command, cwd=ROOT, stdout=out, stderr=subprocess.STDOUT, timeout=args.timeout)
    text = (args.output / f"{label}.log").read_text()
    if r.returncode != 0 or "ChampSim completed" not in text:
        raise SystemExit(f"[{label}] failed (rc={r.returncode}); see {args.output / (label + '.log')}")
    ipcs = [metric(text, f"Core_{i}_IPC") for i in range(cores)]
    if any(v is None for v in ipcs):
        raise SystemExit(f"[{label}] could not parse all Core_i_IPC; see log")
    print(f"    [{label}] {time.time() - started:.0f}s  IPC={['%.3f' % v for v in ipcs]}", flush=True)
    return ipcs


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--output", type=Path, required=True, help="output directory (reused to resume)")
    p.add_argument("--warmup", type=int, default=50_000_000)
    p.add_argument("--instructions", type=int, default=200_000_000)
    p.add_argument("--mixes", nargs="+", default=list(MIXES), choices=list(MIXES),
                   help="subset of mixes to run (default: all)")
    p.add_argument("--configs", nargs="+", default=list(CONFIGS), choices=list(CONFIGS))
    p.add_argument("--skip-build", action="store_true")
    p.add_argument("--timeout", type=int, default=86_400)
    args = p.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)
    results_path = args.output / "results.json"
    results = json.loads(results_path.read_text()) if results_path.is_file() else {"alone": {}, "mix": {}}

    def save():
        results_path.write_text(json.dumps(results, indent=2))

    # Core counts needed: 1 for alone baselines, plus each mix's width (4 or 8).
    needed_cores = sorted({1} | {len(MIXES[m]) for m in args.mixes})
    if not args.skip_build:
        print("Building binaries:", flush=True)
        for cfg in args.configs:
            for cores in needed_cores:
                build_one(cfg, cores, args.output / "build.log")
    for cfg in args.configs:
        for cores in needed_cores:
            if not binary(cfg, cores).is_file():
                raise SystemExit(f"missing binary {binary(cfg, cores)} (drop --skip-build)")

    # distinct benchmarks across the selected mixes
    benches = sorted({b for m in args.mixes for b in MIXES[m]})

    # 1) single-core alone baselines (cached)
    print("\nAlone baselines:", flush=True)
    for cfg in args.configs:
        results["alone"].setdefault(cfg, {})
        for b in benches:
            if b in results["alone"][cfg]:
                continue
            ipc = run(cfg, 1, [b], f"alone_{cfg}_{b}", args)[0]
            results["alone"][cfg][b] = ipc
            save()

    # 2) mixes (core count = mix width: 4 or 8)
    print("\nMixes:", flush=True)
    for cfg in args.configs:
        results["mix"].setdefault(cfg, {})
        for m in args.mixes:
            if m in results["mix"][cfg]:
                continue
            ipcs = run(cfg, len(MIXES[m]), MIXES[m], f"mix_{cfg}_{m}", args)
            results["mix"][cfg][m] = ipcs
            save()

    # 3) weighted speedup + throughput ratio
    report = ["# DoS study: Pythia vs no-prefetcher (4- and 8-core mixes)",
              f"warmup {args.warmup:,} / measured {args.instructions:,} per core", "",
              "Weighted speedup WS = sum_i (IPC_shared_i / IPC_alone_i), normalized within each config.",
              "Ratio = sum(IPC_pythia) / sum(IPC_nopf) in the mix (>1 Pythia helps, <1 Pythia hurts).",
              "degr%_pythia = 100 * (1 - WS_pythia/WS_nopf): how far Pythia's weighted speedup sits",
              "BELOW the no-prefetcher weighted speedup (the with- vs without-Pythia degradation).", "",
              "| mix | cores | benchmarks | WS_pythia | WS_nopf | degr%(nopf->pythia) | "
              "sumIPC_pythia | sumIPC_nopf | Pythia/no-pf |",
              "| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    table = {}
    # report every mix that has results on disk, in definition order, so a run
    # that only adds 8-core mixes still shows the full 4-core table too
    report_mixes = [m for m in MIXES if any(m in results["mix"].get(c, {}) for c in CONFIGS)]
    for m in report_mixes:
        n = len(MIXES[m])
        row = {"benchmarks": MIXES[m], "cores": n}
        for cfg in CONFIGS:
            ipcs = results["mix"].get(cfg, {}).get(m)
            if ipcs is None:
                continue
            ws = sum(ipcs[i] / results["alone"][cfg][MIXES[m][i]] for i in range(n))
            row[cfg] = {"per_core_ipc": ipcs, "sum_ipc": sum(ipcs), "weighted_speedup": ws}
        table[m] = row
        if "pythia" in row and "nopf" in row:
            wp, wn = row["pythia"]["weighted_speedup"], row["nopf"]["weighted_speedup"]
            sp, sn = row["pythia"]["sum_ipc"], row["nopf"]["sum_ipc"]
            traces = ", ".join(nice_name(b) for b in MIXES[m])
            degr = 100 * (1 - wp / wn) if wn else float("nan")
            report.append(f"| {m} | {n} | {traces} | {wp:.3f} | {wn:.3f} | "
                          f"{degr:.1f} | {sp:.3f} | {sn:.3f} | {sp / sn:.3f} |")
    report += ["", "Degradation story: the Pythia/no-pf ratio should fall toward 1 (or below) as mixes "
               "move from compute-bound (M5) to bandwidth-saturated (M1/M4)."]
    results["table"] = table
    save()
    (args.output / "report.md").write_text("\n".join(report) + "\n")
    print("\n" + "\n".join(report))
    print(f"\nWrote {results_path} and {args.output / 'report.md'}")


if __name__ == "__main__":
    main()
