# 后端验收与部署

> **版本状态说明（2026-11-20）**
> - 本文档记录的是 **v1.1 基线**（`REQ-01` ~ `REQ-14`）在 YCY 隔离环境的后端回归验收证据与部署初始化方法。
> - 前端 Demo（`codex/control-center-demo @ cd770ce`）与 [PRD.md](PRD.md) 已升级至 **v1.5**（统一 Workflow 表、Workflow 级插队、全量节点 5 次探测离线模型、两步先排空再删除、统一 `enabled`/`paused` 暂停调度语义，以及「初期 ChatGPT Sites 直出、代码全入 GitHub 仓、支持零业务改动迁至个人服务器」与 `portability_intercept` DT 门禁）。v1.5 后端/Worker 增量开发（`REQ-15` ~ `REQ-20`）、联调补齐（`INT-03/04/05/06/07/08/09/09B/10`）以及全栈端到端联调清单（含 8 条 `★ P0 关键必通` 标注）统一以 [DESIGN.md](DESIGN.md) 为准。

> **正式环境证据（2026-11-20）**：线上控制面为 Cloudflare Worker + D1 `lite-actions-control-center`，正式 Site 版本 17 通过 server-backed API 访问线上数据。前端已移除 Demo/fixture 数据回退，正式库没有活动任务时只显示空队列。Cloudflare Worker 静态包已由 `17ed45f` 切换为同一正式前端构建，线上 `/`、`/api/v1/public/overview` 和 `/api/v1/public/workflows` 实测不含 Demo/fixture/模拟数据，部署版本为 `f332c4d0-131d-4783-8b7b-c0b4749ff811`。Blue 当前发布 SHA 为 `d9fcf5de649540d0e0f5ea0ab503727f7815ea7f`，D1 集群配置 `revision=4` 已应用，两个 A3 节点通过实机指纹与 TorchTitan 容器绑定校验，线上 overview 实测 `offline=false`、`applied_rev=4`。CPU-only self-smoke run `44594574859` 已通过。GitCode 源归档与隔离 profile 的真实 Blue NPU 联调也已通过：8P run `55b7bf72c6e541babc5977eef7f486a8`、16P run `5a7f8c3030a549bab4cadde3a6678ad9` 均 `COMPLETED`，结果为 `PASS`、`exit_code=0`、`cleanup_verified=true`，诊断分别含 `all_reduce_sum=28` 与 `all_reduce_sum=120`。

> **YCY Linux 回归证据（2026-11-20）**：在 Blue 当前发布目录使用 `/usr/bin/python3` 执行 `python3 -m unittest discover -s tests`，`215 tests` 全部通过。macOS 本地运行的 Linux 专属 `/proc`、systemd 和 `/etc/os-release` 测试失败不作为生产回归结论。

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
node --experimental-strip-types control-center/worker/scripts/node-control-center-smoke.mjs
# REQ-20 落地后必跑的托管防锁死 DT 拦截门禁与 Sites 打包校验：
# npm run test:portability --prefix control-center/worker
# node control-center/scripts/build-sites.mjs
python3 control-center/tests/integration/run.py
```

联调启动一个仅绑定环回地址的控制面实例（`REQ-20` 后支持 `--runtime=sites|node` 分别验证 Wrangler/D1 与个人服务器 Node 24/`node:sqlite`）、两个有独立状态目录的 `unified_router.py` 进程和一台临时 TLS 抓包服务器。它使用测试平台响应、测试地址和模拟巡检。给联调脚本传入一个尚不存在的目录，可保留服务日志和数据便于排障。

> **2026-11-20 v1.5 联调重跑**：修正 harness 对 `observe_health` 三元组接口的测试桩后，`python3 control-center/tests/integration/run.py` 全程通过，输出覆盖 INT-01/03 的公开与管理员隔离、INT-07 的连续三次插队、INT-08 的配置发布/重启应用，以及 INT-02/05 的 90s 离线恢复、20+ 事件补发和 86 个 TLS 请求，最大请求 1,790 B。

| 验收项（v1.1 基线证据） | 自动化证据与待补齐缺口 |
|---|---|
| INT-01 | `auth.test.ts`、`access-matrix.test.ts`、`masking.test.ts` 和联调的匿名/登录 HTTP 响应（✅ v1.1 通过） |
| INT-02 | `worker-ingest.test.ts`、`test_control_client.py` 和双进程联调，覆盖去重、跳号重建、分片重试、本地日志与 503 后补发（✅ v1.1 通过） |
| INT-03 | `test_idle_probe.py`、`device-state.test.ts` 和 Blue 线上设备组状态（✅ REQ-15 全量探测、轮次持久化、健康投影与 `last_online_at` 已实现；⏳ 完整 5 次实机失败/恢复演练待补） |
| INT-04 | `schedule-projection.test.ts` 和 `test_queue_telemetry.py`（⏳ 待从同一 D1 数据集核验排除失败及不可靠样本后的 API 响应） |
| INT-05 | `test_upload_intercept.py` 实际 HTTP/1.1 序列化、静态路径注册门禁，以及当前 v1.5 TLS 联调抓取两个 Worker 和两个平台客户端的请求（✅ 2026-11-20 重跑通过，86 个请求，最长 1,790 B） |
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

通过 Worker 现有部署配置为选定区域添加 `control_center.url`，并通过 Worker 环境注入 `LITE_ACTIONS_CONTROL_TOKEN`（托管从 ChatGPT Sites 迁至个人服务器时，Worker 仅需更新 `control_center.url`，零代码修改）。Yellow 可配置 `control_center.ci_reserve_bytes` 为 CI 回报保留每日额度。现场若必须经代理，额外在 root:root、0600 的 `deployment.json` 本地增加 `control_center.proxy`，格式使用占位符 `http://<proxy-user>:<proxy-password>@<proxy-host>:<proxy-port>`；客户端不会读取环境代理，日志和异常不输出完整 URL。机器接口注册在 `/api/v1/worker`，健康检查与公开视图可调用 `/api/v1/public/overview`。接口字段见 [API 契约](api-contract.md)。
