# 验证记录

独立 Pull worker、按 case 排队、完整设备/主机容量/端口租约、已有容器执行、进程清理证明与持久结果上报已实现。代码清理后的硬件复验正在进行，PR 保持 draft：

- [调度仓 #10](https://github.com/depeng1994/lite-actions/pull/10)
- [模型仓 #11](https://github.com/torchtitan-npu/torchtitan-npu/pull/11)

## 真实执行证据

以下运行使用模型提交 `dc10f0e6d5106811a4fb5e8adefaa1c655137902`，已在清理代码前执行，不能替代清理后的复验。

| 场景 | 运行与结果 |
| --- | --- |
| blue/yellow 系统闭环 | [41055641549](https://github.com/torchtitan-npu/torchtitan-npu/actions/runs/41055641549)，两 worker 均成功；日志 13/18 行，Comment 872/1172 字节，外层请求 988/1306 字节 |
| 单节点 8P | [41058552176](https://github.com/torchtitan-npu/torchtitan-npu/actions/runs/41058552176)，all-reduce sum=28，world_size=8，PASS，释放已确认 |
| 双节点 16P | [41059095421](https://github.com/torchtitan-npu/torchtitan-npu/actions/runs/41059095421)，all-reduce sum=120，world_size=16，PASS，释放已确认 |
| 外部占卡及预启动取消 | [8P](https://github.com/torchtitan-npu/torchtitan-npu/actions/runs/41056010151)、[16P](https://github.com/torchtitan-npu/torchtitan-npu/actions/runs/41056178876)，等待后取消，cleanup_verified=true，无新增隔离；不证明运行中 ranks 的取消 |
| 清理后完整 Smoke | [41062066103](https://github.com/torchtitan-npu/torchtitan-npu/actions/runs/41062066103)，系统探测、8P、16P 的 worker 结果均 PASS，释放已确认 |
| 调度仓 CI | [41062412495](https://github.com/depeng1994/lite-actions/actions/runs/41062412495)，Ruff、Device ID 单行检查、生命周期测试通过 |
| DSV4 首轮 | [41062062884](https://github.com/torchtitan-npu/torchtitan-npu/actions/runs/41062062884)，Muon/AdamW 均 FAIL，释放已确认。容器继承的 Python 路径指向旧 TorchTitan override，缺少 `torchtitan.components.checkpointer`；不是训练成功 |

首轮 DSV4 还暴露了失败摘录被 torchrun 收尾信息覆盖的问题，已改为优先保留最早的去重异常及上下文。成功训练必须提供 LOSS，不能用纯通信 smoke 的 PASS 代替。

## 部署条件

- blue 位于 `ssh ycy`，唯一管理 `113.46.17.205:153`、`113.46.17.205:154`；yellow 位于 `ssh hwy`，只验收系统探测，两个训练池仍禁用。
- 两节点均进入已有容器 `cann_master_torchtitan_master_xyj`。容器 ID、镜像 ID 与实际镜像引用逐节点固定，运行前持续核验；不创建或重命名他人的容器。
- 两节点 TorchTitan 均为 0.3.0，torch_npu/NumPy 版本不同，不能仅凭同名环境声称配套相同。真实 16P 通信已验证。
- `:154` 尚无已配置的 DSV4 模型/checkpoint 路径，部署时只启用 `npu_smoke`；DSV4 在资产齐全的 `:153` 排队执行。
- `:153` 的设备 `0–7` 仍保留原有隔离。不得仅凭设备空闲清除隔离；恢复必须有对应任务的进程退出证据。
- 等待其他任务结束后才继续硬件验收。不终止或抢占外部进程。

## 尚需完成

清理后的系统与 8P/16P 复验、两条 DSV4 五步训练、真实并发和运行中取消；通过后再切换生产入口和 required checks。每 case 保持最多 20 行、每行最多 180 字符，以及 Comment/请求 6100/7000 字节预算；Summary 仅元数据与状态。

模型仓相关 CPU/tooling 检查此前通过。完整 Linux pre-commit 的 Pyrefly 仍有上游基线相同的 192 个错误，未宣称完整 Lint 通过。本变更不修改模型计算逻辑；五步训练只证明完成性，不证明新的数值精度对齐。
