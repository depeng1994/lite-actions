"""Legacy quarantine remains authoritative after canonical-node migration."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "migration", ROOT / "scripts/migrate_v2_state.py"
)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.old = Path(self.temp.name)
        (self.old / "config").mkdir()
        (self.old / "state").mkdir()
        (self.old / "config/pools.json").write_text(
            json.dumps(
                {
                    "legacy": {
                        "nodes": [dict(host_id="legacy_host_a", node_ip="192.168.0.30")]
                    }
                }
            )
        )

    def write(self, records):
        (self.old / "state/unified-jobs.json").write_text(json.dumps(records))

    def test_quarantine_keeps_exact_devices_with_new_identity(self):
        self.write(
            {
                "123:1:case": dict(
                    phase="reported",
                    quarantined=True,
                    allocation=[dict(host_id="legacy_host_a", devices=[0, 1])],
                )
            }
        )
        before = (self.old / "state/unified-jobs.json").read_bytes()
        records = migration.guards(self.old, ROOT, "blue")
        guard = next(iter(records.values()))
        self.assertTrue(guard["quarantined"])
        self.assertEqual(
            guard["allocation"], [dict(host_id="113.46.17.205:153", devices=[0, 1])]
        )
        self.assertEqual(before, (self.old / "state/unified-jobs.json").read_bytes())

    def test_unscoped_quarantine_blocks_all_owned_devices(self):
        self.write({"123:1": dict(phase="reported", quarantined=True)})
        guard = next(iter(migration.guards(self.old, ROOT, "blue").values()))
        self.assertEqual(sum(len(x["devices"]) for x in guard["allocation"]), 32)

    def test_incomplete_legacy_task_prevents_migration(self):
        self.write({"123:1": dict(phase="running")})
        with self.assertRaisesRegex(ValueError, "drain"):
            migration.guards(self.old, ROOT, "blue")
