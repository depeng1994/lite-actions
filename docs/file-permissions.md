# Worker 文件权限统一策略

Blue 和 Yellow 都从 `unified_router.py` 启动，共用 `ci_core` 的日志、状态和执行器实现，不按区域维护两套权限配置。

`ci_core/permissions.py` 是 Worker、自动更新器及安装脚本的权限定义入口。Python 使用命名常量；`scripts/install_auto_update.sh` 通过 `python -m ci_core.permissions` 读取同一组值，不复制一套 shell 数字。

| 用途 | 常量 | 默认值 |
| --- | --- | --- |
| 非秘密诊断日志、状态文件 | `DIAGNOSTIC_FILE_MODE` | `0644` |
| 诊断目录 | `DIAGNOSTIC_DIR_MODE` | `0755` |
| Worker 进程默认掩码 | `WORKER_UMASK` | `0022` |
| 凭据、私有事务文件 | `PRIVATE_FILE_MODE` | `0600` |
| 私有暂存、隔离状态目录 | `PRIVATE_DIR_MODE` | `0700` |
| 安装及服务启动前的私有默认掩码 | `PRIVATE_UMASK` | `0077` |
| 可执行源码文件 | `EXECUTABLE_FILE_MODE` | `0755` |
| 仅允许穿越的隔离父目录 | `TRAVERSE_DIR_MODE` | `0711` |
| 使用既有对端身份校验的动态用户 Broker socket | `BROKER_SOCKET_MODE` | `0666` |

状态写入后显式设置文件权限，避免 `mkstemp` 的 `0600` 或重写历史私有文件导致诊断不可读。case 目录复用时恢复 `0755`。activity lock 与 ready 文件共享的父目录按诊断目录创建；安装器也按诊断目录权限创建管理日志路径；更新器写日志时恢复直接父目录的诊断权限，避免其私有 umask 遮蔽新日志。

凭据、更新事务、锁目录以及 DynamicUser Test Worker 的私有状态目录不因诊断需求而放宽。Broker 的 socket 模式选择和对端身份校验保持原逻辑。`0644` 不赋予其他本机用户修改文件的权限。

systemd 的 `UMask` 是静态声明，保留私有启动值；`scripts/lint.py` 校验它与共享 `PRIVATE_UMASK` 一致。进入 Worker 的 `main()` 后统一选用 `WORKER_UMASK`。同一 lint 检查阻止在 `ci_core`、`auto_update` 和 Worker 入口重新写入散落的八进制权限值。独立部署的 Control Center 前端发布程序不依赖 Worker 包，不在这次 Worker 权限变更范围内。

测试中的数值是独立验收标准，刻意不引用被测常量，以免改错常量时测试同步变绿。回归覆盖两区入口、ready/control 文件新建与替换、其余状态发布器、取消任务的 pipeline/runtime 文件；以 root 运行时另降权为 `nobody` 实读并检查不可写。

本变更不执行历史目录递归 chmod。既有上层目录（例如 `/root`）仍需具备相应穿越权限，单个文件的 `0644` 不会绕过上层目录或操作系统访问控制。
