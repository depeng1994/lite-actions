# Prototype Instructions

Run the local server yourself and open the preview in the browser available to this environment. Do not give the user server-start instructions when you can run it.

Before making substantial visual changes, use the Product Design plugin's `get-context` skill when the visual source is unclear or no longer matches the current goal. When the user gives durable prototype-specific design feedback, preferences, or decisions, record them in `AGENTS.md`.

When implementing from a selected generated mock, treat that image as the source of truth for layout, component anatomy, density, spacing, color, typography, visible content, and hierarchy.

Build app UI in `src/`. Keep `.openai/hosting.json`, `worker/index.js`, `scripts/prepare-sites-build.mjs`, and `tests/sites-worker.test.mjs` intact so the same local prototype can be handed to Sites. Before a Sites handoff, run `npm run build` and `npm run test:sites`; the build must leave `dist/client/index.html`, `dist/server/index.js`, and `dist/.openai/hosting.json`.

正式前端采用第 3 张设计稿的上方 Pools 时间轴与下方排队用例布局，支持浅色和深色主题。任务、IP、集群健康和管理回执全部来自控制面 API，不保留本地模拟数据。页面权限和管理语义遵循控制中心 DESIGN.md。

2026-11-19 批注更新：同一 16P 租约按设备组逐行绘制；模型仓用统一的 Workflow 表格展示活动及历史；插队入口位于 Workflow 行并展示各设备队列的控制面回执。

队列视图支持逐列排序与筛选，Workflow 插队每次和前一位交换。设备在线状态与暂停调度独立展示。

打开历史后，负数记录在当前执行和等待队列上方，最近的历史 `-1` 紧邻当前执行的 `0`；等待队列从 `1` 起。

模型仓历史可折叠并恢复已加载记录；调度结果独立于 Actions ID，子用例保留实际阶段与耗时。
