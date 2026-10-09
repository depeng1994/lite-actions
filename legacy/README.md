# 迁移保留（仅执行适配）

仅保留 A3 8P/16P 远程启动适配；日志摘取统一由调度机的 `ci_core/logs.py` 完成，GitHub 结果封装/POST 由 `ci_core/reporting.py` 完成。
旧 Agent、旧 Waiter、旧 Workflow 与启动脚本已删除。
模型训练验证代码继续由 torchtitan-npu 仓库维护。
