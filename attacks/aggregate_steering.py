#!/usr/bin/env python3
"""Collect complete steering experiments without treating windows as replicates.

Each experiment contributes one observation per attack/quiet pair and phase.
Groups separate victims, simulator/config hashes, and attacker schedules. A
recorded attacker trace reused for the same group counts only once in group
statistics, even when its run directory was copied or rerun. Distinct trace
files are not necessarily independent traffic patterns (e.g. ASLR can move
addresses without changing a streaming access sequence).
"""
import argparse
import csv
import hashlib
import json
from collections import defaultdict
from pathlib import Path
import statistics
import sys

METRICS = ("slowdown_pct", "zero_action_fraction_delta",
           "prefetches_per_ki_delta", "high_bw_fraction_delta",
           "mean_degree_delta", "high_bw_reward_fraction_delta")
SCHEDULE = ("warmup", "instructions", "pre", "on", "on_sequence", "off",
            "pulses", "post", "streams", "size_mib", "gap_rounds")


def rows(path):
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream))


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def q_at_warmup(path):
    value = None
    complete = False
    with path.open(errors="replace") as stream:
        for line in stream:
            if line.startswith("Steering_core_0_q_hash_at_roi "):
                value = line.split()[-1]
            if line.startswith("ChampSim completed victim CPU 0"):
                complete = True
            if "Repeating trace" in line or "STEERING_TRACE_EOF" in line:
                raise ValueError(f"trace replay or EOF in {path}")
    if value is None or not complete:
        raise ValueError(f"missing warmup fingerprint or completion marker: {path}")
    return value


def write_csv(path, data):
    if not data:
        return
    fields = list(data[0])
    for row in data[1:]:
        fields.extend(key for key in row if key not in fields)
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(data)


def source_metadata(directory, manifest):
    params = manifest["parameters"]
    provenance = manifest["provenance"]
    schedule = {key: params.get(key, 0 if key == "gap_rounds" else None)
                for key in SCHEDULE}
    # --on-sequence overrides --on; the fallback --on value is irrelevant.
    if schedule["on_sequence"] is not None:
        schedule["on"] = None
    return {
        "experiment": str(directory),
        "victim": Path(params["victim"]).name,
        "victim_sha256": provenance["victim"]["sha256"],
        "config_sha256": provenance["config"]["sha256"],
        "binary_sha256": provenance["binary"]["sha256"],
        "warmup_instructions": int(params["warmup"]),
        "measurement_instructions": int(params["instructions"]),
        "attacker_seed": params.get("seed"),
        "schedule": json.dumps(schedule, sort_keys=True, separators=(",", ":")),
    }


def load_experiment(directory, expected_warmup, expected_instructions):
    manifest_path = directory / "experiment.json"
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("status") != "complete":
        raise ValueError(f"{directory}: experiment is not complete")
    metadata = source_metadata(directory, manifest)
    if expected_warmup is not None and metadata["warmup_instructions"] != expected_warmup:
        raise ValueError(f"{directory}: warmup is not {expected_warmup}")
    if expected_instructions is not None and metadata["measurement_instructions"] != expected_instructions:
        raise ValueError(f"{directory}: measurement is not {expected_instructions}")
    run_by_name = {run["name"]: run for run in manifest["runs"]}
    for run in run_by_name.values():
        if run.get("status") != "complete":
            raise ValueError(f"{directory}: incomplete run {run['name']}")
        interval = directory / run["intervals"]
        if digest(interval) != run.get("intervals_sha256"):
            raise ValueError(f"{directory}: interval checksum mismatch in {run['name']}")
    pair_by_name = {pair["name"]: pair for pair in manifest["pairs"]}
    q_hashes = {}
    for pair in pair_by_name.values():
        attack = run_by_name[pair["attack"]]
        quiet = run_by_name[pair["quiet"]]
        attack_hash = q_at_warmup(directory / attack["log"])
        quiet_hash = q_at_warmup(directory / quiet["log"])
        if attack_hash != quiet_hash:
            raise ValueError(f"{directory}: warmup Q fingerprint differs in {pair['name']}")
        q_hashes[pair["name"]] = attack_hash
    comparisons = rows(directory / "comparison.csv")
    pulses_path = directory / "pulse_comparison.csv"
    pulses = rows(pulses_path) if pulses_path.exists() else []

    def enrich(row):
        pair = pair_by_name.get(row["pair"])
        if pair is None:
            raise ValueError(f"{directory}: unknown pair {row['pair']}")
        mode = pair["mode"]
        trace = manifest["traces"][mode]
        result = {**metadata, "mode": mode, "control": pair["control"],
                  "pair": pair["name"], "warmup_q_hash": q_hashes[pair["name"]],
                  "attacker_trace_sha256": trace["compressed_sha256"],
                  "quiet_trace_sha256": manifest["traces"][f"quiet-{mode}"]["compressed_sha256"]}
        for key, value in row.items():
            if key in ("pair", "mode", "control"):
                continue
            result[key] = value
        return result

    return manifest, [enrich(row) for row in comparisons], [enrich(row) for row in pulses]


def grouped_summary(comparisons):
    groups = defaultdict(dict)
    for row in comparisons:
        key = (row["victim_sha256"], row["config_sha256"], row["binary_sha256"],
               row["schedule"], row["mode"], row["control"], row["phase"])
        # A rerun of the same attacker trace is not a new recording.
        groups[key].setdefault(row["attacker_trace_sha256"], row)
    result = []
    for key, unique in sorted(groups.items(), key=lambda item: tuple(str(x) for x in item[0])):
        samples = list(unique.values())
        entry = {field: samples[0][field] for field in
                 ("victim", "victim_sha256", "config_sha256", "binary_sha256",
                  "schedule", "mode", "control", "phase")}
        entry["distinct_attacker_recordings"] = len(samples)
        entry["experiment_dirs"] = ";".join(sorted(row["experiment"] for row in samples))
        for metric in METRICS:
            values = [float(row[metric]) for row in samples if row.get(metric) not in (None, "")]
            if values:
                entry[metric + "_mean"] = statistics.mean(values)
                entry[metric + "_median"] = statistics.median(values)
                entry[metric + "_min"] = min(values)
                entry[metric + "_max"] = max(values)
                entry[metric + "_sd"] = statistics.stdev(values) if len(values) > 1 else ""
        result.append(entry)
    return result


def causal_controls(comparisons):
    by_experiment = defaultdict(dict)
    for row in comparisons:
        by_experiment[(row["experiment"], row["mode"], row["phase"])][row["control"]] = row
    result = []
    for (experiment, mode, phase), controls in sorted(by_experiment.items()):
        if phase not in ("pressure", "post", "late_post") or "learned" not in controls:
            continue
        learned = controls["learned"]
        entry = {key: learned[key] for key in ("experiment", "victim", "victim_sha256",
                "config_sha256", "binary_sha256", "schedule", "attacker_trace_sha256")}
        entry.update(mode=mode, phase=phase)
        for control in ("learned", "frozen", "reward-ablation"):
            row = controls.get(control)
            for metric in ("slowdown_pct", "zero_action_fraction_delta",
                           "prefetches_per_ki_delta", "high_bw_fraction_delta"):
                entry[control.replace("-", "_") + "_" + metric] = row.get(metric, "") if row else ""
        for other in ("frozen", "reward-ablation"):
            row = controls.get(other)
            for metric in ("zero_action_fraction_delta", "prefetches_per_ki_delta"):
                label = "learned_minus_" + other.replace("-", "_") + "_" + metric
                entry[label] = (float(learned[metric]) - float(row[metric])) if row else ""
        result.append(entry)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="new aggregation directory")
    parser.add_argument("--expected-warmup", type=int)
    parser.add_argument("--expected-instructions", type=int)
    parser.add_argument("experiments", nargs="+", type=Path)
    args = parser.parse_args()
    if any(value is not None and value <= 0 for value in
           (args.expected_warmup, args.expected_instructions)):
        parser.error("expected instruction counts must be positive")
    output = args.output.resolve()
    if output.exists():
        parser.error(f"output already exists: {output}")
    directories = [path.resolve() for path in args.experiments]
    if len(set(directories)) != len(directories):
        parser.error("an experiment directory was listed twice")
    comparisons, pulses, inputs = [], [], []
    for directory in directories:
        manifest, run_comparisons, run_pulses = load_experiment(
            directory, args.expected_warmup, args.expected_instructions)
        comparisons.extend(run_comparisons)
        pulses.extend(run_pulses)
        inputs.append({"experiment": str(directory), "experiment_sha256": digest(directory / "experiment.json"),
                       "victim": Path(manifest["parameters"]["victim"]).name,
                       "attacker_trace_sha256": {name: value["compressed_sha256"] for name, value in
                                                 manifest["traces"].items() if name in manifest["parameters"]["modes"]}})
    summary = grouped_summary(comparisons)
    controls = causal_controls(comparisons)
    output.mkdir(parents=True)
    write_csv(output / "campaign_rows.csv", comparisons)
    write_csv(output / "campaign_pulses.csv", pulses)
    write_csv(output / "campaign_summary.csv", summary)
    write_csv(output / "causal_controls.csv", controls)
    (output / "inputs.json").write_text(json.dumps(inputs, indent=2) + "\n")
    lines = ["# Steering campaign aggregation", "",
             f"Complete experiment directories: {len(directories)}. Each condition/phase contributes one observation per distinct attacker trace file.",
             "Repeated use of the same attacker trace in the same workload/configuration group is listed in raw rows but counted once in group means.",
             "Distinct recordings can share the same access pattern; grouped means and standard deviations are descriptive, and 10,000-cycle windows are not independent samples.", "",
             "| Victim | Mode | Control | Phase | Recordings | Mean slowdown % | Mean Δ no-prefetch fraction | Mean Δ PF / 1k instr. |",
             "|---|---|---|---|---:|---:|---:|---:|"]
    for row in summary:
        if row["phase"] == "all":
            continue
        lines.append("| {victim} | {mode} | {control} | {phase} | {distinct_attacker_recordings} | {slowdown:.3f} | {zero:.5f} | {pf:.4f} |".format(
            **row, slowdown=row.get("slowdown_pct_mean", float("nan")),
            zero=row.get("zero_action_fraction_delta_mean", float("nan")),
            pf=row.get("prefetches_per_ki_delta_mean", float("nan"))))
    lines += ["", "For reward-steering evidence, compare `learned` with `frozen` and `reward-ablation` in `causal_controls.csv`, especially after pressure. An IPC change that also occurs with frozen learning is not attributable to learned policy from these data.",
              "All individual matched-cycle effects are in `campaign_rows.csv`; per-pulse effects are in `campaign_pulses.csv`.", ""]
    (output / "report.md").write_text("\n".join(lines))
    print(f"Aggregated {len(directories)} experiments into {output}")
    print(f"Conditions/phases: {len(comparisons)}; pulse rows: {len(pulses)}; groups: {len(summary)}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        sys.exit(str(error))
