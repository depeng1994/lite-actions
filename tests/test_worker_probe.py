"""Worker-local CPU sampling and bounded, authenticated self-smoke log display."""

import io
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import mock_open, patch

from ci_core import worker_probe
from ci_core.self_smoke import print_probe_receipt


class WorkerProbeTests(unittest.TestCase):
    def test_samples_host_cpu_not_guest_counters_or_load_average(self):
        # Total 80; busy 35. Guest/guest_nice must not be added again.
        with patch(
            "builtins.open", mock_open(read_data="cpu 10 3 12 20 15 4 6 10 5 1\n")
        ):
            self.assertEqual(worker_probe.cpu_ticks(), (35, 80))
        with (
            patch.object(worker_probe, "cpu_ticks", side_effect=[(35, 80), (60, 180)]),
            patch.object(worker_probe.time, "sleep") as sleep,
        ):
            self.assertEqual(worker_probe.snapshot()["WORKER_CPU_BUSY_PCT_1S"], "25.0")
            sleep.assert_called_once_with(1)
        with (
            patch.object(worker_probe, "cpu_ticks", return_value=(35, 80)),
            patch.object(worker_probe.time, "sleep"),
            self.assertRaisesRegex(ValueError, "counter sample"),
        ):
            worker_probe.snapshot()

    def test_real_probe_fits_receipt_and_displays_only_approved_fields(self):
        sample = worker_probe.probe()
        self.assertEqual(sample.returncode, 0, sample.stderr)
        lines = sample.stdout.splitlines()
        actual = dict(line.split("=", 1) for line in lines)
        self.assertEqual(set(actual), worker_probe.FIELDS)
        self.assertEqual(
            int(actual["WORKER_CPU_ONLINE"]), os.sysconf("SC_NPROCESSORS_ONLN")
        )
        self.assertTrue(0 <= float(actual["WORKER_CPU_BUSY_PCT_1S"]) <= 100)
        self.assertRegex(
            actual["WORKER_LOADAVG_1M_5M_15M"], r"^\d+\.\d{2}( \d+\.\d{2}){2}$"
        )
        noisy = lines + [
            "TOKEN=not-for-logs",
            "::warning::command",
            "WORKER_HOSTNAME=a\n::debug::b",
            "PRETTY_NAME=abc::warning::test",
            "WORKER_KERNEL=##[command]abc",
            "WORKER_ARCH=" + "a" * 180,
        ]
        with redirect_stdout(io.StringIO()) as out:
            print_probe_receipt({"last_20_lines": noisy})
        self.assertEqual(out.getvalue().splitlines(), lines)

    def test_legacy_receipt_does_not_invent_cpu_measurements(self):
        with redirect_stdout(io.StringIO()) as out:
            print_probe_receipt(
                {
                    "last_20_lines": [
                        "OS_RELEASE_BEGIN",
                        "WORKER_HOSTNAME=old-worker",
                        'PRETTY_NAME="Ubuntu"',
                        "HOME_URL=not-for-logs",
                        "OS_RELEASE_END",
                    ]
                }
            )
        self.assertEqual(
            out.getvalue().splitlines(),
            ["WORKER_HOSTNAME=old-worker", 'PRETTY_NAME="Ubuntu"'],
        )
