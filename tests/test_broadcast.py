"""Terminal broadcasts are scoped to occupied execution hosts, never dispatcher."""
from pathlib import Path
import json
import tempfile
import unittest
import datetime as dt
from unittest.mock import patch
import subprocess

from ci_core.broadcast import broadcast_job, notification, device_range
from ci_core.config import load_config
from ci_core import dispatch


RUN = {"id": 37868722207, "run_number": 6, "run_attempt": 1,
       "created_at": "2026-10-09T01:13:28Z", "name": "A3-16p-CI-Example",
       "head_sha": "a"*40,
       "display_title":"A3-16p-CI-Example | test=dsv4_flash_a3_16p_example"}


class BroadcastTests(unittest.TestCase):
    def test_two_exit_states_share_end_event_without_false_release(self):
        when = dt.datetime(2026, 10, 9, 2, 42, tzinfo=dt.timezone.utc)
        shared = {"node_rank": 0, "node_ip": "192.168.0.30",
                  "devices": list(range(8, 16)), "nnodes": 2, "ngpus_per_node": 8,
                  "now": when}
        messages = {
            "begin": notification(RUN, "begin", **shared),
            "released": notification(RUN, "end", released=True, **shared),
            "unverified": notification(RUN, "end", released=False, **shared),
        }
        for kind, msg in messages.items():
            with self.subTest(kind=kind):
                self.assertIn("2026-10-09 10:42:00", msg)
                self.assertIn("nodes=2，ngpus_per_node=8", msg)
                self.assertIn("节点：Node 0", msg)
                self.assertIn("小网IP : 192.168.0.30", msg)
                self.assertIn("Device : 8-15（共8P）", msg)
                self.assertIn("【任务ID】：37868722207", msg)
                self.assertIn("https://github.com/torchtitan-npu/torchtitan-npu/actions/runs/37868722207", msg)
                self.assertTrue(msg.startswith("********"))
                self.assertTrue(msg.rstrip().endswith("*" * 80))
        self.assertIn("【本机资源占用】", messages["begin"])
        self.assertIn("请勿占用", messages["begin"])
        self.assertIn("【本机资源释放】", messages["released"])
        self.assertIn("以上 Device 已释放", messages["released"])
        self.assertIn("【本机资源状态】", messages["unverified"])
        self.assertIn("释放尚未确认", messages["unverified"])
        self.assertNotIn("已释放", messages["unverified"])
        with self.assertRaisesRegex(ValueError, "verified release"):
            notification(RUN, "end", **shared)
        with self.assertRaisesRegex(ValueError, "unsupported broadcast phase"):
            notification(RUN, "uncertain", **shared)

    def test_actual_device_range_not_hardcoded(self):
        self.assertEqual(device_range(list(range(8))), "0-7")
        self.assertEqual(device_range(list(range(8, 16))), "8-15")
        self.assertEqual(device_range([0, 1, 4, 5, 9]), "0-1,4-5,9")
        for invalid in ([], [0, 0], [-1], [0, "1"]):
            with self.subTest(devices=invalid), self.assertRaises(ValueError):
                device_range(invalid)

    def test_only_execution_hosts_and_containers(self):
        config=Path(__file__).resolve().parents[1]
        cases=load_config(config)
        pools=json.loads((config/"config/pools.json").read_text())
        calls=[]
        def remote(argv, **kwargs):
            calls.append((argv[-2], argv[-1], kwargs["input"]))
            return subprocess.CompletedProcess(argv,0,"","")
        allocated={**cases["a3-16p"],"allocation":[
            {"host_id":"a3-3","devices":list(range(8))},
            {"host_id":"a3-4","devices":list(range(8,16))}]}
        self.assertEqual(broadcast_job(allocated,pools,RUN,"begin",
                                       executor=remote),[])
        self.assertEqual([x[0] for x in calls],
                         ["a3-3-relay","a3-3-docker-relay",
                          "a3-4-relay","a3-4-docker-relay"])
        self.assertTrue(all(x[1]=="wall -n" for x in calls))
        self.assertFalse(any("dispatcher" in x[0] for x in calls))
        self.assertTrue(all("actions/runs/37868722207" in x[2] for x in calls))
        # Each host and its CI container get only *their* allocated DeviceSet.
        for target, _, payload in calls[:2]:
            self.assertIn("节点：Node 0", payload)
            self.assertIn("小网IP : 192.168.0.30", payload)
            self.assertIn("Device : 0-7（共8P）", payload)
            self.assertNotIn("Device : 8-15", payload)
        for target, _, payload in calls[2:]:
            self.assertIn("节点：Node 1", payload)
            self.assertIn("小网IP : 192.168.0.107", payload)
            self.assertIn("Device : 8-15（共8P）", payload)
            self.assertNotIn("Device : 0-7", payload)


    def test_single_node_eight_p_variants_are_scoped_to_actual_group(self):
        root = Path(__file__).resolve().parents[1]
        spec = load_config(root)["a3-8p"]
        pools = json.loads((root / "config/pools.json").read_text())
        for group in (list(range(8)), list(range(8, 16))):
            with self.subTest(group=group):
                messages = []
                def collect(argv, **kwargs):
                    messages.append(kwargs["input"])
                    return subprocess.CompletedProcess(argv, 0, "", "")
                case = {**spec, "allocation": [{"host_id": "a3-3", "devices": group}]}
                self.assertEqual(broadcast_job(case, pools, RUN, "begin", executor=collect), [])
                self.assertEqual(len(messages), 2)
                for msg in messages:
                    self.assertIn("nodes=1，ngpus_per_node=8", msg)
                    self.assertIn(f"Device : {device_range(group)}（共8P）", msg)
                    self.assertIn("节点：Node 0", msg)

    def test_end_broadcast_per_host_and_release_verification(self):
        root = Path(__file__).resolve().parents[1]
        pools = json.loads((root / "config/pools.json").read_text())
        case = {**load_config(root)["a3-16p"], "allocation": [
            {"host_id": "a3-3", "devices": list(range(8, 16))},
            {"host_id": "a3-4", "devices": list(range(8))}]}
        def render(released):
            messages = {}
            def collect(argv, **kwargs):
                messages[argv[-2]] = kwargs["input"]
                return subprocess.CompletedProcess(argv, 0, "", "")
            self.assertEqual(broadcast_job(case, pools, RUN, "end", released=released,
                                           executor=collect), [])
            return messages
        released = render(True)
        self.assertIn("节点：Node 0", released["a3-3-relay"])
        self.assertIn("Device : 8-15（共8P）", released["a3-3-relay"])
        self.assertIn("节点：Node 1", released["a3-4-relay"])
        self.assertIn("Device : 0-7（共8P）", released["a3-4-relay"])
        self.assertTrue(all("以上 Device 已释放" in msg for msg in released.values()))
        uncertain = render(False)
        self.assertTrue(all("释放尚未确认" in msg for msg in uncertain.values()))
        self.assertTrue(all("以上 Device 已释放" not in msg for msg in uncertain.values()))
        with self.assertRaisesRegex(ValueError, "verified release"):
            broadcast_job(case, pools, RUN, "end", executor=lambda *args, **kwargs: None)

    def test_missing_ip_or_mixed_node_sizes_reject_before_wall(self):
        root = Path(__file__).resolve().parents[1]
        pools = json.loads((root / "config/pools.json").read_text())
        spec = load_config(root)["a3-16p"]
        calls = []
        def remote(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")
        broken = {**spec, "allocation": [
            {"host_id": "a3-3", "devices": list(range(8))},
            {"host_id": "a3-4", "devices": list(range(8, 12))}]}
        with self.assertRaisesRegex(ValueError, "consistent devices"):
            broadcast_job(broken, pools, RUN, "begin", executor=remote)
        node = pools["a3-shared"]["nodes"][1]
        old = node["node_ip"]
        node["node_ip"] = None
        try:
            broken["allocation"][1]["devices"] = list(range(8))
            with self.assertRaisesRegex(ValueError, "missing trusted HCCL node IP"):
                broadcast_job(broken, pools, RUN, "begin", executor=remote)
        finally:
            node["node_ip"] = old
        self.assertEqual(calls, [])

    def test_host_wall_failure_is_reported(self):
        root=Path(__file__).resolve().parents[1]
        spec=load_config(root)["a3-8p"]
        pools=json.loads((root/"config/pools.json").read_text())
        def remote(argv, **kwargs):
            return subprocess.CompletedProcess(argv,1 if argv[-2]=="a3-4-relay" else 0,"","")
        runtime={**spec,"allocation":[{"host_id":"a3-4","devices":list(range(8))}]}
        self.assertEqual(broadcast_job(runtime,pools,RUN,"begin",executor=remote),
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
            def tell(*args, **kwargs):
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
                        "2026-10-09_09-13-28_run_A3-16p-CI-Example_6")
                    self_outer.assertEqual(argv[argv.index("--stamp")+1],
                        "2026-10-09_09-13-28_run-37868722207_attempt-1")
                    kwargs["stdout"].write("TRAINED\n")
                def poll(self): return None
                def wait(self, **kwargs): return 0
            with patch.object(dispatch,"prepare_profile",return_value=("dsv4_flash_a3_16p_example",{"ascend_env":"/path/set_env.sh"})), \
                 patch.object(dispatch,"remote_preflight") as preflight, \
                 patch.object(dispatch,"broadcast_job",side_effect=tell), \
                 patch.object(dispatch.subprocess,"Popen",Runner), \
                 patch.object(dispatch,"metadata",return_value={"host":"a3-4-docker-relay","path":"/mnt/share/ci_tests/fake/run.log"}), \
                 patch.object(dispatch,"select_remote",return_value=["TRAINED"]):
                result=dispatch.execute(case,RUN,root=root)
            self.assertEqual(result.exit_code,0)
            self.assertEqual(events,["begin","end"])
            self.assertEqual(preflight.call_count,2)
            self.assertIn("TRAINED",result.log.read_text())

    def test_end_wall_failure_does_not_relabel_verified_training_as_failed(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            (root/"config").mkdir()
            (root/"config/pools.json").write_text('{"test":{"type":"ascend-a3","nodes":[]}}')
            case={"name":"test-ci","runner":{"kind":"ssh"},"allocation":[
                {"host_id":"a3-4","devices":[0]}],"pool":"test",
                "enabled":True,"timeout_seconds":10}
            events=[]
            def wall(*args, **kwargs):
                events.append((args[-1], kwargs.get("released")))
                return ["a3-4-relay"] if args[-1]=="end" else []
            class Runner:
                def __init__(self,argv,**kwargs):kwargs["stdout"].write("CASE_RESULT approved PASS 0\n")
                def poll(self):return None
                def wait(self,**kwargs):return 0
            with patch.object(dispatch,"prepare_profile",
                              return_value=("approved",{"ascend_env":"/path/set_env.sh"})), \
                 patch.object(dispatch,"remote_preflight"), \
                 patch.object(dispatch,"broadcast_job",side_effect=wall), \
                 patch.object(dispatch.subprocess,"Popen",Runner), \
                 patch.object(dispatch,"metadata",
                              return_value={"host":"a3-4-docker-relay",
                                            "path":"/mnt/share/ci_tests/fake/run.log"}), \
                 patch.object(dispatch,"select_remote",return_value=["step5 PASS"]):
                execution=dispatch.execute(case,RUN,root=root)
            self.assertEqual(execution.exit_code,0)
            self.assertTrue(execution.release_verified)
            self.assertEqual(events,[("begin", None),("end", True)])

    def test_retry_transient_host_wall_only_before_training(self):
        cfg=Path(__file__).resolve().parents[1]
        case={**load_config(cfg)["a3-8p"],"allocation":[{"host_id":"a3-4",
                                                         "devices":list(range(8))}]}
        pools=json.loads((cfg/"config/pools.json").read_text())
        attempts=[]
        def remote(argv,**kwargs):
            attempts.append(argv[-2])
            return subprocess.CompletedProcess(argv, 255 if len(attempts)==1 else 0,
                                               "", "")
        with patch("ci_core.broadcast.time.sleep") as pause:
            self.assertEqual(broadcast_job(case,pools,RUN,"begin",executor=remote),[])
        self.assertEqual(attempts,["a3-4-relay","a3-4-relay",
                                    "a3-4-docker-relay"])
        pause.assert_called_once_with(2)

    def test_release_not_verified_warns_instead_of_falsely_free(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            (root/"config").mkdir()
            (root/"config/pools.json").write_text('{"test":{"type":"ascend-a3","nodes":[]}}')
            case={"name":"test-ci","runner":{"kind":"ssh"},
                  "allocation":[{"host_id":"a3-4","devices":[0]}],
                  "pool":"test","enabled":True,"timeout_seconds":5}
            phases=[]
            releases=[]
            def preflight(*args, **kwargs):
                if phases:
                    raise RuntimeError("NPU process still present")
            def notice(*args, **kwargs):
                phases.append(args[-1])
                if args[-1] == "end":
                    releases.append(kwargs["released"])
                return []
            class Runner:
                def __init__(self, argv, **kwargs): pass
                def poll(self): return None
                def wait(self, **kwargs): return 0
            with patch.object(dispatch,"prepare_profile",return_value=("dsv4_flash_a3_16p_example",{"ascend_env":"/path/set_env.sh"})), \
                 patch.object(dispatch,"remote_preflight",side_effect=preflight), \
                 patch.object(dispatch,"broadcast_job",side_effect=notice), \
                 patch.object(dispatch.subprocess,"Popen",Runner), \
                 patch.object(dispatch.time,"sleep"):
                result=dispatch.execute(case,RUN,root=root)
            self.assertEqual(result.exit_code,125)
            self.assertEqual(phases,["begin","end"])
            self.assertEqual(releases,[False])
            self.assertIn("release unverified",result.log.read_text())

    def test_failed_begin_stops_launch_and_sends_exit(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            (root/"config").mkdir()
            (root/"config/pools.json").write_text('{"test":{"type":"ascend-a3","nodes":[]}}')
            case={"name":"test-ci","runner":{"kind":"ssh"},"allocation":[{"host_id":"a3-4","devices":[0]}],
                  "pool":"test","enabled":True,"timeout_seconds":5}
            events=[]
            def wall(*args, **kwargs):
                events.append(args[-1])
                return ["a3-4-relay"] if args[-1]=="begin" else []
            with patch.object(dispatch,"prepare_profile",return_value=("dsv4_flash_a3_16p_example",{"ascend_env":"/path/set_env.sh"})), \
                 patch.object(dispatch,"remote_preflight"), \
                 patch.object(dispatch,"broadcast_job",side_effect=wall):
                result=dispatch.execute(case,RUN,root=root)
            self.assertEqual(result.exit_code,125)
            self.assertEqual(events,["begin","end"])
            self.assertNotIn("SHOULD_NOT_RUN",result.log.read_text())


if __name__=="__main__":
    unittest.main()
