# Lite Actions

**一句话**：用 GitHub Actions 当远程按钮，内网调度机主动领取任务，锁定指定 NPU、SSH 执行训练，再把 PASS/FAIL 和少量日志回传 GitHub。**无需公网 IP、入站端口或 Self-hosted Runner。**

**解决的痛点**：
* **A5等黄区环境无法使用蓝区agent调试**
* **CI看护用例与对外examples脚本不同仓也不同源，容易导致Daily看护与实际交付脚本的漂移**
* **多人使用环境易冲突，使用actions池化管理验证环境后，支持多人提交任务排队执行**

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

### 1.3 职责边界：测试用例与调度配置分别维护

正确的职责边界应该是：

| 位置 | 负责什么 |
| --- | --- |
| `torchtitan-npu/tests/integration_tests/` | 测试用例、模型配置、执行参数、结果校验 |
| `torchtitan-npu/.github/workflows/` | GitHub Actions 的触发入口 |
| `lite-actions/config/pools.json` | 执行机器、SSH 地址及 NPU 资源 |
| `lite-actions/config/pipelines.json` | **调度通道与资源授权映射**，不是模型测试用例注册表 |
| `lite-actions/ci_core/` | 资源互斥、执行调度、日志摘取和 GitHub 结果回传 |

**补充说明**：`torchtitan-npu/examples/` 维护模型训练脚本；`lite-actions/github/` 是 Workflow/Waiter 的维护副本，需同步到模型仓 `.github/` 下才能在 GitHub Actions 生效；`lite-actions/unified_agent.py` 负责轮询、验证并接收授权任务。

**最重要的判断规则：**

- **仅新增 Integration Test 用例**（复用已有机器和受信任执行入口）：只改 `torchtitan-npu` 的测试定义、suite/入口和校验；**不改 lite-actions 的 `pipelines.json`**。
- **仅增加可调度机器**：修改 `pools.json`；若已有通道需要使用新机器，再修改 `pipelines.json` 里的 pool/allocation。
- **新增独立 GitHub Action**：增加 Workflow，配置调度通道与 Waiter 的固定标识；这属于**通道授权**，不等于每个模型用例都在调度机注册。
- **引入新的硬件/跨节点拓扑**：必须先适配安全执行器、占用检查、环境及跨节点启动，不能只靠 JSON 自动支持。

`MODULE`、`CONFIG`、`STEPS`、`train_args`、CANN 环境及模型文件路径与训练校验由模型仓控制；调度层不维护这些内容的第二份。模型仓单机 `GPUPool` **不能自动代表多机 8×8**。

## 2. 当前已验证能力与运行状态

| Actions 展示名称 | 通道 ID | 作用 | 状态 |
| --- | --- | --- | --- |
| `Dispatcher-Smoke` | `a3-smoke` | 调度机本地 `npu-smi` 连通性 PoC | 可用；不是 2P 训练 |
| `A3-8p-CI-Example` | `a3-8p` | A3-4：DeepSeek-V4 Flash，Eager 5 steps | **统一 SSH Runner 实机已通过** |
| `A3-16p-CI-Example` | `a3-16p` | A3-3 + A3-4：EP16，Eager 5 steps | **统一 SSH Runner + nightly 用例已实机通过 5 steps** |
| `A5-64p-CI`（尚未部署 Workflow） | `a5-64p` | A5 8 台 × 每台 8P | **通用 SSH 执行器已配置；暂未启用，待实机验收** |

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

本节是**真实地址接入配置 + 安全上线流程**，不代表已完成 A5 训练。当前调度机对这 8 个 SSH 地址的探测连接超时，因此 `a5-64p` 暂为 `enabled: false`。它已经与 A3 共用正式 SSH 执行器，但不能在 SSH/环境未验收时触发训练。

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

**1）`config/pools.json`：登记 SSH 登录入口和 HCCL 小网地址，不登记模型资产。** 下例的 SSH IP 已知，`node_ip` 尚未提供，故先填 `null`；请用每台机器真实的**HCCL 小网 IP**替换，不能把 `7.246.125.*`（大网 SSH IP）直接复制过去。`device_ids` 与调度分配一律单行，便于核对。

```json
"a5-64p-lab": {
  "type": "ascend-a5",
  "process_indexing": "card",
  "nodes": [
    {"host_id": "a5-01", "ssh": "root@7.246.125.19", "node_ip": null, "device_ids": [0,1,2,3,4,5,6,7], "broadcast_ssh": ["root@7.246.125.19"]},
    {"host_id": "a5-02", "ssh": "root@7.246.125.20", "node_ip": null, "device_ids": [0,1,2,3,4,5,6,7], "broadcast_ssh": ["root@7.246.125.20"]},
    {"host_id": "a5-03", "ssh": "root@7.246.125.21", "node_ip": null, "device_ids": [0,1,2,3,4,5,6,7], "broadcast_ssh": ["root@7.246.125.21"]},
    {"host_id": "a5-04", "ssh": "root@7.246.125.22", "node_ip": null, "device_ids": [0,1,2,3,4,5,6,7], "broadcast_ssh": ["root@7.246.125.22"]},
    {"host_id": "a5-05", "ssh": "root@7.246.125.23", "node_ip": null, "device_ids": [0,1,2,3,4,5,6,7], "broadcast_ssh": ["root@7.246.125.23"]},
    {"host_id": "a5-06", "ssh": "root@7.246.125.24", "node_ip": null, "device_ids": [0,1,2,3,4,5,6,7], "broadcast_ssh": ["root@7.246.125.24"]},
    {"host_id": "a5-07", "ssh": "root@7.246.125.25", "node_ip": null, "device_ids": [0,1,2,3,4,5,6,7], "broadcast_ssh": ["root@7.246.125.25"]},
    {"host_id": "a5-08", "ssh": "root@7.246.125.26", "node_ip": null, "device_ids": [0,1,2,3,4,5,6,7], "broadcast_ssh": ["root@7.246.125.26"]}
  ]
}
```

说明：此处是完整 `a5-64p-lab` 配置项，写到文件顶层 `pools` JSON 内，不要把上述代码当成整个 `pools.json` 覆盖。实际的 `process_indexing` / `device_ids` 还需以 A5 `npu-smi` 进程表核验；`node_ip=null` 时 64P 必须保持禁用。

**2）`config/pipelines.json`：仅登记通道授权和 8×8 资源，不绑定任何测试 Module。**

```json
"a5-64p": {
  "workflow": "a5-64p-lite-actions.yml",
  "pool": "a5-64p-lab",
  "allocation": [
    {"host_id": "a5-01", "devices": [0,1,2,3,4,5,6,7]},
    {"host_id": "a5-02", "devices": [0,1,2,3,4,5,6,7]},
    {"host_id": "a5-03", "devices": [0,1,2,3,4,5,6,7]},
    {"host_id": "a5-04", "devices": [0,1,2,3,4,5,6,7]},
    {"host_id": "a5-05", "devices": [0,1,2,3,4,5,6,7]},
    {"host_id": "a5-06", "devices": [0,1,2,3,4,5,6,7]},
    {"host_id": "a5-07", "devices": [0,1,2,3,4,5,6,7]},
    {"host_id": "a5-08", "devices": [0,1,2,3,4,5,6,7]}
  ],
  "runner": {"kind": "ssh"},
  "timeout_seconds": 14400,
  "pipeline_id": "a5-64p",
  "enabled": false
}
```

此处的 `pipeline_id` 仅供 GitHub Waiter 识别**CI 通道**，不是具体测试用例名称。`runner` 只有通用的 `kind` 字段，**不应新增 CANN、HF、Checkpoint 或 case 专属环境字段**。启用 A5 前须补齐 8 台 HCCL IP、在**模型仓用例的 `env_vars`** 中填好真实模型资产路径，并验证 SSH/NPU 进程映射；最后才设置 `enabled=true`。

### Step 3 · 在模型仓定义 Nightly 用例（不建立 Registry JSON）

路径：`tests/integration_tests/nightly_all_models_test/`。按照 TorchTitan `OverrideDefinitions` 定义 `build_test_list()`，在定义中指定 `test_name`、`ngpu`、`nnodes`、`override_args` 和 `expected_steps`。多 case 可以放在同一个文件。旧的 `ci_registry.json` 已删除。Lite Actions 通过已授权 SHA 中的 `build_test_list()` 发现用例；**不能**从 GitHub 输入任意模块路径或命令。

**模型及运行时资产归用例所有。** 在 `OverrideDefinitions.env_vars` 中维护：`ASCEND_SET_ENV_PATH`（CANN 的 `set_env.sh`）、`HF_ASSETS_PATH`（tokenizer/model assets）、`CKPT_INIT_LOAD_PATH`（初始权重目录），以及需要的 `CONFIG`、`OPTIMIZER_OVERRIDES` 和模型专用端口等。调度机从请求绑定的 **Commit SHA** 实际 `build_test_list()` 解析这些值、校验绝对路径及保留变量、经 SSH 在执行机先 source CANN 再 export 对应用例环境。只有设备分配和机器拓扑（如 `node_ip` 的 HCCL 小网地址）属于 Lite Actions `pools.json/pipelines.json`。动态生成的 `CKPT_SAVE_LOAD_PATH` 由通用 SSH Runner 设成当次 case 输出目录，不应写死在模型用例里。

示例（在模型仓 `a3_8p_tests.py` 中；A5 应填写自己的真实环境，当前未知而保持禁用）：

```python
ASSET_ENV = {
    "ASCEND_SET_ENV_PATH": "/mnt/share/Ascend/20260805101249091/ascend-toolkit/latest/set_env.sh",
    "HF_ASSETS_PATH": "/mnt/share/models/DeepSeek-V4-Flash-bf16",
    "CKPT_INIT_LOAD_PATH": "/mnt/share/dsv4_ckpt_8rank",
}
# OverrideDefinitions(..., env_vars=ASSET_ENV, ...)
```

后续更换模型、CANN 版本、训练脚本或者 tokenizer 路径，**只改模型仓的测试定义**，无需修改 Lite Actions 代码与部署 JSON。A5 仍须核实网络连通性与上述环境后方可启用。

### Step 4 · Workflow 只选择真实 test ID 或 suite

GitHub `workflow_dispatch.inputs.test_cases` 为有界 JSON 数组，只允许下列两种选择，不再接受 `params/STEPS`、Python module 路径或任意 CLI/Shell 命令：

```json
[{"test_id":"dsv4_flash_a3_8p_adamw"}]
```

```json
[{"suite":"a3_8p_tests"}]
```

第二种会展开成 **Muon 和 AdamW 两个语义不同的 8P Eager case**（各 5 steps），而不是用 5/3 steps 制造重复测试。具体优化器、`override_args`、TensorBoard 预期均由模型仓对应的 `OverrideDefinitions` 决定。AdamW 在共享 Shell 既有 `OPTIMIZER_OVERRIDES=""` 开关上关闭 Muon swap，保留完整 NPU 算子 imports；Muon 保留默认 swap。16P 的非默认 Flash CONFIG、AdamW swap 关闭、CANN/HF/Checkpoint 资产路径及训练端口同样由模型 case 的 `env_vars` 提供；只有设备号、SSH 入口和 HCCL 小网拓扑属于调度机。

```bash
gh workflow run a3-8p-lite-actions.yml --ref refactor/unified-a3-a5-ci \
  -f 'test_cases=[{"suite":"a3_8p_tests"}]'
```

链路为 GitHub Inputs → `.github/scripts/lite_actions/prepare_request.py` → 身份绑定的 Artifact (`run_id/attempt/sha`) → 调度机从已授权 SHA 的 `build_test_list()` 解析 ID/suite → 校验拓扑与**展开后 case 数量**（A3 ≤ 2；A5 ≤ 1）→ 通用 SSH Runner 串行训练、TensorBoard 校验、NPU 空闲检查 → 逐项 `PASS/FAIL/NOT_RUN` Commit Comment → GitHub Waiter 汇总。调度端真实超时以 `config/pipelines.json` 为准，而不是 testcase 中对多机不生效的另一份 `timeout`。继续保持 300 分钟 GitHub Job / 16800 秒 Waiter 预算。

**验收边界：** 既有 Run 38013603734 的两个 8P case 是原来的 5/3 steps 组合，并未验证新 Muon/AdamW 组合；本轮 CPU 参数合约测试不能替代真实训练。A5 继续禁用。新版 Workflow 文件仍须通过后续默认分支合入注册才能按正式文件名直接触发。

### Step 5 · Lite Actions 专属 Workflow/脚本边界

模型仓 `.github/` 是唯一可部署源码；本仓 `github/` 仅是只读镜像。运行 `python3 scripts/sync_ci_mirror.py --model-repo ../torchtitan-npu-ci-refactor [--check]` 完成镜像同步及字节级一致性检查。Workflow 文件名分别是 `a3-8p-lite-actions.yml`、`a3-16p-lite-actions.yml`、`a5-64p-lite-actions.yml`、`lite-actions-dispatcher-smoke.yml`，显示名仍为原来 A3 8P/16P 的用户可读名称；后者仅本地检查 `npu-smi`，不是 2P 训练。

### Step 6 · 分阶段验证并正式启用

按照 **代码单测/配置校验 → 8 台 SSH 连通性 → 只读 NPU/广播检查 → 单机短跑 → 8 机 1 step → 8 机 5 steps + TensorBoard → GitHub Actions E2E** 逐关放行。验收时同时检查 Commit SHA 一致性、所有 64 rank、资源锁互斥、外部占用防护、超时清理、广播、结果回传和 GitHub Job Success/Failure。

如果 SSH、A5 占用解析、跨节点启动或模型测试任一环节没有通过，就维持 `a5-64p` 为**禁用**，不得为了出现在 Actions 页面上提前启用。

## 5. 日常新增测试用例

只需将新的 `OverrideDefinitions` 放进模型仓相应的 `build_test_list()`，并维护其 `env_vars` 中的 CANN/HF/Checkpoint 路径；不用修改 Lite Actions `pipelines.json` 测试注册或模型环境配置。只有新增物理资源通道、SSH/HCCL 网络拓扑或者资源策略需要修改 Lite Actions。手工使用 `python3 -m tests.integration_tests.tools.lite_actions.entrypoint inspect --suite a3_8p_tests` 查看一组测试定义。真实 NPU 训练统一通过 GitHub/Lite Actions 调度；本地 CPU 单测只能验证协议和 argv，不等于训练验收。

## 6. 验收参考、排障与限制

| 已有记录 | 结果 |
| --- | --- |
| [A3 8P Run 37868718990](https://github.com/depeng1994/torchtitan-npu/actions/runs/37868718990) | Eager 5 steps Success |
| [A3 16P Run 37868722207](https://github.com/depeng1994/torchtitan-npu/actions/runs/37868722207) | **旧版独立 Dispatcher**：双机 EP16，Eager，5 steps Success；不代表统一 SSH Runner 已通过 |
| [A3 8P Run 37911399789](https://github.com/depeng1994/torchtitan-npu/actions/runs/37911399789) | **统一 SSH Runner**：8P Eager 5 steps、TensorBoard steps、释放和 GitHub 回传均通过 |
| [A3 16P Run 37911495316](https://github.com/depeng1994/torchtitan-npu/actions/runs/37911495316) | **统一 SSH Runner 未通过**：A3-4 Rank 报 `duplicate registrations for aten.erfc.default`（初始环境参数尚未恢复时） |
| [A3 16P Run 37912372023](https://github.com/depeng1994/torchtitan-npu/actions/runs/37912372023) | **统一 SSH Runner 未通过**：HCCL `Communication_Error_Get_Socket(EI0006)`；另一节点通过作业标识保护终止 |
| [A3 16P Run 37913151439](https://github.com/depeng1994/torchtitan-npu/actions/runs/37913151439) | **统一 SSH Runner 未通过**：恢复 CANN→参数导出顺序后仍发生跨节点 HCCL Socket 超时；已停止重复训练重试 |
| [A3 8P Run 37939445270](https://github.com/depeng1994/torchtitan-npu/actions/runs/37939445270) | **最新 nightly 重构版实机 Success**：`a3_8p_tests.py`，Eager 5 steps、TensorBoard、结束广播、GitHub PASS |
| [A3 16P Run 37942444656](https://github.com/depeng1994/torchtitan-npu/actions/runs/37942444656) | **最新 nightly 重构版实机 Success**：`a3_16p_tests.py`，两节点退出码均为 0、Eager 5 steps、TensorBoard、结束广播、GitHub PASS |
| [A3 8P Run 37952500554](https://github.com/depeng1994/torchtitan-npu/actions/runs/37952500554) | **Inputs + Artifact API 新协议验收 Success**：Run 标题固定 `A3-8p-CI-Example`；Prepare/CI 两个 Job 均 Success；JSON 路径和 `STEPS=5` 由 Artifact 传递，5 steps、TensorBoard、退出码 0、广播和 GitHub PASS |
| [Run 37869377044](https://github.com/depeng1994/torchtitan-npu/actions/runs/37869377044) | 同一 Workflow 排队行为验证 |
| [Run 37876792907](https://github.com/depeng1994/torchtitan-npu/actions/runs/37876792907) | 开始/释放广播已验证 |
| A5 64P | 已接入统一 SSH 执行器与 8×8 资源配置；**SSH 连通性、训练、GitHub E2E 均未通过验证** |

**16P HCCL 历史故障与修复（2026-10-09）**：早期统一 Runner 的多次 16P 验证曾在 `HcclBroadcast` 阶段报 `EI0006`，基础 TCP 双向连通正常，并不等于 HCCL 设备级连接正常。对照旧版成功脚本后发现，新的 SSH Runner 未向模型仓 `scripts/run_train_multinodes.sh` 传递 `ASCEND_SET_ENV_PATH`，导致它没有按旧流程二次加载 CANN 环境。补回该环境变量后，[Run 37942444656](https://github.com/depeng1994/torchtitan-npu/actions/runs/37942444656) 双机通过 5 steps 和 TensorBoard 校验，GitHub Job Success。该结果说明**新 Runner 的 A3 16P 验收已经通过**；历史失败记录仍保留供追溯。A5 64P 未做实机验收。

常见阻塞：**Agent 不接单**，依次看是否运行、Workflow 文件路径/名称、actor、branch、`enabled`、唯一 agent lock；**Waiter 不结束**，核对 `run_id` / attempt / SHA、`pipeline_id`、结果 marker 和 GitHub POST 权限；**SSH 失败**，检查调度机路由、host key、密钥和非交互登录；**GPU/NPU 占用**，检查外部进程，禁止抢占别人作业；**重启后任务未恢复**，先查远端实际进程，再处理本地状态。

**任务目录命名（均使用北京时间 UTC+08:00，时间取 GitHub Actions `created_at`）**：

- **执行机**：`/mnt/share/ci_tests/YYYY-MM-DD_HH-MM-SS_run_<Workflow名称>_<run_number>/node0/`（多机还有 `node1/` 等）。例如 [A3 16P Run #6](https://github.com/depeng1994/torchtitan-npu/actions/runs/37913151439) 对应 `2026-10-09_17-43-56_run_A3-16p-CI-Example_6/node0/`；目录保留 Workflow YAML 中的完整 `name`（例如 `-Example`）；新 Workflow 的 run-name 固定，测试选择和参数只出现在 GitHub Actions Inputs / Artifact 中，不进入目录名称。
- **重新运行**：相同 Run 的第 2 次 attempt 为 `2026-10-09_17-43-56_run_A3-16p-CI-Example_6_attempt-2/`，避免覆盖首次执行的完整训练日志；这里的 `6` 是 GitHub Workflow 的 `run_number`，不是全局 Run ID `37913151439`，也不是 `run_attempt`。
- **调度机（不变）**：`logs/runs/YYYY-MM-DD_HH-MM-SS_run-ID_attempt-N/` 保持 Run ID，用于日志关联和去重；内部作业标识、进程取消校验及 GitHub 回传仍使用 Run ID。既有执行机历史目录不会自动重命名或删除。

`logs/system/` 为 Agent 运行日志，`state/` 保存持久去重状态和设备锁。清理历史 `logs/runs/` 须确认任务已彻底结束，不清理运行中的文件；不要将 Token、隐私信息或大块原始内网日志上传到公开 Commit Comment。

**已知限制**：新动态选择协议已经在模型仓重构分支完成 A3 8P/16P Actions E2E 验收，但仍未与模型仓主分支同步上线；A5 64P 尚未实机验证。部署方式目前无开机自动拉起；跨进程 NPU 锁只能协调本调度机管理的任务，不能阻止外部用户；取消/崩溃后的跨节点一致性清理和恢复仍需加强；Github-hosted Waiter 有运行时长约束，不能代替长稳测试的异步执行框架。A5 的设备映射与进程表解析尚未验证，不能假设与 A3 相同。

**维护准则**：测试事实在模型仓、资源与授权在 lite-actions；能通过补用例解决的，不复制调度器、日志筛选或 GitHub 回传代码。
