# Control Center API v1（v1.5 对齐版）

所有路径以 `/api/v1` 开头。响应为 JSON，时间为 Unix 秒（前端展示转换为北京时间 UTC+8 并注明时区）。错误格式为 `{code, message}`，登录限流和写配额耗尽可包含 `Retry-After`。

> **实现状态说明**
> - **已落地（v1.1 基线）**：认证与用户管理、公开/管理员脱敏与缓存隔离、Worker 接入协议底座、取消 Workflow、集群配置发布与热加载、SQLite 热冷归档与滚删（旧版 `/models/:slug/runs` 与 case 级 `/tasks/:taskUid/move-up` / `MOVE_UP` 已在 v1.5 清理移除）。
> - **v1.5 增量/升级（`REQ-15` ~ `REQ-20`，代码与本地/隔离夹具门禁已完成）**：全部节点 2 分钟在线探测与 5 次离线健康模型（`/timeline` & `/cluster`）、统一 Workflow 表 `GET /workflows` 与 `workflow_runs` 增量汇总/50 条游标分页、Workflow 级插队 `POST /workflows/move-up` & `GET /workflow-priority-ops/:opId`、集群两步「先排空再删除」/统一 `enabled`/`paused` 暂停调度语义与实机复核 `PARTIAL_APPLIED` 回执、`/timeline` 内嵌 `tasks_by_uid` 与 `queued_by_pool`、Worker `TU.sub/wn` 与 `IP.pr/v/mc/lo` 字段及 `SWAP_WORKFLOW` 命令，以及托管解耦（lite-actions.ooguy.com 页面与 API 同源部署、个人服务器 Node 24 + `node:sqlite` 运行时、4 层 `portability_intercept` DT、raw-SQLite `node-runtime.test.ts` 与本地隔离夹具 `run.py --runtime=node/node`）。真实生产部署与线上 server↔Node 切换、真实 Blue/Yellow 跨区 16P 联调、真实 GitHub/GitCode 整次取消及线上 SQLite 80%→70% 写额度/容量演练仍按 `DESIGN.md` Phase 7 的未勾选门禁跟踪。
> - **托管与迁移契约（`REQ-20`）**：初期采用 **lite-actions.ooguy.com 页面与 API 同源部署 + Node.js 24 + SQLite 后端** 架构：server 使用 Node.js 24 + SQLite，部署时注入 `Caddy 域名配置` 与 `Node 同进程 API`；全部页面统一通过同源相对路径 `/api/v1/*` 访问后端（零硬编码托管域名），静态 SPA 路由刷新回退 `/index.html`，`/api/*` 404 始终返回 JSON 错误体（绝不返回 HTML）；后期迁移至个人服务器（Node.js 24 + `node:sqlite`）时复用同一核心业务与 SQLite 迁移，Blue/Yellow Worker 仅修改配置中的 `control_center.url`。
> - 类型定义基准：[types/api.ts](../worker/src/types/api.ts)（`REQ-19` 同步更新为 v1.5 冻结类型）。

## 认证、账号和缓存

- `GET /public/*` 对所有访客返回同一脱敏视图，缓存头为 `public, max-age=10`。匿名响应绝不包含真实 `host_id`、`node_ip`、`container_id`、凭据或日志。
- `GET /admin/*` 以及管理写请求使用 `la_session` Cookie（`HttpOnly; Secure; SameSite=Strict`，7 天有效）。管理员读视图包含完整节点 `host_id`。认证、管理及 Worker 响应固定使用 `private, no-store`。
- `POST /auth/login`：请求 `{username, secret}`，返回 `Session` 并发放 7 天 Cookie；同用户名 + IP 10 分钟失败 5 次锁 10 分钟（`429`）。
- `GET /auth/me`：返回 `Session`；`POST /auth/logout` 撤销会话并清除 Cookie。
- `GET /admin/users`：返回 `{users: User[]}`（仅 Super，不返回密钥或哈希）。
- `POST /admin/users`：接收 `{username}`，返回 `IssuedKey`（`secret_once` 仅展示一次，仅 Super）。
- `POST /admin/users/:id/reset-key`：重置密钥并立即撤销旧密钥与全部会话，返回 `IssuedKey`（仅 Super）。
- `DELETE /admin/users/:id`：软删账号并立即撤销全部会话，保留审计（仅 Super）。
- `GET /admin/audits?cursor=&limit=50`：返回 `{records, next_cursor}`（管理员与 Super 均可读）。

## 看板读接口

| GET 路径 | 状态 | 响应与说明 |
|---|---|---|
| `/{public,admin}/overview` | 已落地 | `Overview`：双 Worker 在线/心跳/版本/活动与排队数、`page_refresh_sec: 30`。 |
| `/{public,admin}/timeline` | v1.5 增强 (`REQ-15/16/18`) | `Timeline`：Worker → Pool → Node → DeviceGroup 四层树（节点直接以 `display_host_id` / `host_id` 呈现，无独立节点别名）；每个 DeviceGroup 含 `health`（`PENDING_PROBE/ONLINE/PROBE_WARN/OFFLINE` + `missed_count` + `last_online_at`，**全部节点均探测，含暂停调度 Pool**）、`state`（`LEASED/UNKNOWN_BUSY/CONFIRMED_IDLE`）、`paused`（Pool `enabled: false` 与节点/组 `paused: true` 语义统一）、`draining`、`quarantined`、`worker_offline`、`solid_blocks[]`、`dashed_blocks[]`；节点与 Pool 含汇总 `health`；同租约覆盖多组时各组独立返回 `solid_block` 并共享 `task_uid` 去重；顶层附 `tasks_by_uid`、`queued_by_pool`、`zero_card_tasks[]`（零 N+1 请求）。传 `history_hours=0..72` 时，额外返回窗口内有实际分配且有开始时间的已完成任务 `history_tasks[]`、历史 `solid_blocks` 和设备组外部占用区间，默认不查询历史。可同时传 `compact_history=1`，改用 `history_task_uids[]` 引用 `tasks_by_uid` 中的同一份历史摘要。 |
| `/{public,admin}/models` | v1.5 微调 (`REQ-19`) | `{default_model, models: ModelTab[]}`：全部逻辑模型（无任务也返回），每个 repo 含 `provider, repo, cancel_supported, repo_scope?`（`组织仓 \| 个人仓`）。 |
| `/{public,admin}/workflows` | **v1.5 新增 (`REQ-16`)** | `WorkflowList`：统一活动与历史 Workflow 表格接口。查询参数：`model?`（留空浏览全部）、`repo?`、`platform?`、`pool?`、`status?`（`""\|"active"\|"history"`）、`history_limit?`（0 或 50，首次打开传 0 仅查活动队列）、`cursor?`（按 Workflow 分页游标）。返回：`queue_version`、`active: WorkflowRow[]`（序号 `0` 为在途，`1..N` 为待调度）、`history: WorkflowRow[]`（全局固定负序号 `-1, -2, ...`，按 50 个完整 Workflow 分页，冷热共用）、`history_total`、`next_cursor`、`has_more`、`truncated_by_capacity`、`purged_notice`。每条 `WorkflowRow` 包含：`sequence`、`submitted_at`、`provider`、`repo`、`repo_scope?`、`logical_model`、`run_id`、`attempt`、`run_url`、`workflow_name`、`workflow_file`、`pools[]`、`max_cards`、`result`（`"QUEUED"\|"PASS"\|"FAIL"`）、`trigger_actor`、`event_type`、`pr_number`、`pr_author`、`started_at`、`finished_at`、`card_hours`、`card_hours_as_of_now`、`card_hours_partial`、`cancel_supported`、`can_move_up`、`active_cancel_id?`、`cases: TaskSummary[]`（含该 run 下全部活动及已结束子用例）。 |
| `/{public,admin}/models/:slug/runs` | 已移除 | 原 v1.1 按 task 分页接口，已由统一 `/workflows` 替代并下线移除。 |
| `/{public,admin}/tasks/:taskUid` | 已落地 | `TaskDetail`：热冷透明读取，返回各阶段起止时间与耗时、分配节点与设备、最终结果、`storage_tier`（`hot\|cold`）、平台链接；严禁返回日志或诊断行。 |
| `/{public,admin}/cluster` | v1.5 增强 (`REQ-15/18`) | 期望配置、双 Worker 已生效版本/同步回执/心跳时间、**按 Pool / Node / DeviceGroup 输出全量节点在线健康状态、最近在线时间、未知占用、暂停调度（Pool `enabled: false` 与节点/组 `paused: true` 统一）、排空中（`draining`）、隔离标记**；节点直接以 `display_host_id` / `host_id` 呈现；管理员另可读历史配置版本列表与同步失败原因。 |

节点在公开视图使用 `node_ref` 做稳定行键、`display_host_id` 展示掩码（如 `113.xxx.xxx.205:153`）；管理员视图同时返回完整 `host_id`；不另设节点显示别名。任务分配、实线租约、虚线排期与子用例表共用同一 `task_uid`。

## 管理写接口

| 路径 | 状态 | 请求与结果 |
|---|---|---|
| `POST /admin/cancellations` | 已落地 | `RunIdentity`，支持 GitHub 与 GitCode；发送前核对最新 attempt 与状态，重复请求返回同一 `Cancellation`。 |
| `GET /admin/cancellations/:cancelId` | 已落地 | `Cancellation` 及关联两区全部用例的停止进度与设备释放/隔离状态。 |
| `GET /admin/capabilities/cancel` | 已落地 | 按仓库列出可取消状态（GitHub App 安装态 / GitCode Token 就绪态）。 |
| `POST /admin/workflows/move-up` | **v1.5 新增 (`REQ-17`)** | `{provider, repo, run_id, attempt, queue_version, idempotency_key?}`。校验 `queue_version`（过期返回 `409 QUEUE_VERSION_CONFLICT`），将待调度 Workflow（`seq >= 2`）与真实前一位（`seq - 1`）原子交换；队首 `1` 返回 `200 NOOP`，已进在途 `0` 或离队返回 `409 WORKFLOW_NOT_WAITING`；成功返回 `202 {op_id, queue_version, from_seq, to_seq, swapped_with, worker_commands}`。 |
| `GET /admin/workflow-priority-ops/:opId` | **v1.5 新增 (`REQ-17`)** | 分 Worker、Pool、任务类型返回 `SWAP_WORKFLOW` 受理/已应用/无变化/冲突/失败回执及受影响子用例新旧顺位。 |
| `POST /admin/tasks/:taskUid/move-up` | 已移除 | 原 v1.1 case 级插队接口及 `MOVE_UP` 命令，已由 `/admin/workflows/move-up`（`SWAP_WORKFLOW`）替代并下线移除。 |
| `GET /admin/commands/:cmdId` | 已落地 | 查询单条 Worker 命令状态及回执。 |
| `POST /admin/cluster/preview` | v1.5 增强 (`REQ-18`) | `{base_revision, operation}` → `ClusterPreview`（差异、受影响组/跨机组合、排空取消用例、活动租约、`blocking_reasons[]`、`preview_hash`）。`operation.op` 支持 `PAUSE`、`RESUME`（同时支持退出排空态）、`DRAIN`（两步删除第一步：置暂停调度 + 标排空中 + 可选取消唯一依赖排队用例）、`ADD_POOL`/`ADD_NODE`/`ADD_DEVICE_GROUP`、`DELETE_POOL`/`DELETE_NODE`/`DELETE_DEVICE_GROUP`（两步删除第二步：强校验已排空且活动租约=0、排队引用=0、隔离=0，否则返回 `409 RESOURCE_NOT_DRAINED` / `NODE_QUARANTINED`）。 |
| `POST /admin/cluster/publish` | v1.5 增强 (`REQ-18`) | `{base_revision, operation, preview_hash}` → `ClusterSync`（递增版本并向涉及 Worker 下发 `SYNC_CONFIG`；Pool 暂停/恢复统一写 `enabled: false/true`）。 |
| `POST /admin/cluster/rollback` | 已落地 | `{base_revision, target_revision}` → `ClusterSync`（基于历史版本发布新 `revision`）。 |

## Worker 协议（`/api/v1/worker`）

`Authorization: Bearer <worker_token>` 绑定 Worker 身份。全部 Worker 写请求与 Yellow 平台写请求由同一出口策略校验，**真实 HTTP/1.1 整包严格小于 2,000 字节**，由 `tests/test_upload_intercept.py` 强制门禁。Worker 每 30 秒心跳一次，仅在 `pending_commands > 0` 时调用 `GET /commands`。

| 路径 | 请求与回包 |
|---|---|
| `POST /worker/heartbeat` | `{w,i,ts,rev,sha,ok,act,q,idle_ts,probe_ts?,eu}`，返回 `{ok,pending_commands,latest_revision,need_snapshot}`。稳态巡检无变化时以 `probe_ts`/`idle_ts` 刷新在线与空闲新鲜度，不额外写设备表。 |
| `GET /worker/commands` | 返回 `{commands:[{cmd_id,cmd_type,payload}]}`，`cmd_type` 支持 `SYNC_CONFIG`、`SWAP_WORKFLOW`（v1.5）及兼容 `MOVE_UP`。读取不改状态。 |
| `POST /worker/command-receipts` | `{cmd_id,status,rev?,pos?,pools?,err?,ts}`。`status` 支持 `ACKED`/`APPLIED`/`PARTIAL_APPLIED`/`NOOP`/`CONFLICT`/`FAILED`。当 `SYNC_CONFIG` 实机复核存在部分节点不可达/探针失败时，健康节点生效且失败节点本地隔离暂停，Worker 回报 `PARTIAL_APPLIED`（带 `rev` 与 `err` 不可达原因，更新 `applied_rev`，不再伪造 `APPLIED`；`err` 仅管理员可见，公开接口不泄露真实 IP）。`SWAP_WORKFLOW` 回执携带紧凑按池结果 `pools: [{p,k,st,c?}]`（整包 `< 2,000` B）。 |
| `POST /worker/events` | `{w,i,ev:[{s,t,ts,...}]}`，返回 `{ok,need_snapshot,expected_seq?}`。每包最多 6 条事件，按 `1,880` B 目标自动拆包。 |
| `POST /worker/snapshots` | `{w,i,sid,p,n,seq,kind,items}`，返回 `{ok,complete}`。分片类型含 `task`、`lease`、`device`、`queue`（v1.5 含 `wf_order`），同快照共序号。 |
| `GET /worker/configs/:revision` | `{revision,sha256,config,config_json}`，SHA-256 按 `config_json` UTF-8 字节计算，支持 `ETag`。 |

事件代码与 v1.5 扩展字段：
- `TU` 新任务：v1.5 增加 `sub`（平台 `run.created_at` Unix 秒）与 `wn`（Workflow 名称，≤ 64B）；
- `TP` 阶段、`TL` 租约（长租约由 `TL.ap` 分片）、`TF` 终态；
- `IP` 设备组探测：v1.5 增加 `pr`（单调探测轮次，重放去重不重复累加失败次数）、`v`（`0=online, 1=warn, 2=offline`）、`mc`（连续失败次数 0..5）、`lo`（最近在线时间 Unix 秒）；保留 `st`（`idle/busy/probe_error`）与 `qu/qid`（隔离）；
- `QP` 队序（候选组可由 `off` 分片）。
