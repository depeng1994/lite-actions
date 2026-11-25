"""End-to-end ST coverage for multi-node HCCL binding, container cleanup, and Actions logs."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ci_core import ssh_runner
from ci_core.contract import SELF_REPO, SELF_WORKFLOW
from ci_core.executor import execute
from ci_core.reporting import body_for, report
from ci_core.self_smoke import prepare, wait
from tests.scenarios import write_workspace

A3_DIVIDER = "+---------------------------+---------------+----------------------------------------------------------------------+\n"
A3_FOOTER = "+===========================+===============+======================================================================+\n"


def _a3_hardware_inventory():
    output = (
        "| NPU   Name | Health | Detail |\n"
        "| Chip  Phy-ID | Bus-Id | Detail |\n" + A3_FOOTER
    )
    for card in range(8):
        for chip in range(2):
            phy = card * 2 + chip
            output += f"| {card} Ascend910 | OK | 100 |\n"
            output += f"| {chip} {phy} | 0000:{0x80 + phy:02X}:00.0 | 0 |\n"
            output += A3_DIVIDER
    return output.removesuffix(A3_DIVIDER) + A3_FOOTER + A3_DIVIDER


A3_HEADER = (
    _a3_hardware_inventory()
    + "| NPU     Chip              | Process id    | Process name       | Process memory(MB)    | Process id in container |\n"
    + A3_FOOTER
)

# Real-world container-side `npu-smi info` output when another container runs on NPU 0..3:
# the Process name column is blank for foreign-container PIDs while NPU 4..7 are idle.
A3_CONTAINER_FOREIGN_BLANK_NAME = (
    A3_HEADER
    + "| 0       0                 | 999970        |                    | 16805                 | 1032417                 |\n"
    + "| 0       1                 | 999973        |                    | 16805                 | 1032418                 |\n"
    + "| 1       0                 | 999979        |                    | 17066                 | 1032419                 |\n"
    + "| 1       1                 | 999982        |                    | 16808                 | 1032420                 |\n"
    + "| 2       0                 | 999985        |                    | 16807                 | 1032421                 |\n"
    + "| 2       1                 | 999987        |                    | 17067                 | 1032422                 |\n"
    + "| 3       0                 | 999990        |                    | 16809                 | 1032423                 |\n"
    + "| 3       1                 | 999993        |                    | 16809                 | 1032424                 |\n"
    + "| No running processes found in NPU 4                                                                              |\n"
    + "| No running processes found in NPU 5                                                                              |\n"
    + "| No running processes found in NPU 6                                                                              |\n"
    + "| No running processes found in NPU 7                                                                              |\n"
    + A3_FOOTER
)

A3_ALL_IDLE = (
    A3_HEADER
    + "".join(
        f"| No running processes found in NPU {card}                                                                              |\n"
        for card in range(8)
    )
    + A3_FOOTER
)


class DispatchSmokeEndToEndST(unittest.TestCase):
    # =========================================================================
    # ST-1：拦截多机 16P 跨节点 HCCL 握手误选本地 Docker 网桥导致 EI0015 卡死的问题
    #
    # 故障场景：
    #   在蓝区双机 A3 宿主机（如 113.46.17.205:153 与 :154）上存在额外的本地 Docker
    #   网桥网卡（例如 br-942379d5944b / 172.30.153.1/28）。此前 ci_core/ssh_runner.py
    #   仅在 VLLM=True 分支里导出 HCCL_SOCKET_IFNAME / GLOO_SOCKET_IFNAME / HCCL_IF_IP，
    #   导致非 vLLM 仓库（lite-actions 自测与 torchtitan-npu 烟测）在 2 节点 16P 场景下
    #   node0 绑定到不可达网桥 IP，node1 连接 120s 超时并报 Communication_Error_Ranktable_Detect(EI0015)。
    #
    # 拦截目标：
    #   端到端驱动 2 节点 16P 调度入口（ssh_runner.run），验证非 vLLM 场景下 node0 与 node1
    #   的实际远端启动命令均显式导出节点配置的 HCCL_SOCKET_IFNAME、GLOO_SOCKET_IFNAME、
    #   各节点自身的 HCCL_IF_IP、NODE_IPS、NODE_RANK 以及调度器分配的端口租约。
    # =========================================================================
    def test_st_multinode_16p_exports_hccl_interface_and_ip_against_docker_bridge(self):
        pool_config = {
            "a3_shared_blue": {
                "owner": "blue",
                "type": "ascend-a3",
                "process_indexing": "card-die",
                "enabled": True,
                "verified_multinode_allocations": [
                    [
                        {"host_id": "192.0.2.153:153", "devices": list(range(8, 16))},
                        {"host_id": "192.0.2.154:154", "devices": list(range(8))},
                    ]
                ],
                "nodes": [
                    {
                        "host_id": "192.0.2.153:153",
                        "ssh": "192.0.2.153:153",
                        "node_ip": "192.168.0.30",
                        "network_interface": "enp23s0f3",
                        "capabilities": ["train", "npu_smoke"],
                        "device_ids": list(range(16)),
                        "allocation_groups": [
                            {"devices": list(range(8)), "verified": True},
                            {"devices": list(range(8, 16)), "verified": True},
                        ],
                        "environments": {
                            "cann_master_torchtitan_master_xyj": {
                                "container_id": "a" * 64,
                                "image_id": "sha256:" + "1" * 64,
                                "image_ref": "img:a",
                            }
                        },
                    },
                    {
                        "host_id": "192.0.2.154:154",
                        "ssh": "192.0.2.154:154",
                        "node_ip": "192.168.0.107",
                        "network_interface": "enp23s0f3",
                        "capabilities": ["train", "npu_smoke"],
                        "device_ids": list(range(16)),
                        "allocation_groups": [
                            {"devices": list(range(8)), "verified": True},
                            {"devices": list(range(8, 16)), "verified": True},
                        ],
                        "environments": {
                            "cann_master_torchtitan_master_xyj": {
                                "container_id": "b" * 64,
                                "image_id": "sha256:" + "2" * 64,
                                "image_ref": "img:b",
                            }
                        },
                    },
                ],
            }
        }
        case = {
            "pool": "a3_shared_blue",
            "docker": "cann_master_torchtitan_master_xyj",
            "kind": "npu_smoke",
            "resources": {"nodes": 2, "devices_per_node": 8},
            "execution_timeout_seconds": 180,
            "timeout_seconds": 960,
            "master_port": 16001,
            "hccl_if_base_port": 20128,
            "hccl_host_port_range": "24128-24191",
            "hccl_npu_port_range": "28128-28191",
            "verified_env": {
                "ASCEND_SET_ENV_PATH": "/usr/local/Ascend/ascend-toolkit/set_env.sh"
            },
            "allocation": [
                {"host_id": "192.0.2.153:153", "devices": list(range(8, 16))},
                {"host_id": "192.0.2.154:154", "devices": list(range(8))},
            ],
            "pools_snapshot": pool_config,
        }
        launch_cmds = {}

        def fake_stage(node, base, archive, sha, test_case):
            return f"{base}/node{node['position']}"

        def fake_run_remote(host, script, **kwargs):
            if "tests.integration_tests.tools.lite_actions.entrypoint launch" in script:
                launch_cmds[host] = script
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        def fake_exec_node(node, environment):
            cid = node["environments"][environment]["container_id"]
            return {**node, "ssh": f"{node['ssh']}/{cid}"}

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(ssh_runner, "VLLM", False),
            patch("ci_core.config.execution_node", side_effect=fake_exec_node),
            patch.object(ssh_runner, "stage", side_effect=fake_stage),
            patch("ci_core.transport.run", side_effect=fake_run_remote),
            patch.object(ssh_runner, "cancel"),
            patch.object(ssh_runner, "owned_processes_gone"),
        ):
            archive = Path(tmp) / "src.tar.gz"
            archive.write_bytes(b"fixture")
            case["_ci_stage_archive"] = str(archive)
            rc = ssh_runner.run(
                case,
                "c" * 40,
                "stamp_st_16p",
                test_case="dispatch_smoke_16p",
                root=Path(tmp),
            )

        self.assertEqual(rc, 0)
        node0_ssh = f"192.0.2.153:153/{'a' * 64}"
        node1_ssh = f"192.0.2.154:154/{'b' * 64}"
        self.assertEqual(set(launch_cmds), {node0_ssh, node1_ssh})

        for ssh_target, expected_rank, expected_ip in (
            (node0_ssh, "0", "192.168.0.30"),
            (node1_ssh, "1", "192.168.0.107"),
        ):
            cmd = launch_cmds[ssh_target]
            self.assertIn("HCCL_SOCKET_IFNAME=enp23s0f3", cmd)
            self.assertIn("GLOO_SOCKET_IFNAME=enp23s0f3", cmd)
            self.assertIn(f"HCCL_IF_IP={expected_ip}", cmd)
            self.assertIn("NODE_IPS=192.168.0.30,192.168.0.107", cmd)
            self.assertIn(f"NODE_RANK={expected_rank}", cmd)
            self.assertIn("MASTER_PORT=16001", cmd)
            self.assertIn("HCCL_IF_BASE_PORT=20128", cmd)

    # =========================================================================
    # ST-2：拦截同宿主机其他容器占卡时容器内 npu-smi 空 Process name 导致清理误报失败的问题
    #
    # 故障场景：
    #   在共享 A3 宿主机上，当其他业务容器正在 NPU 0..3（卡 0-7）运行训练，而 CI 的 8P 用例
    #   在 NPU 4..7（卡 8-15）顺利执行通过（DISPATCH_SMOKE_PASS）进入 cleanup 阶段时，
    #   docker exec 内执行的 `npu-smi info` 对跨容器进程会输出空白 Process name 列
    #   （`| 0 0 | 999970 |   | 16805 | 1032417 |`）。此前 `_process_rows` 因 `not cells[2]`
    #   直接抛异常，导致 3 次清理校验全部失败、已通过的用例被误判为 INFRA_ERROR (rc=125)
    #   且租约被错误置为 REVOKE_FAILED / 隔离。
    #
    # 拦截目标：
    #   端到端驱动 executor.execute() 全生命周期（preflight -> 启动子进程 PASS -> cleanup
    #   远端租约吊销 + 容器内 npu-smi 释放复核），在 cleanup 探活返回包含跨容器空白 Process name
    #   的真实 A3 输出时，断言用例最终 exit_code=0、release_verified=True，且日志无
    #   `ERROR: remote resource release unverified`。
    # =========================================================================
    def test_st_cleanup_verifies_release_with_foreign_container_blank_process_name(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_workspace(root)
            pool = {
                "a3_shared_blue": {
                    "owner": "blue",
                    "type": "ascend-a3",
                    "process_indexing": "card-die",
                    "enabled": True,
                    "force_reclaim": True,
                    "nodes": [
                        {
                            "host_id": "192.0.2.154:154",
                            "ssh": "192.0.2.154:154",
                            "host_ssh": "192.0.2.154:154",
                            "occupancy_policy": "preempt",
                            "node_ip": "192.168.0.107",
                            "network_interface": "enp23s0f3",
                            "machine_fingerprint": "f" * 64,
                            "device_ids": list(range(16)),
                            "allocation_groups": [
                                {"devices": list(range(8)), "verified": True},
                                {"devices": list(range(8, 16)), "verified": True},
                            ],
                            "environments": {
                                "cann_master_torchtitan_master_xyj": {
                                    "container_id": "d" * 64,
                                    "image_id": "sha256:" + "3" * 64,
                                    "image_ref": "img:d",
                                }
                            },
                        }
                    ],
                }
            }
            (root / "config/pools.json").write_text(json.dumps(pool))
            case = {
                "name": "case_004_dispatch_smoke_8p",
                "pool": "a3_shared_blue",
                "docker": "cann_master_torchtitan_master_xyj",
                "kind": "npu_smoke",
                "enabled": True,
                "resources": {"nodes": 1, "devices_per_node": 8},
                "execution_timeout_seconds": 75,
                "timeout_seconds": 855,
                "verified_env": {
                    "ASCEND_SET_ENV_PATH": "/usr/local/Ascend/ascend-toolkit/set_env.sh"
                },
                "allocation": [
                    {"host_id": "192.0.2.154:154", "devices": list(range(8, 16))}
                ],
                "pools_snapshot": pool,
                "lease_id": "lease_st_blank_proc",
            }
            run = {
                "id": 9001,
                "run_attempt": 1,
                "run_number": 6,
                "name": "lite-ci-pr-smoke",
                "created_at": "2026-11-25T06:00:00Z",
                "head_sha": "e" * 40,
                "_ci_test_id": "dispatch_smoke_8p",
                "_ci_case_id": "case_004_dispatch_smoke_8p",
            }
            container_ssh = f"192.0.2.154:154/{'d' * 64}"
            probe_calls = []

            def fake_transport(host, script, **kwargs):
                probe_calls.append((host, script))
                if "npu-smi info" in script and host == "192.0.2.154:154":
                    # Preflight runs on host SSH and sees all idle.
                    return SimpleNamespace(returncode=0, stdout=A3_ALL_IDLE, stderr="")
                if "npu-smi info" in script and host == container_ssh:
                    # Cleanup runs inside container SSH while another container runs on NPU 0..3.
                    return SimpleNamespace(
                        returncode=0,
                        stdout=A3_CONTAINER_FOREIGN_BLANK_NAME,
                        stderr="",
                    )
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            def fake_popen(cmd, stdout=None, **kwargs):
                # Simulate ssh_runner child writing PASS markers to pipeline.log and exiting 0.
                stdout.write("CASE_START dispatch_smoke_8p\n")
                stdout.write(f"RESULT_HOST: {container_ssh}\n")
                stdout.write(
                    "RESULT_LOG: /mnt/share/ci_tests/st_case_004/node0/run.log\n"
                )
                stdout.write("RESULT_EXIT_CODE: 0\n")
                stdout.write("CASE_RESULT dispatch_smoke_8p PASS 0\n")
                stdout.flush()
                proc = Mock()
                proc.pid = 424242
                proc.returncode = 0
                proc.wait.return_value = 0
                return proc

            with (
                patch.dict(
                    os.environ,
                    {"LITE_ACTIONS_STATE_DIR": str(root / "state")},
                    clear=False,
                ),
                patch(
                    "ci_core.config.execution_node",
                    side_effect=lambda node, _env: {**node, "ssh": container_ssh},
                ),
                patch("ci_core.transport.run", side_effect=fake_transport),
                patch("ci_core.executor.subprocess.Popen", side_effect=fake_popen),
                patch(
                    "ci_core.executor.select_remote",
                    return_value=["DISPATCH_SMOKE_PASS world_size=8 all_reduce_sum=28"],
                ),
                patch(
                    "ci_core.ssh_runner.revoke_remote_lease",
                    return_value={container_ssh},
                ),
                patch("ci_core.ssh_runner.cancel"),
                patch("ci_core.ssh_runner.owned_processes_gone"),
                patch("ci_core.executor.broadcast_job", return_value=[]),
            ):
                execution = execute(case, run, root=root)

            pipeline_text = execution.log.read_text()
            self.assertEqual(execution.exit_code, 0)
            self.assertTrue(execution.release_verified)
            self.assertIn("CASE_RESULT dispatch_smoke_8p PASS 0", pipeline_text)
            self.assertNotIn("ERROR: remote resource release unverified", pipeline_text)
            self.assertTrue(
                any(h == container_ssh and "npu-smi info" in s for h, s in probe_calls)
            )

    # =========================================================================
    # ST-3：拦截 GitHub Actions Job 日志仅有 Pass/Fail 缺少进度与 Debug 尾部日志的问题
    #
    # 故障场景：
    #   此前 `python3 -m ci_core.self_smoke wait-case` 仅在 `worker_probe` 用例上打印回执，
    #   对 `npu_smoke`、`test_worker_deploy`、`test_worker_cleanup` 等用例吞掉了
    #   `Lite-CI-PROGRESS` 阶段流转、`log_locator` 远端日志路径以及 `last_20_lines` 尾部报错，
    #   导致在 GitHub Actions 界面只能看到一行 PASS/FAIL，完全无法直接定位失败原因。
    #
    # 拦截目标：
    #   端到端串联 `self_smoke.prepare()` -> `reporting.report()/body_for()` 回执生成 ->
    #   `self_smoke.wait()` 多轮轮询消费，验证：
    #     1) PASS 与 FAIL 用例均实时输出 `[progress]` 阶段流转（不重复刷屏）；
    #     2) 用例结束时完整输出 `worker`/`pool`/`resources`/`log_locator` 与 `last_20_lines` 尾部日志；
    #     3) 尾部日志中的 Actions 命令注入（`::error::` / `##[warning]`）被彻底中和。
    # =========================================================================
    def test_st_actions_wait_streams_progress_and_sanitized_diagnostic_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = Path(tmp) / "ci-request.json"
            base_env = {
                "GITHUB_REPOSITORY": SELF_REPO,
                "GITHUB_RUN_ID": "99001",
                "GITHUB_RUN_ATTEMPT": "1",
                "GITHUB_SHA": "a" * 40,
                "CI_RESULT_AUTHOR": "depeng1994",
                "GH_TOKEN": "fixture-token",
            }
            with patch("ci_core.self_smoke.PLAN", plan_path):
                plan = prepare(base_env)

            smoke_case = next(
                c for c in plan["cases"] if c["test_id"] == "dispatch_smoke_16p"
            )
            run_meta = {
                "id": plan["run_id"],
                "run_attempt": plan["attempt"],
                "head_sha": plan["sha"],
                "path": ".github/workflows/" + SELF_WORKFLOW,
                "_ci_repo": SELF_REPO,
            }

            def make_progress(phase):
                return {
                    "user": {"login": "depeng1994"},
                    "body": "Lite-CI-PROGRESS "
                    + json.dumps(
                        {
                            "run_id": plan["run_id"],
                            "attempt": plan["attempt"],
                            "case_id": smoke_case["case_id"],
                            "worker": smoke_case["worker"],
                            "plan_digest": plan["plan_digest"],
                            "phase": phase,
                        },
                        separators=(",", ":"),
                    ),
                }

            # Sub-case A: PASS receipt prints progress stream + locator + diagnostic tail.
            pass_receipt = report(
                run_meta,
                smoke_case,
                plan["plan_digest"],
                "PASS",
                0,
                [
                    "[DISPATCH_SMOKE_LAUNCH] torch.distributed.run --nnodes 2",
                    "DISPATCH_SMOKE_PASS world_size=16 all_reduce_sum=120",
                ],
                True,
            )
            pass_receipt["log_locator"] = {
                "host": "113.46.17.205:153",
                "path": "/mnt/share/ci_tests/st_pass/node0/run.log",
            }
            pass_comment = {
                "user": {"login": "depeng1994"},
                "body": body_for(run_meta, smoke_case, pass_receipt),
            }

            client_pass = Mock()
            client_pass.commit_comments.side_effect = [
                [make_progress("QUEUED")],
                [make_progress("QUEUED"), make_progress("RUNNING")],
                [
                    make_progress("QUEUED"),
                    make_progress("RUNNING"),
                    make_progress("CLEANING"),
                    pass_comment,
                ],
            ]
            out_pass = io.StringIO()
            with (
                patch("ci_core.self_smoke.PLAN", plan_path),
                patch("ci_core.self_smoke.GitHubClient", return_value=client_pass),
                patch("ci_core.self_smoke.time.sleep"),
                redirect_stdout(out_pass),
            ):
                wait(base_env, only=smoke_case["case_id"])
            pass_text = out_pass.getvalue()
            self.assertEqual(pass_text.count("phase=QUEUED"), 1)
            self.assertEqual(pass_text.count("phase=RUNNING"), 1)
            self.assertEqual(pass_text.count("phase=CLEANING"), 1)
            self.assertIn(f"{smoke_case['case_id']}: PASS", pass_text)
            self.assertIn(
                "log=113.46.17.205:153:/mnt/share/ci_tests/st_pass/node0/run.log",
                pass_text,
            )
            self.assertIn(
                "DISPATCH_SMOKE_PASS world_size=16 all_reduce_sum=120", pass_text
            )

            # Sub-case B: FAIL receipt prints diagnostic tail and neutralizes Actions commands.
            fail_receipt = report(
                run_meta,
                smoke_case,
                plan["plan_digest"],
                "FAIL",
                1,
                [
                    "::error::fake-workflow-command",
                    "##[warning]fake-legacy-command",
                    "Communication_Error_Ranktable_Detect(EI0015): unconnected ranks",
                ],
                True,
            )
            fail_receipt["log_locator"] = {
                "host": "113.46.17.205:153",
                "path": "/mnt/share/ci_tests/st_fail/node0/run.log",
            }
            client_fail = Mock()
            client_fail.commit_comments.return_value = [
                make_progress("RUNNING"),
                {
                    "user": {"login": "depeng1994"},
                    "body": body_for(run_meta, smoke_case, fail_receipt),
                },
            ]
            out_fail = io.StringIO()
            with (
                patch("ci_core.self_smoke.PLAN", plan_path),
                patch("ci_core.self_smoke.GitHubClient", return_value=client_fail),
                redirect_stdout(out_fail),
                self.assertRaisesRegex(ValueError, "self-smoke cases failed"),
            ):
                wait(base_env, only=smoke_case["case_id"], once=True)
            fail_text = out_fail.getvalue()
            self.assertIn("phase=RUNNING", fail_text)
            self.assertIn(
                "Communication_Error_Ranktable_Detect(EI0015): unconnected ranks",
                fail_text,
            )
            self.assertIn(
                "log=113.46.17.205:153:/mnt/share/ci_tests/st_fail/node0/run.log",
                fail_text,
            )
            self.assertNotIn("::error::", fail_text)
            self.assertNotIn("##[warning]", fail_text)


if __name__ == "__main__":
    unittest.main()
