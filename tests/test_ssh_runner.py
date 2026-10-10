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
from ci_core.registry import selected_test, parse_request, resolve_request, _discovery_tar_filter

ROOT = Path(__file__).resolve().parents[1]
STAMP = "2026-10-09_17-43-56_run-37913151439_attempt-1"
TEST_8P = "dsv4_flash_a3_8p_example"
TEST_16P = "dsv4_flash_a3_16p_example"
TEST_64P = "dsv4_pro_a5_64p"
ASSET_ENV={"ASCEND_SET_ENV_PATH":"/opt/ascend/set_env.sh",
           "HF_ASSETS_PATH":"/models/flash", "CKPT_INIT_LOAD_PATH":"/weights"}
DEFAULT={"env_vars":ASSET_ENV,"mode":"single"}
STATIC = {
    "a3-8p": [{"host_id": "a3-4", "devices": list(range(8))}],
    "a3-16p": [{"host_id": "a3-3", "devices": list(range(8))},
               {"host_id": "a3-4", "devices": list(range(8, 16))}],
    "a5-64p": [{"host_id": f"a5-{i:02d}", "devices": list(range(8))}
               for i in range(1, 9)],
}

def runtime(case: dict) -> dict:
    return {**case, "allocation": STATIC[case["name"]]}



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

    def test_suite_expansion_topology_budget_and_selector_is_fail_closed(self):
        with patch('ci_core.registry.inspect_archive',return_value=[
            {'test_id':TEST_8P,'nnodes':1,'ngpu':8,'env_vars':ASSET_ENV},
            {'test_id':'dsv4_flash_a3_8p_adamw','nnodes':1,'ngpu':8,'env_vars':{**ASSET_ENV,'OPTIMIZER_OVERRIDES':''}}
        ]):
            s=[{'suite':'a3_8p_tests'}]
            rows=resolve_request(Path('/unused'),s,nodes=1,ngpu=8,max_cases=2)
            self.assertEqual(len(rows),2)
            with self.assertRaisesRegex(ValueError,'topology'):
                resolve_request(Path('/unused'),s,nodes=2,ngpu=8,max_cases=2)
            with self.assertRaisesRegex(ValueError,'budget'):
                resolve_request(Path('/unused'),s,nodes=1,ngpu=8,max_cases=1)
        req={'schema':1,'run_id':42,'attempt':1,'sha':'a'*40,'cases':[{'suite':'a3_8p_tests'}]}
        self.assertEqual(parse_request(req,{'id':42,'run_attempt':1,'head_sha':'a'*40})[0]['suite'],'a3_8p_tests')

    def test_model_owned_environment_is_forwarded_without_any_pipeline_asset_fields(self):
        for name in ('a3-8p', 'a3-16p', 'a5-64p'):
            self.assertEqual(self.cases[name]['runner'], {'kind': 'ssh'})
        nodes = ssh_runner.allocations(runtime(self.cases['a3-8p']), self.pools)
        profile = {'kind': 'ssh', 'mode': 'single', 'env_vars': {
            **ASSET_ENV, 'OPTIMIZER_OVERRIDES': ''}}
        dest = '/mnt/share/ci_tests/example/node0'
        cmd = ssh_runner.remote_command(nodes[0], nodes, profile,
                                        dest, STAMP, 120, TEST_8P)
        self.assertIn('source /opt/ascend/set_env.sh', cmd)
        self.assertIn('HF_ASSETS_PATH=/models/flash', cmd)
        self.assertIn('CKPT_INIT_LOAD_PATH=/weights', cmd)
        self.assertIn('OPTIMIZER_OVERRIDES=', cmd)
        self.assertIn(f'CKPT_SAVE_LOAD_PATH={dest}/checkpoints', cmd)
        self.assertNotIn('hf_assets_path', cmd)
        self.assertEqual(subprocess.run(['bash', '-n', '-c', cmd],
                                        capture_output=True).returncode, 0)

    def test_reserved_or_unconfigured_test_environment_rejected_before_ssh(self):
        from ci_core.registry import validate_case_environment
        self.assertEqual(validate_case_environment(ASSET_ENV), ASSET_ENV)
        for extra in ({'ASCEND_RT_VISIBLE_DEVICES': '0,1'},
                      {'CKPT_SAVE_LOAD_PATH': '/tmp/overwrite'},
                      {'NODE_IPS': '127.0.0.1'},
                      {'Network_Interface': 'evil'},
                      {'HF_ASSETS_PATH': ''},
                      {'ASCEND_SET_ENV_PATH': 'relative/set_env.sh'},
                      {'CONFIG': 'unexpected\nnew line'}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                validate_case_environment({**ASSET_ENV, **extra})

    def test_discovery_archive_skips_links_and_keeps_path_traversal_guard(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            regular=tarfile.TarInfo("tests/example.py")
            self.assertIsNotNone(_discovery_tar_filter(regular,str(root)))
            for kind in (tarfile.SYMTYPE,tarfile.LNKTYPE):
                link=tarfile.TarInfo(".claude/CLAUDE.md")
                link.type=kind; link.linkname="../.agents/AGENTS.md"
                self.assertIsNone(_discovery_tar_filter(link,str(root)))
            escape=tarfile.TarInfo("../escape.py")
            with self.assertRaises(tarfile.FilterError):
                _discovery_tar_filter(escape,str(root))

    def test_real_model_archive_resolves_8p_suite_owned_environment(self):
        model = ROOT.parent / 'torchtitan-npu-ci-refactor'
        if not model.is_dir():
            self.skipTest('adjacent model checkout not available')
        with tempfile.TemporaryDirectory() as td:
            archive = Path(td) / 'model-source.tar.gz'
            with tarfile.open(archive, 'w:gz') as tar:
                for item in (model/'tests/__init__.py',
                             model/'tests/integration_tests/__init__.py'):
                    tar.add(item, arcname=str(item.relative_to(model)))
                for subfolder in ('tests/integration_tests/nightly_all_models_test',
                                  'tests/integration_tests/tools'):
                    for file in (model/subfolder).rglob('*.py'):
                        tar.add(file, arcname=str(file.relative_to(model)))
            found=resolve_request(archive,[{'suite':'a3_8p_tests'}],
                                  nodes=1,ngpu=8,max_cases=2)
            self.assertEqual([x['test_id'] for x in found],
                ['dsv4_flash_a3_8p_example','dsv4_flash_a3_8p_adamw'])
            for case in found:
                self.assertTrue(case['env_vars']['ASCEND_SET_ENV_PATH'].startswith('/'))
                self.assertTrue(case['env_vars']['HF_ASSETS_PATH'].startswith('/'))
            # A generic dispatcher must not freeze a particular model's
            # optimizer implementation (AdamW may use Virtual, swap, etc.).
            # Inspect should preserve the testcase-owned value unchanged.
            value = found[1]['env_vars'].get('OPTIMIZER_OVERRIDES')
            self.assertIsInstance(value, str)

    def test_unknown_hccl_address_blocks_a5_before_training(self):
        self.assertFalse(self.cases["a5-64p"]["enabled"])
        with self.assertRaisesRegex(ValueError, "missing SSH/HCCL"):
            ssh_runner.allocations(runtime(self.cases["a5-64p"]), self.pools)

    def test_a3_and_a5_share_same_hardware_independent_ssh_runner(self):
        import copy
        pools = copy.deepcopy(self.pools)
        for index,node in enumerate(pools["a5-64p-lab"]["nodes"]):
            node["node_ip"] = "192.168.50." + str(index+1)
        for key,nodes,test in (("a3-8p",1,TEST_8P),("a3-16p",2,TEST_16P),("a5-64p",8,TEST_64P)):
            with self.subTest(case=key):
                c = self.cases[key]
                self.assertEqual(c["runner"]["kind"], "ssh")
                selected = ssh_runner.allocations(runtime(c),pools)
                self.assertEqual(len(selected),nodes)
                profile = {**DEFAULT, **c['runner'], "nnodes":nodes, "mode":"distributed" if nodes>1 else "single"}
                for node in selected:
                    path=f"/mnt/share/ci_tests/job/node{node['position']}"
                    script=ssh_runner.remote_command(node,selected,profile,path,STAMP,90,test)
                    self.assertEqual(subprocess.run(["bash","-n","-c",script],capture_output=True).returncode,0)
                    self.assertIn("tools.lite_actions.entrypoint launch", script)
                    self.assertIn("LITE_CI_JOB_ID",script)
                    self.assertIn("NGPU=8",script)
                    self.assertIn("ASCEND_SET_ENV_PATH",script)
                    self.assertNotIn("git@github.com",script)

    def test_dir_override_changes_remote_path_not_process_identity(self):
        profile = {**DEFAULT, **self.cases['a3-8p']['runner']}
        selected = ssh_runner.allocations(runtime(self.cases["a3-8p"]),self.pools)
        dest="/mnt/share/ci_tests/2026-10-09_run_A3-8p-CI-Example_4/node0"
        cmd=ssh_runner.remote_command(selected[0],selected,profile,dest,STAMP,90,TEST_8P)
        self.assertIn(dest,cmd)
        self.assertIn('job.pgid',cmd)
        self.assertIn('start_new_session=True',cmd)
        self.assertIn("LITE_CI_JOB_ID="+STAMP,cmd)
        self.assertIn(TEST_8P,cmd)

    def test_failed_preflight_never_stages_untrusted_code(self):
        with patch.object(ssh_runner,'model_archive',return_value=Path('/unused')), \
             patch.object(ssh_runner,'resolve_request',side_effect=ValueError('unknown test')), \
             patch.object(ssh_runner,'stage') as stage:
            with self.assertRaisesRegex(ValueError,'unknown test'):
                ssh_runner.run(runtime(self.cases['a3-8p']),'a'*40,STAMP,test_case=TEST_8P)
            stage.assert_not_called()

    def test_manifest_executes_multiple_cases_sequentially(self):
        import sys
        from unittest.mock import patch
        request={"schema":1,"run_id":1234,"attempt":1,"sha":"a"*40,
                 "cases":[
                    {"test_id":TEST_8P},
                    {"test_id":"dsv4_flash_a3_8p_adamw"},
                 ]}
        with tempfile.TemporaryDirectory() as td:
            manifest=Path(td)/"request.json"
            manifest.write_text(json.dumps(request))
            allocation=Path(td)/"allocation.json"
            allocation.write_text(json.dumps({"allocation": STATIC["a3-8p"]}))
            args=["runner","--case","a3-8p","--sha","a"*40,
                  "--stamp",STAMP,"--remote-dir","job-123",
                  "--run-id","1234","--run-attempt","1",
                  "--request-file",str(manifest),
                  "--allocation-file",str(allocation)]
            with patch.object(sys,"argv",args), patch.object(ssh_runner,"run",return_value=0) as execute, \
                 patch('ci_core.dispatch.remote_preflight'), \
                 patch.object(ssh_runner,'model_archive',return_value=Path('/unused')), \
                 patch('ci_core.registry.inspect_archive',side_effect=[
                    [{'test_id':TEST_8P,'ngpu':8,'nnodes':1,'env_vars':ASSET_ENV}],
                    [{'test_id':'dsv4_flash_a3_8p_adamw','ngpu':8,'nnodes':1,
                       'env_vars':{**ASSET_ENV,'OPTIMIZER_OVERRIDES':''}}]]):
                self.assertEqual(ssh_runner.main(),0)
                self.assertEqual(execute.call_count,2)
                self.assertEqual(execute.call_args_list[0].kwargs["remote_dir"],
                                 "job-123_case-01_"+TEST_8P)
                self.assertEqual(execute.call_args_list[1].kwargs["test_case"],"dsv4_flash_a3_8p_adamw")
            with patch.object(sys,"argv",args), patch.object(ssh_runner,"run",side_effect=[0,1]) as execute, \
                 patch('ci_core.dispatch.remote_preflight'), \
                 patch.object(ssh_runner,'model_archive',return_value=Path('/unused')), \
                 patch('ci_core.registry.inspect_archive',side_effect=[
                    [{'test_id':TEST_8P,'ngpu':8,'nnodes':1,'env_vars':ASSET_ENV}],
                    [{'test_id':'dsv4_flash_a3_8p_adamw','ngpu':8,'nnodes':1,
                       'env_vars':{**ASSET_ENV,'OPTIMIZER_OVERRIDES':''}}]]):
                self.assertEqual(ssh_runner.main(),1)
                self.assertEqual(execute.call_count,2)
            # Successful training is also required to release devices before
            # next case; a successful process exit alone is insufficient.
            with patch.object(sys,"argv",args), patch.object(ssh_runner,"run",side_effect=[0,0]) as execute, \
                 patch('ci_core.dispatch.remote_preflight',side_effect=RuntimeError('busy')), \
                 patch.object(ssh_runner,'model_archive',return_value=Path('/unused')), \
                 patch('ci_core.registry.inspect_archive',side_effect=[
                    [{'test_id':TEST_8P,'ngpu':8,'nnodes':1,'env_vars':ASSET_ENV}],
                    [{'test_id':'dsv4_flash_a3_8p_adamw','ngpu':8,'nnodes':1,
                       'env_vars':{**ASSET_ENV,'OPTIMIZER_OVERRIDES':''}}]]):
                self.assertEqual(ssh_runner.main(),125)
                self.assertEqual(execute.call_count,1)
            with patch.object(sys,"argv",args), patch.object(ssh_runner,"run",side_effect=[1,0]) as execute, \
                 patch('ci_core.dispatch.remote_preflight',side_effect=RuntimeError('busy')), \
                 patch.object(ssh_runner,'model_archive',return_value=Path('/unused')), \
                 patch('ci_core.registry.inspect_archive',side_effect=[
                    [{'test_id':TEST_8P,'ngpu':8,'nnodes':1,'env_vars':ASSET_ENV}],
                    [{'test_id':'dsv4_flash_a3_8p_adamw','ngpu':8,'nnodes':1,
                       'env_vars':{**ASSET_ENV,'OPTIMIZER_OVERRIDES':''}}]]):
                self.assertEqual(ssh_runner.main(),125)
                self.assertEqual(execute.call_count,1)

    def test_single_process_group_ownership(self):
        """Real local supervisor must record the process group it actually owns."""
        import signal
        import time
        with tempfile.TemporaryDirectory() as td:
            d=Path(td)
            (d/'repo/tests/integration_tests/tools/lite_actions').mkdir(parents=True)
            (d/'repo/tests/__init__.py').touch()
            (d/'repo/tests/integration_tests/__init__.py').touch()
            (d/'repo/tests/integration_tests/tools/lite_actions/entrypoint.py').write_text(
                'import time; time.sleep(30)\n')
            (d/'set_env.sh').write_text('export FAKE_TOOLKIT_ENV=1\n')
            local_profile={**DEFAULT,'env_vars':{**ASSET_ENV,
                           'ASCEND_SET_ENV_PATH':str(d/'set_env.sh')}}
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
