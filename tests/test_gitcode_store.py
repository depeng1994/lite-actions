"""Request recovery uses retained metadata, not remote artifacts or source imports."""

import copy
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from ci_core.gitcode_contract import canonical, request_id
from ci_core.gitcode_source import SourceError
from ci_core.gitcode_store import GitCodeRequestStore, StoreError
from tests.test_gitcode_pipeline import HostingAPI


class GitCodeStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.api = HostingAPI(self.root)
        self.plan = self.api.produce()
        self.api.job.update(status="completed", conclusion="success")
        self.loaded = self.api.reader.load(100, self.root / "admission", now=1100)
        self.store = GitCodeRequestStore(self.root / "requests")
        self.rid = request_id(self.plan)
        self.digest = self.plan["plan_digest"]

    def test_save_and_reopen_pin_both_source_and_runner(self):
        self.store.save(self.loaded)
        reopened = GitCodeRequestStore(self.store.root)
        result = reopened.load_execution(self.rid, self.digest)
        self.assertEqual(result.verified.plan, self.plan)
        self.assertEqual(result.source.metadata(), self.loaded.source.metadata())
        result.verify_files()
        self.assertNotEqual(result.source.bundle_path, self.loaded.source.bundle_path)

    def test_results_survive_deleted_remote_artifacts_and_local_source_cache(self):
        self.store.save(self.loaded)
        self.api.metadata.clear()
        self.api.zips.clear()
        record = self.store.load_execution(self.rid, self.digest)
        Path(record.source.bundle_path).unlink()
        record.runner_path.unlink()
        self.api.calls.clear()
        self.assertEqual(self.store.load_plan(self.rid, self.digest).plan, self.plan)
        self.assertEqual(self.api.calls, [])
        with self.assertRaises((SourceError, StoreError)):
            self.store.load_execution(self.rid, self.digest)

    def test_mutated_bundles_never_fall_back_to_model_archive(self):
        self.store.save(self.loaded)
        stored = self.store.load_execution(self.rid, self.digest)
        source = Path(stored.source.bundle_path)
        original = source.read_bytes()
        source.write_bytes(b"tampered source")
        with self.assertRaises(SourceError):
            self.store.load_execution(self.rid, self.digest)
        source.write_bytes(original)
        stored.runner_path.write_bytes(b"tampered runner")
        with self.assertRaisesRegex(StoreError, "RUNNER_CHANGED"):
            self.store.load_execution(self.rid, self.digest)

    def test_metadata_unknown_version_or_changed_digest_is_rejected(self):
        self.store.save(self.loaded)
        path = self.store._directory(self.rid) / "request.json"
        original = json.loads(path.read_text())
        for change in ("schema", "request_id", "plan"):
            data = copy.deepcopy(original)
            if change == "schema":
                data["schema"] = 99
            elif change == "request_id":
                data["request_id"] = "wrong/100/1"
            else:
                data["plan"]["pr"]["head_sha"] = "0" * 40
            path.write_bytes(canonical(data))
            with self.subTest(change=change), self.assertRaises(StoreError):
                self.store.load_plan(self.rid, self.digest)
        path.write_bytes(canonical(original))
        with self.assertRaises(StoreError):
            self.store.load_plan(self.rid, "0" * 64)

    def test_different_source_cannot_rebind_existing_request(self):
        self.store.save(self.loaded)
        changed = replace(self.loaded.source, tested_tree_sha="0" * 40)
        with self.assertRaisesRegex(StoreError, "BINDING_IMMUTABLE"):
            self.store.save(replace(self.loaded, source=changed))

    def test_concurrent_idempotent_persistence_has_one_record(self):
        with ThreadPoolExecutor(max_workers=4) as workers:
            results = list(workers.map(lambda _: self.store.save(self.loaded), range(8)))
        self.assertTrue(all(x.plan == self.plan for x in results))
        self.assertEqual(len(list(self.store.root.iterdir())), 1)
        self.assertEqual((self.store.root.stat().st_mode & 0o777), 0o700)
        self.assertEqual(((self.store._directory(self.rid) / "request.json").stat().st_mode & 0o777), 0o600)

    def test_symlink_metadata_and_entry_are_rejected(self):
        self.store.save(self.loaded)
        directory = self.store._directory(self.rid)
        path = directory / "request.json"
        outside = self.root / "outside.json"
        outside.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(outside)
        with self.assertRaisesRegex(StoreError, "METADATA_MISSING_OR_UNSAFE"):
            self.store.load_plan(self.rid, self.digest)
        other_root = self.root / "linked-root"
        other_root.symlink_to(self.store.root, target_is_directory=True)
        with self.assertRaisesRegex(StoreError, "ROOT_CANNOT_BE_SYMLINK"):
            GitCodeRequestStore(other_root)

    def test_source_identity_is_independently_rechecked_for_execution(self):
        self.store.save(self.loaded)
        path = self.store._directory(self.rid) / "request.json"
        data = json.loads(path.read_text())
        data["source"]["tested_tree_sha"] = "0" * 40
        path.write_bytes(canonical(data))
        with self.assertRaisesRegex(StoreError, "SOURCE_PLAN_MISMATCH"):
            self.store.load_execution(self.rid, self.digest)


if __name__ == "__main__":
    unittest.main()
