# RFC：Lite Actions 原生 Smoke 双平台接入收尾

状态：实现与试点验证已完成，等待 release PR #23 合入及少量收尾验收。
记录时间：2026-11-11。
实现基线：Lite Actions `e248bcd0261f6206425749a84615ad1846cbf05a`；合入后的 `main` 为 `197cf0ad727f64b3f45859a98b35908b3522cb15`，两者源码树一致。

## 动机与范围

模型仓需要一套可追溯的 OS、Smoke 8P、Smoke 16P、Smoke 8P 流程，同时支持 GitHub 主线和 release 分支、GitCode 原生入口。原实现包含单仓专用名称、PR 事件路由、旧 A5 适配和固定用例假设，导致入口与 Worker 强耦合，也无法在 main 与 release 用例不同时安全验证被测版本。

本次方案把入口识别统一为 `lite-ci-` 工作流名前缀；各接入仓的 Lite Actions 外部入口仅允许手动 Actions 触发。控制器先锁定 Commit 或 PR 的 Head、Base、预合并提交和源码树，再从被测提交读取 Smoke 定义生成计划。Worker 继续独立校验计划、身份、资源租约、拓扑、执行结果和清理。

控制器会在准备阶段导入被测提交的模型 Python。这个步骤持有仓库状态写入令牌，沿用了本次已确认的 Fork PR 信任决定，不提供控制面凭据隔离；这是有意的权限边界，后续若要恢复隔离，必须另立方案和验收。

## 已完成事项

### Lite Actions Worker

- [Lite Actions #18](https://github.com/depeng1994/lite-actions/pull/18) 已合入，合入提交 `197cf0ad727f64b3f45859a98b35908b3522cb15`。
- 运行代码相对目标分支净减少约 310 行；候选 CPU 测试 113 项通过、2 项跳过，工作流语法检查通过。
- Worker 已部署 `e248bcd`，Blue 服务正常运行；旧版本 `2d8798a` 和状态快照保留用于回滚。
- GitHub、GitCode 均按 `lite-ci-` 前缀发现运行；事件来源不再参与 Worker 路由。资源租约、同池队列、失败清理、身份匹配和缺失结果失败关闭仍保留。

### vLLM Ascend 主线

- [vLLM #22](https://github.com/cann-framework/vllm-ascend/pull/22) 已合入，合入提交 `a41c613e229188a552589203e5f0588951a4c108`。
- 默认分支工作流为 `lite-ci-dispatcher-smoke`，仅 `workflow_dispatch`；旧 `Dispatcher-Smoke` 已停用。
- 计划生成从被测源码读取用例；因此 main 和 release 可以定义不同 Smoke，而不需要把模型用例固定写进 Worker 或控制器。
- 个人仓真实验证覆盖：当前 Commit 手动运行、PR 预合并运行、release 差异用例、显式 Commit 输入、PR 与 Commit 冲突拒绝，以及 Head 变化取消。

### vLLM Ascend release

- [vLLM #23](https://github.com/cann-framework/vllm-ascend/pull/23) 当前 Head `584447d105675b99c7d0b50173f881407bd76829`，目标 Base `1480e16d8e46ea94c8d91bcd4dedcea8a06da070`，已退出草稿、等待 Review。
- #23 已补入与 main 完全一致的工作流和五个控制脚本，以及 release 自己的模型 Smoke 定义和两项接口测试。
- 组织仓当前 Head 的预合并提交为 `a357bd34e47b04daa06215f7f84ce4b9ffa1def6`，源码树为 `1430bfd24cf870438a6ce20a682dce0fde753a2b`。
- 组织仓临时标签加载当前 #23 Head 的[完整运行](https://github.com/cann-framework/vllm-ascend/actions/runs/42985886727)已通过 OS、8P、16P、8P、清理和汇总。标签用于合入前加载新文件，不能替代合入后的真实 `releases/v0.30.0` 分支运行。
- 直接以当前 030 分支触发在文件补入前返回 422；#23 合入后必须再从真实 `releases/v0.30.0` 选择工作流手动运行一次。

### GitCode TorchTitan

- [#1244](https://gitcode.com/cann/torchtitan-npu/pull/1244) 当前仍开放，Head `9185a17c86390fccef0f20d407adb45a0a5f8645`；三个入口只保留手动触发，dispatcher 另有内部复用入口。
- 使用 Lite Actions `e248bcd` 的个人仓完整 Smoke 已通过 [OS、8P、16P、8P](https://gitcode.com/depeng1994/torchtitan-npu/actions/runs/6c34e64979bb4a489d5abaf48b618428)。组织仓 #1244 合入仍是后续门槛。

## 必须保留的验收证据

| 验收项 | 结果 |
| --- | --- |
| 组织仓 main 控制器测试 #23 预合并版本 | [组织仓完整运行](https://github.com/cann-framework/vllm-ascend/actions/runs/42984301805)通过；对应旧 Head `bc0c7af`。 |
| 当前 #23 Head 的组织仓 release 工作流 | [当前 Head 标签试点](https://github.com/cann-framework/vllm-ascend/actions/runs/42985886727)通过，Head、Base、预合并提交和源码树均已记录。 |
| 当前 main 控制器个人仓全量 | [第 2 次运行](https://github.com/depeng1994/vllm-ascend/actions/runs/42977697784/attempts/2)通过；第 1 次因清理时外部占卡失败，失败证据保留。 |
| GitCode 完整 Smoke | [正式 Blue Worker 运行](https://gitcode.com/depeng1994/torchtitan-npu/actions/runs/6c34e64979bb4a489d5abaf48b618428)通过。 |
| 不同用例集合 | [release 单 OS 差异用例](https://github.com/depeng1994/vllm-ascend/actions/runs/42977326437)只生成并通过该 OS Job。 |
| Head 变化取消 | [取消验证](https://github.com/depeng1994/vllm-ascend/actions/runs/42981936831)正确回写 `CANCELLED` 并失败关闭；外部占卡导致清理未确认。 |
| 显式 Commit 和非法输入 | [显式 Commit](https://github.com/depeng1994/vllm-ascend/actions/runs/42980665327)通过；[PR 与 Commit 同填](https://github.com/depeng1994/vllm-ascend/actions/runs/42980665684)在准备阶段拒绝。 |

## 遗留问题与后续门槛

1. **#23 合入与真实 030 分支复核**：完成 Committer 审核后合入 #23；在真实 `releases/v0.30.0` 分支选择 `lite-ci-dispatcher-smoke`，确认四例和汇总成功。合入前不得用标签试点替代这一项。
2. **#1244 合入**：重新核对 Head、Base 和组织仓入口，合入后再做一次组织仓 GitCode 手动 Smoke。
3. **取消和版本变化**：已有 Head 变化取消证据，但一次清理受外部占卡影响未确认；运行中取消、Base 变化及取消后的完整释放仍需维护窗口实测。
4. **跨平台争卡**：CPU 测试覆盖共享锁和队列；尚未取得最终版本 GitHub 与 GitCode 同时争用 Blue 资源池的完整实机证据。当前 Blue 两节点使用 `wait`，不能杀外部训练任务。
5. **全局调度边界**：当前队列序号和资源锁在一个 Worker 上持久化；多个独立 Worker 之间没有新的中心化硬件租约服务，跨 Worker 全局 FIFO 仍是后续设计项。
6. **独立审批 PR #25**：评论审批和自动合并不是本 Smoke 特性的合入依赖；开关仍关闭，真实 `/approve` 全链路未上线。
7. **文档收敛**：`README.md` 和 `docs/blue-worker.md`仍包含较早的入口及部署说明，RFC 是本轮收尾记录；后续应将生产操作文档统一到实际 `e248bcd`、当前入口和真实 030 分支流程。

## 回滚与清理

- Blue 回滚：停止 `lite-actions-native-pilot.service`，将 `/opt/lite-actions/native-pilot` 恢复到 `native-2d8798a`，确认状态和配置快照后启动；不可并行启动两个 Blue Worker。
- 组织仓临时标签 `codex-lite-ci-030-probe-20261111` 仅用于合入前验证，完成后删除；运行记录仍保留在 GitHub。
- 本地和 YCY 临时工作树、试点 checkout、临时日志在确认没有活动进程和未回写结果后删除；生产 `native-e248bcd`、回滚 `native-2d8798a`、生产状态和必要证据不删除。
