# Control Center API v1

所有路径以 `/api/v1` 开头。响应为 JSON，时间为 Unix 秒。错误格式为 `{code, message}`，登录限流和写配额耗尽可包含 `Retry-After`。字段类型见 [types/api.ts](../worker/src/types/api.ts)。

## 认证和缓存

- `GET /public/*` 对所有访客返回同一脱敏视图，缓存头为 `public, max-age=10`。
- `GET /admin/*` 以及管理写请求使用 `la_session` Cookie。管理员读视图包含完整节点地址。认证、管理及 Worker 响应使用 `private, no-store`。
- `POST /auth/login` 请求 `{username, secret}`，返回 Session，并发放七天 Cookie。
- `GET /auth/me` 返回 Session，`POST /auth/logout` 撤销会话并清除 Cookie。
- `GET /admin/users` 返回 `{users: User[]}`。`POST /admin/users` 接收 `{username}`。`POST /admin/users/:id/reset-key` 重置密钥，两者均返回 IssuedKey。`DELETE /admin/users/:id` 软删账号。四项均要求 Super。
- `GET /admin/audits?cursor=&limit=50` 返回 `{records,next_cursor}`。

## 看板

| GET 路径 | 响应 |
|---|---|
| `/{public,admin}/overview` | Overview |
| `/{public,admin}/timeline` | Timeline |
| `/{public,admin}/models` | `{models: ModelTab[]}` |
| `/{public,admin}/models/:slug/runs?cursor=&limit=50` | ModelRuns，历史按任务分页，响应按 Workflow 分组 |
| `/{public,admin}/tasks/:taskUid` | TaskDetail |
| `/{public,admin}/cluster` | 期望配置、两区应用状态，管理员可读历史版本 |

节点沿用 `node_ref` 做行标识。`display_host_id` 在公开接口仅展示掩码。任务分配、实线租约和虚线排期使用同一任务 UID。

## 管理操作

| 路径 | 请求与结果 |
|---|---|
| `POST /admin/cancellations` | RunIdentity，返回 Cancellation。重复请求返回同一张单。 |
| `GET /admin/cancellations/:cancelId` | Cancellation 和关联用例的停止、清理情况。 |
| `GET /admin/capabilities/cancel` | 按仓库列出可取消状态。 |
| `POST /admin/tasks/:taskUid/move-up` | 返回 `{cmd_id,status:"PENDING"}` 或 `{status:"NOOP",reason:"ALREADY_AT_HEAD"}`。 |
| `GET /admin/commands/:cmdId` | 查询命令状态及回执。 |
| `POST /admin/cluster/preview` | `{base_revision, operation}` → ClusterPreview。 |
| `POST /admin/cluster/publish` | `{base_revision, operation, preview_hash}` → ClusterSync。 |
| `POST /admin/cluster/rollback` | `{base_revision, target_revision}` → ClusterSync。 |

## Worker 协议

`Authorization: Bearer <worker_token>` 绑定 Worker 身份。全部 Worker 请求由同一出口策略校验，HTTP/1.1 整包小于 2,000 字节。Worker 定期心跳，仅在发现待办时主动取令。

| 路径 | 请求 |
|---|---|
| `POST /worker/heartbeat` | `{w,i,ts,rev,sha,ok,act,q,idle_ts,eu}`，返回 `{ok,pending_commands,latest_revision,need_snapshot}`。 |
| `GET /worker/commands` | 返回 `{commands:[{cmd_id,cmd_type,payload}]}`。 |
| `POST /worker/command-receipts` | `{cmd_id,status,rev?,pos?,err?,ts}`。 |
| `POST /worker/events` | `{w,i,ev:[{s,t,ts,...}]}`，返回 `{ok,need_snapshot,expected_seq?}`。 |
| `POST /worker/snapshots` | `{w,i,sid,p,n,seq,kind,items}`，返回 `{ok,complete}`。 |
| `GET /worker/configs/:revision` | `{revision,sha256,config,config_json}`，其中 SHA-256 按 `config_json` 的 UTF-8 字节计算，配置版本不可变。 |

事件代码 `TU` 新任务，`TP` 阶段，`TL` 租约，`TF` 终态，`IP` 巡检，`QP` 队序。每个事件包最多接受 6 个事件。长租约由 `TL.ap` 分片，同任务第 1 片替换租约，后续片追加。队列项可用 `off` 指明候选组合分片的偏移。

快照分为 `task`、`lease`、`device` 和 `queue`，同一快照的所有分片共用序号。租约项使用 `{tid,a}`，设备和队列项沿用相应事件的业务字段。Worker 重启可沿用 Outbox 的实例与序号，保证未确认事件能继续重放。
