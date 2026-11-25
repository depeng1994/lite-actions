"""Re-resolve the frozen model definition before accepting deployment routing."""

from types import SimpleNamespace

from ci_core.contract import normalize, parse_worker_identity
from ci_core.registry import inspect_archive


def verify_definition(archive, item, defaults, *, repo=None):
    rows = inspect_archive(archive, {"test_id": item["test_id"]}, repo=repo)
    if len(rows) != 1:
        raise ValueError("missing pinned definition")
    r = rows[0]
    test = SimpleNamespace(
        test_name=r["test_id"],
        test_descr=r["label"],
        ngpu=r["ngpu"],
        nnodes=r["nnodes"],
        ci_kind=r["kind"],
        ci_overrides=r["ci_overrides"],
        timeout=r["timeout"],
    )
    expected = normalize(test, defaults, item["order"])
    ident = parse_worker_identity(item["worker"])
    if ident["is_test"]:
        if expected["worker"] != ident["zone"]:
            raise ValueError("pinned definition/plan zone mismatch")
        expected = {**expected, "worker": item["worker"]}
    if expected != item:
        raise ValueError("pinned definition/plan mismatch")
    return r
