"""Per-node wait/preempt policy at the NPU transport boundary."""

import ast
import hashlib
import io
import json
import os
import shlex
import signal
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
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
        for occupied in (SHARED, HEADER):
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

    def test_pool_default_preempts_with_node_wait_override(self):
        self.pool["force_reclaim"] = True
        del self.preempt["occupancy_policy"]
        self.preflight([EMPTY, EMPTY, EMPTY], [BUSY, BUSY, EMPTY, EMPTY])
        [(host, command)] = self.signals()
        self.assertEqual(host, "worker-preempt:22")
        self.assertEqual(
            command[3:], ["123456", "[0]", FINGERPRINT, "card-die", "[0, 1, 2, 3]"]
        )

        self.calls.clear()
        with self.assertRaises(ExternalNpuOccupancy):
            self.preflight([BUSY], [])
        self.assertFalse(self.signals())

        self.case["allocation"] = self.case["allocation"][:1]
        for policy in (None, "preempt"):
            self.calls.clear()
            if policy:
                self.preempt["occupancy_policy"] = policy
            with self.subTest(policy=policy), self.assertRaises(ExternalNpuOccupancy):
                self.preflight([], [BUSY], cleanup=True)
            self.assertFalse(self.signals())

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

    def test_preflight_reclaims_on_a_host_with_legacy_python(self):
        self.case["allocation"] = [{"host_id": self.preempt["host_id"], "devices": [0]}]
        poller = Mock()
        poller.poll.return_value = []
        libc = Mock()
        libc.syscall.side_effect = [7, 0]

        def execute_on_host(_host, command):
            if not command.startswith("python3 -c "):
                return
            _, _, program, *args = shlex.split(command)
            stdout = io.StringIO()
            with (
                patch.object(sys, "argv", ["-c", *args]),
                patch.object(sys, "platform", "linux"),
                patch("platform.machine", return_value="aarch64"),
                patch.object(os, "pidfd_open", None, create=True),
                patch.object(os, "close"),
                patch.object(signal, "pidfd_send_signal", None, create=True),
                patch("ctypes.CDLL", return_value=libc),
                patch("select.poll", return_value=poller),
                patch("pathlib.Path.read_bytes", return_value=HOST_ID),
                patch(
                    "subprocess.run",
                    return_value=SimpleNamespace(returncode=0, stdout=BUSY),
                ),
                redirect_stdout(stdout),
            ):
                # Python 3.8 lacks subscriptable builtins and the native pidfd wrappers.
                exec(
                    compile(program, "<reclaim helper>", "exec", dont_inherit=True),
                    {"list": object},
                )
            self.assertEqual(stdout.getvalue(), "SIGNALED\n")

        self.preflight([], [BUSY, BUSY, EMPTY], on_probe=execute_on_host)
        opened, signalled = libc.syscall.call_args_list
        self.assertEqual([arg.value for arg in opened.args], [434, 123456, 0])
        self.assertEqual(
            [arg.value for arg in signalled.args], [424, 7, signal.SIGKILL, None, 0]
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

    def test_preflight_rejects_malformed_tables_and_defers_shared_occupancy(self):
        for bad in (
            BUSY.replace("Process id |", "Container id |", 1),
            BUSY.replace("| No running processes found in NPU 1 |\n", ""),
            BUSY.replace("| 1 3 |", "| 1 9 |"),
        ):
            self.calls.clear()
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.preflight([EMPTY], [bad])
            self.assertFalse(self.signals())

        shared = BUSY.replace(
            "| No running processes found in NPU 1 |",
            "| 1 0 | 123456 | python3.12 | 100 | NA |",
        )
        self.calls.clear()
        with self.assertRaisesRegex(ExternalNpuOccupancy, "spans allocated"):
            self.preflight([EMPTY], [shared])
        self.assertFalse(self.signals())

    def test_pidfd_signal_is_bound_and_reused_pid_is_not_signalled(self):
        cases = (
            ([[], []], True),
            ([[(7, 1)]], False),
            ([[], [(7, 1)]], False),
        )
        for polls, signalled in cases:
            poller = Mock()
            poller.poll.side_effect = polls
            with (
                self.subTest(polls=polls),
                patch.object(os, "pidfd_open", return_value=7, create=True),
                patch.object(os, "close"),
                patch.object(signal, "pidfd_send_signal", create=True) as send,
                patch("select.poll", return_value=poller),
                patch("pathlib.Path.read_bytes", return_value=HOST_ID),
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
            patch(
                "subprocess.run",
                return_value=SimpleNamespace(returncode=0, stdout=BUSY),
            ),
        ):
            self.assertTrue(
                _bound_host_signal(123456, [0], FINGERPRINT, "card-die", [0, 1, 2, 3])
            )
        send.assert_called_once_with(7, signal.SIGKILL)

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
            for invalid in ("ignore", "inherit", []):
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

            del pools["pool_blue"]["nodes"][0]["occupancy_policy"]
            pools["pool_blue"]["force_reclaim"] = True
            path.write_text(json.dumps(pools))
            load(root, "blue")
            del pools["pool_blue"]["nodes"][0]["machine_fingerprint"]
            path.write_text(json.dumps(pools))
            with self.assertRaisesRegex(ValueError, "fingerprint"):
                load(root, "blue")
            pools["pool_blue"]["force_reclaim"] = "true"
            path.write_text(json.dumps(pools))
            with self.assertRaisesRegex(ValueError, "boolean"):
                load(root, "blue")

    def test_bound_host_signal_returns_gone_when_process_exits_during_probe(self):
        poller = Mock()
        # Alive before probe, exited by the time npu-smi returns a teardown glitch.
        poller.poll.side_effect = [[], [(7, 1)]]
        with (
            patch.object(os, "pidfd_open", return_value=7, create=True),
            patch.object(os, "close"),
            patch.object(signal, "pidfd_send_signal", create=True) as send,
            patch("select.poll", return_value=poller),
            patch("pathlib.Path.read_bytes", return_value=HOST_ID),
            patch(
                "subprocess.run",
                return_value=SimpleNamespace(returncode=1, stdout=""),
            ),
        ):
            self.assertFalse(
                _bound_host_signal(123456, [0], FINGERPRINT, "card-die", [0, 1, 2, 3])
            )
        send.assert_not_called()

    def test_bound_host_signal_handles_pidfd_einval_during_teardown(self):
        import errno

        with (
            patch.object(
                os,
                "pidfd_open",
                side_effect=OSError(errno.EINVAL, "Invalid argument"),
                create=True,
            ),
            patch.object(signal, "pidfd_send_signal", create=True) as send,
            patch("pathlib.Path.read_bytes", return_value=HOST_ID),
            patch("pathlib.Path.read_text", side_effect=FileNotFoundError),
        ):
            self.assertFalse(
                _bound_host_signal(123456, [0], FINGERPRINT, "card-die", [0, 1, 2, 3])
            )
        send.assert_not_called()

    def test_bound_host_signal_retries_transient_probe_error_for_live_pid(self):
        poller = Mock()
        poller.poll.return_value = []
        with (
            patch.object(os, "pidfd_open", return_value=7, create=True),
            patch.object(os, "close"),
            patch.object(signal, "pidfd_send_signal", create=True) as send,
            patch("select.poll", return_value=poller),
            patch("pathlib.Path.read_bytes", return_value=HOST_ID),
            patch("time.sleep"),
            patch(
                "subprocess.run",
                side_effect=[
                    SimpleNamespace(returncode=1, stdout=""),
                    SimpleNamespace(returncode=0, stdout=BUSY),
                ],
            ) as probe,
        ):
            self.assertTrue(
                _bound_host_signal(123456, [0], FINGERPRINT, "card-die", [0, 1, 2, 3])
            )
        self.assertEqual(probe.call_count, 2)
        send.assert_called_once_with(7, signal.SIGKILL)

    def test_bound_host_signal_fails_closed_immediately_on_shared_pid(self):
        poller = Mock()
        poller.poll.return_value = []
        with (
            patch.object(os, "pidfd_open", return_value=7, create=True),
            patch.object(os, "close"),
            patch.object(signal, "pidfd_send_signal", create=True) as send,
            patch("select.poll", return_value=poller),
            patch("pathlib.Path.read_bytes", return_value=HOST_ID),
            patch("time.sleep") as sleep_mock,
            patch(
                "subprocess.run",
                return_value=SimpleNamespace(returncode=0, stdout=SHARED),
            ) as probe,
        ):
            with self.assertRaisesRegex(ValueError, "spans allocated"):
                _bound_host_signal(123456, [0], FINGERPRINT, "card-die", [0, 1, 2, 3])
        self.assertEqual(probe.call_count, 1)
        sleep_mock.assert_not_called()
        send.assert_not_called()

    def test_preflight_logs_reclaim_unverified_detail(self):
        evidence = io.StringIO()
        with self.assertRaisesRegex(RuntimeError, "PID-bound.*rc=1.*dcmi busy"):
            self.preflight(
                [EMPTY, EMPTY],
                [BUSY, BUSY],
                evidence=evidence,
                signal_result=SimpleNamespace(
                    returncode=1, stdout="", stderr="RuntimeError: dcmi busy\n"
                ),
            )
        self.assertIn("RECLAIM_UNVERIFIED", evidence.getvalue())
        self.assertIn("dcmi busy", evidence.getvalue())

    def test_container_npu_smi_blank_process_name_is_parsed_cleanly(self):
        # Processes belonging to other containers appear with an empty Process name
        # column inside docker exec npu-smi output; unallocated NPUs must not fail
        # cleanup verification for our allocated devices.
        foreign_on_npu0 = (
            HEADER
            + "| 0       0                 | 999970        |                    | 16805                 | 1032417                 |\n"
            + "| No running processes found in NPU 1 |\n"
            + FOOTER
        )
        self.case["allocation"] = [{"host_id": self.wait["host_id"], "devices": [2, 3]}]
        self.preflight([foreign_on_npu0], [])
        self.assertEqual(
            process_evidence(foreign_on_npu0, [0], indexing="card-die"),
            [(0, 999970)],
        )


if __name__ == "__main__":
    unittest.main()
