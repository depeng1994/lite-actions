"""CPU-only contracts: GitHub selector, model registry and generic N-node SSH."""
import contextlib
import gzip
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from ci_core.config import load_config
from ci_core import ssh_runner
from ci_core.registry import selected_test, load_profile

ROOT = Path(__file__).resolve().parents[1]
STAMP = "2026-10-09_17-43-56_run-37913151439_attempt-1"
TEST_8P = "dsv4_flash_a3_8p_example"
TEST_16P = "dsv4_flash_a3_16p_example"
TEST_64P = "dsv4_pro_a5_64p"
DEFAULT = {
    "module": "tests.integration_tests.nightly_all_models_test.a3_8p_tests",
    "mode": "single", "nnodes": 1, "ngpu": 8,
    "ascend_env": "/opt/ascend/set_env.sh",
    "hf_assets_path": "/models/flash",
    "ckpt_init_path": "/weights",
    "env_vars": {"STEPS": "5"}
}

def archive_with(items, dest):
    with tarfile.open(dest, "w:gz") as t:
        data = json.dumps(items).encode()
        info = tarfile.TarInfo("tests/integration_tests/ci_registry.json")
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
    return dest

class SharedSshRunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pools = json.loads((ROOT / "config/pools.json").read_text())
        cls.cases = load_config(ROOT)

    def test_selector_reads_run_name_and_never_executable_commands(self):
        r = {"name": "A3-16p-CI-Example | test=dsv4_flash_a3_16p_example",
             "display_title": "A3-16p-CI-Example | test=dsv4_flash_a3_16p_example"}
        self.assertEqual(selected_test(r), TEST_16P)
        for bad in ({"display_title": "old run"}, {"display_title": "A3-16p-CI-Example | test=;rm -rf x"},
                    {"display_title": "A3-16p-CI-Example | test=../foo"},
                    {"display_title": "A3-16p-CI-Example | test=TEST;$(echo hi)"}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    selected_test({**r, "name": bad.get("display_title", r["name"]), **bad})

    def test_profile_follows_pinned_archive_and_validates_hardware(self):
        with tempfile.TemporaryDirectory() as td:
            tar = archive_with({TEST_8P: DEFAULT}, Path(td) / "model.tar.gz")
            got = load_profile(tar, TEST_8P, nodes=1, ngpu=8)
            self.assertEqual(got["module"], DEFAULT["module"])
            with self.assertRaisesRegex(ValueError, "not registered"):
                load_profile(tar, TEST_16P, nodes=1, ngpu=8)
            with self.assertRaisesRegex(ValueError, "resource requirements"):
                load_profile(tar, TEST_8P, nodes=2, ngpu=8)

    def test_registry_cannot_override_device_assignment(self):
        with tempfile.TemporaryDirectory() as td:
            for data in ({"NODE_IPS": "127.0.0.1"}, {"LITE_CI_JOB_ID": "bad"},
                         {"ASCEND_RT_VISIBLE_DEVICES": "5"}, {"NGPU": "64"},
                         {"evil-name": "x"}):
                with self.subTest(env=data):
                    tar = archive_with({TEST_8P: {**DEFAULT, "env_vars": data}},
                                       Path(td) / "model.tar.gz")
                    with self.assertRaisesRegex(ValueError, "environment"):
                        load_profile(tar, TEST_8P, nodes=1, ngpu=8)

    def test_unknown_hccl_address_blocks_a5_before_training(self):
        self.assertFalse(self.cases["a5-64p"]["enabled"])
        with self.assertRaisesRegex(ValueError, "missing SSH/HCCL"):
            ssh_runner.allocations(self.cases["a5-64p"], self.pools)

    def test_a3_and_a5_share_same_hardware_independent_ssh_runner(self):
        import copy
        pools = copy.deepcopy(self.pools)
        for index,node in enumerate(pools["a5-64p-lab"]["nodes"]):
            node["node_ip"] = "192.168.50." + str(index+1)
        for key,nodes,test in (("a3-8p",1,TEST_8P),("a3-16p",2,TEST_16P),("a5-64p",8,TEST_64P)):
            with self.subTest(case=key):
                c = self.cases[key]
                self.assertEqual(c["runner"], {"kind":"ssh"})
                selected = ssh_runner.allocations(c,pools)
                self.assertEqual(len(selected),nodes)
                profile = {**DEFAULT, "nnodes":nodes, "mode":"distributed" if nodes>1 else "single"}
                for node in selected:
                    path=f"/mnt/share/ci_tests/job/node{node['position']}"
                    script=ssh_runner.remote_command(node,selected,profile,path,STAMP,90,test)
                    self.assertEqual(subprocess.run(["bash","-n","-c",script],capture_output=True).returncode,0)
                    self.assertIn("ci_entrypoint launch", script)
                    self.assertIn("LITE_CI_JOB_ID",script)
                    self.assertIn("NGPU=8",script)
                    self.assertNotIn("git@github.com",script)

    def test_dir_override_changes_remote_path_not_process_identity(self):
        profile = DEFAULT
        selected = ssh_runner.allocations(self.cases["a3-8p"],self.pools)
        dest="/mnt/share/ci_tests/2026-10-09_run_A3-8p-CI-Example_4/node0"
        cmd=ssh_runner.remote_command(selected[0],selected,profile,dest,STAMP,90,TEST_8P)
        self.assertIn(dest,cmd)
        self.assertIn('job.pgid',cmd)
        self.assertIn('start_new_session=True',cmd)
        self.assertIn("LITE_CI_JOB_ID="+STAMP,cmd)
        self.assertIn(TEST_8P,cmd)

    def test_validated_run_requires_registered_test_id(self):
        with tempfile.TemporaryDirectory() as td:
            profile = {**DEFAULT}
            tar=archive_with({TEST_8P: profile},Path(td)/"source.tar.gz")
            with patch.object(ssh_runner,"model_archive",return_value=tar), \
                 patch.object(ssh_runner,"stage",side_effect=RuntimeError("stage failed")), \
                 patch.object(ssh_runner,"ssh") as remote:
                with self.assertRaisesRegex(RuntimeError,"staging failed"):
                    ssh_runner.run(self.cases["a3-8p"],"a"*40,STAMP,test_case=TEST_8P)
                remote.assert_not_called()
            with self.assertRaisesRegex(ValueError,"unsafe run"):
                ssh_runner.run(self.cases["a3-8p"],"a"*40,STAMP,test_case=TEST_8P,
                               remote_dir="../unsafe")

    def test_single_process_group_ownership(self):
        """Real local supervisor must record the process group it actually owns."""
        import signal
        import time
        with tempfile.TemporaryDirectory() as td:
            d=Path(td)
            (d/'repo/tests/integration_tests').mkdir(parents=True)
            (d/'repo/tests/__init__.py').touch()
            (d/'repo/tests/integration_tests/__init__.py').touch()
            (d/'repo/tests/integration_tests/ci_entrypoint.py').write_text(
                'import time; time.sleep(30)\n')
            (d/'set_env.sh').write_text('export FAKE_TOOLKIT_ENV=1\n')
            local_profile={**DEFAULT,'ascend_env':str(d/'set_env.sh')}
            node={'host_id':'local','node_ip':'127.0.0.1','position':0,
                  'ssh':'local','devices':[0]}
            cmd=ssh_runner.remote_command(node,[node],local_profile,
                                           str(d),STAMP,90,TEST_8P)
            proc=subprocess.Popen(['bash','-lc',cmd],stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE)
            pid=None
            try:
                for _ in range(100):
                    marker=d/'job.pgid'
                    if marker.exists() and marker.read_text().strip():
                        pid=int(marker.read_text().strip())
                        break
                    time.sleep(.05)
                self.assertIsNotNone(pid,'supervised rank PID not recorded')
                self.assertEqual(os.getpgid(pid),pid)
                os.killpg(pid,signal.SIGTERM)
                self.assertNotEqual(proc.wait(timeout=10),0)
                self.assertTrue((d/'exit_code.txt').is_file())
            finally:
                if pid and proc.poll() is None:
                    try: os.killpg(pid,signal.SIGKILL)
                    except ProcessLookupError: pass
                if proc.poll() is None: proc.kill()
                proc.communicate(timeout=10)


if __name__=="__main__":
    unittest.main()
