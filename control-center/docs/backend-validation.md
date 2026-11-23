# 后端验收与部署

> **版本状态说明（2026-11-20）**
> - 本文档记录的是 **v1.1 基线**（`REQ-01` ~ `REQ-14`）在 YCY 隔离环境的后端回归验收证据与部署初始化方法。
> - 前端 Demo（`codex/control-center-demo @ cd770ce`）与 [PRD.md](PRD.md) 已升级至 **v1.5**（统一 Workflow 表、Workflow 级插队、全量节点 5 次探测离线模型、两步先排空再删除、统一 `enabled`/`paused` 暂停调度语义，以及按决策 2-A 的「初期 lite-actions.ooguy.com 前端与 API 同源部署 + Node.js 24/SQLite 后端、代码全入 GitHub 仓、支持零业务改动迁至个人服务器」与 `portability_intercept` DT 门禁）。v1.5 后端/Worker 增量开发（`REQ-15` ~ `REQ-20`）、联调补齐（`INT-03/04/05/06/07/08/09/09B/10`）以及全栈端到端联调清单（含 8 条 `★ P0 关键必通` 标注）统一以 [DESIGN.md](DESIGN.md) 为准。

> **历史环境基线证据（2026-11-20，PR #44 本轮 `REQ-15`~`REQ-20` 改造与 `0010` 迁移前的旧版线上实例；不代表当前 PR #44 已完成生产发布验收）**：旧版线上 personal server control plane（`f332c4d0-131d-4783-8b7b-c0b4749ff811`）+ SQLite `lite-actions-control-center` 与旧版前端版本 v17 曾验证真实无 Demo 数据；Blue 当时发布 SHA `d9fcf5de649540d0e0f5ea0ab503727f7815ea7f`、`revision=4`、`offline=false`，CPU-only self-smoke run `44594574859` 与 GitCode 8P (`55b7bf72c6e541babc5977eef7f486a8`) / 16P (`5a7f8c3030a549bab4cadde3a6678ad9`) 真实 Blue NPU 隔离联调通过。**当前 PR #44 尚未发布到生产 Worker/SQLite 与 lite-actions.ooguy.com，实机验收项见 [DESIGN.md](DESIGN.md) Phase 7 未勾选门禁。**

> **Linux CI 与本机 macOS Python 回归证据（2026-11-23，PR #44，GitHub Actions Run `45134291763`）**：在 Linux CI（`ubuntu-latest`、Python 3.12.16 + Node 24）执行 `python -m unittest discover -s tests`，**`233 tests OK (skipped=3)`**（0 failures，0 errors）；同一提交在 macOS 本机（Python 3.12）运行全量 `unittest discover -s tests` 为 `233 tests` 中 `5 failures / 3 errors`（均因 macOS 缺少 `/etc/os-release`、`/proc`、`systemctl` 及 `/var`→`/private/var` 路径解析差异，不能以 Linux CI 绿灯掩盖本机运行差异，Linux 专属用例需以 Linux CI/YCY 为准）。历史 2026-11-20 在 Blue YCY 主机执行为 `215 tests` 全绿。

v1.1 实现基线运行在 personal server control plane + 单 SQLite 库；完成 `REQ-20` 托管解耦与决策 2-A 收敛后，初期正式采用 **lite-actions.ooguy.com 前端与 API 同源部署（`Node.js 24 + SQLite，部署期注入 `CONTROL_CENTER_DOMAIN` 与运行时 `CONTROL_CENTER_ORIGIN`）+ Node.js 24 + SQLite 后端**，后期同一套核心业务与 `migrations/*.sql` 可零业务改动迁移至 **个人服务器（Node.js 24 + 内置 `node:sqlite`）**。Blue 和 Yellow Worker 仅通过 HTTPS 出站接入，迁移时只改 `control_center.url`。

## 验收命令

从仓库根目录运行。需要 Node.js 24、Python 3.12（或 3.11+）及 OpenSSL；完整 `unittest discover` 需在 Linux CI/YCY 运行（macOS 缺少 `/etc/os-release`、`/proc` 与 `systemctl`）。

```sh
python3 -m pip install -r requirements-dev.txt
python3 scripts/lint.py
python3 -m unittest tests/test_upload_intercept.py -v
python3 -m unittest discover -s tests
npm ci --prefix control-center/worker
npm ci --prefix control-center/web
npm run lint --prefix control-center/worker
npm test --prefix control-center/worker
npm test --prefix control-center/web
npm run test:runtime --prefix control-center/worker
CONTROL_CENTER_DOMAIN=lite-actions.ooguy.com CONTROL_CENTER_ORIGIN=https://control-center-ci.example \
  node control-center/worker/scripts/start-node-server.mjs --require-origin
python3 control-center/tests/integration/run.py
```

上述集成脚本属于 **isolated fixture evidence（本地隔离夹具验证）**：启动一个仅绑定环回地址的控制面实例（支持 `--runtime=node`  分别验证 Node.js/SQLite 与个人服务器 Node 24/`node:sqlite`）、两个有独立状态目录的 `unified_router.py` 进程和一台本地 TLS 抓包代理服务器。其中平台写请求发往本地 Proxy stub（直接返回 `201 {"id":1}`），**仅证明协议线序 `< 2,000` B 与双运行时控制面闭环，不替代真实外部平台写回或线上生产部署验收**。给联调脚本传入一个尚不存在的目录，可保留服务日志和数据便于排障。

> **2026-11-23 PR #44 隔离夹具双运行时实测**：`python3 control-center/tests/integration/run.py --runtime=node`  均全绿通过——GitHub Actions Linux CI（Run `45134291763`）实测 `node` TLS 集成请求，本机 macOS 实测 `node` TLS 集成请求（总请求数随实时心跳轮询时序在 ~79–86 间小幅波动，均含 8 次本地 stub 平台写请求，确定性峰值 1,795 B `< 2,000` B），覆盖 INT-01/03 公开与管理员隔离、INT-07 连续三次插队、INT-08 配置发布与重启应用、INT-02/05 90s 离线恢复与 20+ 事件补发。此为 **isolated fixture evidence**，不替代真实外部平台或线上生产验收。

| 验收项（v1.1 基线证据） | 自动化证据与待补齐缺口 |
|---|---|
| INT-01 | `auth.test.ts`、`access-matrix.test.ts`、`masking.test.ts` 和联调的匿名/登录 HTTP 响应（✅ v1.1 通过） |
| INT-02 | `worker-ingest.test.ts`、`test_control_client.py` 和双进程联调，覆盖去重、跳号重建、分片重试、本地日志与 503 后补发（✅ v1.1 通过） |
| INT-03 | `test_idle_probe.py`、`device-state.test.ts` 和 Blue 线上设备组状态（✅ REQ-15 全量探测、轮次持久化、健康投影与 `last_online_at` 已实现；⏳ 完整 5 次实机失败/恢复演练待补） |
| INT-04 | `schedule-projection.test.ts` 和 `test_queue_telemetry.py`（⏳ 待从同一 SQLite 数据集核验排除失败及不可靠样本后的 API 响应） |
| INT-05 | `test_upload_intercept.py` 实际 HTTP/1.1 序列化、静态路径注册门禁，以及 `run.py` 本地隔离夹具 TLS 抓包（✅ CI 与本机 Node 24 集成请求峰值 1,795 B `< 2,000` B；含 8 次本地 stub 平台写请求，真实 Yellow 外发证据另见 `REL-01`） |
| INT-06 | `cancellations.test.ts` 使用模拟平台核验 App 令牌、attempt 校验、幂等和强制取消（⏳ 待补齐双平台、双 Worker 在网站断连时的整次取消联调） |
| INT-07 | `move-up.test.ts`、`test_move_up.py`、`test_smoke_queue.py`（⏳ **v1.5 需按 `REQ-17` 升级为 Workflow 级跨区插队联调，并补齐虚拟时钟 71h59m/72h01m 超时实测**） |
| INT-08 | `cluster-config.test.ts`（新增 DRAIN→RESUME、删除前四项门禁与 `enabled: false` 断言）、`test_preflight_export.py`、`test_pools_hot_reload.py`（⏳ **仍需补齐 Worker 侧排空取消、双 Worker 单端失败、实机重启与自动更新闭环**） |
| INT-09 | `retention-storage.test.ts` 和 `board-read.test.ts`（⏳ 待补齐同一次配额熔断触发 Worker 暂存恢复，并按 `REQ-16` 升级为 50 Workflow 跨 30 天分页验证） |
| INT-10 | `node-runtime.test.ts`（4 层防锁死扫描 + 反向自证 + Node 24/SQLite 对等 + `CONTROL_CENTER_DOMAIN`/`CONTROL_CENTER_ORIGIN` 强校验注入与代理实测）、直传原始 `SQLiteDatabase` binding 的 `node-runtime.test.ts`、`scripts/start-node-server.mjs --require-origin` 与 `run.py`（✅ 本地与隔离夹具对等子集全绿；⏳ 真实线上 personal server SQLite 部署与真实主机切至 Node 24 自建实例验收仍待现场执行，见 `DESIGN.md` `INT-10` / `E2E-12` / `REL-00`） |

lite-actions.ooguy.com 实机能力核验、真实硬件、真实平台测试仓及在线免费套餐的验收对应 `REL-00` 至 `REL-04`。浏览器端验收依赖正式前端集成（`FE-01` ~ `FE-05`）。

## 初始化控制面与多托管部署

在 `control-center/worker` 中应用迁移，并从仓库 Pools 配置初始化版本 1。种子脚本可重复运行，后续配置由网站发布。

```sh
# 本地 Node.js / SQLite 开发
node control-center/worker/scripts/seed.mjs --sqlite /var/lib/lite-actions/control-center.sqlite

# 初期 lite-actions.ooguy.com 前端与 API 同源部署打包（决策 2-A：必须注入非空 CONTROL_CENTER_DOMAIN 与运行时上游地址 CONTROL_CENTER_ORIGIN）
CONTROL_CENTER_DOMAIN=lite-actions.ooguy.com \
  CONTROL_CENTER_ORIGIN=https://lite-actions.ooguy.com \
  node control-center/worker/scripts/start-node-server.mjs --require-origin
# 产出 control-center/web/dist/ 与 control-center/deploy/Caddyfile.personal-server.example（Node.js 24 + SQLite）

# 后期迁移个人服务器（Node.js 24 + 内置 node:sqlite，零第三方原生依赖）
npm run seed --prefix control-center/worker -- --sqlite /var/lib/lite-actions/control-center.sqlite
npm run build:node --prefix control-center/worker
DB_PATH=/var/lib/lite-actions/control-center.sqlite \
  STATIC_DIR=control-center/web/dist \
  TRUST_PROXY_HEADERS=1 \
  node --experimental-strip-types control-center/worker/scripts/start-node-server.mjs
```

部署初期后端（Node.js 24 + SQLite）时，应用远端迁移与种子（另提供 `.github/workflows/deploy-control-center.yml` 一键执行远端 SQLite 迁移、Worker 部署及 前端与 API 同源部署打包核验），lite-actions.ooguy.com 前端运行时注入 `CONTROL_CENTER_ORIGIN` 代理同源 `/api/v1/*`；迁移至个人服务器时使用 `seed.mjs --sqlite <path>` 初始化本地 SQLite，通过 `scripts/start-node-server.mjs`（或 `control-center/deploy/Dockerfile`）启动并配 Caddy/Nginx HTTPS 反代。两种后端方式均通过环境变量 / 服务端 Secret 配置相同凭据：`KEY_PEPPER`、`SUPER_SECRET`、`WORKER_BLUE_KEY_HASH`、`WORKER_YELLOW_KEY_HASH`（使用相同 pepper 做 HMAC-SHA256 保存为小写十六进制）、`GITHUB_APP_ID`、`GITHUB_APP_PRIVATE_KEY` 和 `GITCODE_CANCEL_TOKEN`。

通过 Worker 现有部署配置为选定区域添加 `control_center.url`，并通过 Worker 环境注入 `LITE_ACTIONS_CONTROL_TOKEN`（托管从 lite-actions.ooguy.com 迁至个人服务器时，Worker 仅需更新 `control_center.url`，零代码修改）。Yellow 可配置 `control_center.ci_reserve_bytes` 为 CI 回报保留每日额度。现场若必须经代理，额外在 root:root、0600 的 `deployment.json` 本地增加 `control_center.proxy`，格式使用占位符 `http://<proxy-user>:<proxy-password>@<proxy-host>:<proxy-port>`；客户端不会读取环境代理，日志和异常不输出完整 URL。机器接口注册在 `/api/v1/worker`，健康检查与公开视图可调用 `/api/v1/public/overview`。接口字段见 [API 契约](api-contract.md)。
