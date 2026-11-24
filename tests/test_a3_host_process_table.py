"""Regression coverage for A3 host-format npu-smi process tables."""

from ci_core.executor import _process_rows


def _table(process_rows: str) -> str:
    return "\n".join(
        [
            "+----------------+------------+----------------------+---------------------+",
            "| NPU Chip       | Process id | Process name         | Process memory(MB)  |",
            "+----------------+------------+----------------------+---------------------+",
            process_rows,
            "+----------------+------------+----------------------+---------------------+",
        ]
    )


def test_a3_host_empty_range_covers_all_eight_cards():
    output = _table("| No running processes found in NPU 0..7                              |")
    assert _process_rows(
        output,
        list(range(16)),
        indexing="card-die",
        require_exclusive=False,
        require_end=True,
        require_coverage=True,
    ) == []


def test_a3_host_process_row_maps_npu_chip_to_physical_device():
    output = _table(
        "| 3 1            | 24680      | python3              | 1024                |"
    )
    assert _process_rows(
        output,
        [6, 7],
        indexing="card-die",
        require_exclusive=False,
        require_end=True,
        require_coverage=False,
    ) == [(7, 24680, None)]
