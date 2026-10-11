#!/usr/bin/env python3
"""Build and record a complete PrefetchSteer schedule, with a provenance manifest."""
import argparse
import csv
import gzip
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
RECORD_BYTES = 64  # non-CloudSuite x86-64 ChampSim trace format


def sha256(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pin-root", default=os.environ.get("PIN_ROOT"))
    parser.add_argument("--output", required=True, type=Path, help="new output directory")
    parser.add_argument("--max-instructions", type=int, default=100_000_000,
                        help="safety cap; reaching this before ROI end is an error")
    parser.add_argument("--keep-raw", action="store_true")
    parser.add_argument("application_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not args.pin_root:
        parser.error("set PIN_ROOT or pass --pin-root")
    pin_root = Path(args.pin_root).resolve()
    if not (pin_root / "pin").is_file():
        parser.error("pin executable not found in PIN_ROOT")
    if args.max_instructions <= 0 or args.max_instructions >= 2**64:
        parser.error("max-instructions must be in [1, 2**64-1]")
    application_args = args.application_args
    if application_args[:1] == ["--"]:
        application_args = application_args[1:]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)  # never append/overwrite an experiment
    env = dict(os.environ, PIN_ROOT=str(pin_root))
    with (output / "build.log").open("w") as log:
        # Rebuild so build.log records the actual compiler and flags used.
        subprocess.run(["make", "-B", "-C", str(ROOT / "attacks")],
                       env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
        subprocess.run(["bash", str(ROOT / "tracer/make_tracer.sh")],
                       env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
    raw = output / "attacker.trace"
    phases = output / "phases.csv"
    app = ROOT / "attacks/prefetch_steer"
    tool = ROOT / "tracer/obj-intel64/champsim_tracer.so"
    command = [str(pin_root / "pin"), "-t", str(tool), "-roi", "1",
               "-phases", str(phases), "-o", str(raw), "-s", "0",
               "-t", str(args.max_instructions), "--", str(app), *application_args]
    manifest = {
        "command": command,
        "repo_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "repo_status": subprocess.check_output(["git", "status", "--short"], cwd=ROOT, text=True),
        "host": platform.platform(), "machine": platform.machine(),
        "record_bytes": RECORD_BYTES, "complete_roi": False,
        "schedule_units": "application rounds/batches; phases.csv uses emitted attacker instructions",
        "source_sha256": {str(p.relative_to(ROOT)): sha256(p) for p in (
            ROOT / "attacks/prefetch_steer.cpp", ROOT / "attacks/Makefile",
            ROOT / "tracer/champsim_tracer.cpp", Path(__file__))},
        "binary_sha256": {"application": sha256(app), "tracer": sha256(tool),
                          "pin": sha256(pin_root / "pin")},
    }
    with (output / "application.log").open("w") as log:
        result = subprocess.run(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    manifest["returncode"] = result.returncode
    rows = []
    if phases.exists():
        with phases.open() as stream:
            rows = list(csv.DictReader(stream))
        manifest["phase_events"] = rows
    if raw.exists():
        size = raw.stat().st_size
        manifest.update(trace_bytes=size, trace_records=size // RECORD_BYTES,
                        trace_sha256=sha256(raw))
        manifest["complete_roi"] = bool(
            result.returncode == 0 and size > 0 and size % RECORD_BYTES == 0 and
            rows and rows[0]["event"] == "roi_begin" and rows[-1]["event"] == "roi_end" and
            int(rows[-1]["trace_instruction"]) == size // RECORD_BYTES)
    if manifest["complete_roi"]:
        compressed = output / "attacker.trace.gz"
        with raw.open("rb") as source, compressed.open("wb") as target:
            with gzip.GzipFile(filename="", mode="wb", fileobj=target, mtime=0) as archive:
                shutil.copyfileobj(source, archive)
        manifest["compressed_sha256"] = sha256(compressed)
        if not args.keep_raw:
            raw.unlink()
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    if not manifest["complete_roi"]:
        raise RuntimeError(f"incomplete trace; inspect {output}/application.log and phases.csv. "
                           "If trace_limit was reached, increase --max-instructions in a new output directory.")
    print(f"Created {output / 'attacker.trace.gz'} ({manifest['trace_records']} instructions)")
    print(f"Phase boundaries: {phases}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
