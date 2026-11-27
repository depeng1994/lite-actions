"""End-to-end ST coverage for multi-node HCCL binding, container cleanup, and Actions logs."""

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ci_core import ssh_runner
from ci_core.contract import SELF_REPO, SELF_WORKFLOW
from ci_core.executor import execute
from ci_core.reporting import body_for, progress_body, report
from ci_core.self_smoke import prepare, wait
from tests.integration_tests.tools.lite_actions import entrypoint
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
    def test_st_smoke_execution_budget_starts_after_queue_and_preparation(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = Path(tmp) / "ci-request.json"
            environ = {
                "GITHUB_REPOSITORY": SELF_REPO,
                "GITHUB_RUN_ID": "99002",
                "GITHUB_RUN_ATTEMPT": "1",
                "GITHUB_SHA": "a" * 40,
                "CI_RESULT_AUTHOR": "reporter",
                "GH_TOKEN": "fixture-token",
            }
            with patch("ci_core.self_smoke.PLAN", plan_path):
                plan = prepare(environ)
            run = {
                "id": plan["run_id"],
                "run_attempt": plan["attempt"],
                "head_sha": plan["sha"],
                "path": ".github/workflows/" + SELF_WORKFLOW,
                "_ci_repo": SELF_REPO,
            }
            digest = plan["plan_digest"]

            def comment(body):
                return {"user": {"login": "reporter"}, "body": body}

            for case in plan["cases"]:
                if case["kind"] != "npu_smoke":
                    continue
                for execution_seconds in (299, 301):
                    with self.subTest(case=case["case_id"], seconds=execution_seconds):
                        elapsed = 0
                        launch_at = 8 * 60
                        nodes = case["resources"]["nodes"]

                        def sleep(seconds):
                            nonlocal elapsed
                            elapsed += seconds

                        def run_collective(command, *, env, check, timeout):
                            if execution_seconds > timeout:
                                raise subprocess.TimeoutExpired(command, timeout)
                            return subprocess.CompletedProcess(command, 0)

                        def commit_comments(_sha):
                            if elapsed < launch_at:
                                phase = "QUEUED" if elapsed < 6 * 60 else "STAGING"
                                return [
                                    comment(progress_body(run, case, digest, phase))
                                ]
                            with (
                                patch.dict(
                                    os.environ,
                                    {
                                        "NODE_IPS": ",".join(
                                            f"192.0.2.{i + 1}" for i in range(nodes)
                                        ),
                                        "NODE_RANK": "0",
                                        "NNODES": str(nodes),
                                        "NGPU": "8",
                                        "MASTER_PORT": "16001",
                                    },
                                    clear=True,
                                ),
                                patch(
                                    "sys.argv",
                                    [
                                        "entrypoint",
                                        "launch",
                                        "--test-id",
                                        case["test_id"],
                                        "--output-dir",
                                        tmp,
                                    ],
                                ),
                                patch(
                                    "tests.integration_tests.tools.lite_actions.runner.subprocess.run",
                                    side_effect=run_collective,
                                ),
                            ):
                                try:
                                    entrypoint.main()
                                except RuntimeError as exc:
                                    receipt = report(
                                        run, case, digest, "FAIL", 1, [str(exc)], True
                                    )
                                else:
                                    receipt = report(
                                        run, case, digest, "PASS", 0, [], True
                                    )
                            return [comment(body_for(run, case, receipt))]

                        output = io.StringIO()
                        with (
                            patch("ci_core.self_smoke.PLAN", plan_path),
                            patch("ci_core.self_smoke.GitHubClient") as client,
                            patch(
                                "ci_core.self_smoke.time",
                                SimpleNamespace(monotonic=lambda: elapsed, sleep=sleep),
                            ),
                            redirect_stdout(output),
                        ):
                            client.return_value.commit_comments.side_effect = (
                                commit_comments
                            )
                            if execution_seconds == 299:
                                wait(environ, only=case["case_id"])
                                self.assertIn(
                                    f"{case['case_id']}: PASS", output.getvalue()
                                )
                            else:
                                with self.assertRaisesRegex(
                                    ValueError, "self-smoke cases failed"
                                ):
                                    wait(environ, only=case["case_id"])
                                self.assertIn(
                                    "exceeded 300-second execution budget",
                                    output.getvalue(),
                                )
                        self.assertEqual(elapsed, launch_at)

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
                    side_effect=lambda node, _env, **_: {**node, "ssh": container_ssh},
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
            self.assertFalse(
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

    # =========================================================================
    # ST-4：拦截 Yellow 隔离测试 Worker 在 tick()/maybe_ack() 中因 BrokerClient 缺多仓路由而崩溃瘫痪的问题
    #
    # 故障场景：
    #   在 Yellow 区的 sandboxed test worker（如 yellow-test-id45558549653a1）运行期间，
    #   Router.tick() 会因 _zone_name=="yellow" 对未 ACK 的 PR/GitCode 用例调用 maybe_ack()。
    #   此前 maybe_ack() 在进入 _broker_active 分支前先调用了 self.client_for(run)，
    #   而 BrokerClient 未实现 for_run()/for_repo() 且 self.repo=None，导致对
    #   depeng1994/lite-actions 与 depeng1994/torchtitan-npu 均抛出
    #   `ValueError: run belongs to another GitHub repository`，使整个 tick() 循环反复异常退出，
    #   无法继续拉取 GitCode 流水线、发送 ACK 或重试外部占卡等待的 case_007。
    #
    # 拦截目标：
    #   端到端使用真实 BrokerClient + Yellow Test Router（yellow-test-id999a1）驱动 tick()，
    #   分别验证 depeng1994/lite-actions PR smoke 与 GitCode depeng1994/torchtitan-npu 用例：
    #     1) maybe_ack() 通过 broker.post_progress() 正常发送 QUEUED ACK 且不抛仓库不匹配异常；
    #     2) tick() 后续继续推进 discovery（running_workflows）与 pump/flush，不再瘫痪。
    # =========================================================================
    def test_st_yellow_test_worker_broker_client_acks_and_ticks_multi_repo_runs(self):
        from ci_core.router import Router
        from ci_core.test_broker import BrokerClient

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_workspace(root)
            broker = BrokerClient(root / "broker.sock", worker_id="yellow-test-id999a1")
            posted_progress = []

            def fake_post_progress(
                run, item, digest, phase, progress_id=None, *, retry=False
            ):
                posted_progress.append((run.get("_ci_repo"), item["case_id"], phase))
                return f"prog-{len(posted_progress)}"

            with (
                patch.dict(
                    os.environ,
                    {
                        "LITE_ACTIONS_STATE_DIR": str(root / "state"),
                        "LITE_ACTIONS_RESOURCE_LOCK_ROOT": str(root / "leases"),
                    },
                    clear=False,
                ),
                patch.object(broker, "post_progress", side_effect=fake_post_progress),
                patch.object(
                    broker,
                    "check_run_status",
                    return_value={"status": "in_progress", "run_attempt": 1},
                ),
                patch.object(broker, "running_workflows", return_value=[]) as poll_mock,
            ):
                router = Router(
                    root,
                    "yellow-test-id999a1",
                    broker,
                    "depeng1994",
                    broker_client=broker,
                )
                try:
                    router.next_discovery = 0
                    pr_run = {
                        "id": 999,
                        "run_attempt": 1,
                        "head_sha": "a" * 40,
                        "path": ".github/workflows/lite_actions_pr_smoke.yml",
                        "name": "lite-ci-pr-smoke",
                        "_ci_repo": SELF_REPO,
                        "_ci_provider": "github",
                    }
                    gc_run = {
                        "id": "gcrun999",
                        "run_attempt": 1,
                        "head_sha": "b" * 40,
                        "path": ".gitcode/workflows/dispatcher_smoke.yml",
                        "name": "lite-ci-dispatcher-smoke",
                        "_ci_repo": "depeng1994/torchtitan-npu",
                        "_ci_provider": "gitcode",
                        "_ci_dispatch_id": "disp999",
                    }
                    router.state.admit(
                        [
                            (
                                "depeng1994/lite-actions:999:1:case_007_dispatch_smoke_yellow_a3_8p",
                                {
                                    "phase": "QUEUED",
                                    "run": pr_run,
                                    "item": {
                                        "case_id": "case_007_dispatch_smoke_yellow_a3_8p",
                                        "order": 0,
                                        "test_id": "dispatch_smoke_yellow_a3_8p",
                                        "kind": "npu_smoke",
                                        "worker": "yellow-test-id999a1",
                                        "target_pool": "a3_shared_yellow",
                                        "docker": "torchtitan_ci_a3_r2",
                                        "resources": {
                                            "nodes": 1,
                                            "devices_per_node": 8,
                                        },
                                        "execution_timeout_seconds": 75,
                                    },
                                    "digest": "1" * 64,
                                    "defaults": {},
                                    "created_at": 1.0,
                                    "retry_after": 9999999999.0,
                                },
                            )
                        ]
                    )
                    router.state.admit(
                        [
                            (
                                "gitcode:depeng1994/torchtitan-npu:gcrun999:1:disp999:case_005_dispatch_smoke_yellow_a3_8p",
                                {
                                    "phase": "QUEUED",
                                    "run": gc_run,
                                    "item": {
                                        "case_id": "case_005_dispatch_smoke_yellow_a3_8p",
                                        "order": 0,
                                        "test_id": "dispatch_smoke_yellow_a3_8p",
                                        "kind": "npu_smoke",
                                        "worker": "yellow-test-id999a1",
                                        "target_pool": "a3_shared_yellow",
                                        "docker": "torchtitan_ci_a3_r2",
                                        "resources": {
                                            "nodes": 1,
                                            "devices_per_node": 8,
                                        },
                                        "execution_timeout_seconds": 75,
                                    },
                                    "digest": "2" * 64,
                                    "defaults": {},
                                    "created_at": 2.0,
                                    "retry_after": 9999999999.0,
                                },
                            )
                        ]
                    )
                    router.tick()
                    self.assertEqual(
                        posted_progress,
                        [
                            (
                                SELF_REPO,
                                "case_007_dispatch_smoke_yellow_a3_8p",
                                "QUEUED",
                            ),
                            (
                                "depeng1994/torchtitan-npu",
                                "case_005_dispatch_smoke_yellow_a3_8p",
                                "QUEUED",
                            ),
                        ],
                    )
                    self.assertEqual(poll_mock.call_count, 1)
                finally:
                    router.close()

    # =========================================================================
    # ST-5：拦截宿主机 Health 列非 OK（Warning/Alarm/Critical）导致硬件映射校验误报失败的问题
    #
    # 故障场景：
    #   Yellow A3 节点（7.246.124.101:22）切换为 `occupancy_policy="preempt"` 后，
    #   preflight 阶段启用 `_verified_inventory` 校验整机 `npu-smi info` 硬件映射表；
    #   宿主机部分 NPU 的 Health 列为 `Warning`/`Alarm`/`Critical` 时，旧代码硬性校验
    #   `npu_cells[1] != "OK"`，导致直接抛出 `ValueError: unrecognized NPU hardware mapping`，
    #   阻断整机全部抢占模式任务。
    #
    # 拦截目标：
    #   验证 `_verified_inventory` 不再对 Health 列做任何阻断校验（OK/Warning/Alarm/Critical
    #   均放行），仅严格校验 NPU/Chip/Phy-ID/Bus-Id 硬件拓扑映射本身。
    # =========================================================================
    def test_st_verified_inventory_ignores_health_column_values(self):
        from ci_core.executor import _verified_inventory

        def make_a3_inventory(npu6_health: str) -> str:
            lines = [
                "+------------------------------------------------------------------------------------------------+",
                "| npu-smi 25.3.rc1.b071            Version: 25.3.rc1.b071                                        |",
                "+---------------------------+---------------+----------------------------------------------------+",
                "| NPU   Name                | Health        | Power(W)    Temp(C)           Hugepages-Usage(page)|",
                "| Chip  Phy-ID              | Bus-Id        | AICore(%)   Memory-Usage(MB)  HBM-Usage(MB)        |",
                "+===========================+===============+====================================================+",
            ]
            for card in range(8):
                for die in (0, 1):
                    phy = card * 2 + die
                    health = npu6_health if (card == 6 and die == 0) else "OK"
                    bus = f"0000:{0x80 + phy:02X}:00.0"
                    lines.append(
                        f"| {card:<5} Ascend910           | {health:<13} | 160.0       41                0    / 0             |"
                    )
                    lines.append(
                        f"| {die:<5} {phy:<19} | {bus:<13} | 0           0    / 0          3200 / 65536         |"
                    )
                    lines.append(
                        "+------------------------------------------------------------------------------------------------+"
                    )
            lines.extend(
                [
                    "+---------------------------+---------------+----------------------------------------------------+",
                    "| NPU     Chip              | Process id    | Process name             | Process memory(MB)      |",
                    "+===========================+===============+====================================================+",
                    "| No running processes found in NPU 0                                                             |",
                    "+===========================+===============+====================================================+",
                ]
            )
            return "\n".join(lines)

        for health in ("OK", "Warning", "Alarm", "Critical"):
            with self.subTest(health=health):
                _verified_inventory(
                    make_a3_inventory(health), "card-die", list(range(16))
                )

    # =========================================================================
    # ST-6：拦截并发执行场景下容器清理 `revoke_remote_lease` 遇瞬态 SSH 255 或详情丢失导致误判失败的问题
    #
    # 故障场景：
    #   Run 45579526201 中 `case_006_dispatch_smoke_8p`（:153 0–7）已输出
    #   `DISPATCH_SMOKE_PASS`，但由于同容器另一用例并发重负载，旧 `revoke_remote_lease`
    #   仅设 45s 超时且未重试瞬态 SSH `exit=255`、同时吞掉了 `failed[host_ssh]` 原因，
    #   导致连续 3 次返回空集合并触发 `UnverifiedRemoteRevoke`，将正常卡组误写入隔离区。
    #
    # 拦截目标：
    #   端到端驱动 `executor.execute()` 在清理阶段先后注入一次 `revoke_remote_lease`
    #   SSH 瞬态 `exit=255`（验证自动重试恢复与 >=90s 超时裕度使 `cleanup_verified=True`），
    #   以及在全 3 次均失败时验证 `pipeline.log` 精确记录 `REVOKE_ERROR host=... detail=...`。
    # =========================================================================
    def test_st_concurrent_cleanup_retries_ssh_255_and_surfaces_revoke_detail(self):
        from types import SimpleNamespace

        from ci_core import ssh_runner
        from ci_core.executor import execute

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config"
            config_dir.mkdir(parents=True)
            host = "192.0.2.153:153"
            container_ssh = f"{host}/{'a' * 64}"
            pools = {
                "a3_shared_blue": {
                    "type": "ascend-a3",
                    "owner": "blue",
                    "enabled": True,
                    "force_reclaim": False,
                    "indexing": "card-die",
                    "process_indexing": "card-die",
                    "nodes": [
                        {
                            "host_id": host,
                            "ssh": host,
                            "host_ssh": host,
                            "node_ip": "192.0.2.153",
                            "network_interface": "enp23s0f3",
                            "device_ids": list(range(16)),
                            "allocation_groups": [
                                {"devices": list(range(8)), "verified": True},
                                {"devices": list(range(8, 16)), "verified": True},
                            ],
                            "environments": {
                                "cann_master_torchtitan_master_xyj": {
                                    "container_id": "a" * 64,
                                    "image_id": "sha256:" + "1" * 64,
                                    "image_ref": "cann:latest",
                                }
                            },
                            "machine_fingerprint": "a" * 64,
                        }
                    ],
                }
            }
            (config_dir / "pools.json").write_text(json.dumps(pools), encoding="utf-8")
            (config_dir / "workers.json").write_text(
                json.dumps({"blue": {"enabled": True}}), encoding="utf-8"
            )
            setup = root / "set_env.sh"
            setup.write_text("# setup\n", encoding="utf-8")
            run = {
                "id": 99002,
                "run_attempt": 1,
                "run_number": 99,
                "name": "lite-ci-self-smoke",
                "created_at": "2026-11-25T09:00:00Z",
                "head_sha": "c" * 40,
                "path": ".github/workflows/lite_actions_self_smoke.yml",
                "_ci_repo": SELF_REPO,
                "_ci_provider": "github",
                "_ci_test_id": "dispatch_smoke_8p",
                "_ci_lease": SimpleNamespace(
                    lease_id="leaseconc001",
                    state="RESERVED",
                    mark_launching=lambda **kwargs: None,
                    mark_active=lambda: None,
                    set_remote_revoked_hosts=lambda hosts: None,
                ),
            }
            case = {
                "case_id": "case_006_dispatch_smoke_8p",
                "enabled": True,
                "kind": "npu_smoke",
                "pool": "a3_shared_blue",
                "docker": "cann_master_torchtitan_master_xyj",
                "lease_id": "leaseconc001",
                "resources": {"nodes": 1, "devices_per_node": 8},
                "execution_timeout_seconds": 60,
                "verified_env": {"ASCEND_SET_ENV_PATH": str(setup)},
                "allocation": [
                    {"host_id": host, "devices": [8, 9, 10, 11, 12, 13, 14, 15]}
                ],
                "timeout_seconds": 120,
            }

            revoke_calls = []

            def fake_revoke_ssh(target, command, *, timeout=45, check=False):
                revoke_calls.append((target, timeout))
                if len(revoke_calls) == 1:
                    return SimpleNamespace(
                        returncode=255, stdout="", stderr="Connection closed by relay"
                    )
                return SimpleNamespace(
                    returncode=0, stdout="REVOKED:leaseconc001\n", stderr=""
                )

            errors: dict[str, str] = {}
            revoked = ssh_runner.revoke_remote_lease(
                {
                    "lease_id": "leaseconc001",
                    "remote_nodes": [
                        {
                            "ssh": container_ssh,
                            "devices": list(range(8)),
                            "indexing": "card-die",
                        }
                    ],
                },
                execute=fake_revoke_ssh,
                check_npu=False,
                errors=errors,
            )
            self.assertEqual(revoked, {container_ssh})
            self.assertEqual(errors, {})
            self.assertEqual(len(revoke_calls), 2)
            self.assertTrue(all(t >= 90 for _, t in revoke_calls))

            # Now simulate persistent failure inside execute() cleanup and assert detail is logged.
            def fake_transport(target, script, *, timeout=90, check=True, stdin=None):
                return SimpleNamespace(
                    returncode=0,
                    stdout=A3_CONTAINER_FOREIGN_BLANK_NAME,
                    stderr="",
                )

            def fake_popen(cmd, stdout=None, **kwargs):
                stdout.write("CASE_START dispatch_smoke_8p\n")
                stdout.write(f"RESULT_HOST: {container_ssh}\n")
                stdout.write("RESULT_LOG: /mnt/share/ci_tests/st_conc/node0/run.log\n")
                stdout.write("RESULT_EXIT_CODE: 0\n")
                stdout.write("CASE_RESULT dispatch_smoke_8p PASS 0\n")
                stdout.flush()
                proc = Mock()
                proc.pid = 434343
                proc.returncode = 0
                proc.wait.return_value = 0
                return proc

            def failing_revoke(lease, **kwargs):
                err_map = kwargs.get("errors")
                if err_map is not None:
                    err_map[container_ssh] = "leased npu remains occupied: [(0, 12345)]"
                return set()

            with (
                patch.dict(
                    os.environ,
                    {"LITE_ACTIONS_STATE_DIR": str(root / "state")},
                    clear=False,
                ),
                patch(
                    "ci_core.config.execution_node",
                    side_effect=lambda node, _env, **_: {**node, "ssh": container_ssh},
                ),
                patch("ci_core.transport.run", side_effect=fake_transport),
                patch("ci_core.executor.subprocess.Popen", side_effect=fake_popen),
                patch(
                    "ci_core.executor.select_remote",
                    return_value=["DISPATCH_SMOKE_PASS world_size=8 all_reduce_sum=28"],
                ),
                patch(
                    "ci_core.ssh_runner.revoke_remote_lease", side_effect=failing_revoke
                ),
                patch("ci_core.ssh_runner.cancel"),
                patch("ci_core.ssh_runner.owned_processes_gone"),
                patch("ci_core.executor.broadcast_job", return_value=[]),
                patch("ci_core.executor.time.sleep"),
            ):
                execution = execute(case, run, root=root)

            pipeline_text = execution.log.read_text()
            self.assertFalse(execution.release_verified)
            self.assertIn(f"REVOKE_ERROR host={container_ssh}", pipeline_text)
            self.assertIn("leased npu remains occupied: [(0, 12345)]", pipeline_text)

    # =========================================================================
    # ST-7：拦截任务取消后残留永久 quarantine 隔离锁的故障模式
    #
    # 故障场景：
    #   1) 任务在 STAGING 阶段被取消，远端尚未写入 job.intent；此前 execute() 在
    #      owned_processes_gone 报错后跳过 set_remote_revoked_hosts，导致即使远端
    #      revoke 已成功，本地 lease.close() 仍将整机 NPU 卡写入永久 quarantine。
    #   2) 取消瞬间若 SSH 通道瞬断触发 REVOKE_FAILED 隔离锁，此前缺乏自动对账恢复，
    #      网络恢复后隔离锁永久死锁后续所有 CI 任务。
    # =========================================================================
    def test_st_cancel_preserves_revoked_hosts_and_auto_recovers_quarantine(self):
        import threading

        from ci_core.resources import (
            PhysicalResourceCoordinator,
            UnverifiedRemoteRevoke,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "worker"
            write_workspace(root)
            coord = PhysicalResourceCoordinator(Path(tmp) / "leases")
            container_ssh = "192.0.2.153:153/" + "a" * 64
            pools = {
                "a3_shared_blue": {
                    "owner": "blue",
                    "type": "ascend-a3",
                    "process_indexing": "card-die",
                    "enabled": True,
                    "nodes": [
                        {
                            "host_id": "192.0.2.153:153",
                            "ssh": "192.0.2.153:153",
                            "node_ip": "192.168.0.30",
                            "network_interface": "enp23s0f3",
                            "capabilities": ["train", "npu_smoke"],
                            "device_ids": list(range(8)),
                            "allocation_groups": [
                                {"devices": list(range(8)), "verified": True}
                            ],
                            "environments": {
                                "cann_master_torchtitan_master_xyj": {
                                    "container_id": "a" * 64,
                                    "image_id": "sha256:" + "1" * 64,
                                    "image_ref": "img:a",
                                }
                            },
                        }
                    ],
                }
            }
            alloc = [{"host_id": "192.0.2.153:153", "devices": list(range(8))}]
            lease = coord.acquire(
                alloc,
                owner_worker="blue-test-id1a1",
                pool="a3_shared_blue",
                is_production=False,
                task_key="cancel-during-staging",
                docker="cann_master_torchtitan_master_xyj",
            )
            cancel_ev = threading.Event()
            case = {
                "case_id": "case_004_dispatch_smoke_8p",
                "enabled": True,
                "kind": "npu_smoke",
                "pool": "a3_shared_blue",
                "docker": "cann_master_torchtitan_master_xyj",
                "resources": {"nodes": 1, "devices_per_node": 8},
                "allocation": alloc,
                "pools_snapshot": pools,
                "timeout_seconds": 180,
                "execution_timeout_seconds": 150,
                "verified_env": {
                    "ASCEND_SET_ENV_PATH": "/usr/local/Ascend/ascend-toolkit/set_env.sh"
                },
                "master_port": 16000,
                "hccl_if_base_port": 20000,
                "hccl_host_port_range": "24000-24063",
                "hccl_npu_port_range": "28000-28063",
                "port_slot": 0,
            }
            run = {
                "_ci_repo": SELF_REPO,
                "_ci_test_id": "dispatch_smoke_8p",
                "_ci_zone": "blue",
                "_ci_lease": lease,
                "_ci_cancel_event": cancel_ev,
                "id": 909,
                "run_number": 1,
                "run_attempt": 1,
                "name": "lite-ci-pr-smoke",
                "path": ".github/workflows/lite_actions_pr_smoke.yml",
                "head_sha": "c" * 40,
                "created_at": "2026-11-25T12:00:00Z",
            }

            def fake_popen(cmd, stdout=None, **kwargs):
                cancel_ev.set()
                proc = Mock()
                proc.pid = 454545
                proc.poll.return_value = None
                proc.returncode = -15
                proc.wait.return_value = -15
                return proc

            with (
                patch.dict(
                    os.environ,
                    {"LITE_ACTIONS_STATE_DIR": str(root / "state")},
                    clear=False,
                ),
                patch(
                    "ci_core.config.execution_node",
                    side_effect=lambda node, _env, **_: {**node, "ssh": container_ssh},
                ),
                patch(
                    "ci_core.transport.run",
                    return_value=SimpleNamespace(
                        returncode=0, stdout=A3_ALL_IDLE, stderr=""
                    ),
                ),
                patch("ci_core.executor.subprocess.Popen", side_effect=fake_popen),
                patch("ci_core.executor.os.killpg"),
                patch(
                    "ci_core.ssh_runner.revoke_remote_lease",
                    return_value={container_ssh},
                ),
                patch("ci_core.ssh_runner.cancel"),
                patch(
                    "ci_core.ssh_runner.owned_processes_gone",
                    side_effect=AssertionError(
                        "should not require job.intent before staging"
                    ),
                ),
                patch("ci_core.executor.broadcast_job", return_value=[]),
            ):
                execution = execute(case, run, root=root)
                lease.close()

            self.assertTrue(execution.release_verified)
            self.assertEqual(coord.all_quarantined_resources(), set())
            self.assertEqual(coord.all_active_leases(), [])

            # 场景 2：瞬时 SSH 断连导致 REVOKE_FAILED 后，不写入 quarantine 隔离锁且网络恢复自动自愈释放
            lease2 = coord.acquire(
                alloc,
                owner_worker="blue-test-id2a1",
                pool="a3_shared_blue",
                is_production=False,
                task_key="cancel-ssh-blip",
                docker="cann_master_torchtitan_master_xyj",
            )
            lease2.mark_launching(
                remote_nodes=[
                    {
                        "host_id": "192.0.2.153:153",
                        "ssh": container_ssh,
                        "devices": list(range(8)),
                        "indexing": "card-die",
                    }
                ]
            )
            with self.assertRaises(UnverifiedRemoteRevoke):
                lease2.release(remote_revoked_hosts=set())
            self.assertEqual(coord.all_quarantined_resources(), set())
            self.assertEqual(len(coord.all_active_leases()), 1)

            recovered = coord.reconcile_quarantined_leases(
                revoke_fn=lambda doc, **_kw: {container_ssh}
            )
            self.assertEqual(recovered, [lease2.lease_id])
            self.assertEqual(coord.all_quarantined_resources(), set())
            self.assertEqual(coord.all_active_leases(), [])

    # =========================================================================
    # ST-8：拦截旧分支 PR 部署 TestWorker 运行过期 router 代码（或命中 GitHub
    #       数小时未刷新的陈旧 merge_commit_sha）导致 PR 烟测卡死的故障模式
    #
    # 故障场景：
    #   PR 基于旧版 main 创建，随后 main 合入了新的 router 修复；此时 GitHub 的
    #   pr.merge_commit_sha 可能仍停留在数小时前的旧 main。TestSupervisor 必须在
    #   本地实时拉取最新 origin/main 与 refs/pull/<pr>/head 合并部署，确保
    #   code_dir 同时包含最新 main 修复与 PR 自身改动。
    # =========================================================================
    def test_st_deploy_merges_fresh_main_with_pr_head_locally(self):
        import subprocess

        from ci_core.test_supervisor import _prepare_merged_pr_checkout

        with tempfile.TemporaryDirectory() as tmp:
            origin = Path(tmp) / "origin.git"
            work = Path(tmp) / "work"
            code_dir = Path(tmp) / "deployed"
            git_env = {
                **os.environ,
                "GIT_AUTHOR_NAME": "test",
                "GIT_AUTHOR_EMAIL": "t@test",
                "GIT_COMMITTER_NAME": "test",
                "GIT_COMMITTER_EMAIL": "t@test",
            }
            subprocess.run(
                ["git", "init", "--bare", "-q", str(origin)], check=True, env=git_env
            )
            subprocess.run(["git", "init", "-q", str(work)], check=True, env=git_env)
            subprocess.run(
                ["git", "-C", str(work), "checkout", "-q", "-b", "main"],
                check=True,
                env=git_env,
            )
            (work / "router.py").write_text("ALLOWLIST_OLD = True\n")
            (work / "README.md").write_text("base readme\n")
            subprocess.run(["git", "-C", str(work), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(work), "commit", "-q", "-m", "base"],
                check=True,
                env=git_env,
            )
            subprocess.run(
                ["git", "-C", str(work), "remote", "add", "origin", str(origin)],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(work), "push", "-q", "origin", "main"],
                check=True,
                env=git_env,
            )

            # 旧 PR 分支仅改文档，仍携带旧 router.py
            subprocess.run(
                ["git", "-C", str(work), "checkout", "-q", "-b", "doc-pr"],
                check=True,
                env=git_env,
            )
            (work / "README.md").write_text("pr updated readme\n")
            subprocess.run(["git", "-C", str(work), "add", "README.md"], check=True)
            subprocess.run(
                ["git", "-C", str(work), "commit", "-q", "-m", "docs"],
                check=True,
                env=git_env,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(work),
                    "push",
                    "-q",
                    "origin",
                    "HEAD:refs/pull/60/head",
                ],
                check=True,
                env=git_env,
            )

            # 随后 main 修复了 router.py
            subprocess.run(
                ["git", "-C", str(work), "checkout", "-q", "main"],
                check=True,
                env=git_env,
            )
            (work / "router.py").write_text("ALLOWLIST_OLD = False\n")
            subprocess.run(["git", "-C", str(work), "add", "router.py"], check=True)
            subprocess.run(
                ["git", "-C", str(work), "commit", "-q", "-m", "fix router"],
                check=True,
                env=git_env,
            )
            subprocess.run(
                ["git", "-C", str(work), "push", "-q", "origin", "main"],
                check=True,
                env=git_env,
            )

            _prepare_merged_pr_checkout(
                str(origin), 60, code_dir, base_branch="main", git_env=git_env
            )
            self.assertEqual(
                (code_dir / "router.py").read_text(), "ALLOWLIST_OLD = False\n"
            )
            self.assertEqual(
                (code_dir / "README.md").read_text(), "pr updated readme\n"
            )

    # =========================================================================
    # ST-9：拦截共享宿主机外部容器占用导致已 PASS 任务在 cleanup 误判失败并写入隔离锁的问题
    #
    # 故障场景：
    #   在共享宿主机（如 113.46.17.205:153）上，其它容器（例如 wln）在我们的 16P 用例
    #   跑完 `DISPATCH_SMOKE_PASS` 后正在 NPU 芯片上运行。旧代码在 cleanup 阶段调用
    #   `revoke_remote_lease(check_npu=True)` 与 `remote_preflight(cleanup=True)`，
    #   把其它容器的进程误判为清理未完成，将已 PASS 的用例翻转成 INFRA_ERROR，并将全部
    #   16 张卡写入 `quarantine/*.json` 永久隔离锁。
    #
    # 拦截目标：
    #   1) 外部容器进程在清理阶段占用所分配芯片时，只要本租约自身进程已退出，
    #      `execute()` 返回 PASS 且 `release_verified=True`；
    #   2) 不写任何 `quarantine/*.json` 隔离锁，下一任务可直接 `acquire()` 并按策略抢占。
    # =========================================================================
    def test_st_cleanup_checks_only_owned_processes_and_never_writes_quarantine(self):
        from types import SimpleNamespace

        from ci_core.executor import execute
        from ci_core.resources import PhysicalResourceCoordinator

        foreign_busy_on_cards_0_to_3 = (
            A3_HEADER
            + "| 0       0                 | 515080        | python3.12         | 60189                 | NA                      |\n"
            + "| 1       0                 | 515081        | python3.12         | 60189                 | NA                      |\n"
            + "| 2       0                 | 515082        | python3.12         | 60189                 | NA                      |\n"
            + "| 3       0                 | 515083        | python3.12         | 60189                 | NA                      |\n"
            + "".join(
                f"| No running processes found in NPU {card}                                                                              |\n"
                for card in range(4, 8)
            )
            + A3_FOOTER
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "worker"
            write_workspace(root)
            coord = PhysicalResourceCoordinator(Path(tmp) / "leases")
            host0 = "192.0.2.153:153"
            host1 = "192.0.2.154:154"
            cssh0 = f"{host0}/{'a' * 64}"
            cssh1 = f"{host1}/{'b' * 64}"
            pools = {
                "a3_shared_blue": {
                    "type": "ascend-a3",
                    "owner": "blue",
                    "enabled": True,
                    "force_reclaim": False,
                    "indexing": "card-die",
                    "process_indexing": "card-die",
                    "nodes": [
                        {
                            "host_id": host0,
                            "ssh": host0,
                            "host_ssh": host0,
                            "node_ip": "192.0.2.153",
                            "network_interface": "enp23s0f3",
                            "device_ids": list(range(16)),
                            "allocation_groups": [
                                {"devices": list(range(8)), "verified": True},
                                {"devices": list(range(8, 16)), "verified": True},
                            ],
                            "environments": {
                                "cann_master_torchtitan_master_xyj": {
                                    "container_id": "a" * 64,
                                    "image_id": "sha256:" + "1" * 64,
                                    "image_ref": "cann:latest",
                                }
                            },
                            "machine_fingerprint": "a" * 64,
                        },
                        {
                            "host_id": host1,
                            "ssh": host1,
                            "host_ssh": host1,
                            "node_ip": "192.0.2.154",
                            "network_interface": "enp23s0f3",
                            "device_ids": list(range(16)),
                            "allocation_groups": [
                                {"devices": list(range(8)), "verified": True},
                                {"devices": list(range(8, 16)), "verified": True},
                            ],
                            "environments": {
                                "cann_master_torchtitan_master_xyj": {
                                    "container_id": "b" * 64,
                                    "image_id": "sha256:" + "1" * 64,
                                    "image_ref": "cann:latest",
                                }
                            },
                            "machine_fingerprint": "b" * 64,
                        },
                    ],
                    "verified_multinode_allocations": [
                        [
                            {"host_id": host0, "devices": list(range(8))},
                            {"host_id": host1, "devices": list(range(8))},
                        ]
                    ],
                }
            }
            (root / "config/pools.json").write_text(json.dumps(pools), encoding="utf-8")
            setup = root / "set_env.sh"
            setup.write_text("# setup\n", encoding="utf-8")
            alloc = [
                {"host_id": host0, "devices": list(range(8))},
                {"host_id": host1, "devices": list(range(8))},
            ]
            lease = coord.acquire(
                alloc,
                owner_worker="blue-test-id16a1",
                pool="a3_shared_blue",
                is_production=False,
                task_key="16p-shared-host",
                docker="cann_master_torchtitan_master_xyj",
            )
            case = {
                "case_id": "case_005_dispatch_smoke_16p",
                "enabled": True,
                "kind": "npu_smoke",
                "pool": "a3_shared_blue",
                "docker": "cann_master_torchtitan_master_xyj",
                "lease_id": lease.lease_id,
                "resources": {"nodes": 2, "devices_per_node": 8},
                "allocation": alloc,
                "pools_snapshot": pools,
                "execution_timeout_seconds": 60,
                "timeout_seconds": 120,
                "verified_env": {"ASCEND_SET_ENV_PATH": str(setup)},
                "master_port": lease.ports["master_port"],
                "hccl_if_base_port": lease.ports["hccl_if_base_port"],
                "hccl_host_port_range": lease.ports["hccl_host_port_range"],
                "hccl_npu_port_range": lease.ports["hccl_npu_port_range"],
                "port_slot": lease.ports["port_slot"],
            }
            run = {
                "id": 99003,
                "run_attempt": 1,
                "run_number": 99,
                "name": "lite-ci-pr-smoke",
                "created_at": "2026-11-26T09:45:00Z",
                "head_sha": "d" * 40,
                "path": ".github/workflows/lite_actions_pr_smoke.yml",
                "_ci_repo": SELF_REPO,
                "_ci_provider": "github",
                "_ci_test_id": "dispatch_smoke_16p",
                "_ci_lease": lease,
            }

            transport_calls = {"count": 0}
            revoke_check_npu_flags = []

            def fake_transport(target, script, *, timeout=90, check=True, stdin=None):
                transport_calls["count"] += 1
                # Preflight (1st round across 2 nodes) sees idle NPUs; if cleanup ever
                # called remote_preflight again, foreign container occupies NPU 0..3.
                out = (
                    A3_ALL_IDLE
                    if transport_calls["count"] <= 2
                    else foreign_busy_on_cards_0_to_3
                )
                return SimpleNamespace(returncode=0, stdout=out, stderr="")

            def fake_popen(cmd, stdout=None, **kwargs):
                stdout.write("CASE_START dispatch_smoke_16p\n")
                stdout.write(f"RESULT_HOST: {cssh0}\n")
                stdout.write("RESULT_LOG: /mnt/share/ci_tests/st16/node0/run.log\n")
                stdout.write("RESULT_EXIT_CODE: 0\n")
                stdout.write("CASE_RESULT dispatch_smoke_16p PASS 0\n")
                stdout.flush()
                proc = Mock()
                proc.pid = 515099
                proc.returncode = 0
                proc.wait.return_value = 0
                return proc

            def fake_revoke(lease_doc, **kwargs):
                revoke_check_npu_flags.append(kwargs.get("check_npu"))
                return {cssh0, cssh1}

            with (
                patch.dict(
                    os.environ,
                    {"LITE_ACTIONS_STATE_DIR": str(root / "state")},
                    clear=False,
                ),
                patch(
                    "ci_core.config.execution_node",
                    side_effect=lambda node, _env, **_: {
                        **node,
                        "ssh": cssh0 if node["host_id"] == host0 else cssh1,
                    },
                ),
                patch("ci_core.transport.run", side_effect=fake_transport),
                patch("ci_core.executor.subprocess.Popen", side_effect=fake_popen),
                patch(
                    "ci_core.executor.select_remote",
                    return_value=[
                        "DISPATCH_SMOKE_PASS world_size=16 all_reduce_sum=120"
                    ],
                ),
                patch(
                    "ci_core.ssh_runner.revoke_remote_lease",
                    side_effect=fake_revoke,
                ),
                patch("ci_core.ssh_runner.cancel"),
                patch("ci_core.ssh_runner.owned_processes_gone"),
                patch("ci_core.executor.broadcast_job", return_value=[]),
            ):
                execution = execute(case, run, root=root)
                lease.close()

            self.assertEqual(execution.exit_code, 0)
            self.assertTrue(execution.release_verified)
            self.assertEqual(revoke_check_npu_flags, [False])
            # remote_preflight only ran before launch (once per node), never during cleanup.
            self.assertEqual(transport_calls["count"], 2)
            # Zero quarantine files exist and next job can immediately re-acquire.
            self.assertEqual(coord.all_quarantined_resources(), set())
            self.assertEqual(list(coord.quarantine_dir.glob("*.json")), [])
            self.assertEqual(coord.all_active_leases(), [])
            next_lease = coord.acquire(
                alloc,
                owner_worker="blue-test-id17a1",
                pool="a3_shared_blue",
                is_production=False,
                task_key="16p-next",
                docker="cann_master_torchtitan_master_xyj",
            )
            next_lease.close()

    # =========================================================================
    # ST-10：拦截跨平台/多仓库串行阻塞以及黄区 TestWorker 本地缓存发现被误限流 60s 的性能退化
    #
    # 故障场景：
    #   1) 当主 Worker 同时监听多个 GitHub 仓库与 GitCode 仓库时，串行轮询或串行状态检查会
    #      导致慢响应或异常仓库阻塞其余平台/仓库的发现与取消判定；
    #   2) 黄区沙箱 TestWorker 的 `POLL_RUNS` 仅通过本地 Unix socket 读取主黄区内存缓存、
    #      不产生外部 HTTP 请求，但此前复用主黄区 60s 发现节流，导致 TestWorker 在发现新
    #      下发的 GitCode/GitHub 任务前白白空等最多 60 秒。
    #
    # 拦截目标：
    #   1) 通过 `threading.Barrier` 证明 `MultiPlatformClient` + `MultiRepoGitHubClient`
    #      真正并发轮询所有 GitHub 与 GitCode 仓库，且单仓故障不影响其他平台/仓库返回结果；
    #   2) 驱动黄区 Broker 托管 `Router.tick()`，证明多平台活跃 Run 状态查询真正并发执行，
    #      且连续多轮 `tick()` 均立即触发 `POLL_RUNS` 不受 60s 外部节流阻塞。
    # =========================================================================
    def test_st_cross_platform_multi_repo_polling_and_yellow_broker_discovery_run_concurrently(
        self,
    ):
        import threading

        from ci_core.contract import TORCHTITAN_REPO, VLLM_REPO
        from ci_core.gitcode import MultiPlatformClient
        from ci_core.github import MultiRepoGitHubClient
        from ci_core.router import Router
        from ci_core.test_broker import BrokerClient

        # Sub-case A: MultiPlatformClient + MultiRepoGitHubClient poll all repos/platforms in parallel.
        poll_barrier = threading.Barrier(4)

        def make_poller(result=None, error=None):
            def _poll():
                poll_barrier.wait(timeout=5)
                if error is not None:
                    raise error
                return list(result)

            return _poll

        gh_multi = MultiRepoGitHubClient("tok", (TORCHTITAN_REPO, VLLM_REPO))
        gh_multi.for_repo(TORCHTITAN_REPO).running_workflows = make_poller(
            result=[
                {
                    "id": 101,
                    "created_at": "2026-11-26T00:00:01Z",
                    "_ci_repo": TORCHTITAN_REPO,
                }
            ]
        )
        gh_multi.for_repo(VLLM_REPO).running_workflows = make_poller(
            error=RuntimeError("simulated github repo timeout")
        )
        gc_ok = Mock()
        gc_ok.running_workflows.side_effect = make_poller(
            result=[
                {
                    "id": "gc-1",
                    "_ci_provider": "gitcode",
                    "_ci_repo": "depeng1994/torchtitan-npu",
                }
            ]
        )
        gc_fail = Mock()
        gc_fail.running_workflows.side_effect = make_poller(
            error=RuntimeError("simulated gitcode repo failure")
        )

        multi = MultiPlatformClient(
            gh_multi,
            {
                "depeng1994/torchtitan-npu": gc_ok,
                "cann-framework/torchtitan-npu": gc_fail,
            },
        )
        with self.assertLogs("root", level="WARNING"):
            discovered = multi.running_workflows()
        self.assertEqual({r["id"] for r in discovered}, {101, "gc-1"})

        # Sub-case B: Router.tick() checks GitHub + GitCode active runs concurrently and
        # broker-backed Yellow TestWorker does not wait 60s between local cache polls.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_workspace(root)
            broker = BrokerClient(root / "broker.sock", worker_id="yellow-test-id998a1")
            status_barrier = threading.Barrier(2)

            def concurrent_status(run):
                status_barrier.wait(timeout=5)
                return {"status": "in_progress", "run_attempt": 1}

            with (
                patch.dict(
                    os.environ,
                    {
                        "LITE_ACTIONS_STATE_DIR": str(root / "state"),
                        "LITE_ACTIONS_RESOURCE_LOCK_ROOT": str(root / "leases"),
                    },
                    clear=False,
                ),
                patch.object(broker, "check_run_status", side_effect=concurrent_status),
                patch.object(broker, "running_workflows", return_value=[]) as poll_mock,
            ):
                router = Router(
                    root,
                    "yellow-test-id998a1",
                    broker,
                    "depeng1994",
                    broker_client=broker,
                )
                try:
                    router.state.admit(
                        [
                            (
                                "depeng1994/lite-actions:998:1:case_001_gh",
                                {
                                    "phase": "RUNNING",
                                    "progress_id": "ack-1",
                                    "run": {
                                        "id": 998,
                                        "run_attempt": 1,
                                        "head_sha": "a" * 40,
                                        "path": ".github/workflows/lite_actions_pr_smoke.yml",
                                        "name": "lite-ci-pr-smoke",
                                        "_ci_repo": SELF_REPO,
                                        "_ci_provider": "github",
                                    },
                                    "item": {
                                        "case_id": "case_001_gh",
                                        "order": 0,
                                        "test_id": "gh",
                                        "kind": "npu_smoke",
                                        "worker": "yellow-test-id998a1",
                                        "target_pool": "a3_shared_yellow",
                                        "docker": "torchtitan_ci_a3_r2",
                                        "resources": {
                                            "nodes": 1,
                                            "devices_per_node": 8,
                                        },
                                        "execution_timeout_seconds": 75,
                                    },
                                    "digest": "1" * 64,
                                    "defaults": {},
                                },
                            )
                        ]
                    )
                    router.state.admit(
                        [
                            (
                                "gitcode:depeng1994/torchtitan-npu:gc998:1:d1:case_002_gc",
                                {
                                    "phase": "RUNNING",
                                    "progress_id": "ack-2",
                                    "run": {
                                        "id": "gc998",
                                        "run_attempt": 1,
                                        "head_sha": "b" * 40,
                                        "path": ".gitcode/workflows/dispatcher_smoke.yml",
                                        "name": "lite-ci-dispatcher-smoke",
                                        "_ci_repo": "depeng1994/torchtitan-npu",
                                        "_ci_provider": "gitcode",
                                        "_ci_dispatch_id": "d1",
                                    },
                                    "item": {
                                        "case_id": "case_002_gc",
                                        "order": 0,
                                        "test_id": "gc",
                                        "kind": "npu_smoke",
                                        "worker": "yellow-test-id998a1",
                                        "target_pool": "a3_shared_yellow",
                                        "docker": "torchtitan_ci_a3_r2",
                                        "resources": {
                                            "nodes": 1,
                                            "devices_per_node": 8,
                                        },
                                        "execution_timeout_seconds": 75,
                                    },
                                    "digest": "2" * 64,
                                    "defaults": {},
                                },
                            )
                        ]
                    )
                    with patch(
                        "ci_core.router.time.monotonic",
                        side_effect=(100.0, 105.0, 110.0),
                    ):
                        router.tick()
                        router.tick()
                        router.tick()
                    self.assertEqual(poll_mock.call_count, 3)
                finally:
                    router.close()


if __name__ == "__main__":
    unittest.main()
