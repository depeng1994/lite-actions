"""Per-node wait/preempt policy at the NPU transport boundary."""

import ast
import hashlib
import io
import json
import os
import shlex
import signal
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ci_core.config import load
from ci_core.executor import (
    ExternalNpuOccupancy,
    _bound_host_signal,
    process_evidence,
    remote_preflight,
)
from tests.scenarios import write_workspace

FOOTER = (
    "+===========================+===============+==================================+\n"
)
DIVIDER = (
    "+---------------------------+---------------+----------------------------------+\n"
)


def hardware(indexing):
    output = (
        "| NPU   Name | Health | Detail |\n"
        "| Chip  Phy-ID | Bus-Id | Detail |\n" + FOOTER
    )
    for card in range(2):
        for chip in range(2) if indexing == "card-die" else range(1):
            phy = card * 2 + chip if indexing == "card-die" else card
            output += f"| {card} Ascend910 | OK | 100 |\n"
            output += f"| {chip} {phy} | 0000:{0x90 + phy:02X}:00.0 | 0 |\n"
            output += DIVIDER
    return output.removesuffix(DIVIDER) + FOOTER + DIVIDER


HEADER = (
    hardware("card-die")
    + "| NPU     Chip | Process id | Process name | Process memory(MB) | Process id in container |\n"
    + FOOTER
)
EMPTY = (
    HEADER
    + "| No running processes found in NPU 0 |\n"
    + "| No running processes found in NPU 1 |\n"
    + FOOTER
)
BUSY = (
    HEADER
    + "| 0 0 | 123456 | python3.12 | 60189 | 987654 |\n"
    + "| 0 1 | 123457 | python3.12 | 59869 | NA |\n"
    + "| No running processes found in NPU 1 |\n"
    + FOOTER
)
SHARED = BUSY.replace("123457", "123456")
HOST_ID = b"test-host\n"
FINGERPRINT = hashlib.sha256(HOST_ID).hexdigest()


class NodeOccupancyTests(unittest.TestCase):
    def setUp(self):
        self.wait = {
            "host_id": "192.0.2.1:22",
            "ssh": "worker-wait:22",
            "host_ssh": "host-wait:22",
            "occupancy_policy": "wait",
        }
        self.preempt = {
            "host_id": "192.0.2.2:22",
            "ssh": "worker-preempt:22",
            "host_ssh": "host-preempt:22",
            "occupancy_policy": "preempt",
            "machine_fingerprint": FINGERPRINT,
            "device_ids": [0, 1, 2, 3],
        }
        self.pool = {
            "owner": "blue",
            "type": "ascend-a3",
            "process_indexing": "card-die",
            "nodes": [self.wait, self.preempt],
        }
        # Put the preempt node first to test that wait takes precedence.
        self.case = {
            "pool": "blue",
            "docker": "fixture",
            "allocation": [
                {"host_id": self.preempt["host_id"], "devices": [0]},
                {"host_id": self.wait["host_id"], "devices": [0]},
            ],
        }
        self.calls = []

    def preflight(
        self,
        wait,
        preempt,
        *,
        cleanup=False,
        cancel=None,
        on_probe=None,
        signal_result=None,
        evidence=None,
    ):
        wait, preempt = iter(wait), iter(preempt)

        def transport(host, command, **kwargs):
            self.calls.append((host, command))
            if on_probe is not None:
                on_probe(host, command)
            if command.startswith("python3 -c "):
                return signal_result or SimpleNamespace(
                    returncode=0, stdout="SIGNALED\n"
                )
            if host == "worker-wait:22/fixture":
                return SimpleNamespace(returncode=0, stdout=next(wait))
            if host in ("worker-preempt:22", "worker-preempt:22/fixture"):
                return SimpleNamespace(returncode=0, stdout=next(preempt))
            raise AssertionError((host, command))

        with (
            patch(
                "ci_core.config.execution_node",
                side_effect=lambda node, _: {"ssh": node["ssh"] + "/fixture"},
            ),
            patch("ci_core.transport.run", side_effect=transport),
        ):
            remote_preflight(
                self.case,
                {"blue": self.pool},
                cleanup=cleanup,
                ascend_env="/fixture/container/cann.sh",
                cancel=cancel,
                evidence=evidence,
            )

    def signals(self):
        return [
            (host, shlex.split(command))
            for host, command in self.calls
            if command.startswith(("python3 -c ", "kill -9 -- "))
        ]

    def test_waiting_node_blocks_before_other_node_is_preempted(self):
        unknown = HEADER + "| NA 0 | 123456 | python3 | 60189 | NA |\n"
        truncated = HEADER + "| 0 1 | 123457 | python3 | 60189 | NA |\n"
        for occupied in (
            SHARED,
            BUSY.replace("123456", "NA", 1),
            unknown,
            HEADER,
            truncated,
        ):
            self.calls.clear()
            with (
                self.subTest(occupied=occupied),
                self.assertRaises(ExternalNpuOccupancy),
            ):
                self.preflight([occupied], [])
            self.assertEqual(self.calls[0][0], "worker-wait:22/fixture")
            self.assertFalse(self.signals())

    def test_preempt_only_targets_host_pid_and_rechecks_wait_node(self):
        self.preflight([EMPTY, EMPTY, EMPTY], [BUSY, BUSY, EMPTY, EMPTY])
        [(host, command)] = self.signals()
        self.assertEqual(host, "worker-preempt:22")
        self.assertEqual(command[:2], ["python3", "-c"])
        ast.parse(command[2], feature_version=(3, 9))
        self.assertEqual(
            command[3:], ["123456", "[0]", FINGERPRINT, "card-die", "[0, 1, 2, 3]"]
        )
        self.assertTrue(
            all(
                command == "npu-smi info"
                for host, command in self.calls
                if host == "worker-preempt:22" and not command.startswith("python3 -c ")
            )
        )

    def test_explicit_a5_preemption_uses_bound_host_pid(self):
        self.pool.update(type="ascend-a5", process_indexing="card")
        self.preempt["device_ids"] = [0, 1]
        header = (
            hardware("card")
            + "| NPU ID | Process id | Process name | Process memory(MB) | Process id in container |\n"
            + FOOTER
        )
        empty = (
            header
            + "| No running processes found in NPU 0 |\n| No running processes found in NPU 1 |\n"
            + FOOTER
        )
        busy = (
            header
            + "| 0 | 123456 | python3 | 100 | 987654 |\n| No running processes found in NPU 1 |\n"
            + FOOTER
        )
        self.preflight([empty, empty, empty], [busy, busy, empty, empty])
        [(host, command)] = self.signals()
        self.assertEqual(host, "worker-preempt:22")
        self.assertEqual(command[3:], ["123456", "[0]", FINGERPRINT, "card", "[0, 1]"])
        poller = Mock()
        poller.poll.return_value = []
        stat = "123456 (python3) " + " ".join(["S"] + ["0"] * 18 + ["10"])
        with (
            patch.object(os, "pidfd_open", return_value=7, create=True),
            patch.object(os, "close"),
            patch.object(signal, "pidfd_send_signal", create=True) as send,
            patch("select.poll", return_value=poller),
            patch("pathlib.Path.read_bytes", return_value=HOST_ID),
            patch("pathlib.Path.read_text", return_value=stat),
            patch(
                "subprocess.run",
                return_value=SimpleNamespace(returncode=0, stdout=busy),
            ),
        ):
            self.assertTrue(
                _bound_host_signal(123456, [0], FINGERPRINT, "card", [0, 1])
            )
            send.assert_called_once_with(7, signal.SIGKILL)

    def test_wait_audit_does_not_invent_pid_namespace(self):
        evidence = io.StringIO()
        with self.assertRaises(ExternalNpuOccupancy):
            self.preflight([SHARED], [], evidence=evidence)
        self.assertIn('"observed_pid": 123456', evidence.getvalue())
        self.assertNotIn('"container_pid"', evidence.getvalue())
        self.assertNotIn('"host_pid"', evidence.getvalue())

    def test_wait_node_becoming_busy_blocks_preemption(self):
        with self.assertRaises(ExternalNpuOccupancy):
            self.preflight([EMPTY, SHARED], [BUSY])
        self.assertFalse(self.signals())

    def test_cancel_during_confirmation_prevents_signalling(self):
        cancel = threading.Event()
        scans = []

        def on_probe(host, command):
            if host == "worker-preempt:22" and command == "npu-smi info":
                scans.append(command)
                if len(scans) == 2:
                    cancel.set()

        with self.assertRaises(InterruptedError):
            self.preflight(
                [EMPTY, EMPTY], [BUSY, BUSY], cancel=cancel, on_probe=on_probe
            )
        self.assertFalse(self.signals())

    def test_cancel_after_one_signal_prevents_the_next(self):
        cancel = threading.Event()
        self.case["allocation"] = [
            {"host_id": self.preempt["host_id"], "devices": [0, 1]}
        ]

        def on_probe(host, command):
            if command.startswith("python3 -c "):
                cancel.set()

        with self.assertRaises(InterruptedError):
            self.preflight([], [BUSY, BUSY], cancel=cancel, on_probe=on_probe)
        self.assertEqual(len(self.signals()), 1)

    def test_unavailable_pidfd_fails_without_unbound_signal(self):
        with self.assertRaisesRegex(RuntimeError, "PID-bound"):
            self.preflight(
                [EMPTY, EMPTY],
                [BUSY, BUSY],
                signal_result=SimpleNamespace(returncode=1, stdout=""),
            )
        self.assertFalse(
            any(command.startswith("kill -9") for _, command in self.calls)
        )

    def test_multi_node_recheck_blocks_if_external_work_appears(self):
        scenarios = (
            ([EMPTY, EMPTY, SHARED], [BUSY, BUSY, EMPTY]),
            ([EMPTY, EMPTY, EMPTY], [BUSY, BUSY, EMPTY, BUSY]),
        )
        for wait, preempt in scenarios:
            self.calls.clear()
            with self.subTest(wait=wait), self.assertRaises(ExternalNpuOccupancy):
                self.preflight(wait, preempt)
            [(host, command)] = self.signals()
            self.assertEqual(
                (host, command[3:]),
                (
                    "worker-preempt:22",
                    ["123456", "[0]", FINGERPRINT, "card-die", "[0, 1, 2, 3]"],
                ),
            )

    def test_cleanup_never_preempts_even_when_node_is_configured_to_preempt(self):
        self.case["allocation"] = self.case["allocation"][:1]
        with self.assertRaises(ExternalNpuOccupancy):
            self.preflight([], [BUSY], cleanup=True)
        self.assertEqual(self.calls[0][0], "worker-preempt:22/fixture")
        self.assertFalse(self.signals())

    def test_unrecognized_host_pid_table_fails_closed(self):
        for bad in (
            BUSY.replace("Process id |", "Container id |", 1),
            BUSY.replace("123456", "NA", 1),
            HEADER,
            BUSY.replace("| No running processes found in NPU 1 |\n", ""),
            BUSY.removesuffix(FOOTER),
            BUSY.removesuffix(FOOTER) + "+\n",
            BUSY.replace("Chip  Phy-ID", "Chip Unknown"),
            BUSY.replace("| 1 3 |", "| 1 9 |"),
            BUSY.replace(
                "| No running processes found in NPU 1 |",
                "| 1 0 | 123456 | python3.12 | 100 | NA |",
            ),
        ):
            self.calls.clear()
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.preflight([EMPTY], [bad])
            self.assertFalse(self.signals())

    def test_pidfd_signal_is_bound_and_reused_pid_is_not_signalled(self):
        def stat(start):
            return "123456 (python3) " + " ".join(["S"] + ["0"] * 18 + [str(start)])

        cases = (
            ((10, 10), [[], []], True),
            ((10, 11), [[]], False),
            ((10, 10), [[], [(7, 1)]], False),
        )
        for starts, polls, signalled in cases:
            poller = Mock()
            poller.poll.side_effect = polls
            with (
                self.subTest(starts=starts),
                patch.object(os, "pidfd_open", return_value=7, create=True),
                patch.object(os, "close"),
                patch.object(signal, "pidfd_send_signal", create=True) as send,
                patch("select.poll", return_value=poller),
                patch("pathlib.Path.read_bytes", return_value=HOST_ID),
                patch("pathlib.Path.read_text", side_effect=[stat(x) for x in starts]),
                patch(
                    "subprocess.run",
                    return_value=SimpleNamespace(returncode=0, stdout=BUSY),
                ),
            ):
                self.assertEqual(
                    _bound_host_signal(
                        123456, [0], FINGERPRINT, "card-die", [0, 1, 2, 3]
                    ),
                    signalled,
                )
                if signalled:
                    send.assert_called_once_with(7, signal.SIGKILL)
                else:
                    send.assert_not_called()
        with (
            patch.object(os, "pidfd_open", None, create=True),
            patch.object(signal, "pidfd_send_signal", create=True),
            patch("sys.platform", "darwin"),
            patch("pathlib.Path.read_bytes", return_value=HOST_ID),
            self.assertRaisesRegex(RuntimeError, "unavailable"),
        ):
            _bound_host_signal(123456, [0], FINGERPRINT, "card-die", [0, 1, 2, 3])
        with (
            patch.object(os, "pidfd_open", create=True) as open_pid,
            patch.object(signal, "pidfd_send_signal", create=True) as send,
            patch("pathlib.Path.read_bytes", return_value=b"different-host"),
            self.assertRaisesRegex(RuntimeError, "host identity"),
        ):
            _bound_host_signal(123456, [0], FINGERPRINT, "card-die", [0, 1, 2, 3])
        open_pid.assert_not_called()
        send.assert_not_called()

        poller = Mock()
        poller.poll.return_value = []
        libc = Mock()
        libc.syscall.return_value = 7
        with (
            patch.object(os, "pidfd_open", None, create=True),
            patch.object(os, "close"),
            patch.object(signal, "pidfd_send_signal", create=True) as send,
            patch("sys.platform", "linux"),
            patch("platform.machine", return_value="aarch64"),
            patch("ctypes.CDLL", return_value=libc),
            patch("select.poll", return_value=poller),
            patch("pathlib.Path.read_bytes", return_value=HOST_ID),
            patch("pathlib.Path.read_text", return_value=stat(10)),
            patch(
                "subprocess.run",
                return_value=SimpleNamespace(returncode=0, stdout=BUSY),
            ),
        ):
            self.assertTrue(
                _bound_host_signal(123456, [0], FINGERPRINT, "card-die", [0, 1, 2, 3])
            )
        self.assertEqual(libc.syscall.call_args.args[0].value, 434)
        send.assert_called_once_with(7, signal.SIGKILL)

    def test_explicit_empty_table_remains_valid(self):
        self.preflight([EMPTY, EMPTY], [EMPTY, EMPTY])
        self.assertFalse(self.signals())

    def test_selected_cards_need_complete_process_table_coverage(self):
        self.case["allocation"] = [
            {"host_id": self.wait["host_id"], "devices": [0, 14]}
        ]
        with self.assertRaises(ExternalNpuOccupancy):
            self.preflight([EMPTY], [])
        self.preflight(
            [
                EMPTY.removesuffix(FOOTER)
                + "| No running processes found in NPU 7 |\n"
                + FOOTER
            ],
            [],
        )
        a5 = "| NPU ID | Process id | Process name | Process memory(MB) | Process id in container |\n"
        a5 += "| No running processes found in NPU 0 |\n"
        with self.assertRaises(ValueError):
            process_evidence(a5, [0, 7], indexing="card")
        self.assertEqual(
            process_evidence(
                a5 + "| No running processes found in NPU 7 |\n",
                [0, 7],
                indexing="card",
            ),
            [],
        )

    def test_config_rejects_unknown_node_policies(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write_workspace(root)
            path = root / "config/pools.json"
            pools = json.loads(path.read_text())
            for invalid in ("ignore", "inherit", None, True, []):
                pools["pool_blue"]["nodes"][0]["occupancy_policy"] = invalid
                path.write_text(json.dumps(pools))
                with self.subTest(value=invalid), self.assertRaises(ValueError):
                    load(root, "blue")
            pools["pool_blue"]["nodes"][0]["occupancy_policy"] = "preempt"
            pools["pool_blue"]["nodes"][1]["occupancy_policy"] = "wait"
            pools["pool_blue"]["type"] = "ascend-a3"
            pools["pool_blue"]["process_indexing"] = "card"
            path.write_text(json.dumps(pools))
            with self.assertRaises(ValueError):
                load(root, "blue")
            pools["pool_blue"]["process_indexing"] = "card-die"
            path.write_text(json.dumps(pools))
            with self.assertRaises(ValueError):
                load(root, "blue")
            pools["pool_blue"]["nodes"][0]["machine_fingerprint"] = FINGERPRINT
            path.write_text(json.dumps(pools))
            load(root, "blue")


if __name__ == "__main__":
    unittest.main()
