"""CPU-only device lease, conflict, and A5 extension tests."""
import multiprocessing as mp
from pathlib import Path
import tempfile
import unittest

from ci_core.resources import (AgentAlreadyRunning, GlobalSlotBusy,
                               agent_lock_path, global_slot_path, reserve,
                               ResourceBusy, single_agent, wait_global_slot, reserve_ports,
                               shared_resource_lock_root)
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


def hold_global_slot(lockfile, ready, release):
    with wait_global_slot(Path(lockfile), timeout=2, interval=0.01):
        ready.set()
        release.wait(2)


def hold_agent_lock(lockfile, ready, release):
    with single_agent(Path(lockfile)):
        ready.set()
        release.wait(2)


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

    def test_agent_singleton_excludes_other_process(self):
        with tempfile.TemporaryDirectory() as td:
            lockfile = Path(td) / "agent.lock"
            ready = mp.Event()
            release = mp.Event()
            p = mp.Process(target=hold_agent_lock,
                           args=(str(lockfile), ready, release))
            p.start()
            try:
                self.assertTrue(ready.wait(timeout=5))
                with self.assertRaises(AgentAlreadyRunning):
                    with single_agent(lockfile):
                        pass
            finally:
                release.set()
                p.join(timeout=5)
                if p.is_alive():
                    p.terminate()
                    p.join(timeout=5)
            self.assertEqual(p.exitcode, 0)
            with single_agent(lockfile):
                pass

    def test_resource_lock_root_shared_across_repos_and_absolute(self):
        with tempfile.TemporaryDirectory() as td:
            with unittest.mock.patch.dict("os.environ", {"LITE_ACTIONS_RESOURCE_LOCK_ROOT": td}):
                self.assertEqual(shared_resource_lock_root(), Path(td))
        self.assertTrue(shared_resource_lock_root().is_absolute())

    def test_global_lock_defaults_are_worktree_independent(self):
        for path in (agent_lock_path(), global_slot_path()):
            self.assertTrue(path.is_absolute())
            self.assertNotIn("review-lite-phase1-audit", str(path))

    def test_global_training_slot_excludes_other_process(self):
        with tempfile.TemporaryDirectory() as td:
            lockfile = Path(td) / "global-training.lock"
            ready = mp.Event()
            release = mp.Event()
            p = mp.Process(target=hold_global_slot,
                           args=(str(lockfile), ready, release))
            p.start()
            try:
                self.assertTrue(ready.wait(timeout=5))
                with self.assertRaises(GlobalSlotBusy):
                    with wait_global_slot(lockfile, timeout=0.05, interval=0.01):
                        pass
            finally:
                release.set()
                p.join(timeout=5)
                if p.is_alive():
                    p.terminate()
                    p.join(timeout=5)
            self.assertEqual(p.exitcode, 0)
            with wait_global_slot(lockfile, timeout=0.2, interval=0.01):
                pass

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
        self.assertEqual(a5["resources"], {"nodes": 8, "devices_per_node": 8})

    def test_pipeline_ids_unique(self):
        cfg = load_config(Path(__file__).resolve().parents[1])
        self.assertEqual(len(cfg), 4)


if __name__ == "__main__":
    unittest.main()
