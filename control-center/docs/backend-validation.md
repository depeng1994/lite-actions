# 后端验收与部署

> **版本状态说明（2026-11-19）**
> - 本文档记录的是 **v1.1 基线**（`REQ-01` ~ `REQ-14`）在 YCY 隔离环境的后端回归验收证据与部署初始化方法。
> - 前端 Demo（`codex/control-center-demo @ cd770ce`）与 [PRD.md](PRD.md) 已升级至 **v1.5**（统一 Workflow 表、Workflow 级插队、全量节点 5 次探测离线模型、两步先排空再删除、统一 `enabled`/`paused` 暂停调度语义，以及「初期 ChatGPT Sites 直出、代码全入 GitHub 仓、支持零业务改动迁至个人服务器」与 `portability_intercept` DT 门禁）。v1.5 后端/Worker 增量开发（`REQ-15` ~ `REQ-20`）、联调补齐（`INT-03/04/05/06/07/08/09/09B/10`）以及全栈端到端联调清单（含 8 条 `★ P0 关键必通` 标注）统一以 [DESIGN.md](DESIGN.md) 为准。

> **正式环境证据（2026-11-20）**：线上控制面为 Cloudflare Worker + D1 `lite-actions-control-center`，正式 Site 版本 13 通过 server-backed API 访问线上数据。前端已移除 Demo/fixture 数据回退，正式库没有活动任务时只显示空队列。Blue 已配置线上 URL、机器密钥和请求 User-Agent，线上 overview 实测 `offline=false`、`applied_rev=1`，四个 Blue 设备组实测 `ONLINE` 且包含 `last_online_at`。CPU-only self-smoke run `44570266554` 已在部署 SHA `1b02490033df239f94adff60cd0dc91a6714eafa` 上通过。YCY Linux 全量 Python 回归为 `211 tests OK`，Worker lint 与 `15 files / 36 tests` 全绿。正式前端站点版本 13 与 Sites 静态资源、SPA 回退、正式集群暂停态和无错误日志核验全绿。REQ-15 定向门禁、REQ-16 读模型子集、REQ-17 服务端插队子集（含 Worker 原子交换/幂等单测）、REQ-18 服务端状态机和 REQ-20 portability 静态/BLOB 子集与 Node 24 `node:sqlite` 与 Node server adapter smoke 已通过，完整 103 条冷热分页/滚删、5 次实机故障演练、Worker 排空回执、Workflow Worker 原子回执、双运行时迁移和 Yellow 生产运行仍由 INT-03/INT-07/INT-08/INT-09/INT-10/REL-01 跟踪。

v1.1 实现基线运行在 Cloudflare Worker + 单 D1 库；完成 `REQ-20` 托管解耦后，同一套核心业务与 `migrations/*.sql` 同时支持初期 **ChatGPT Sites** 直接部署、后期迁移 **个人服务器（Node.js 24 + 内置 `node:sqlite`）** 及备选 **Cloudflare Workers Free + D1 Free**。Blue 和 Yellow Worker 仅通过 HTTPS 出站接入，迁移时只改 `control_center.url`。

## 验收命令

从仓库根目录运行。需要 Node.js 24、Python 3.11 或更新版本及 OpenSSL。

```sh
python3 -m pip install -r requirements-dev.txt
python3 scripts/lint.py
python3 -m unittest tests/test_upload_intercept.py -v
python3 -m unittest discover -s tests
npm ci --prefix control-center/worker
npm run lint --prefix control-center/worker
npm test --prefix control-center/worker
# REQ-20 落地后必跑的托管防锁死 DT 拦截门禁与 Sites 打包校验：
# npm run test:portability --prefix control-center/worker
# node control-center/scripts/build-sites.mjs
python3 control-center/tests/integration/run.py
```

联调启动一个仅绑定环回地址的控制面实例（`REQ-20` 后支持 `--runtime=sites|node` 分别验证 Wrangler/D1 与个人服务器 Node 24/`node:sqlite`）、两个有独立状态目录的 `unified_router.py` 进程和一台临时 TLS 抓包服务器。它使用测试平台响应、测试地址和模拟巡检。给联调脚本传入一个尚不存在的目录，可保留服务日志和数据便于排障。

| 验收项（v1.1 基线证据） | 自动化证据与待补齐缺口 |
|---|---|
| INT-01 | `auth.test.ts`、`access-matrix.test.ts`、`masking.test.ts` 和联调的匿名/登录 HTTP 响应（✅ v1.1 通过） |
| INT-02 | `worker-ingest.test.ts`、`test_control_client.py` 和双进程联调，覆盖去重、跳号重建、分片重试、本地日志与 503 后补发（✅ v1.1 通过） |
| INT-03 | `test_idle_probe.py`、`device-state.test.ts` 和 Blue 线上设备组状态（✅ REQ-15 全量探测、轮次持久化、健康投影与 `last_online_at` 已实现；⏳ 完整 5 次实机失败/恢复演练待补） |
| INT-04 | `schedule-projection.test.ts` 和 `test_queue_telemetry.py`（⏳ 待从同一 D1 数据集核验排除失败及不可靠样本后的 API 响应） |
| INT-05 | `test_upload_intercept.py` 实际 HTTP/1.1 序列化、静态路径注册门禁，以及 TLS 联调抓取两个 Worker 和两个平台客户端的请求（✅ v1.1 通过，最长 1,748 B；⏳ **`REQ-15/16/17` 新增协议字段后须重跑回归**） |
| INT-06 | `cancellations.test.ts` 使用模拟平台核验 App 令牌、attempt 校验、幂等和强制取消（⏳ 待补齐双平台、双 Worker 在网站断连时的整次取消联调） |
| INT-07 | `move-up.test.ts`、`test_move_up.py`、`test_smoke_queue.py`（⏳ **v1.5 需按 `REQ-17` 升级为 Workflow 级跨区插队联调，并补齐虚拟时钟 71h59m/72h01m 超时实测**） |
| INT-08 | `cluster-config.test.ts`（新增 DRAIN→RESUME、删除前四项门禁与 `enabled: false` 断言）、`test_preflight_export.py`、`test_pools_hot_reload.py`（⏳ **仍需补齐 Worker 侧排空取消、双 Worker 单端失败、实机重启与自动更新闭环**） |
| INT-09 | `retention-storage.test.ts` 和 `board-read.test.ts`（⏳ 待补齐同一次配额熔断触发 Worker 暂存恢复，并按 `REQ-16` 升级为 50 Workflow 跨 30 天分页验证） |
| INT-10 | `portability-intercept.test.ts`（当前已覆盖运行时锁死静态扫描与 BLOB 形态对等）、`sites-parity.test.ts`、`build-sites.mjs` 与双运行时联调（⏳ **`REQ-20` 仍需补齐 ChatGPT Sites 产物包、Sites/D1 与个人服务器 Node 24/`node:sqlite` 双运行时对等及无 Cron 取消跟踪验证**） |

ChatGPT Sites 实机能力核验、真实硬件、真实平台测试仓及在线免费套餐的验收对应 `REL-00` 至 `REL-04`。浏览器端验收依赖正式前端集成（`FE-01` ~ `FE-05`）。

## 初始化控制面与多托管部署

在 `control-center/worker` 中应用迁移，并从仓库 Pools 配置初始化版本 1。种子脚本可重复运行，后续配置由网站发布。

```sh
# 本地 Wrangler / D1 开发
npx wrangler d1 migrations apply DB --local
npm run seed -- --local
npm run dev

# 初期 ChatGPT Sites 打包部署（REQ-20 + FE-05 完成后）
# node control-center/scripts/build-sites.mjs   # 产出 control-center/dist/{client,server/index.js,.openai/hosting.json}

# 后期迁移个人服务器（Node.js 24 + 内置 node:sqlite，零第三方原生依赖；REQ-20 完成后）
# npm run seed -- --sqlite /var/lib/lite-actions/control-center.sqlite
# DB_PATH=/var/lib/lite-actions/control-center.sqlite STATIC_DIR=control-center/dist/client \
#   TRUST_PROXY_HEADERS=1 node control-center/worker/src/adapters/nodeServer.ts
```

部署到 ChatGPT Sites / Cloudflare Workers（使用 D1）时，应用远端迁移与种子；部署到个人服务器时使用 `seed.mjs --sqlite <path>` 初始化本地 SQLite 并配 Caddy/Nginx HTTPS 反代（详见 `control-center/deploy/`）。两种方式均通过环境变量 / 服务端 Secret 配置相同凭据：`KEY_PEPPER`、`SUPER_SECRET`、`WORKER_BLUE_KEY_HASH`、`WORKER_YELLOW_KEY_HASH`（使用相同 pepper 做 HMAC-SHA256 保存为小写十六进制）、`GITHUB_APP_ID`、`GITHUB_APP_PRIVATE_KEY` 和 `GITCODE_CANCEL_TOKEN`。

通过 Worker 现有部署配置为选定区域添加 `control_center.url`，并通过 Worker 环境注入 `LITE_ACTIONS_CONTROL_TOKEN`（托管从 ChatGPT Sites 迁至个人服务器时，Worker 仅需更新 `control_center.url`，零代码修改）。Yellow 可配置 `control_center.ci_reserve_bytes` 为 CI 回报保留每日额度。机器接口注册在 `/api/v1/worker`，健康检查与公开视图可调用 `/api/v1/public/overview`。接口字段见 [API 契约](api-contract.md)。
