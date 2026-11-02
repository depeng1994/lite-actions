"""Whitelist-only pre-capture scan evidence, synthetic transport/no devices."""

import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ci_core import gitcode_execution as backend
from ci_core.executor import ExternalNpuOccupancy


class ScanAuditTests(unittest.TestCase):
    def setUp(self):
        self.node = {
            "position": 0,
            "device_ids": [8, 9],
            "ssh": "SECRET_SSH",
            "host_id": "SECRET_HOST",
        }
        self.identity = ("a" * 32, "case_000_dispatch_smoke_8p")

    def scan(self, output=None, error=None):
        with (
            patch.object(backend, "verify_node"),
            patch.object(
                backend,
                "transport_run",
                return_value=SimpleNamespace(stdout=output),
                side_effect=error,
            ),
        ):
            backend.scan_idle(
                [self.node],
                "/SECRET_SETUP",
                "card",
                io.StringIO(),
                audit_identity=self.identity,
            )

    def evidence(self, logs):
        self.assertEqual(len(logs.output), 1)
        line = logs.output[0]
        self.assertNotIn("SECRET", line)
        value = json.loads(line.split("GITCODE_DEVICE_SCAN_AUDIT ", 1)[1])
        self.assertEqual(
            set(value),
            {
                "execution_id",
                "case_id",
                "utc",
                "monotonic_ns",
                "node",
                "allocated_chips",
                "processes",
                "process_count",
                "truncated",
                "status",
            },
        )
        self.assertEqual(value["execution_id"], self.identity[0])
        self.assertEqual(value["case_id"], self.identity[1])
        self.assertEqual(value["allocated_chips"], [8, 9])
        self.assertIs(type(value["monotonic_ns"]), int)
        self.assertTrue(value["utc"].endswith("+00:00"))
        return value

    def test_empty_table_is_idle_with_no_pid(self):
        with self.assertLogs(level="WARNING") as logs:
            self.scan(
                "SECRET_RAW_PREFIX Process id in container\n| No running processes found in NPU 8 |\n"
            )
        value = self.evidence(logs)
        self.assertEqual(value["status"], "IDLE")
        self.assertEqual(value["processes"], [])
        self.assertEqual(value["process_count"], 0)

    def test_selected_occupancy_preserves_exception_and_numeric_evidence_only(self):
        with self.assertLogs(level="WARNING") as logs:
            with self.assertRaises(ExternalNpuOccupancy):
                self.scan(
                    "Process id in container\n| 8 | 12345 | SECRET_PROCESS_COMMAND | 1024 | 76543 |\n"
                )
        value = self.evidence(logs)
        self.assertEqual(value["status"], "OCCUPIED")
        self.assertEqual(value["processes"], [{"chip": 8, "pid": 12345}])
        self.assertNotIn("76543", logs.output[0])

    def test_unallocated_pid_does_not_become_occupancy(self):
        with self.assertLogs(level="WARNING") as logs:
            self.scan(
                "Process id in container\n| 3 | 12345 | SECRET_PROCESS | 1024 | NA |\n"
            )
        self.assertEqual(self.evidence(logs)["status"], "IDLE")

    def test_parse_error_does_not_dump_input_or_exception(self):
        with self.assertLogs(level="WARNING") as logs:
            with self.assertRaises(ValueError):
                self.scan("SECRET_RAW_STDOUT_WITHOUT_PROCESS_TABLE")
        self.assertEqual(self.evidence(logs)["status"], "PARSE_ERROR")

    def test_command_error_does_not_dump_command_or_exception(self):
        with self.assertLogs(level="WARNING") as logs:
            with self.assertRaises(RuntimeError):
                self.scan(error=RuntimeError("SECRET_COMMAND_ENV_EXCEPTION"))
        self.assertEqual(self.evidence(logs)["status"], "COMMAND_ERROR")

    def test_untrusted_audit_identity_cannot_inject_log_text(self):
        self.identity = ("SECRET\nINJECTION", "case")
        with patch.object(backend.logging, "warning") as warning:
            with self.assertRaisesRegex(ValueError, "INVALID_SCAN_AUDIT_IDENTITY"):
                self.scan("Process id in container\n")
            warning.assert_not_called()
