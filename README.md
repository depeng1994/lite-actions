# Lite Actions

**Lite Actions** 是一套面向 NPU 集群的轻量级 CI 调度系统。它将 GitHub Actions 作为云端远程控制台，内网调度机通过 Pull 模式主动领取任务，锁定指定物理 NPU、通过 SSH 远程执行训练，并将执行结果及少量关键指标回传 GitHub。

> **核心特性**：**无需公网 IP、无需入站端口映射、无需在测试机部署 Self-hosted Runner**。

---

## 解决的核心痛点

* **A5 等黄区环境无法使用蓝区 Agent 调试**：受限网络区域外部无法直接接入，通过内网调度机主动拉取（Outbound Pull），免去开通公网入站端口或复杂网络打通的困难。
* **CI 看护用例与对外 Examples 脚本不同仓也不同源**：容易导致 Daily 看护与实际交付脚本脱节。Lite Actions 直接调度模型仓原生的训练用例与启动脚本，保证测试与交付完全同源。
* **多人共用集群易冲突**：使用 Actions 对物理验证环境进行池化调度，支持多人提交任务自动排队执行，杜绝抢卡踩踏。

---

## 1. 模式对比：Lite Actions vs 标准 GitHub Self-Hosted Runner

在专有算力集群（特别是黄区等隔离受限网络）中，Lite Actions 与传统的 GitHub Self-Hosted Runner（自建 Runner）在网络拓扑、资源互斥与安全边界上有着本质不同的设计考量：

| 对比维度 | 标准 GitHub Self-Hosted Runner | Lite Actions 调度模式 | 核心差异与选型考量 |
| :--- | :--- | :--- | :--- |
| **网络要求** | **每台执行机均需公网出站**连接 GitHub | **集中出站代理**：仅需 1 台调度机连接外网，执行机处于纯内网 | **免公网连接**：A5 等黄区/隔离区机器无需开通外网或复杂网关策略 |
| **机器侵入性** | 需在每台执行机上安装常驻 Runner 守护进程、.NET/Node 运行时 | **执行机零侵入**：执行机无需安装任何额外 Agent，仅需标准 SSH 与已有 Docker/CANN 环境 | **零系统污染**：不修改物理机系统服务，版本升级或故障排查仅需在调度机单点操作 |
| **硬件互斥与调度** | **无卡级感知**：仅按主机（Host）维度分配 Job，多任务同时派发时易发生 NPU 显存争抢与冲突 | **原生物理卡级原子锁**：支持按 Device ID（如 0~7 / 8~15）细粒度加锁与排队，全局单任务串行保护 | **杜绝踩卡冲突**：多人共用物理集群时自动排队，并支持对可信外部进程（如 auto-occupy/vllm）按 PID 定点回收 |
| **分布式多机支持** | 原生单 Job 绑定单机；跨机训练（如 16P/64P）需在用户脚本内自行写复杂的 SSH 跨机编排与通信检查 | **原生支持多机拓扑**：直接在通道配置声明 `nodes: 2, devices_per_node: 8`，调度机统一原子申请跨机卡锁并校验 HCCL 小网 | **开箱即用多机**：由调度机统一编排跨机分布式启动、网络检查与日志汇聚 |
| **凭据与执行安全** | 执行机持有 GitHub Token 与 Runner 注册凭据，外部 PR 存在恶意脚本执行与宿主机逃逸风险 | **执行机零凭据**：执行机完全无 Token；仅允许执行受信 Commit SHA 的白名单预置用例，拒绝任意 Shell 输入 | **安全受控防逃逸**：隔离区机器完全无 GitHub 写入权限，杜绝外部不可信代码注入 |
| **日志脱敏与私网保护** | 训练标准输出实时全量上传至 GitHub Actions 控制台，易暴露内网 IP/路径，超大日志易导致网页卡死 | **有界脱敏回传**：完整日志保留在内网执行机本地，仅摘取最后 20 行关键日志与 TensorBoard 标量回传 | **保护内网隐私**：避免敏感内网信息泄露，GitHub 界面轻量直观展示 PASS/FAIL 诊断 |
| **通用生态兼容性** | **完全自由**：支持完整 GitHub Actions 语法、Marketplace Action 插件与任意 Shell 步骤 | **受控收敛**：仅支持指定通道与标准测试用例（`test_id` / `suite`），不支持任意 Shell 自由组合 | **权衡说明**：专为大规模 AI 算力与分布式训练 Daily 看护定制，牺牲任意编排自由度换取极致的安全与硬件稳定性 |

---

## 2. 架构总览

### 2.1 代码同步架构

```mermaid
flowchart LR
    U["GitCode upstream/master<br/>源码 · tests · examples · CI 配置"]
    S["Sync Upstream<br/>定时镜像与同步"]
    M["GitHub master<br/>代码镜像 · GitHub Actions 控制台"]
    U --> S --> M
    classDef base fill:#eef2f7,stroke:#9aa8b8,color:#172033;
    classDef target fill:#e3f4ee,stroke:#4aaf90,color:#164e3b;
    class U,S base;
    class M target;
```

* **双主源规则**：模型仓 GitCode `master` 是上游开发主源，GitHub `master` 为镜像及 Actions 触发入口。
* 正式发布时，确保同一发布提交在两侧 `master` 保持一致，避免定时镜像覆盖分支改动。

---

### 2.2 CI 运行架构

```mermaid
flowchart LR
    subgraph GH["云端 · GitHub"]
        direction TB
        W["① GitHub Actions<br/>用户手动触发 / 定时触发"]
        H["② Waiter<br/>等待 Run ID / Commit SHA"]
        C["⑥ Commit Comment<br/>PASS / FAIL + 关键指标与日志"]
        W -->|"启动"| H
        C -->|"获取结果"| H
    end

    subgraph DIS["内网调度机 · lite-actions"]
        direction TB
        A["③ Pull Agent<br/>主动轮询任务 / 去重排队"]
        D["④ Dispatcher<br/>原子锁卡 / SSH 远端触发 / 收集结果"]
        A --> D
    end

    subgraph EX["内网执行机 · A3 / A5"]
        X["⑤ 真实训练 + 指标校验<br/>完整日志与 TensorBoard 留存本机"]
    end

    W -.->|"API 轮询发现任务"| A
    D -->|"SSH 启动训练"| X
    X -.->|"SSH 读取最后日志与指标"| D
    D -.->|"GitHub REST API 回传"| C

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

**完整交互时序**：
1. **触发任务**：开发者在 GitHub Actions 页面触发 Workflow。
2. **云端等待**：GitHub Waiter Job 启动并等待调度结果。
3. **主动发现**：内网调度机的 Agent 定期轮询 GitHub API，主动发现新任务。
4. **资源加锁**：调度机检查物理卡状态，通过**两级锁机制**（全局任务串行槽位 + 设备级原子锁）锁定目标 NPU。
5. **远程执行**：调度机通过 SSH 在目标执行机上运行训练容器与脚本，执行机保留完整日志。
6. **脱敏回传**：训练结束后，调度机通过 SSH 读取尾部关键日志（最多 20 行）和 TensorBoard 结果，调用 GitHub API 回写 Commit Comment，Waiter Job 获得终态并成功结束。

> **安全与性能保障**：
> - 执行机**无需配置 GitHub Token**，也不直接连外网；
> - 回传仅包含脱敏后的核心指标，保证敏感数据不泄露；
> - 全过程不调用任何第三方 AI Agent API。

---

### 2.3 职责边界划分

| 关注点 | 模型仓 (`torchtitan-npu`) | 调度仓 (`lite-actions`) |
| :--- | :--- | :--- |
| **测试用例与参数** | 测试逻辑、超参、步数、模型 Flavor | 仅透传执行，不解析参数细节 |
| **运行时环境** | CANN 路径、权重路径、HF 缓存路径 | 负责在执行机 source 与 export 对应环境变量 |
| **触发入口** | `.github/workflows/` 定义触发参数 | 负责拉取并校验任务合法性 |
| **硬件与机器** | 仅声明需要多少卡（如 8P / 16P） | 维护具体 IP、SSH 账号、网卡与物理 Device ID |
| **资源互斥** | 不关心 | 维护设备原子锁与排队机制，防止抢卡 |

---

## 3. 当前已验证通道与状态

| Actions 通道展示名称 | 通道 ID | 硬件规格与作用 | 状态 |
| :--- | :--- | :--- | :--- |
| **`Dispatcher-Smoke`** | `a3-smoke` | 调度机本地 `npu-smi` 连通性冒烟验证 | 可用 |
| **`A3-8p-CI-Example`** | `a3-8p` | A3 单机 8P 动态选卡：Muon + AdamW 5 steps | **正式可用（两个独立 Job 串行执行）** |
| **`A3-16p-CI-Example`** | `a3-16p` | A3 双机 16P（跨机 EP16）：Eager 5 steps | **正式可用** |
| **`A5-64p-CI`** | `a5-64p` | A5 八机 64P（每台 8P）集群训练 | 预留模板，实机验收前保持禁用 |

---

## 4. 首次部署指南（在内网调度机操作）

### 4.1 环境要求

调度机是一台**能够主动访问 `api.github.com`，且能通过 SSH 访问内网各执行机**的 Linux 主机。
- **软件依赖**：Python 3.10+、Git、OpenSSH Client、GitHub CLI (`gh`)。
- **权限要求**：调度机账号具有访问执行机的 SSH 免密权限；持有具有目标仓库 Actions 读取与 Commit Comment 写入权限的 GitHub Token。

### 4.2 步骤一：克隆代码与出站认证

在**调度机**上执行：

```bash
git clone https://github.com/depeng1994/lite-actions.git
cd lite-actions

# 配置 GitHub 出站认证（两者选一即可）
gh auth login
# 或配置环境变量：export A3_CI_GITHUB_TOKEN="ghp_xxx"

# 验证与执行机的 SSH 免密连通性
ssh -o BatchMode=yes <execution-host> hostname
```

### 4.3 步骤二：校验配置并启动调度 Agent

```bash
# 1. 运行自测单测与配置语法校验
python3 -m unittest discover -s tests
python3 unified_agent.py --validate

# 2. 确认当前没有残留旧 Agent 进程
pgrep -af 'python3 -u unified_agent.py' || true

# 3. 后台启动唯一的调度守护进程
nohup ./run-unified-agent.sh >/dev/null 2>&1 < /dev/null &

# 4. 确认 Agent 成功运行
pgrep -af 'python3 -u unified_agent.py'
tail -n 30 logs/system/unified-agent.log
```

> **启动说明**：`run-unified-agent.sh` 通过 `flock` 文件锁保证全局单例运行，默认每 60 秒轮询一次 GitHub Actions 队列。

---

## 5. 日常运维与应急排障

### 5.1 快速健康巡检

```bash
# 检查 Agent 是否存活
pgrep -af 'python3 -u unified_agent.py'

# 查看调度机实时日志
tail -f logs/system/unified-agent.log

# 查看最近任务的运行记录与上报指标
ls -ltr logs/runs/ | tail -n 5
```

### 5.2 任务取消与残留清理（GitHub Cancel 应对）

当用户在 GitHub 页面点击 `Cancel workflow` 时：
1. 调度机 Agent 会捕获取消事件，并向该任务分配的执行机发送终止指令。
2. **精准清理机制**：根据本次任务唯一的 `LITE_CI_JOB_ID` 环境变量，调度机通过 `/proc/<pid>/environ` 精确识别本次任务启动的 `torchrun` 及其全部 Rank 进程并终止，**绝不通过 `pkill` 或进程名误杀其他用户的正常训练**。
3. 随后调度机会通过 `npu-smi` 复查设备释放状态，确认 NPU 空闲后才会释放设备锁。

### 5.3 异常隔离（Quarantine）与手动恢复

若在任务执行过程中发生 SSH 闪断、执行机异常重启或进程无法判定退出，调度机会将相关 NPU 标记为 **Quarantine（隔离状态）**，防止后续任务因脏环境报错。

**解除 Quarantine 的标准步骤**：
1. **暂停调度**：停止当前 Agent（`kill <agent-pid>`）。
2. **检查远端真实物理卡状态**：
   SSH 登录执行机，运行 `npu-smi info`，确认对应卡上确实已无残留进程。如有僵尸进程，核对 PID 后手动 `kill -9` 清理。
3. **解除隔离记录**：
   在调度机检查 `state/unified-jobs.json`，将对应 Task 的 `quarantined` 字段由 `true` 改为 `false`，并记录修复说明。
4. **重启 Agent**：重新执行 `./run-unified-agent.sh` 恢复调度。

### 5.4 磁盘空间预检（Docker Overlay / 共享 NFS 缓存）

* **常见隐患**：部分执行机容器使用的是宿主机 `/data` 作为 Docker Overlay 底层，若宿主机磁盘写满，会导致容器内报错 `Not enough disk space`。
* **规避方案**：模型仓用例统一将 Hugging Face 缓存重定向至共享存储（`HF_HOME=/mnt/share/ci_tests/.lite-actions-hf-cache`）。
* **排查命令**：
  ```bash
  # 在执行机上检查根目录与共享目录可用空间
  df -h /
  df -h /mnt/share
  ```

### 5.5 版本平滑升级与安全回滚

* **平滑升级**：
  1. 查看日志确认当前无正在执行的训练任务；
  2. 终止当前 Agent 进程；
  3. 执行 `git pull` 拉取最新代码，执行 `python3 unified_agent.py --validate` 校验配置；
  4. 启动新 Agent 并通过 `Dispatcher-Smoke` 工作流做冒烟验收。
* **安全回滚**：
  如新版本出现非预期异常，停止 Agent 后执行 `git checkout <stable-tag/sha>`，重新拉起旧版本即可。

---

## 6. 环境扩展指南：如何新增机器与调度通道

当集群新增了机器或需要开辟新的测试通道时，按以下三个步骤在 `lite-actions` 仓配置。

### 步骤一：在 `config/pools.json` 中登记物理机与 NPU

机器池配置文件用于描述物理节点、SSH 登录入口与 NPU 卡号分配：

```json
{
  "pools": {
    "a3-shared": {
      "type": "ascend-a3",
      "process_indexing": "card",
      "nodes": [
        {
          "host_id": "a3-03",
          "ssh": "root@10.x.x.3",
          "node_ip": "192.168.1.3",
          "device_ids": [0, 1, 2, 3, 4, 5, 6, 7]
        },
        {
          "host_id": "a3-04",
          "ssh": "root@10.x.x.4",
          "node_ip": "192.168.1.4",
          "device_ids": [0, 1, 2, 3, 4, 5, 6, 7]
        }
      ]
    }
  }
}
```

* `host_id`：机器唯一标识；
* `ssh`：调度机访问该节点所用的 SSH 用户名与 IP；
* `node_ip`：多机分布式训练时节点间通信的 HCCL 小网 IP；
* `device_ids`：该节点允许被 CI 调度的物理卡号列表。

### 步骤二：在 `config/pipelines.json` 中配置调度通道

调度通道配置文件用于将 GitHub Actions 的 Workflow 与物理机器池建立授权映射：

```json
{
  "pipelines": {
    "a3-8p": {
      "workflow": "a3-8p-lite-actions.yml",
      "pool": "a3-shared",
      "resources": {
        "nodes": 1,
        "devices_per_node": 8
      },
      "runner": {
        "kind": "ssh"
      },
      "timeout_seconds": 7500,
      "enabled": true,
      "pipeline_id": "a3-8p"
    }
  }
}
```

* `workflow`：关联的模型仓 GitHub Actions Workflow 文件名；
* `pool`：指定使用的机器池名称；
* `resources`：该通道单次运行所需的拓扑规格（节点数、单节点卡数）；
* `timeout_seconds`：单任务最大超时时间（秒）。

### 步骤三：网络与连通性只读自检

添加配置后，在调度机上运行只读预检脚本（不会启动训练，不占用物理卡）：

```bash
# 验证 SSH 连通性
python3 scripts/preflight_pool.py --pool a3-shared

# 验证远端 npu-smi 命令与卡号识别
python3 scripts/preflight_pool.py --pool a3-shared --npu
```

自检全绿后，即可在 GitHub 触发该通道的任务。

---

## 7. 相关参考

* **如何编写和新增测试用例**：参见模型仓 [集成测试使用指南 (tests/integration_tests/README.md)](https://github.com/depeng1994/torchtitan-npu/blob/master/tests/integration_tests/README.md)。
* **历史架构演进与阶段设计备忘**：参见 [REFACTOR_PLAN.md](REFACTOR_PLAN.md)。
