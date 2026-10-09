"""Terminal broadcasts are scoped to occupied execution hosts, never dispatcher."""
from pathlib import Path
import json
import tempfile
import unittest
import datetime as dt
from unittest.mock import patch
import subprocess

from ci_core.broadcast import broadcast_job, notification, display_width, star_box
from ci_core.config import load_config
from ci_core import dispatch


RUN = {"id": 37868722207, "run_number": 6, "run_attempt": 1,
       "created_at": "2026-10-09T01:13:28Z", "name": "A3-16p-CI-Example",
       "head_sha": "a"*40}


class BroadcastTests(unittest.TestCase):
    def test_exact_frame_and_beijing_timestamp(self):
        when = dt.datetime(2026,10,9,2,42,0,tzinfo=dt.timezone.utc)
        msg = notification(RUN,"begin",now=when)
        self.assertEqual(len(msg.splitlines()),6)
        self.assertEqual({display_width(line) for line in msg.splitlines()}, {88})
        self.assertIn("[2026-10-09 10:42:00] 本环境正在用于torchtitan CI验证，请勿使用！",msg)
        self.assertIn("https://github.com/depeng1994/torchtitan-npu/actions/runs/37868722207",msg)
        self.assertTrue(msg.startswith("*"*88))
        self.assertIn("本任务所占NPU资源已释放",notification(RUN,"end",now=when))
        self.assertIn("NPU仍可能被占用，请勿使用",notification(RUN,"uncertain",now=when))

    def test_only_execution_hosts_and_containers(self):
        config=Path(__file__).resolve().parents[1]
        cases=load_config(config)
        pools=json.loads((config/"config/pools.json").read_text())
        calls=[]
        def remote(argv, **kwargs):
            calls.append((argv[-2], argv[-1], kwargs["input"]))
            return subprocess.CompletedProcess(argv,0,"","")
        self.assertEqual(broadcast_job(cases["a3-16p"],pools,RUN,"begin",
                                       executor=remote),[])
        self.assertEqual([x[0] for x in calls],
                         ["a3-3-relay","a3-3-docker-relay",
                          "a3-4-relay","a3-4-docker-relay"])
        self.assertTrue(all(x[1]=="wall -n" for x in calls))
        self.assertFalse(any("dispatcher" in x[0] for x in calls))
        self.assertTrue(all("actions/runs/37868722207" in x[2] for x in calls))

    def test_host_wall_failure_is_reported(self):
        root=Path(__file__).resolve().parents[1]
        spec=load_config(root)["a3-8p"]
        pools=json.loads((root/"config/pools.json").read_text())
        def remote(argv, **kwargs):
            return subprocess.CompletedProcess(argv,1 if argv[-2]=="a3-4-relay" else 0,"","")
        self.assertEqual(broadcast_job(spec,pools,RUN,"begin",executor=remote),
                         ["a3-4-relay"])

    def test_begin_then_end_while_device_lock_held(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            (root/"config").mkdir()
            (root/"config/pools.json").write_text(json.dumps({"test":{"type":"ascend-a3","nodes":[
                {"host_id":"a3-4","ssh":"a3-4-docker-relay","device_ids":[0],
                 "broadcast_ssh":["a3-4-relay","a3-4-docker-relay"]}]}}))
            case={"name":"test-ci","runner":{"kind":"ssh"},"allocation":[
                {"host_id":"a3-4","devices":[0]}],"pool":"test",
                "timeout_seconds":10,"enabled":True}
            events=[]
            def tell(*args):
                # Resource still held for BOTH start and exit broadcasts.
                lk=root/"state/device-locks/a3-4_chip-000.lock"
                import fcntl
                with lk.open("a+b") as fd:
                    try:
                        fcntl.flock(fd.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                    except BlockingIOError:
                        events.append(args[-1])
                    else:
                        self.fail("Device lock unexpectedly free during wall")
                return []
            self_outer = self
            class Runner:
                def __init__(self, argv, **kwargs):
                    self.argv = argv
                    self_outer.assertIn("--remote-dir", argv)
                    self_outer.assertEqual(argv[argv.index("--remote-dir")+1],
                        "2026-10-09_09-13-28_run_A3-16p-CI_6")
                    self_outer.assertEqual(argv[argv.index("--stamp")+1],
                        "2026-10-09_09-13-28_run-37868722207_attempt-1")
                    kwargs["stdout"].write("TRAINED\n")
                def wait(self, **kwargs): return 0
            with patch.object(dispatch,"remote_preflight") as preflight, \
                 patch.object(dispatch,"broadcast_job",side_effect=tell), \
                 patch.object(dispatch.subprocess,"Popen",Runner), \
                 patch.object(dispatch,"metadata",return_value={"host":"a3-4-docker-relay","path":"/mnt/share/ci_tests/fake/run.log"}), \
                 patch.object(dispatch,"select_remote",return_value=["TRAINED"]):
                result=dispatch.execute(case,RUN,root=root)
            self.assertEqual(result.exit_code,0)
            self.assertEqual(events,["begin","end"])
            self.assertEqual(preflight.call_count,2)
            self.assertIn("TRAINED",result.log.read_text())

    def test_release_not_verified_warns_instead_of_falsely_free(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            (root/"config").mkdir()
            (root/"config/pools.json").write_text('{"test":{"type":"ascend-a3","nodes":[]}}')
            case={"name":"test-ci","runner":{"kind":"ssh"},
                  "allocation":[{"host_id":"a3-4","devices":[0]}],
                  "pool":"test","enabled":True,"timeout_seconds":5}
            phases=[]
            def preflight(*args):
                if phases:
                    raise RuntimeError("NPU process still present")
            def notice(*args):
                phases.append(args[-1])
                return []
            class Runner:
                def __init__(self, argv, **kwargs): pass
                def wait(self, **kwargs): return 0
            with patch.object(dispatch,"remote_preflight",side_effect=preflight), \
                 patch.object(dispatch,"broadcast_job",side_effect=notice), \
                 patch.object(dispatch.subprocess,"Popen",Runner), \
                 patch.object(dispatch.time,"sleep"):
                result=dispatch.execute(case,RUN,root=root)
            self.assertEqual(result.exit_code,125)
            self.assertEqual(phases,["begin","uncertain"])
            self.assertIn("release unverified",result.log.read_text())

    def test_failed_begin_stops_launch_and_sends_exit(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            (root/"config").mkdir()
            (root/"config/pools.json").write_text('{"test":{"type":"ascend-a3","nodes":[]}}')
            case={"name":"test-ci","runner":{"kind":"ssh"},"allocation":[{"host_id":"a3-4","devices":[0]}],
                  "pool":"test","enabled":True,"timeout_seconds":5}
            events=[]
            def wall(*args):
                events.append(args[-1])
                return ["a3-4-relay"] if args[-1]=="begin" else []
            with patch.object(dispatch,"remote_preflight"), \
                 patch.object(dispatch,"broadcast_job",side_effect=wall):
                result=dispatch.execute(case,RUN,root=root)
            self.assertEqual(result.exit_code,125)
            self.assertEqual(events,["begin","end"])
            self.assertNotIn("SHOULD_NOT_RUN",result.log.read_text())


if __name__=="__main__":
    unittest.main()
