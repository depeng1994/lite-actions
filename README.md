# Lite Actions

**一句话**：用 GitHub Actions 当远程按钮，内网调度机主动领取任务，锁定指定 NPU、SSH 执行训练，再把 PASS/FAIL 和少量日志回传 GitHub。**无需公网 IP、入站端口或 Self-hosted Runner。**

## 1. 架构总览

### 1.1 代码同步架构

```mermaid
flowchart LR
    U["GitCode upstream/master<br/>源码 · tests · examples · CI 配置"]
    S["Sync Upstream<br/>同步代码和 Workflow"]
    M["GitHub master<br/>代码镜像 · GitHub Actions"]
    U --> S --> M
    classDef base fill:#eef2f7,stroke:#9aa8b8,color:#172033;
    classDef target fill:#e3f4ee,stroke:#4aaf90,color:#164e3b;
    class U,S base;
    class M target;
```

**目标**：将 GitHub Workflow 和 Waiter 直接合入 **GitCode 主仓**，经 `Sync Upstream` 原样同步到 GitHub，不另设配置分支或同步流程。**当前**：上游尚未合入这些文件，强制同步仍可能覆盖 GitHub 专属 Workflow。

### 1.2 CI 运行架构（已上线）

```mermaid
flowchart LR
    subgraph GH["云端 · GitHub"]
        direction TB
        W["① GitHub Actions<br/>手工触发 / 排队"]
        H["② Waiter<br/>等待 Run ID / SHA"]
        C["⑥ Commit Comment<br/>PASS / FAIL + 最多 20 行"]
        W -->|"启动"| H
        C -->|"获取结果"| H
    end

    subgraph DIS["内网调度机 · lite-actions"]
        direction TB
        A["③ Pull Agent<br/>轮询任务 / 去重"]
        D["④ Dispatcher Python<br/>锁卡 / SSH / 日志 / POST"]
        A --> D
    end

    subgraph EX["内网执行机 · A3 / A5"]
        X["⑤ 训练 + TensorBoard 校验<br/>完整日志仅留执行机"]
    end

    W -.->|"API 轮询发现"| A
    D -->|"SSH 启动"| X
    X -.->|"SSH 有界回读"| D
    D -.->|"GitHub REST POST"| C

    style GH fill:#f2f5fb,stroke:#899db7,stroke-width:2px
    style DIS fill:#edf5fb,stroke:#6d98ba,stroke-width:2px
    style EX fill:#e8f6ef,stroke:#4eaa82,stroke-width:2px
    classDef cloud fill:#ffffff,stroke:#899db7,color:#172033
    classDef dispatch fill:#ffffff,stroke:#6d98ba,color:#172033
    classDef execution fill:#ffffff,stroke:#4eaa82,color:#164e3b
    class W,H,C cloud
    class A,D dispatch
    class X execution
```

**按编号阅读**：① 在 GitHub 触发 Action → ② 云端 Waiter 开始等待 → ③ 内网 Agent 主动发现任务 → ④ Dispatcher 预留 NPU、检查占用并广播 → ⑤ 执行机运行训练并保留完整日志 → ④ 调度机 Python 从执行机通过 SSH 读取最多 128 KiB 原始日志、摘取最多 20 行、封装结果 → ⑥ **调度机 Python 直接调用 GitHub REST API** 写入 Commit Comment，由 Waiter 获取结果并结束 Job。**Pull Agent 不参与日志筛选、封装或回传；全过程不调用 AI Agent API。** ② 与 ③～⑤ 并行等待/执行，不是串行阻塞。

**数据路径**：执行机保留完整 `run.log`；调度机 Python 通过 SSH 仅读取最后 128 KiB 原始日志，再统一筛选性能指标或异常上下文（最多 20 行，每行最多 180 字符）。`ci_core/reporting.py` 在调度机生成结果 JSON 并调用 GitHub REST API，**执行机无 Token，Pull Agent 只负责发现与提交任务，无任何 AI Agent API 调用**。

同一个 Workflow 使用 `concurrency.queue: max`、`cancel-in-progress: false` 串行排队；不同 Workflow 通过 `(宿主机 ID, 物理 NPU ID)` 锁互斥，设备不重叠可并行。`wall -n` 只广播到**执行机宿主机和训练容器**，不广播调度机。

### 1.3 职责边界：新增机器、调度通道和测试用例是三件事

| 仓库 / 路径 | 归属与职责 | 什么情况下修改 |
| --- | --- | --- |
| [torchtitan-npu](https://github.com/depeng1994/torchtitan-npu) `tests/integration_tests/`、`examples/` | **测试用例的唯一来源**：模型、EP/DP/TP、优化器、step、断言、TensorBoard 验证 | 新增或修改测试用例 |
| torchtitan-npu `.github/workflows/`、`.github/scripts/a3-ci-wait-result.py` | Actions **触发入口**及云端等待/结果协议 | 新增独立 Workflow 通道 |
| [lite-actions](https://github.com/depeng1994/lite-actions) `config/pools.json` | 调度机眼中的**物理资源清单**（机器、SSH、chip ID、广播地址） | 新增/调整机器 |
| lite-actions `config/pipelines.json` | **已授权调度通道**：受信任 Workflow 与物理资源池/设备分配、超时、可信执行器的映射 | 新建通道、换资源、改执行器；**不是模型用例注册表** |
| lite-actions `unified_agent.py`、`ci_core/` | 拉取 GitHub 任务、授权过滤、NPU 锁、SSH 分发、日志摘取、GitHub REST 回传 | 调度或执行协议需要扩展 |
| lite-actions `github/` | Workflow/Waiter 的维护副本，须同步回模型仓才能在 GitHub 生效 | 改 GitHub 通道协议 |

**最重要的判断规则：**

- **仅新增 Integration Test 用例**（复用已有机器和受信任执行入口）：只改 `torchtitan-npu` 的测试定义、suite/入口和校验；**不改 lite-actions 的 `pipelines.json`**。
- **仅增加可调度机器**：修改 `pools.json`；若已有通道需要使用新机器，再修改 `pipelines.json` 里的 pool/allocation。
- **新增独立 GitHub Action**：增加 Workflow，配置调度通道与 Waiter 的固定标识；这属于**通道授权**，不等于每个模型用例都在调度机注册。
- **引入新的硬件/跨节点拓扑**：必须先适配安全执行器、占用检查、环境及跨节点启动，不能只靠 JSON 自动支持。

`MODULE`、`CONFIG`、`STEPS`、`train_args`、模型文件路径与训练校验由模型仓控制；调度层不维护这些内容的第二份。模型仓单机 `GPUPool` **不能自动代表多机 8×8**。

## 2. 当前已验证能力与运行状态

| Actions 展示名称 | 通道 ID | 作用 | 状态 |
| --- | --- | --- | --- |
| `A3-2p-CI` | `a3-smoke` | 调度机本地 `npu-smi` 连通性 PoC | 可用；不是 2P 训练 |
| `A3-8p-CI-Example` | `a3-8p` | A3-4：DeepSeek-V4 Flash，Eager 5 steps | 示例，已有成功记录 |
| `A3-16p-CI-Example` | `a3-16p` | A3-3 + A3-4：EP16，Eager 5 steps | 示例，已有成功记录 |
| `A5-64p-CI`（尚未部署 Workflow） | `a5-64p` | A5 8 台 × 每台 8P | **已登记真实 SSH 资源；禁用、Mock 执行器、未实机验证** |

`github/workflows/` 是维护副本，不等于 GitHub 上已部署。目标是先把 Workflow 和 Waiter 合入 GitCode 上游主仓，再通过 `Sync Upstream` 镜像到 GitHub；在此之前，镜像同步可能覆盖 GitHub 上专有的 Workflow。已有 PR [#26](https://github.com/depeng1994/torchtitan-npu/pull/26) 涉及 A3 Example 名称，但须以 GitHub 当前实际部署的文件为准。

**安全提示**：Workflow 展示名和公开仓 README 可被外部看到，尽量不用内网 IP 命名。下文 A5 地址为指定的接入示例；在公开仓推送真实地址前应确认符合内部信息发布要求。

## 3. 首次部署与日常运维（在内网调度机操作）

### 3.1 首次部署

**Step 1 · 准备调度机与代码**

调度机必须是**能够主动访问 `api.github.com` 且能 SSH 访问执行机的独立 Linux 环境**，不要求公网 IP、入站端口或 Self-hosted Runner。需要 Python 3、Git、OpenSSH Client、GitHub CLI（`gh`）。以下命令都在**调度机**上执行，首次部署才需要 clone：

```bash
git clone https://github.com/depeng1994/lite-actions.git
cd lite-actions
python3 --version
git status --short --branch
```

如果已有部署，则进入已有工作目录，例如 `/mnt/workspace/work/multi-agent-workflow/lite-actions`，**不要在运行中覆盖配置或随手删除 `state/`**。执行机须单独准备 CANN/torch-npu、模型训练资产、`npu-smi` 和需要的 `wall` 命令。

**Step 2 · 配好 GitHub 出站认证与 SSH**

```bash
gh auth login
gh auth status
# 对需要的执行机验证：先核对 SSH 主机公钥指纹，再接受 known_hosts
ssh -o BatchMode=yes a3-4-docker-relay hostname
```

调度机也可使用 `A3_CI_GITHUB_TOKEN` 替代 `gh auth token`；Token 必须能读取该仓库 Actions/Commit Comment 并**写入 Commit Comment**（一般需要对应的 Actions 读取与 Contents 写入权限），不能配置到共享执行机。不要把 Token 放在 README、命令输出或训练日志里。SSH 使用非交互认证；新机器的 host key 应通过可信渠道核对并预先写入 `known_hosts`，不要为方便而全局关闭校验。

**Step 3 · 校验配置并只启动一个调度 Agent**

检查 `config/pools.json` 中 SSH 与物理芯片对应关系，及 `config/pipelines.json` 中已授权 Workflow/资源分配。随后执行：

```bash
python3 -m unittest discover -s tests
python3 unified_agent.py --validate
pgrep -af 'python3 -u unified_agent.py' || true
# 确认没有旧 Agent 在运行后，才启动：
nohup ./run-unified-agent.sh >/dev/null 2>&1 < /dev/null &
pgrep -af 'python3 -u unified_agent.py'
tail -n 30 logs/system/unified-agent.log
```

`run-unified-agent.sh` 通过 `flock` 保证同一目录只有一个实例；默认每 60 秒轮询。只接收指定账户手工触发、受信任分支（默认 `master`）和 Workflow 的任务。需要测试其他可信分支时可显式设置 `A3_CI_ALLOWED_BRANCHES`，切勿开放所有分支。**当前没有 systemd 或容器重启后自动拉起机制**，环境重启后需重新执行 Step 3 的启动部分。

### 3.2 日常操作

**Step 1 · 检查 Agent 与可用性**

```bash
cd /mnt/workspace/work/multi-agent-workflow/lite-actions
pgrep -af 'python3 -u unified_agent.py'
tail -n 50 logs/system/unified-agent.log
python3 unified_agent.py --validate
```

如果没有 Agent 进程，先判断上次任务是否仍在执行机上运行，确认空闲后按首次部署 Step 3 重启；不要通过删除 `state/unified-agent.lock` 强制绕过排他锁。

**Step 2 · 从 GitHub 触发已启用的 CI 通道**

进入 [torchtitan-npu Actions](https://github.com/depeng1994/torchtitan-npu/actions) → 选择已部署的 Workflow → `Run workflow` → 选择已允许的分支并运行。Waiter 保持等待；调度机轮询到对应 Run ID/SHA 后锁定资源、广播、SSH 执行，并自行摘取日志和写入 Commit Comment。成功/失败最终以 **GitHub Job 状态**为准。

**Step 3 · 查看结果、排障和维护**

```bash
# 调度机日志（不含完整训练日志）
tail -f logs/system/unified-agent.log
# 该次任务调度和上传统计
ls -ltr logs/runs/ | tail
# 示例：查看某次任务目录中的 pipeline.log / upload-metrics.json
```

完整 `run.log`、TensorBoard、模型输出保留在**执行机**。调度机仅通过 SSH 有界读取日志、挑选最多 20 行，每行不超过 180 字符，直接调用 GitHub REST API 回传；**不调用 AI Agent API，执行机不持有 GitHub Token**。`state/` 包含任务去重及 NPU 锁；不可清空运行中的状态。需要升级/重启 Agent 时，应先确认无正在执行的任务，再停止唯一 Agent、拉取代码、通过 Step 3 测试并启动。意外中断后不应假定远端训练自动停止，须先人工排查残留进程及占用。

## 4. 实战：新增 A5 64P（8 台 × 8P）

本节是**真实地址接入配置 + 安全上线流程**，不代表已完成 A5 训练。当前调度机对这 8 个 SSH 地址的探测连接超时；因此配置中 `a5-64p` 保持 `enabled: false`、`runner.kind: mock`，不能直接触发实机 CI。

### Step 1 · 确认 8 台机器的 SSH / NPU / 网络基础条件

| host_id | 调度机使用的 SSH |
| --- | --- |
| `a5-01` | `root@7.246.125.19` |
| `a5-02` | `root@7.246.125.20` |
| `a5-03` | `root@7.246.125.21` |
| `a5-04` | `root@7.246.125.22` |
| `a5-05` | `root@7.246.125.23` |
| `a5-06` | `root@7.246.125.24` |
| `a5-07` | `root@7.246.125.25` |
| `a5-08` | `root@7.246.125.26` |

逐机核对 host key 指纹并预置信任后，运行 Lite Actions 提供的**只读**检查（不会启动训练、占用设备或终止进程）：

```bash
cd /mnt/workspace/work/multi-agent-workflow/lite-actions
python3 scripts/preflight_pool.py --pool a5-64p-lab
python3 scripts/preflight_pool.py --pool a5-64p-lab --npu
```

第二条只测试 `npu-smi info` 能否执行，**不等于设备空闲检查**。还需人工核对每机确有 8 个可用计算单元、A5 上的真实 device ID、宿主机/容器关系、驱动及 CANN/torch-npu、模型与数据路径、NPU 进程表格式、8 机 HCCL 网络互通及选定网卡。当前配置假设各机 device ID 为 `0..7`，须由实机证实。`wall -n` 广播指向 SSH 登录的宿主机；若在容器中训练，需另外登记容器 SSH 地址，不得把调度机填入广播目标。

### Step 2 · 在 Lite Actions 登记物理资源与调度通道

仓库内**已经新增** `config/pools.json` 的 `a5-64p-lab`：8 个 `host_id`，SSH 地址如上，每台 `device_ids: [0,1,2,3,4,5,6,7]`，合计 64P。现有 `a5-mock` 仅供 CPU 模拟，不应与真实资源池混用。

`config/pipelines.json` 的 `a5-64p` 也已把 `pool` 指向 `a5-64p-lab`，并分配 8×8。这个 JSON **不是测试用例表**：它仅声明“将来 `a5-64p-ci.yml` 这条受信任通道占用哪 64 个芯片、采用哪个可信执行器、超时多少”。保持：

```json
{
  "pool": "a5-64p-lab",
  "runner": { "kind": "mock" },
  "enabled": false
}
```

以上是**关键字段摘录**，不是替换整个文件的内容。`mock` 会拒绝真实运行；即使误将 `enabled` 改为 `true`，`--validate` 也会拒绝启动，避免尚未验证的 A5 流水线被意外使能。

```bash
python3 -m unittest discover -s tests
python3 unified_agent.py --validate
```

### Step 3 · 在 torchtitan-npu 编写真正的 A5 64P Integration Test

**测试逻辑只能放在模型仓**。建议为跨节点 A5 创建如下可执行入口（**下列 64P 文件是待实现的建议路径，不是当前已有可运行脚本**）：

- `examples/deepseek_v4/debug/deepseek_v4_pro_64p_cpt_4k_a5.sh`：64P 模型训练 recipe，独立确定 EP/DP/TP/优化器/数据资产。
- `tests/integration_tests/run_a5_64p_multinode_tests.py`：`launch` / `verify` 两个入口，分别启动训练并验证所有 steps、TensorBoard 标量和退出码。
- `tests/integration_tests/ci_distributed_cases.py` / 对应单测：定义和验证 8 节点 × 每节点 8P 的拓扑、期望步数等。该文件目前只在现有重构候选分支中提供 **specification-only** 示例，不是已部署的 runner。
- `tests/integration_tests/README.md`：记录用例名称、运行命令、依赖、校验标准与真实测试状态。

已有 A5 Pro `32p` Shell **不能不经修改就当作 64P 脚本**；候选参数 EP32、DP_SHARD32、DP_REPLICATE2 仅做静态合法性校验，必须以实际模型、显存、通信和正确的 64-rank 配置验证。保留 `NNODES=8`、每节点 `NGPU=8`，由通用多机启动器注入经过验证的 rank、master 地址/端口、网卡、设备可见性及集群地址，绝不能把 64P 当成单机 `--ngpu 64` 用 `GPUPool` 启动。

### Step 4 · 扩展调度执行器（A5 真正可运行前必须完成）

**当前代码尚不具备通用 A5 64P 实机执行能力。** `ci_core/dispatch.py` 只实现 A3 的进程表占用检查，现有 `legacy_16p` 固定两台 A3 SSH 目标。需要为 A5 完成：

1. 通用 N 节点 SSH 启动器：从 `pool` 和 `allocation` 获取节点，向 8 台机器分发**同一受信任 Commit SHA** 的代码，并行运行模型仓固定入口；校验每个节点退出码及 rank 编排。
2. A5 对应的只读占用检测：先确认 `npu-smi` 进程表映射；检测失败要 **fail closed**，不自动杀死其他用户的任务；使用已存在的按 `(host_id, physical chip ID)` 加锁逻辑。
3. 生命周期管理：超时/失败时清理**本 CI 自己启动**的跨节点进程，确认释放状态后广播。不能把 SSH 客户端退出直接解释成远端 rank 退出。
4. 日志与结果：原始日志只保存在执行机，调度机通过 `ci_core/logs.py` 读取有界原始日志，统一选择最多 20 行，再由 `ci_core/reporting.py` 通过 GitHub API 发布。执行机不能调用 GitHub，也不负责裁剪日志。
5. 在 `ci_core/config.py` 的可信执行器白名单和 `ci_core/dispatch.py` 中注册/实现新 runner，并补齐模拟失败、并发资源锁、异常/超时清理和结果协议测试。**不能执行来自 GitHub 输入的任意 Shell 命令。**

**只有**新执行器通过离线检查与实机阶段验证后，才能把 `runner.kind` 从 `mock` 改成新 runner，并将 `enabled` 设置为 `true`。此项不是单纯修改配置就能跳过的工作。

### Step 5 · 为新增的独立 64P 通道部署 GitHub Workflow

如果需要**独立的 `A5-64p-CI` 按钮**，才在模型仓增加 `.github/workflows/a5-64p-ci.yml`。可参考 lite-actions 的 `github/workflows/a3-16p-ci.yml`，分别设置独立 `name`、`workflow_dispatch`、`concurrency.queue: max`、受信任 actor/branch、超时，以及 Waiter 的固定 `--case a5-64p`。

**独立通道还需要保持三处协议一致**：`config/pipelines.json` 的 `workflow` / `pipeline_id`、`ci_core/reporting.py` 的名称白名单、`github/wait_result.py` 的 `PIPELINES`。当新的 A5 Workflow 与 Waiter 测试、代码审查完成后，将维护副本合入 `torchtitan-npu`（GitCode 上游主仓），再通过 `Sync Upstream` 同步到 GitHub；**只修改 lite-actions 副本，GitHub 不会自动出现新 Action**。正式启用前不要发布可触发但无人接单的 Workflow。

### Step 6 · 分阶段验证并正式启用

按照 **代码单测/CPU Mock → 8 台 SSH 连通性 → 只读 NPU/广播检查 → 单机短跑 → 8 机 1 step → 8 机 5 steps + TensorBoard → GitHub Actions E2E** 逐关放行。验收时同时检查 Commit SHA 一致性、所有 64 rank、资源锁互斥、外部占用防护、超时清理、广播、结果回传和 GitHub Job Success/Failure。

如果 SSH、A5 占用解析、跨节点启动或模型测试任一环节没有通过，就维持 `a5-64p` 为**禁用**，不得为了出现在 Actions 页面上提前启用。

## 5. 日常新增 torchtitan-npu Integration Test 用例（通常不用修改 Lite Actions）

**Step 1 · 在模型仓定义测试及判定标准**

对常规**单机**用例，在 `tests/integration_tests/` 对应模型文件增加 `OverrideDefinitions`，设置 `test_name`、`ngpu`、`train_script`、`env_vars`、`train_args`、`expected_steps`、`timeout` 及必要的 loss/TensorBoard 校验；Examples 保存可独立运行的合理默认值。对于**多机**用例，单机 `run_tests.py` 的资源池不能跨主机分配，必须使用明确的多节点 `launch` / `verify` 入口（参照既有 A3 16P 的思路，再泛化至 N 节点）。

**Step 2 · 把用例接入已有 suite / CI 入口并先做本地验证**

普通用例应由模型仓 `run_tests.py` 的 suite 构造函数（`_TEST_SUITES_FUNCTION`）包含，能通过 `--test_suite` / `--test_name` 选择；例如已准备完整**单机** NPU 环境时，可从模型仓根目录执行：

```bash
python3 -m tests.integration_tests.run_tests /tmp/my-integration-test \
  --test_suite deepseek_v4 --test_name dsv4_my_new_8p_test --ngpu 8
```

以上是**单机测试命令模板**，`dsv4_my_new_8p_test` 是占位示例，需要替换为已经注册到该 suite 的实际 `test_name`；并非 A5 64P 启动命令。对于多机/专用 E2E，则需接入该 CI 通道**固定、受信任**的模型仓 `launch` / `verify` 入口，而非把任意 shell 命令通过 GitHub 参数透传。在独立测试环境先做最小 step 数验证并明确失败判据。**此阶段不需要在 lite-actions 的 `pipelines.json` 加入测试名称。**

**Step 3 · 判断要不要改调度层**

- **复用已有 Workflow、资源与 runner**：只提交模型仓测试代码/入口；Lite Actions **零改动**。
- **同一 CI 通道换机器/卡数**：仅改 `pools.json` 和必要的 `pipelines.json` 资源映射，确保执行器支持对应拓扑。
- **新建独立按钮/新硬件执行器**：按第 4 节 Step 4～6 扩展可信 runner 与 Workflow/Waiter 通道协议。

每次按 `python3 -m unittest discover -s tests` 和 `python3 unified_agent.py --validate` 检查调度侧变更；模型仓新增用例还必须跑它自己对应的单元测试/Integration Test，不能用调度机静态测试代替真实模型验证。

## 6. 验收参考、排障与限制

| 已有记录 | 结果 |
| --- | --- |
| [A3 8P Run 37868718990](https://github.com/depeng1994/torchtitan-npu/actions/runs/37868718990) | Eager 5 steps Success |
| [A3 16P Run 37868722207](https://github.com/depeng1994/torchtitan-npu/actions/runs/37868722207) | 双机 EP16，Eager，5 steps Success |
| [Run 37869377044](https://github.com/depeng1994/torchtitan-npu/actions/runs/37869377044) | 同一 Workflow 排队行为验证 |
| [Run 37876792907](https://github.com/depeng1994/torchtitan-npu/actions/runs/37876792907) | 开始/释放广播已验证 |
| A5 64P | 目前只有 CPU 静态模拟与 8×8 资源登记；**SSH 连通性、训练、GitHub E2E 均未通过验证** |

常见阻塞：**Agent 不接单**，依次看是否运行、Workflow 文件路径/名称、actor、branch、`enabled`、唯一 agent lock；**Waiter 不结束**，核对 `run_id` / attempt / SHA、`pipeline_id`、结果 marker 和 GitHub POST 权限；**SSH 失败**，检查调度机路由、host key、密钥和非交互登录；**GPU/NPU 占用**，检查外部进程，禁止抢占别人作业；**重启后任务未恢复**，先查远端实际进程，再处理本地状态。

日志落地：`logs/runs/YYYY-MM-DD_HH-MM-SS_run-ID_attempt-N/`（北京时间）是调度端每次任务目录；`logs/system/` 为 Agent 运行日志，`state/` 保存持久去重状态和设备锁。清理历史 `logs/runs/` 须确认任务已彻底结束，不清理运行中的文件；不要将 Token、隐私信息或大块原始内网日志上传到公开 Commit Comment。

**已知限制**：部署方式目前无开机自动拉起；跨进程 NPU 锁只能协调本调度机管理的任务，不能阻止外部用户；取消/崩溃后的跨节点一致性清理和恢复仍需加强；Github-hosted Waiter 有运行时长约束，不能代替长稳测试的异步执行框架。A5 的设备映射与进程表解析尚未验证，不能假设与 A3 相同。

**维护准则**：测试事实在模型仓、资源与授权在 lite-actions；能通过补用例解决的，不复制调度器、日志筛选或 GitHub 回传代码。
