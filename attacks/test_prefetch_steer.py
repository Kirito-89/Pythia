#!/usr/bin/env python3
"""Native validation, plus optional end-to-end Pin/trace validation."""
import csv
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from inspect_trace import inspect

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "attacks/prefetch_steer"
SMALL = ["--size-mib", "1", "--pre", "1000", "--on", "200",
         "--off", "1000", "--pulses", "2", "--post", "1000"]


class NativeTests(unittest.TestCase):
    def test_modes_streams_and_repeatability(self):
        for mode in ("stream", "random", "chase", "quiet"):
            for streams in (1, 2, 4, 8, 16):
                with self.subTest(mode=mode, streams=streams):
                    command = [str(APP), *SMALL, "--mode", mode, "--streams", str(streams)]
                    first = subprocess.check_output(command)
                    self.assertEqual(first, subprocess.check_output(command))

    def test_bad_arguments(self):
        for args in (["--mode", "oops"], ["--streams", "3"], ["--size-mib", "0"],
                     ["--seed", "0"], ["--on", "-1"], ["--on", "1x"], ["--on"],
                     ["--gap-rounds", "-1"], ["--gap-rounds", "1000000000001"],
                     ["--on-sequence", "200"], ["--on-sequence", "200,nope"],
                     ["--on-sequence", "200,0"], ["--on-sequence", "200,1000000000001"],
                     ["--mode", "chase", "--write-every", "1"],
                     ["--pre", "0", "--pulses", "0", "--post", "0"]):
            with self.subTest(args=args):
                self.assertNotEqual(subprocess.run([str(APP), *args], capture_output=True).returncode, 0)

    def test_writes_change_checksum(self):
        base = [str(APP), *SMALL, "--on", "20000", "--streams", "1"]
        self.assertNotEqual(subprocess.check_output(base),
                            subprocess.check_output([*base, "--write-every", "1"]))

    def test_pacing_preserves_memory_work(self):
        for mode in ("stream", "random", "chase"):
            for streams in (1, 2, 4, 8, 16):
                with self.subTest(mode=mode, streams=streams):
                    base = [str(APP), *SMALL, "--mode", mode, "--streams", str(streams)]
                    self.assertEqual(subprocess.check_output(base),
                                     subprocess.check_output([*base, "--gap-rounds", "32"]))

    def test_mixed_pulse_lengths_preserve_total_stream_work(self):
        base = [str(APP), *SMALL, "--mode", "stream", "--streams", "8"]
        uniform = subprocess.check_output(base)
        mixed = subprocess.check_output([*base, "--on-sequence", "100,300"])
        self.assertEqual(uniform, mixed)


@unittest.skipUnless(os.environ.get("PREFETCHSTEER_TEST_PIN") == "1", "set PREFETCHSTEER_TEST_PIN=1 for Pin integration")
class PinTests(unittest.TestCase):
    def record(self, directory, extra=(), tracer_args=()):
        trace = Path(directory) / "test.trace"
        phases = Path(directory) / "test.csv"
        command = [str(Path(os.environ["PIN_ROOT"]) / "pin"), "-t",
                   str(ROOT / "tracer/obj-intel64/champsim_tracer.so"),
                   "-o", str(trace), "-phases", str(phases),
                   *([] if "-t" in tracer_args else ["-t", "1000000"]),
                   *tracer_args, "--", str(APP), *SMALL, *extra]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0,
                         f"Pin failed ({result.returncode}): {result.stdout}\n{result.stderr}")
        return trace, phases

    def test_pacing_reduces_trace_access_density(self):
        for mode in ("stream", "random", "chase"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                trace, phases = self.record(directory, ["--mode", mode], ["-roi", "1"])
                fast = [r for r in inspect(trace, phases) if r["phase"] == "pressure"]
                trace, phases = self.record(directory, ["--mode", mode, "--gap-rounds", "32"],
                                            ["-roi", "1"])
                paced = [r for r in inspect(trace, phases) if r["phase"] == "pressure"]
                self.assertEqual(len(fast), len(paced))
                for original, delayed in zip(fast, paced):
                    self.assertGreaterEqual(delayed["instructions"] - original["instructions"],
                                            4 * 32 * 199)
                    self.assertGreaterEqual(delayed["loads"], 800)
                    self.assertLess(delayed["loads"] / delayed["instructions"],
                                    original["loads"] / original["instructions"])

    def test_phase_order_and_memory_pressure(self):
        for mode in ("stream", "random", "chase", "quiet"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                trace, phases = self.record(directory, ["--mode", mode], ["-roi", "1"])
                rows = inspect(trace, phases)
                expected = "quiet-control" if mode == "quiet" else "pressure"
                self.assertEqual([r["phase"] for r in rows],
                                 ["marker-overhead", "pre", expected, "off", expected, "off", "post"])
                for row in rows:
                    if row["phase"] in ("pre", "off", "post"):
                        self.assertLess(row["loads"] + row["stores"], 60)
                    if row["phase"] == "pressure":
                        self.assertGreaterEqual(row["loads"], 800)  # 200 batches * 4 streams

    def test_mixed_pulse_trace_has_distinct_pressure_intervals(self):
        with tempfile.TemporaryDirectory() as directory:
            trace, phases = self.record(directory, ["--on-sequence", "50,200"], ["-roi", "1"])
            pressure = [row for row in inspect(trace, phases) if row["phase"] == "pressure"]
            self.assertEqual(len(pressure), 2)
            self.assertGreater(pressure[1]["loads"], 3 * pressure[0]["loads"])

    def test_legacy_skip_limit_and_fresh_output(self):
        with tempfile.TemporaryDirectory() as directory:
            # The output must be replaced, not appended, when recording again.
            for _ in range(2):
                trace, _ = self.record(directory, tracer_args=["-s", "100", "-t", "123"])
                self.assertEqual(trace.stat().st_size, 123 * 64)

    def test_truncated_roi_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            trace, phases = self.record(directory, tracer_args=["-roi", "1", "-t", "123"])
            with phases.open() as stream:
                events = list(csv.DictReader(stream))
            self.assertEqual(events[-1]["event"], "trace_limit")
            self.assertEqual(trace.stat().st_size, 123 * 64)
            with self.assertRaises(ValueError):
                inspect(trace, phases)


if __name__ == "__main__":
    unittest.main()
