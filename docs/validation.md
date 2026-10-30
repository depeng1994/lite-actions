# 验证记录

独立 Pull worker、按 case 排队、完整设备/主机容量/端口租约、已有容器执行、进程清理证明与持久结果上报已实现。PR 保持 draft，尚未切换生产入口与 required checks：

- [调度仓 #10](https://github.com/depeng1994/lite-actions/pull/10)
- [模型仓 #11](https://github.com/torchtitan-npu/torchtitan-npu/pull/11)

## 清理后的真实执行证据

| 场景 | 运行与结果 |
| --- | --- |
| 完整 Smoke | [41062066103](https://github.com/torchtitan-npu/torchtitan-npu/actions/runs/41062066103)，blue/yellow 系统探测、8P、16P 均 PASS，释放已确认 |
| 实际并发 | [41064741967](https://github.com/torchtitan-npu/torchtitan-npu/actions/runs/41064741967)，完整 Smoke 成功；8P 使用 `:154` 设备 8–15，16P 使用 `:153` 设备 8–15 与 `:154` 设备 0–7，端口租约不同。按同一节点的远端任务起止记录，执行生命周期重叠 25.39 秒；不将其解释为逐个 kernel 的重叠测量 |
| 失败报错回传 | [41063913729](https://github.com/torchtitan-npu/torchtitan-npu/actions/runs/41063913729)，DSV4 真实失败，18 行摘录包含 `ModuleNotFoundError: No module named 'torchtitan.components.checkpointer'`，释放已确认；[41064744199](https://github.com/torchtitan-npu/torchtitan-npu/actions/runs/41064744199) 保留旧 SDK 的真实 `seqUsedQ should be nullptr now` 报错 |
| DSV4 五步训练 | [41068456622](https://github.com/torchtitan-npu/torchtitan-npu/actions/runs/41068456622)，模型提交 `37c1ed93a6a1478246b8071af0a310bb090acd2e`。Muon/AdamW 均完成五步并 PASS，Actions 实际各显示 5 行 rank 0 LOSS（12.27908 → 12.14706、12.23797 → 10.85831），释放已确认；两条 Comment/请求大小分别为 1515/1595、1509/1589 字节 |
| 运行中取消 | [41072934315](https://github.com/torchtitan-npu/torchtitan-npu/actions/runs/41072934315)，取消前 8 个 NPU PID 均匹配本任务标记；取消后任务所属进程数和所选 NPU 进程数均为 0，CANCELLED、cleanup_verified=true，无新增隔离。20 行为真实执行机摘录，不再出现缺失远端日志位置的调度器 traceback |
| 取消后的同组设备复用 | [41073332553](https://github.com/torchtitan-npu/torchtitan-npu/actions/runs/41073332553)，取消用例与后续 8P smoke 均使用 `:153` 设备 8–15，后续 PASS、all-reduce sum=28、cleanup_verified=true。验收时临时停止 `:154` 的新任务准入以固定复用目标；未清除 `:153` 原有设备隔离 |
| 调度仓 CI | [41072989745](https://github.com/depeng1994/lite-actions/actions/runs/41072989745)，Ruff、Device ID 单行检查和生命周期测试通过 |

Smoke 验证通信与调度，不证明训练通过。训练成功必须有 LOSS；失败摘录优先保留最早的去重异常及上下文，避免根因被 torchrun 收尾信息覆盖。每 case 最多 20 行、每行 180 字符，Comment/请求上限为 6100/7000 字节。Summary 仅元数据与状态。

早期的 [8P](https://github.com/torchtitan-npu/torchtitan-npu/actions/runs/41056010151) 与 [16P](https://github.com/torchtitan-npu/torchtitan-npu/actions/runs/41056178876) 已验证外部占卡等待及预启动取消，cleanup_verified=true，无新增隔离；它们不证明运行中 ranks 的取消。

## 部署与依赖

- blue 位于 `ssh ycy`，唯一管理 `113.46.17.205:153`、`113.46.17.205:154`；yellow 位于 `ssh hwy`，只验收系统探测，两个训练池仍禁用。
- 两节点均进入已有容器 `cann_master_torchtitan_master_xyj`。容器 ID、镜像 ID 与实际镜像引用逐节点固定，运行前核验。
- 容器的 torch_npu/NumPy 版本不同，同名环境不代表依赖相同；真实 16P 通信已验证。
- `:154` 尚无已配置的 DSV4 模型/checkpoint 路径，只启用 `npu_smoke`。DSV4 在资产齐全的 `:153` 排队执行。
- `:153` 的设备 0–7 保留原有隔离，不因空闲自动清除。等待其他任务结束后才验收，不终止或抢占外部进程。
- 首轮 DSV4 暴露继承的旧 TorchTitan override。现为 CI 单独提供模型仓要求的 TorchTitan 0.3.0 源码，固定到 `086bf6c166ec85c1298eb5596fa9bf95f6a2d840`；staged 模型源码优先，固定依赖目录随后，最后才是继承路径。未替换共享容器的 Python 包。
- 原共享 CANN 路径在反向算子失败。模型用例已改为所选容器标准入口 `/usr/local/Ascend/ascend-toolkit/set_env.sh`；不改 Muon/AdamW 训练参数、序列长度或模型计算逻辑。

## CPU 与格式检查

仅保留保护工作流端到端能力的场景，新增门槛和外部替身边界见 [测试范围](testing.md)。共 12 个测试：本地 macOS 11 个通过、1 个 Linux/root 场景跳过；blue 的 Linux/root 全部通过。生产配置不作为测试快照。

`python3 scripts/lint.py` 执行 Ruff lint/format，并独立检查 JSON 设备数组单行布局；Ruff 本身不负责 JSON 格式化。写入容器绑定也使用同一紧凑格式，不会重新展开 Device ID。

模型仓相关 CPU/tooling 检查 54 项通过，另有 24 个 subtests。完整 Linux pre-commit 的 Pyrefly 仍有与上游基线相同的 192 个错误，未宣称完整 Lint 通过。五步训练只证明完成性，不证明新的数值精度对齐。

## 尚需完成

硬件与日志验收已完成。候选 worker 在结果全部送达后停止并禁用准入，原生产服务恢复，原有隔离保留；未合并 PR，也未切换生产入口与 required checks。生产切换仍需评审这两个 draft PR。
