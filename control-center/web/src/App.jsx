import { Moon, Sun } from "@phosphor-icons/react";
import { useEffect, useEffectEvent, useRef, useState } from "react";
import liteActionsIcon from "./assets/lite-actions-icon.png";
import { ClusterView, PoolsView, UsersView } from "./Views.jsx";
import { ModelsView } from "./ModelsView.jsx";
import {
  AddNodeDialog, buildClusterOperation, CancelDialog, CancellationReceipt, CaseDialog, ConfigDialog, DeleteUserDialog, ExpediteDialog,
  HistoryDialog, LoginForm, NewUserDialog, PriorityReceipt, ResourceDetail, ResetUserKeyDialog, UserKeyDialog,
} from "./Dialogs.jsx";
import { dateTimeSecondFormatter, initialCases, initialNodes, initialQueueOrder, poolId, repositories, runs, timelineRuns, workflowKey } from "./formal-data.js";
import { currentWorkflowQueue, initialWorkflowHistorySize, queuedChildren, workflowHistoryPageSize } from "./workflows.js";
import {
  cancelWorkflow as apiCancelWorkflow,
  cancellationDetail as apiCancellationDetail,
  clusterPreview,
  clusterPublish,
  clusterRollback,
  createUser as apiCreateUser,
  deleteUser as apiDeleteUser,
  getSession,
  listAudits,
  listUsers,
  loadControlCenter,
  loadMoreHistory as apiLoadMoreHistory,
  login as apiLogin,
  logout as apiLogout,
  moveWorkflowUp as apiMoveWorkflowUp,
  priorityOpDetail as apiPriorityOpDetail,
  resetUserKey as apiResetUserKey,
} from "./control-api.js";

const pages = [["pools", "Pools任务看板"], ["models", "代码仓任务看板"], ["cluster", "集群管理"]];
const defaultModel = "";
const initialConfig = { desired: 0, applied: {}, flags: {}, pending: null };

function readRoute() {
  const [, page, model] = window.location.hash.split("/");
  return {
    page: [...pages.map(([id]) => id), "users"].includes(page) ? page : "pools",
    model: repositories.some(({ name }) => name === model) ? model : defaultModel,
  };
}

function useTheme() {
  const [theme, setTheme] = useState(() => localStorage.getItem("lite-actions-theme") === "dark" ? "dark" : "light");
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    document.querySelector('meta[name="theme-color"]').content = theme === "dark" ? "#15191d" : "#f7f6f4";
    localStorage.setItem("lite-actions-theme", theme);
  }, [theme]);
  return [theme, setTheme];
}

function mapAuditRecord(record) {
  const area = record.target_type === "user" ? "users"
    : record.target_type === "configuration" || record.target_type === "storage" ? "cluster"
      : "workflow";
  return {
    id: String(record.audit_id),
    actor: record.actor_username,
    time: record.created_at ? dateTimeSecondFormatter.format(record.created_at * 1000) : "—",
    area,
    action: record.action,
    target: record.target_id,
    result: record.error_msg ? `${record.result} (${record.error_msg})` : record.result,
  };
}

function Header({ page, session, theme, setTheme, open, logout, live }) {
  const dark = theme === "dark";
  const nav = session?.role === "Super" ? [...pages, ["users", "用户管理"]] : pages;
  const ThemeIcon = dark ? Sun : Moon;
  return <header className="app-header">
    <a href="#/pools" className="wordmark" aria-label="Lite Actions 首页">
      <img className="wordmark-icon" src={liteActionsIcon} alt="" width="30" height="30" />
      <span>Lite Actions</span>
    </a>
    <nav className="top-nav" aria-label="主导航">
      {nav.map(([id, label]) => <a aria-current={id === page ? "page" : undefined} key={id} href={`#/${id}`}>{label}</a>)}
    </nav>
    <div className="header-actions">
      <span className={`live-indicator ${live ? "is-live" : ""}`} title={live ? "已连接控制面 API，30 秒自动刷新" : "正在连接控制面 API"}>{live ? "实时数据" : "连接中"}</span>
      <button className="icon-button theme-button" aria-label={dark ? "切换浅色主题" : "切换深色主题"}
        title={dark ? "切换浅色主题" : "切换深色主题"} onClick={() => setTheme(dark ? "light" : "dark")}>
        <ThemeIcon size={20} weight="regular" />
      </button>
      {session ? <div className="session-area"><span title={session.name}>{session.role === "Super" ? "Super" : session.name}</span>
        <button className="header-button" onClick={logout}>退出登录</button></div>
        : <button className="header-button" onClick={() => open({ type: "login" })}>登录</button>}
    </div>
  </header>;
}

export function App() {
  const [route, setRoute] = useState(readRoute);
  const [theme, setTheme] = useTheme();
  const [nodes, setNodes] = useState(initialNodes);
  const [clusterNodes, setClusterNodes] = useState(initialNodes);
  const [clusterPools, setClusterPools] = useState([]);
  const [clusterHistory, setClusterHistory] = useState([]);
  const [cases, setCases] = useState(initialCases);
  const [timelineCases, setTimelineCases] = useState(initialCases);
  const [workflowOrder, setWorkflowOrder] = useState(initialQueueOrder);
  const [queueVersion, setQueueVersion] = useState(0);
  const [workflowHistory, setWorkflowHistory] = useState(null);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [truncatedByCapacity, setTruncatedByCapacity] = useState(false);
  const [purgedNotice, setPurgedNotice] = useState(null);
  const [accounts, setAccounts] = useState([]);
  const [signedIn, setSignedIn] = useState(null);
  const [audit, setAudit] = useState([]);
  const [config, setConfig] = useState(initialConfig);
  const [cancellations, setCancellations] = useState({});
  const [priorities, setPriorities] = useState({});
  const [modal, setModal] = useState(null);
  const [message, setMessage] = useState("");
  const [dataMode, setDataMode] = useState("loading");
  const toastTimer = useRef(0);
  const operationTimers = useRef(new Set());
  const historyRequest = useRef(null);

  const session = signedIn && accounts.find(({ name }) => name === signedIn);
  const isSuper = session?.role === "Super";
  const canManage = Boolean(session);
  const access = signedIn ? "admin" : "public";
  const historyPage = workflowHistory?.access === access ? workflowHistory : null;
  const history = historyPage?.runs ?? [];

  const applySnapshot = (snapshot) => {
    if (!snapshot) return;
    setNodes(snapshot.nodes);
    setClusterNodes(snapshot.clusterNodes);
    setClusterPools(snapshot.clusterPools || []);
    setClusterHistory(snapshot.clusterHistory || []);
    setCases(snapshot.cases);
    setTimelineCases(snapshot.timelineCases || snapshot.cases);
    setWorkflowOrder(snapshot.workflowOrder);
    setQueueVersion(snapshot.queueVersion || 0);
    setTruncatedByCapacity(Boolean(snapshot.truncatedByCapacity));
    setPurgedNotice(snapshot.purgedNotice || null);
    if (snapshot.clusterConfig) setConfig(snapshot.clusterConfig);
    setDataMode("live");
  };

  const refreshAudits = () => {
    if (!signedIn) return;
    listAudits(50)
      .then((payload) => { if (payload?.records) setAudit(payload.records.map(mapAuditRecord)); })
      .catch(() => undefined);
  };

  useEffect(() => {
    let cancelled = false;
    const refresh = async () => {
      const snapshot = await loadControlCenter(signedIn ? "admin" : "public");
      if (cancelled) return;
      if (!snapshot) { setDataMode("offline"); return; }
      applySnapshot(snapshot);
    };
    const refreshWhenVisible = () => { if (!document.hidden) refresh(); };
    refreshWhenVisible();
    const timer = setInterval(refreshWhenVisible, 30_000);
    document.addEventListener("visibilitychange", refreshWhenVisible);
    return () => {
      cancelled = true;
      clearInterval(timer);
      document.removeEventListener("visibilitychange", refreshWhenVisible);
    };
  }, [signedIn]);

  useEffect(() => {
    setWorkflowHistory(null);
    setHistoryLoading(false);
    return () => {
      historyRequest.current?.abort();
      historyRequest.current = null;
    };
  }, [access, route.page]);

  const loadInitialHistory = useEffectEvent(() => {
    if (!historyPage) void loadMoreHistory();
  });
  useEffect(() => {
    if (route.page === "models" && dataMode === "live") loadInitialHistory();
  }, [access, dataMode, route.page]);

  useEffect(() => {
    if (!canManage || dataMode !== "live") {
      if (!canManage) setAudit([]);
      return undefined;
    }
    let cancelled = false;
    listAudits(50).then((payload) => {
      if (!cancelled && payload?.records) setAudit(payload.records.map(mapAuditRecord));
    }).catch(() => undefined);
    return () => { cancelled = true; };
  }, [canManage, dataMode, signedIn]);

  useEffect(() => {
    let cancelled = false;
    getSession().then((remote) => {
      if (!cancelled && remote?.authenticated) {
        setAccounts((previous) => previous.some((account) => account.name === remote.username) ? previous : [...previous, {name: remote.username, role: remote.role === "super" ? "Super" : "管理员", key: "", userId: remote.user_id}]);
        setSignedIn(remote.username);
      }
    });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (!isSuper || dataMode !== "live") return undefined;
    let cancelled = false;
    listUsers().then((payload) => {
      if (cancelled || !payload?.users) return;
      setAccounts((previous) => {
        const superAccount = previous.find((account) => account.role === "Super") || {name: signedIn, role: "Super", key: ""};
        return [superAccount, ...payload.users.map((user) => ({name: user.username, userId: user.user_id, role: "管理员", created: new Date(user.created_at * 1000).toLocaleString("zh-CN", {timeZone: "Asia/Shanghai"}), key: ""}))];
      });
    }).catch(() => undefined);
    return () => { cancelled = true; };
  }, [isSuper, dataMode, signedIn]);

  useEffect(() => {
    const onRoute = () => {
      setRoute(readRoute());
      setModal(null);
      window.scrollTo(0, 0);
    };
    window.addEventListener("hashchange", onRoute);
    const timers = operationTimers.current;
    return () => {
      window.removeEventListener("hashchange", onRoute);
      clearTimeout(toastTimer.current);
      timers.forEach(clearTimeout);
    };
  }, []);

  const open = setModal;
  const close = () => setModal(null);
  const notify = (note) => {
    clearTimeout(toastTimer.current);
    setMessage(note);
    toastTimer.current = setTimeout(() => setMessage(""), 4700);
  };
  const later = (callback, delay) => {
    const timer = setTimeout(() => {
      operationTimers.current.delete(timer);
      callback();
    }, delay);
    operationTimers.current.add(timer);
  };
  const go = (page, model) => {
    const destination = `#/${page}${model ? `/${model}` : ""}`;
    if (window.location.hash !== destination) window.location.hash = destination;
  };
  const login = async (name, key) => {
    if (dataMode !== "live") { notify("控制面尚未连接"); return false; }
    try {
      const remote = await apiLogin(name, key);
      if (!remote?.authenticated) return {ok: false};
      setAccounts((previous) => previous.some((account) => account.name === remote.username) ? previous : [...previous, {name: remote.username, role: remote.role === "super" ? "Super" : "管理员", key: "", userId: remote.user_id}]);
      setSignedIn(remote.username);
      close();
      notify(`已登录为 ${remote.role === "super" ? "Super" : remote.username}`);
      return {ok: true};
    } catch (error) {
      if (error.code === "WRITE_QUOTA_EXHAUSTED") return {ok: false, message: error.message};
      notify(error.message);
      return {ok: false};
    }
  };
  const logout = async () => {
    if (dataMode === "live") await apiLogout().catch(() => undefined);
    setSignedIn(null);
    close();
    if (route.page === "users") go("pools");
    notify("已退出登录");
  };

  const queue = currentWorkflowQueue(cases, workflowOrder);

  const loadMoreHistory = async () => {
    if (dataMode !== "live" || historyRequest.current || (historyPage && !historyPage.cursor)) return false;
    const controller = new AbortController();
    historyRequest.current = controller;
    setHistoryLoading(true);
    try {
      const page = await apiLoadMoreHistory(access, {
        cursor: historyPage?.cursor,
        limit: historyPage ? workflowHistoryPageSize : initialWorkflowHistorySize,
        signal: controller.signal,
      });
      if (controller.signal.aborted) return false;
      const existing = new Set(history.map(workflowKey));
      setWorkflowHistory({...page, access, runs: [...history, ...page.runs.filter((run) => !existing.has(workflowKey(run)))]});
      setTruncatedByCapacity(Boolean(page.truncatedByCapacity));
      setPurgedNotice(page.purgedNotice || null);
      return true;
    } catch (error) {
      if (!controller.signal.aborted) notify(error.message);
      return false;
    } finally {
      if (historyRequest.current === controller) {
        historyRequest.current = null;
        setHistoryLoading(false);
      }
    }
  };

  const pollPriorityOp = async (run, opId, attempts = 0) => {
    const key = workflowKey(run);
    try {
      const detail = await apiPriorityOpDetail(opId);
      const terminal = ["APPLIED", "NOOP", "CONFLICT", "FAILED"].includes(detail.status);
      setPriorities((previous) => ({
        ...previous,
        [key]: {
          opId: detail.op_id,
          status: detail.status,
          from: detail.from_seq,
          to: detail.to_seq,
          commands: detail.commands || [],
        },
      }));
      if (terminal || attempts >= 40) {
        const snapshot = await loadControlCenter(access);
        applySnapshot(snapshot);
        refreshAudits();
      } else {
        later(() => pollPriorityOp(run, opId, attempts + 1), 2000);
      }
    } catch {
      if (attempts < 40) later(() => pollPriorityOp(run, opId, attempts + 1), 3000);
    }
  };

  const expedite = async (run) => {
    const key = workflowKey(run);
    if (!canManage || dataMode !== "live" || cancellations[key]) return;
    let response;
    try {
      response = await apiMoveWorkflowUp(run, queueVersion);
    } catch (error) {
      if (error.status === 409) {
        const snapshot = await loadControlCenter(access);
        applySnapshot(snapshot);
        notify("队列版本或状态已更新，已刷新正式顺序");
      } else notify(error.message);
      return;
    }
    if (response?.status === "NOOP") {
      notify("该 Workflow 已在等待队首");
      close();
      return;
    }
    const receipt = {
      opId: response.op_id,
      status: response.status || "PENDING",
      from: response.from_seq,
      to: response.to_seq,
      commands: response.commands || [],
    };
    if (response.queue_version_after != null) setQueueVersion(response.queue_version_after);
    setPriorities((previous) => ({ ...previous, [key]: receipt }));
    open({ type: "priority-receipt", runKey: key });
    const snapshot = await loadControlCenter(access);
    applySnapshot(snapshot);
    refreshAudits();
    if (response.op_id && response.status === "PENDING") {
      later(() => pollPriorityOp(run, response.op_id, 0), 1500);
    }
  };

  const cancelRun = async (run) => {
    if (!canManage || dataMode !== "live" || cancellations[workflowKey(run)]) return;
    const affected = cases.filter((item) => item.run === workflowKey(run) && ["排队中", "运行中", "准备中", "清理中"].includes(item.status));
    if (!affected.length) return;
    try {
      const cancellation = await apiCancelWorkflow(run);
      const update = (fields) => setCancellations((previous) => ({...previous, [workflowKey(run)]: {...previous[workflowKey(run)], ...fields}}));
      update({cancelId: cancellation?.cancel_id, platform: cancellation?.status || "已提交取消", done: ["COMPLETED", "FAILED"].includes(cancellation?.status), release: "等待控制面回执", workers: {Blue: "等待受理", Yellow: "等待受理"}});
      open({type: "cancel-receipt", runKey: workflowKey(run)});
      notify("取消请求已提交到控制面");
      refreshAudits();
      if (cancellation?.cancel_id) pollCancellation(run, cancellation.cancel_id);
    } catch (error) { notify(error.message); }
  };

  const pollCancellation = async (run, cancelId) => {
    try {
      const detail = await apiCancellationDetail(cancelId);
      const workers = {Blue: "已停止", Yellow: "已停止"};
      for (const item of detail.cases || []) {
        const worker = item.worker_id === "blue" ? "Blue" : "Yellow";
        if (item.phase) workers[worker] = "等待清理";
      }
      const done = ["COMPLETED", "COMPLETED_WITH_QUARANTINE", "FAILED"].includes(detail.status);
      setCancellations((previous) => ({...previous, [workflowKey(run)]: {
        ...previous[workflowKey(run)], cancelId, platform: detail.status, done,
        workers, release: detail.status === "COMPLETED_WITH_QUARANTINE" ? "清理失败，已隔离" : done ? "设备已释放" : "等待设备释放",
      }}));
      if (!done) later(() => pollCancellation(run, cancelId), 3000);
    } catch { later(() => pollCancellation(run, cancelId), 5000); }
  };

  const publishConfig = async (target, action, addedNode, precomputedHash = null) => {
    if (!canManage || dataMode !== "live" || config.pending) return;
    const operation = addedNode
      ? {op: "ADD_NODE", pool: addedNode.pool, value: addedNode.value}
      : buildClusterOperation(target, action);
    setConfig((previous) => ({...previous, pending: target.worker}));
    close();
    try {
      let previewHash = precomputedHash;
      if (!previewHash) {
        const preview = await clusterPreview(config.desired, operation);
        if (preview.blocking_reasons?.length) {
          setConfig((previous) => ({...previous, pending: null}));
          notify(`控制面拒绝${action}：${preview.blocking_reasons.join("、")}`);
          return;
        }
        previewHash = preview.preview_hash;
      }
      const sync = await clusterPublish(config.desired, operation, previewHash);
      setConfig((previous) => ({...previous, desired: sync.revision, pending: null,
        workerStatus: {...previous.workerStatus, [target.worker]: {...previous.workerStatus?.[target.worker], desired: sync.revision}},
      }));
      notify(`配置 r${sync.revision} 已提交，等待 Worker 应用`);
      const snapshot = await loadControlCenter(access);
      applySnapshot(snapshot);
      refreshAudits();
    } catch (error) {
      setConfig((previous) => ({...previous, pending: null}));
      notify(error.message);
    }
  };

  const rollbackCluster = async (targetRevision) => {
    if (!canManage || dataMode !== "live" || config.pending || targetRevision === config.desired) return;
    setConfig((previous) => ({...previous, pending: "rollback"}));
    try {
      const sync = await clusterRollback(config.desired, targetRevision);
      setConfig((previous) => ({...previous, desired: sync.revision, pending: null}));
      notify(`已基于 r${targetRevision} 发布配置 r${sync.revision}`);
      const snapshot = await loadControlCenter(access);
      applySnapshot(snapshot);
      refreshAudits();
    } catch (error) {
      setConfig((previous) => ({...previous, pending: null}));
      notify(error.message);
    }
  };

  const createUser = async (name) => {
    if (!isSuper || accounts.some((account) => account.name === name)) return;
    if (dataMode !== "live") { notify("控制面尚未连接"); return; }
    let account;
    try {
      const issued = await apiCreateUser(name);
      account = {name: issued.username, userId: issued.user_id, role: "管理员", key: issued.secret_once, created: new Date().toLocaleString("zh-CN", {timeZone: "Asia/Shanghai"})};
    } catch (error) { notify(error.message); return; }
    setAccounts((previous) => [...previous, account]);
    refreshAudits();
    open({ type: "user-key", name });
  };

  const deleteUser = async (name) => {
    if (!isSuper || !accounts.some((account) => account.name === name && account.role === "管理员")) return;
    const account = accounts.find((item) => item.name === name);
    if (dataMode === "live" && account?.userId) {
      try { await apiDeleteUser(account.userId); } catch (error) { notify(error.message); return; }
    }
    setAccounts((previous) => previous.filter((account) => account.name !== name));
    refreshAudits();
    close();
    notify(`管理员 ${name} 已删除`);
  };

  const resetUserKey = async (name) => {
    if (!isSuper || dataMode !== "live") return;
    const account = accounts.find((item) => item.name === name);
    if (!account?.userId) return;
    try {
      const issued = await apiResetUserKey(account.userId);
      setAccounts((previous) => previous.map((item) => item.name === name ? {...item, key: issued.secret_once} : item));
      refreshAudits();
      open({type: "user-key", name});
    } catch (error) { notify(error.message); }
  };

  const viewProps = {
    nodes, clusterPools, clusterHistory, cases, timelineCases, config, queueVersion,
    history, historyLoading, historyLoaded: Boolean(historyPage), historyHasMore: Boolean(historyPage?.hasMore),
    historyTotal: historyPage?.historyTotal ?? 0, truncatedByCapacity, purgedNotice, loadMoreHistory,
    rollbackCluster, canManage, access, open, go, audit, live: dataMode === "live",
  };
  const item = modal?.caseId && cases.find(({ id }) => id === modal.caseId);
  const run = modal?.runKey && runs.find((candidate) => workflowKey(candidate) === modal.runKey);
  const historicalRun = modal?.historyId && [...history, ...timelineRuns].find((candidate) => workflowKey(candidate) === modal.runKey);
  const historicalCase = historicalRun && historicalRun.cases.find(({ historyId }) => historyId === modal.historyId);
  const user = modal?.name && accounts.find(({ name }) => name === modal.name);
  const dialogProps = { ...viewProps, nodes: route.page === "cluster" ? clusterNodes : nodes, close };
  const renderResourceDetail = (detail) => <ResourceDetail {...detail} cases={cases} config={config} go={go} />;

  return <>
    <Header page={route.page} open={open} session={session} theme={theme} setTheme={setTheme} logout={logout} live={dataMode === "live"} />
    {route.page === "pools" && <PoolsView {...viewProps} renderResourceDetail={renderResourceDetail} renderTimelineDetail={(selected, selectedRun, closeDetail) => {
      const panelProps = {item: selected, run: selectedRun, access, live: viewProps.live, close: closeDetail, inline: true};
      return selected.historyId
        ? <HistoryDialog key={selected.id} {...panelProps} nodes={nodes} />
        : <CaseDialog key={selected.id} {...panelProps} nodes={nodes} config={config} go={go} />;
    }} />}
    {route.page === "models" && <ModelsView key={route.model} {...viewProps} model={route.model} cancellations={cancellations} priorities={priorities} queue={queue} />}
    {route.page === "cluster" && <ClusterView {...viewProps} nodes={clusterNodes} renderResourceDetail={renderResourceDetail} />}
    {route.page === "users" && (isSuper
      ? <UsersView accounts={accounts} open={open} audit={audit} />
      : <main className="standard-view"><h1>用户管理</h1><p className="subtle">此页面需要 Super 登录。</p>
        {!canManage && <button className="button" onClick={() => open({ type: "login" })}>登录</button>}</main>)}

    {modal?.type === "login" && <LoginForm close={close} login={login} />}
    {modal?.type === "case" && item && <CaseDialog item={item} {...dialogProps} />}
    {modal?.type === "history" && historicalCase && <HistoryDialog run={historicalRun} item={historicalCase} nodes={nodes} access={access} live={dataMode === "live"} close={close} />}
    {modal?.type === "cancel" && canManage && run && <CancelDialog run={run} cases={cases} close={close} confirm={() => cancelRun(run)} />}
    {modal?.type === "cancel-receipt" && run && cancellations[workflowKey(run)] && <CancellationReceipt run={run} receipt={cancellations[workflowKey(run)]} close={close} />}
    {modal?.type === "expedite" && canManage && run && <ExpediteDialog run={run} cases={queuedChildren(cases, run)} position={typeof run.sequence === "number" ? run.sequence : (queue.indexOf(workflowKey(run)) + 1)} close={close} confirm={() => expedite(run)} />}
    {modal?.type === "priority-receipt" && run && priorities[workflowKey(run)] && <PriorityReceipt run={run}
      receipt={priorities[workflowKey(run)]} close={close} />}
    {modal?.type === "config" && canManage && <ConfigDialog {...dialogProps} target={modal.target} action={modal.action}
      confirm={(previewHash) => publishConfig(modal.target, modal.action, null, previewHash)} />}
    {modal?.type === "add-node" && canManage && <AddNodeDialog worker={modal.worker} nodes={clusterNodes} close={close}
      save={(node) => publishConfig({ id: poolId(node.worker, node.pool), worker: node.worker, label: node.id }, "新增", node)} />}
    {modal?.type === "new-user" && isSuper && <NewUserDialog accounts={accounts} create={createUser} close={close} />}
    {modal?.type === "user-key" && isSuper && user && user.key && <UserKeyDialog account={user} close={close} />}
    {modal?.type === "reset-user" && isSuper && user && <ResetUserKeyDialog account={user} close={close} confirm={() => resetUserKey(user.name)} />}
    {modal?.type === "delete-user" && isSuper && user && <DeleteUserDialog name={user.name} close={close} confirm={() => deleteUser(user.name)} />}
    <div className="toast-container" aria-live="polite" aria-atomic="true">{message && <div className="toast">{message}</div>}</div>
  </>;
}
