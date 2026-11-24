"""Regression coverage for A3/A5 host-format npu-smi process tables."""

import shlex
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from ci_core.executor import (
    ExternalNpuOccupancy,
    _bound_host_command,
    _process_rows,
    _verified_inventory,
)
from ci_core.idle_probe import observe_health
from tests.test_a5_reclaim import A5_950_BUSY, A5_950_EMPTY

FOOTER = "+----------------+------------+----------------------+---------------------+"


def _table(process_rows, *, indexing="card-die"):
    label = "NPU     Chip" if indexing == "card-die" else "NPU ID"
    return "\n".join(
        [
            FOOTER,
            f"| {label} | Process id | Process name | Process memory(MB) |",
            FOOTER,
            *process_rows,
            FOOTER,
        ]
    )


def _without_container_column(output):
    return output.replace(" | Process id in container |", " |").replace(
        " | NA                      |", " |"
    )


class HostProcessTableTests(unittest.TestCase):
    def parse(self, output, devices, indexing="card-die", **kwargs):
        return _process_rows(
            output,
            devices,
            indexing=indexing,
            require_end=True,
            require_coverage=True,
            **kwargs,
        )

    def test_a3_host_empty_tables_cover_all_eight_cards(self):
        for rows in (
            ["| No running processes found in NPU 0..7 |"],
            [f"| No running processes found in NPU {card} |" for card in range(8)],
        ):
            with self.subTest(rows=rows):
                self.assertEqual(
                    self.parse(_table(rows), list(range(16)), require_exclusive=False),
                    [],
                )

    def test_a3_host_process_row_maps_npu_chip_to_physical_device(self):
        output = _table(
            ["| 3 1 | 24680 | python3 | 1024 |"]
            + [
                f"| No running processes found in NPU {card} |"
                for card in range(8)
                if card != 3
            ]
        )
        self.assertEqual(
            self.parse(output, list(range(16)), require_exclusive=False),
            [(7, 24680, None)],
        )
        self.assertEqual(self.parse(output, list(range(8, 16))), [])

    def test_a5_without_container_column_preserves_host_pid_and_inventory(self):
        for fixture, expected in (
            (A5_950_EMPTY, []),
            (A5_950_BUSY, [(0, 4183608, "NA")]),
        ):
            with self.subTest(expected=expected):
                output = _without_container_column(fixture)
                self.assertNotIn("Process id in container", output)
                _verified_inventory(output, "card", list(range(8)))
                self.assertEqual(
                    self.parse(
                        output,
                        list(range(8)),
                        indexing="card",
                        host_pids=True,
                        coverage_devices=list(range(8)),
                    ),
                    expected,
                )

    def test_a5_container_pid_is_not_a_validation_gate(self):
        for value, evidence in (("NA", "NA"), ("", "NA"), ("N/A", "NA"), ("42", "42")):
            with self.subTest(value=value):
                output = A5_950_BUSY.replace(
                    " | NA                      |", f" | {value} |"
                )
                self.assertEqual(
                    self.parse(
                        output,
                        list(range(8)),
                        indexing="card",
                        host_pids=True,
                        coverage_devices=list(range(8)),
                    ),
                    [(0, 4183608, evidence)],
                )

    def test_required_fields_coverage_and_end_are_still_validated(self):
        output = _without_container_column(A5_950_BUSY)
        for bad in (
            output.replace("4183608", "NA"),
            output.replace("4183608", "1"),
            output.replace("Process name", "Unknown name"),
            output.replace("89915", "NA"),
            output.replace("| No running processes found in NPU 7 |\n", ""),
            "\n".join(output.splitlines()[:-1]),
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.parse(bad, list(range(8)), indexing="card")
        with self.assertRaises(ExternalNpuOccupancy):
            self.parse(
                _table(["| No running processes found in NPU 0 |"]),
                list(range(16)),
                require_exclusive=False,
            )

    def test_health_recovers_both_a3_groups_and_a5(self):
        for indexing, groups, output in (
            (
                "card-die",
                [list(range(8)), list(range(8, 16))],
                _table(
                    ["| 3 1 | 24680 | python3 | 1024 |"]
                    + [
                        f"| No running processes found in NPU {card} |"
                        for card in range(8)
                        if card != 3
                    ]
                ),
            ),
            ("card", [list(range(8))], _without_container_column(A5_950_EMPTY)),
        ):
            with self.subTest(indexing=indexing):
                node = {
                    "host_id": "192.0.2.1:22",
                    "ssh": "fixture",
                    "allocation_groups": [{"devices": group} for group in groups],
                }
                pools = {
                    "fixture": {
                        "owner": "yellow",
                        "process_indexing": indexing,
                        "nodes": [node],
                    }
                }
                previous = {
                    f'["fixture","192.0.2.1:22",[{",".join(map(str, group))}]]': {
                        "v": 2,
                        "mc": 5,
                        "lo": None,
                    }
                    for group in groups
                }
                execute = Mock(return_value=SimpleNamespace(stdout=output))
                states, completed, health = observe_health(
                    "yellow", pools, {}, execute=execute, previous=previous
                )
                self.assertIsNotNone(completed)
                self.assertEqual(
                    [state[0] for state in states.values()],
                    ["busy", "idle"] if indexing == "card-die" else ["idle"],
                )
                for entry in health.values():
                    self.assertEqual((entry["v"], entry["mc"]), (0, 0))
                    self.assertIsNotNone(entry["lo"])
                execute.assert_called_once_with("fixture", "npu-smi info", timeout=20)

    def test_embedded_host_recheck_has_header_parser(self):
        command = _bound_host_command(1234, [0], "a" * 64, "card", list(range(8)))
        program = shlex.split(command)[2]
        self.assertIn("def _process_table_header(", program)
        compile(program, "<bound-host-parser>", "exec")
