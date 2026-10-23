#!/usr/bin/env python3
"""Exercise steering measurements and controls using deterministic, tiny traces.

Build the two-core simulator first, then run this file with Python 3. Set
PREFETCHSTEER_SIMULATOR to test another binary; no Pin installation or downloaded
victim trace is required. The traces use the same 64-byte x86 format as Pin.
"""

import csv
import gzip
import os
from pathlib import Path
import re
import struct
import subprocess
import tempfile
import unittest

from run_steering_experiment import normalize_quiet_prefix


ROOT = Path(__file__).resolve().parents[1]
SIMULATOR = Path(os.environ.get(
    "PREFETCHSTEER_SIMULATOR", ROOT / "bin/perceptron-no-multi-no-ship-2core"
)).resolve()
TIMEOUT = float(os.environ.get("PREFETCHSTEER_TEST_TIMEOUT", "120"))
RECORD = struct.Struct("<QBB2B4B2Q4Q")
WARMUP = 512
MEASURED = 10000
TRACE_LENGTH = 65536


def write_trace(path, count):
    """Independent loads from sequential cache lines at four fixed load PCs."""
    with gzip.open(path, "wb") as stream:
        for index in range(count):
            stream.write(RECORD.pack(
                0x400000 + 4 * (index % 4), 0, 0,
                1 + index % 8, 0, 0, 0, 0, 0, 0, 0,
                0x10000000 + 64 * index, 0, 0, 0,
            ))


def read_csv(path):
    with path.open() as stream:
        return list(csv.DictReader(stream))


class SteeringSimulatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not SIMULATOR.is_file():
            raise RuntimeError(
                f"Simulator missing: {SIMULATOR}. Build the two-core simulator "
                "or set PREFETCHSTEER_SIMULATOR."
            )
        cls.temporary = tempfile.TemporaryDirectory(prefix="steering-simulator-tests-")
        cls.directory = Path(cls.temporary.name)
        cls.trace = cls.directory / "stream.trace.gz"
        cls.short_trace = cls.directory / "short.trace.gz"
        write_trace(cls.trace, TRACE_LENGTH)
        write_trace(cls.short_trace, 32)
        cls.phases = cls.directory / "pin-phases.csv"
        cls.phases.write_text(
            "event,trace_instruction,phase,pulse\n"
            "roi_begin,0,0,0\n"
            "phase,8,0,0\n"
            "phase,2000,1,0\n"
            "phase,4000,2,0\n"
            "phase,6000,1,1\n"
            "phase,8000,3,2\n"
            f"roi_end,{TRACE_LENGTH},0,0\n"
        )
        cls.run_number = 0

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def simulate(self, *options, trace=None, logged=True, valid=True):
        type(self).run_number += 1
        output = self.directory / f"run-{self.run_number}.csv"
        selected_trace = trace or self.trace
        command = [
            str(SIMULATOR), "--config=config/pythia.ini",
            "--l2c_prefetcher_types=scooby",
            f"--warmup_instructions={WARMUP}",
            f"--simulation_instructions={MEASURED}",
        ]
        if logged:
            command += [f"--steering_log={output}", "--steering_epoch=10000"]
        command += [*options, "-traces", str(selected_trace), str(selected_trace)]
        result = subprocess.run(
            command, cwd=ROOT, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, timeout=TIMEOUT,
        )
        if valid:
            self.assertEqual(result.returncode, 0, result.stdout[-8000:])
            self.assertIn("ChampSim completed", result.stdout)
            self.assertNotIn("Repeating trace", result.stdout)
            if logged:
                self.assertTrue(output.is_file(), result.stdout[-8000:])
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout[-8000:])
            self.assertIn("Steering:", result.stdout, result.stdout[-8000:])
        return result.stdout, output

    def metric(self, text, name):
        match = re.search(rf"^{re.escape(name)}\s+(\S+)\s*$", text, re.MULTILINE)
        self.assertIsNotNone(match, f"Missing metric {name}: {text[-8000:]}")
        return match.group(1)

    def test_interval_accounting_excludes_warmup_and_stops_at_core_completion(self):
        text, output = self.simulate(
            f"--steering_phases={self.phases}", "--steering_no_repeat=true"
        )
        rows = read_csv(output)
        self.assertEqual({int(row["core"]) for row in rows}, {0, 1})
        for core in (0, 1):
            with self.subTest(core=core):
                own = [row for row in rows if int(row["core"]) == core]
                self.assertGreater(len(own), 1)
                warmup = re.search(
                    rf"Warmup complete CPU\s+{core}\s+instructions:\s+(\d+)"
                    r"\s+cycles:\s+(\d+)", text,
                )
                self.assertIsNotNone(warmup)
                retired_at_start, cycle_at_start = map(int, warmup.groups())
                self.assertGreater(retired_at_start, WARMUP)
                self.assertEqual(int(own[0]["retired_begin"]), retired_at_start)
                # CSV boundaries use the shared uncore clock. Legacy warmup
                # finishes midway through a per-core tick, so one core's
                # printed local origin can differ from it by one cycle.
                self.assertLessEqual(abs(int(own[0]["cycle_begin"]) - cycle_at_start), 1)
                for index, row in enumerate(own):
                    self.assertGreater(int(row["cycle_end"]), int(row["cycle_begin"]))
                    self.assertEqual(
                        int(row["instructions"]),
                        int(row["retired_end"]) - int(row["retired_begin"]),
                    )
                    self.assertGreaterEqual(int(row["instructions"]), 0)
                    if index:
                        self.assertEqual(row["cycle_begin"], own[index - 1]["cycle_end"])
                        self.assertEqual(row["retired_begin"], own[index - 1]["retired_end"])
                measured = int(self.metric(text, f"Core_{core}_instructions"))
                cycles = int(self.metric(text, f"Core_{core}_cycles"))
                self.assertGreaterEqual(measured, MEASURED)
                self.assertEqual(sum(int(row["instructions"]) for row in own), measured)
                self.assertEqual(int(own[-1]["retired_end"]), retired_at_start + measured)
                logged_cycles = sum(int(row["cycle_end"]) - int(row["cycle_begin"])
                                    for row in own)
                self.assertLessEqual(abs(logged_cycles - cycles), 1)
                self.assertLessEqual(abs(int(own[-1]["cycle_end"]) - (cycle_at_start + cycles)), 1)
        self.assertTrue({"pre", "pressure", "off", "post"}.issubset(
            {row["phase"] for row in rows}
        ))

    def test_logging_does_not_change_legacy_performance(self):
        plain, _ = self.simulate(logged=False)
        logged, _ = self.simulate(f"--steering_phases={self.phases}")
        for core in (0, 1):
            for suffix in ("instructions", "cycles", "IPC"):
                name = f"Core_{core}_{suffix}"
                with self.subTest(metric=name):
                    self.assertEqual(self.metric(plain, name), self.metric(logged, name))

    def test_freeze_keeps_decisions_and_rewards_but_stops_learning(self):
        for featurewise in ("true", "false"):
            with self.subTest(featurewise=featurewise):
                text, output = self.simulate(
                    f"--scooby_enable_featurewise_engine={featurewise}",
                    "--steering_freeze_learning=true", "--steering_no_repeat=true",
                )
                own = [row for row in read_csv(output) if row["core"] == "0"]
                self.assertGreater(len(own), 1)
                self.assertGreater(sum(int(row["decisions"]) for row in own), 0)
                self.assertGreater(sum(int(row["rewards"]) for row in own), 0)
                self.assertGreater(sum(int(row["frozen_updates"]) for row in own), 0)
                self.assertEqual(sum(int(row["updates"]) for row in own), 0)
                self.assertEqual(
                    sum(int(row["train_attempts"]) for row in own),
                    sum(int(row["frozen_updates"]) for row in own),
                )
                roi_hash = self.metric(text, "Steering_core_0_q_hash_at_roi")
                self.assertEqual({row["q_hash"] for row in own}, {roi_hash})

    def test_learning_enabled_applies_updates(self):
        text, output = self.simulate("--steering_freeze_learning=false")
        own = [row for row in read_csv(output) if row["core"] == "0"]
        self.assertGreater(sum(int(row["updates"]) for row in own), 0)
        self.assertEqual(sum(int(row["frozen_updates"]) for row in own), 0)
        self.assertEqual(
            sum(int(row["train_attempts"]) for row in own),
            sum(int(row["updates"]) for row in own),
        )
        self.assertTrue(any(row["q_hash"] != self.metric(text, "Steering_core_0_q_hash_at_roi")
                            for row in own))

    def test_malformed_or_unordered_pin_phases_are_rejected(self):
        header = "event,trace_instruction,phase,pulse\n"
        cases = {
            "unordered": "roi_begin,0,0,0\nphase,20,1,0\nphase,10,2,0\nroi_end,100,0,0\n",
            "bad-instruction": "roi_begin,0,0,0\nphase,oops,1,0\nroi_end,100,0,0\n",
            "missing-end": "roi_begin,0,0,0\nphase,20,1,0\n",
            "bad-event": "roi_begin,0,0,0\nunknown,20,1,0\nroi_end,100,0,0\n",
            "bad-phase": "roi_begin,0,0,0\nphase,20,99,0\nroi_end,100,0,0\n",
        }
        for name, body in cases.items():
            with self.subTest(case=name):
                path = self.directory / f"invalid-{name}.csv"
                path.write_text(header + body)
                self.simulate(f"--steering_phases={path}", valid=False)

    def test_cycle_schedule_and_mutually_exclusive_phase_sources(self):
        path = self.directory / "cycle-phases.csv"
        path.write_text(
            "cycle,phase,pulse\n0,pre,0\n10000,pressure,0\n"
            "20000,off,0\n30000,post,1\n"
        )
        _, output = self.simulate(f"--steering_cycle_phases={path}")
        rows = read_csv(output)
        self.assertEqual({row["phase"] for row in rows}, {"pre", "pressure", "off", "post"})
        self.simulate(
            f"--steering_cycle_phases={path}", f"--steering_phases={self.phases}", valid=False
        )
        path.write_text("cycle,phase,pulse\n0,pre,0\n20000,pressure,0\n10000,post,1\n")
        self.simulate(f"--steering_cycle_phases={path}", valid=False)

    def test_no_repeat_fails_on_short_trace(self):
        text, _ = self.simulate("--steering_no_repeat=true", trace=self.short_trace, valid=False)
        self.assertRegex(text.lower(), r"trace|eof")
        self.assertNotIn("Repeating trace", text)

    def test_victim_stop_and_attacker_prefetch_bypass(self):
        text, path = self.simulate(
            "--steering_stop_at_victim=true", "--steering_no_repeat=true",
            "--steering_disable_attacker_prefetch=true",
        )
        self.assertIn("ChampSim completed victim CPU 0", text)
        self.assertEqual(self.metric(text, "Core_0_measurement_complete"), "1")
        self.assertEqual(self.metric(text, "Core_1_L2_prefetch_disabled"), "1")
        own = [row for row in read_csv(path) if row["core"] == "1"]
        self.assertTrue(own)
        self.assertEqual(sum(int(row["decisions"]) for row in own), 0)
        self.assertEqual(sum(int(row["instructions"]) for row in own),
                         int(self.metric(text, "Core_1_instructions")))

    def test_control_trace_reuses_identical_warmup_prefix(self):
        attack = self.directory / "prefix-attack.trace.gz"
        quiet = self.directory / "prefix-quiet.trace.gz"
        record = RECORD.pack(0x401000, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0,
                             0x10000000, 0, 0, 0)
        altered = RECORD.pack(0x401000, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0,
                              0x20000000, 0, 0, 0)
        with gzip.open(attack, "wb") as stream:
            stream.write(record * 9000)
        with gzip.open(quiet, "wb") as stream:
            stream.write(altered * 9000)
        a_phases = self.directory / "prefix-attack.csv"
        q_phases = self.directory / "prefix-quiet.csv"
        a_phases.write_text("event,trace_instruction,phase,pulse\n"
                            "roi_begin,0,0,0\nphase,8,0,0\nphase,8000,1,0\nroi_end,9000,0,0\n")
        q_phases.write_text("event,trace_instruction,phase,pulse\n"
                            "roi_begin,0,0,0\nphase,8,0,0\nphase,8000,4,0\nroi_end,9000,0,0\n")
        normalized = normalize_quiet_prefix(
            {"trace": str(attack), "phases": str(a_phases), "compressed_sha256": "attack"},
            {"trace": str(quiet), "phases": str(q_phases), "compressed_sha256": "quiet"},
            self.directory / "normalized-prefix", 1000)
        self.assertEqual(normalized["normalized_prefix_records"], 7984)
        with gzip.open(normalized["trace"], "rb") as stream:
            body = stream.read()
        self.assertEqual(body[:7984 * 64], record * 7984)
        self.assertEqual(body[7984 * 64:], altered * (9000 - 7984))

    def test_reward_ablation_starts_after_identical_warmup(self):
        # Threshold zero makes every reward high-BW in the normal run, so the
        # control path is exercised even in a tiny synthetic simulation.
        normal, normal_csv = self.simulate("--scooby_high_bw_thresh=0")
        ablated, ablated_csv = self.simulate(
            "--scooby_high_bw_thresh=0",
            "--steering_disable_hbw_reward_after_warmup=true",
        )
        self.assertEqual(self.metric(normal, "Steering_core_0_q_hash_at_roi"),
                         self.metric(ablated, "Steering_core_0_q_hash_at_roi"))
        normal_rows = [r for r in read_csv(normal_csv) if r["core"] == "0"]
        ablated_rows = [r for r in read_csv(ablated_csv) if r["core"] == "0"]
        self.assertGreater(sum(int(r["high_bw_rewards"]) for r in normal_rows), 0)
        self.assertEqual(sum(int(r["high_bw_rewards"]) for r in ablated_rows), 0)
        self.assertGreater(sum(int(r["updates"]) for r in ablated_rows), 0)


    def test_oracle_rejects_invalid_configuration(self):
        cases = [
            ("--steering_oracle_enable=true",),  # conditional mode needs a target
            ("--steering_oracle_enable=true", "--steering_oracle_target_action=999"),
            ("--steering_oracle_enable=true", "--steering_oracle_mode=budget",
             "--steering_oracle_budget_num=2", "--steering_oracle_budget_den=1"),
            ("--steering_oracle_enable=true", "--steering_oracle_mode=sideways"),
        ]
        for options in cases:
            with self.subTest(options=options):
                self.simulate(*options, "--steering_stop_at_victim=true", valid=False)

    def test_oracle_budget_full_suppress_removes_attacker_dram_without_deadlock(self):
        # Full suppression short-circuits every attacker DRAM read. The attacker
        # core keeps running (no deadlock on either core), the victim completes,
        # and the reported loud fraction is zero.
        text, _ = self.simulate(
            "--steering_oracle_enable=true", "--steering_oracle_mode=budget",
            "--steering_oracle_budget_num=1", "--steering_oracle_budget_den=1",
            "--steering_stop_at_victim=true", "--steering_no_repeat=true",
        )
        requests = int(self.metric(text, "steering_oracle_attacker_requests"))
        suppressed = int(self.metric(text, "steering_oracle_attacker_suppressed"))
        loud = int(self.metric(text, "steering_oracle_attacker_loud"))
        self.assertGreater(requests, 0)
        self.assertEqual(suppressed, requests)
        self.assertEqual(loud, 0)
        self.assertEqual(float(self.metric(text, "steering_oracle_attacker_loud_fraction")), 0.0)
        self.assertEqual(self.metric(text, "Core_0_measurement_complete"), "1")
        self.assertGreater(int(self.metric(text, "Core_1_instructions")), 0)

    def test_oracle_conditional_gate_is_action_conditional(self):
        # The gate suppresses the attacker's DRAM traffic only while the victim's
        # latest action is the target. Targeting the action the synthetic victim
        # actually chooses (1) suppresses; targeting one it never chooses (0)
        # never suppresses. Both complete without deadlock.
        def run(target):
            text, _ = self.simulate(
                "--steering_oracle_enable=true", "--steering_oracle_mode=conditional",
                f"--steering_oracle_target_action={target}", "--steering_stop_at_victim=true",
            )
            req = int(self.metric(text, "steering_oracle_attacker_requests"))
            sup = int(self.metric(text, "steering_oracle_attacker_suppressed"))
            loud = int(self.metric(text, "steering_oracle_attacker_loud"))
            self.assertGreater(req, 0)
            self.assertEqual(sup + loud, req)
            frac = float(self.metric(text, "steering_oracle_attacker_loud_fraction"))
            self.assertAlmostEqual(frac, loud / req, places=6)
            return sup, loud, req

        chosen_sup, _, chosen_req = run(1)       # dominant action -> suppressed
        unchosen_sup, unchosen_loud, unchosen_req = run(0)  # unchosen -> never suppressed
        self.assertGreater(chosen_sup, 0)
        self.assertEqual(unchosen_sup, 0)
        self.assertEqual(unchosen_loud, unchosen_req)


if __name__ == "__main__":
    unittest.main(verbosity=2)
