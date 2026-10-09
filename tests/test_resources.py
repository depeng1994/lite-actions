"""CPU-only device lease, conflict, and A5 extension tests."""
import multiprocessing as mp
from pathlib import Path
import tempfile
import unittest

from ci_core.resources import reserve, ResourceBusy
from ci_core.config import load_config


A8 = [{"host_id": "a3-4", "devices": list(range(8))}]
A16 = [
    {"host_id": "a3-3", "devices": list(range(8))},
    {"host_id": "a3-4", "devices": list(range(8, 16))},
]
OVERLAP = [{"host_id": "a3-4", "devices": [7, 8]}]


def other_process(lockdir, allocation, out):
    try:
        with reserve(Path(lockdir), allocation):
            out.put("acquired")
    except ResourceBusy:
        out.put("busy")


class ResourceTests(unittest.TestCase):
    def test_disjoint_8p_16p_can_coexist(self):
        with tempfile.TemporaryDirectory() as td:
            with reserve(Path(td), A8):
                with reserve(Path(td), A16):
                    pass

    def test_overlap_rejected_across_processes(self):
        with tempfile.TemporaryDirectory() as td:
            with reserve(Path(td), A8):
                q = mp.Queue()
                p = mp.Process(target=other_process, args=(td, OVERLAP, q))
                p.start()
                self.assertEqual(q.get(timeout=5), "busy")
                p.join(timeout=5)
                self.assertEqual(p.exitcode, 0)

    def test_failed_acquisition_releases_partial_leases(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with reserve(root, A8):
                with self.assertRaises(ResourceBusy):
                    with reserve(root, [{"host_id":"a3-3","devices":[0]},
                                        {"host_id":"a3-4","devices":[0]}]):
                        pass
                with reserve(root, [{"host_id":"a3-3","devices":[0]}]):
                    pass

    def test_a5_64p_configured_as_shared_ssh_and_has_8x8_devices(self):
        root = Path(__file__).resolve().parents[1]
        cfg = load_config(root)
        a5 = cfg["a5-64p"]
        self.assertFalse(a5["enabled"])
        self.assertEqual(len(a5["allocation"]), 8)
        self.assertEqual(sum(len(n["devices"]) for n in a5["allocation"]), 64)

    def test_pipeline_ids_unique(self):
        cfg = load_config(Path(__file__).resolve().parents[1])
        self.assertEqual(len(cfg), 4)


if __name__ == "__main__":
    unittest.main()
