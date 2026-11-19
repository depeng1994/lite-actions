# 后端验收与部署

实现以 v1.1 详细设计为基线。控制面运行在 Cloudflare Worker，使用一个 D1 库。Blue 和 Yellow Worker 仅通过 HTTPS 出站接入。

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
python3 control-center/tests/integration/run.py
```

联调启动一个仅绑定环回地址的 Wrangler/D1 实例、两个有独立状态目录的 `unified_router.py` 进程和一台临时 TLS 抓包服务器。它使用测试平台响应、测试地址和模拟巡检。给联调脚本传入一个尚不存在的目录，可保留服务日志和 D1 数据便于排障。

| 验收项 | 自动化证据 |
|---|---|
| INT-01 | `auth.test.ts`、`access-matrix.test.ts`、`masking.test.ts` 和联调的匿名/登录 HTTP 响应 |
| INT-02 | `worker-ingest.test.ts`、`test_control_client.py` 和双进程联调，覆盖去重、跳号重建、分片重试、本地日志与 503 后补发 |
| INT-03 | `test_idle_probe.py`、`device-state.test.ts` 和联调中发布的设备组状态 |
| INT-04 | `schedule-projection.test.ts` 和 `test_queue_telemetry.py` |
| INT-05 | `test_upload_intercept.py` 实际 HTTP/1.1 序列化、静态路径注册门禁，以及 TLS 联调抓取两个 Worker 和两个平台客户端的请求 |
| INT-06 | `cancellations.test.ts` 使用模拟平台核验 App 令牌、attempt 校验、幂等和强制取消。现有 Workflow 测试验证 Worker 取消、旧 attempt 和清理语义 |
| INT-07 | `move-up.test.ts`、`test_move_up.py`、`test_smoke_queue.py` 及联调连续三次实际取令与回执 |
| INT-08 | `cluster-config.test.ts`、`test_preflight_export.py`、`test_pools_hot_reload.py` 和联调实际发布、拉取、应用及本地重启加载 |
| INT-09 | `retention-storage.test.ts` 和 `board-read.test.ts`，包括本地 D1 `meta.size_after` 水位实测及配额熔断模拟 |

真实硬件、真实平台测试仓及在线免费套餐的验收对应 `REL-01` 至 `REL-03`。浏览器端验收依赖前端验收项。

## 初始化控制面

在 `control-center/worker` 中应用迁移，并从仓库 Pools 配置初始化版本 1。种子脚本可重复运行，后续配置由网站发布。

```sh
npx wrangler d1 migrations apply DB --local
npm run seed -- --local
npm run dev
```

部署到 Cloudflare 时，将 Wrangler 中的占位 ID 替换为目标 D1 库 ID，并对目标库应用远端迁移与种子。通过 Cloudflare Secrets 配置 `KEY_PEPPER`、`SUPER_SECRET` 及 `WORKER_BLUE_KEY_HASH`、`WORKER_YELLOW_KEY_HASH`。机器密钥哈希使用相同 pepper 做 HMAC-SHA256 并保存为小写十六进制。双平台取消凭据为 `GITHUB_APP_ID`、`GITHUB_APP_PRIVATE_KEY` 和 `GITCODE_CANCEL_TOKEN`。

通过 Worker 现有部署配置为选定区域添加 `control_center.url`，并通过 Worker 环境注入 `LITE_ACTIONS_CONTROL_TOKEN`。Yellow 可配置 `control_center.ci_reserve_bytes` 为 CI 回报保留每日额度。机器接口注册在 `/api/v1/worker`，健康检查与公开视图可调用 `/api/v1/public/overview`。接口字段见 [API 契约](api-contract.md)。
