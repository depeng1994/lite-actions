"""Best-effort terminal broadcast to CI *execution* machines only.

Prefer execution hosts for human sessions; also cover their CI containers.
No dispatcher-local terminal notification.
"""

from __future__ import annotations

import datetime as dt
import logging
import subprocess
import time
from ipaddress import ip_address

from ci_core.contract import REPO

BEIJING = dt.timezone(dt.timedelta(hours=8))
BORDER = "*" * 80


def device_range(devices: list[int]) -> str:
    """Compact a trusted physical DeviceSet without hiding gaps in its IDs."""
    if (
        not devices
        or any(type(device) is not int or device < 0 for device in devices)
        or len(set(devices)) != len(devices)
    ):
        raise ValueError("invalid broadcast device allocation")
    result = []
    ordered = sorted(devices)
    start = last = ordered[0]
    for number in ordered[1:]:
        if number == last + 1:
            last = number
            continue
        result.append(f"{start}-{last}" if start != last else str(start))
        start = last = number
    result.append(f"{start}-{last}" if start != last else str(start))
    return ",".join(result)


def action_url(run: dict) -> str:
    ident = int(run["id"])
    if ident <= 0:
        raise ValueError("invalid GitHub run id")
    # Derived from trusted numeric run_id, never accepts arbitrary remote URL.
    return f"https://github.com/{REPO}/actions/runs/{ident}"


def notification(
    run: dict,
    phase: str,
    *,
    node_rank: int,
    node_ip: str,
    devices: list[int],
    nnodes: int,
    ngpus_per_node: int,
    released: bool | None = None,
    now: dt.datetime | None = None,
) -> str:
    """Render one wall notice for the current job's actual local DeviceSet.

    `end` is the only exit event. Never state "released" without a successful
    remote preflight; a failed check is still a task-end notification.
    """
    if phase not in ("begin", "end"):
        raise ValueError("unsupported broadcast phase")
    if phase == "end" and type(released) is not bool:
        raise ValueError("end broadcast requires verified release status")
    if type(nnodes) is not int or nnodes < 1 or not 0 <= node_rank < nnodes:
        raise ValueError("invalid task node rank")
    if type(ngpus_per_node) is not int or len(devices) != ngpus_per_node:
        raise ValueError("broadcast resources must match local allocation")
    if not node_ip:
        raise ValueError("missing trusted HCCL node IP")
    ip_address(node_ip)  # Never display SSH / relay or an untrusted hostname.

    at = (now or dt.datetime.now(BEIJING)).astimezone(BEIJING)
    timestamp = at.strftime("%Y-%m-%d %H:%M:%S")
    if phase == "begin":
        title = "资源占用 🔒"
        section = "本机资源占用"
        advice = "以上 Device 正在进行 CI 验证，请勿占用。"
    elif released:
        title = "资源释放 ✅"
        section = "本机资源释放"
        advice = "以上 Device 已释放。"
    else:
        title = "任务结束 ⚠️"
        section = "本机资源状态"
        advice = "CI 验证已结束，但 Device 释放尚未确认，请勿占用。"

    return (
        f"************** TorchTitan CI · {title} {timestamp} **************\n"
        f"\n"
        f"【任务ID】：{int(run['id'])}\n"
        f"{action_url(run)}\n"
        f"\n"
        f"【任务总体资源】：nodes={nnodes}，ngpus_per_node={ngpus_per_node}\n"
        f"\n"
        f"【{section}】：\n"
        f"节点：Node {node_rank}\n"
        f"小网IP : {node_ip}\n"
        f"Device : {device_range(devices)}（共{len(devices)}P）\n"
        f"\n"
        f"{advice}\n"
        f"{BORDER}\n"
    )


def broadcast_job(
    case: dict,
    pools: dict,
    run: dict,
    phase: str,
    *,
    released: bool | None = None,
    executor=subprocess.run,
) -> list[str]:
    """Broadcast once per assigned host, to host user sessions and CI container.

    The execution host endpoint is mandatory. Container broadcast is best-effort
    because container /dev/pts typically has no interactive login sessions.
    """
    requests = case.get("allocation", [])
    if not requests:
        return []
    nodes = {item["host_id"]: item for item in pools[case["pool"]]["nodes"]}
    counts = {len(allocated["devices"]) for allocated in requests}
    if len(counts) != 1:
        raise ValueError("broadcast requires consistent devices per node")
    ngpus_per_node = counts.pop()
    # Validate all node-specific notices *before* sending any wall message.
    deliveries = []
    for node_rank, allocated in enumerate(requests):
        node = nodes[allocated["host_id"]]
        destinations = list(dict.fromkeys(node.get("broadcast_ssh", [])))
        if not destinations:
            raise ValueError(
                f"no host broadcast SSH configured for {allocated['host_id']}"
            )
        msg = notification(
            run,
            phase,
            node_rank=node_rank,
            node_ip=node.get("node_ip"),
            devices=allocated["devices"],
            nnodes=len(requests),
            ngpus_per_node=ngpus_per_node,
            released=released,
        )
        deliveries.append((destinations, msg))
    failures = []
    for destinations, msg in deliveries:
        for n, target in enumerate(destinations):
            # Host wall on A3 may intermittently reject SSH at key exchange.
            # Retry ONLY pre-start host notice; exit notice is advisory and
            # should not change a verified training/test result.
            retries = 3 if n == 0 and phase == "begin" else 1
            delivered = False
            for attempt in range(retries):
                try:
                    finished = executor(
                        [
                            "ssh",
                            "-o",
                            "BatchMode=yes",
                            "-o",
                            "ConnectTimeout=10",
                            "-o",
                            "ServerAliveInterval=10",
                            target,
                            "wall -n",
                        ],
                        input=msg,
                        text=True,
                        capture_output=True,
                        timeout=22,
                        check=False,
                    )
                    if finished.returncode:
                        raise RuntimeError(f"wall exited {finished.returncode}")
                    delivered = True
                    logging.info(
                        "CI %s wall delivered run=%s target=%s",
                        phase,
                        run["id"],
                        target,
                    )
                    break
                except (subprocess.SubprocessError, OSError, RuntimeError) as exc:
                    logging.warning(
                        "CI %s wall attempt=%s/%s failed run=%s target=%s: %s",
                        phase,
                        attempt + 1,
                        retries,
                        run["id"],
                        target,
                        type(exc).__name__,
                    )
                    if attempt + 1 < retries:
                        time.sleep(2)
            if not delivered and n == 0:
                failures.append(target)
    return failures
