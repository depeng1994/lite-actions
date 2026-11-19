import { Moon, Sun } from "@phosphor-icons/react";
import { useEffect, useRef, useState } from "react";
import { ClusterView, PoolsView, UsersView } from "./Views.jsx";
import { ModelsView } from "./ModelsView.jsx";
import {
  AddNodeDialog, CancelDialog, CancellationReceipt, CaseDialog, ConfigDialog, DeleteUserDialog, ExpediteDialog,
  HistoryDialog, LoginForm, NewUserDialog, PriorityReceipt, ResourceDialog, ResetUserKeyDialog, UserKeyDialog,
} from "./Dialogs.jsx";
import { history, impactOf, initialCases, initialNodes, initialQueueOrder, poolId, repositories, runs, workflowKey } from "./formal-data.js";
import { currentWorkflowQueue, moveWorkflow, queuedChildren } from "./workflows.js";
import { cancelWorkflow as apiCancelWorkflow, clusterPreview, clusterPublish, clusterRollback, createUser as apiCreateUser, deleteUser as apiDeleteUser, getSession, listUsers, loadControlCenter, loadMoreHistory as apiLoadMoreHistory, login as apiLogin, logout as apiLogout, moveWorkflowUp as apiMoveWorkflowUp, resetUserKey as apiResetUserKey } from "./control-api.js";

const pages = [["pools", "Pools 时间轴"], ["models", "模型仓"], ["cluster", "集群管理"]];
const defaultModel = repositories[0].name;
const initialConfig = { desired: 24, applied: { Blue: 24, Yellow: 24 }, flags: {}, pending: null };

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

function Header({ page, session, theme, setTheme, open, logout, live }) {
  const dark = theme === "dark";
  const nav = session?.role === "Super" ? [...pages, ["users", "用户管理"]] : pages;
  const ThemeIcon = dark ? Sun : Moon;
  return <header className="app-header">
    <a href="#/pools" className="wordmark" aria-label="Lite Actions 首页">Lite Actions</a>
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
  const [clusterPools, setClusterPools] = useState([]);
  const [clusterHistory, setClusterHistory] = useState([]);
  const [cases, setCases] = useState(initialCases);
  const [workflowOrder, setWorkflowOrder] = useState(initialQueueOrder);
  const [queueVersion, setQueueVersion] = useState(0);
  const [historyCursor, setHistoryCursor] = useState(null);
  const [historyHasMore, setHistoryHasMore] = useState(false);
  const [, setHistoryVersion] = useState(0);
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

  const session = signedIn && accounts.find(({ name }) => name === signedIn);
  const isSuper = session?.role === "Super";
  const canManage = Boolean(session);

  useEffect(() => {
    let cancelled = false;
    const refresh = async () => {
      const snapshot = await loadControlCenter(signedIn ? "admin" : "public");
      if (cancelled) return;
      if (!snapshot) { setDataMode("offline"); return; }
      setNodes(snapshot.nodes);
      setClusterPools(snapshot.clusterPools || []);
      setClusterHistory(snapshot.clusterHistory || []);
      setCases(snapshot.cases);
      setWorkflowOrder(snapshot.workflowOrder);
      setQueueVersion(snapshot.queueVersion || 0);
      setHistoryCursor(snapshot.historyCursor || null);
      setHistoryHasMore(Boolean(snapshot.historyHasMore));
      if (snapshot.clusterConfig) setConfig(snapshot.clusterConfig);
      setDataMode("live");
    };
    refresh();
    const timer = setInterval(refresh, 30_000);
    return () => { cancelled = true; clearInterval(timer); };
  }, [signedIn]);

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
        return [superAccount, ...payload.users.map((user) => ({name: user.username, userId: user.user_id, role: "管理员", created: new Date(user.created_at * 1000).toLocaleString("zh-CN"), key: ""}))];
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
      if (!remote?.authenticated) return false;
      setAccounts((previous) => previous.some((account) => account.name === remote.username) ? previous : [...previous, {name: remote.username, role: remote.role === "super" ? "Super" : "管理员", key: "", userId: remote.user_id}]);
      setSignedIn(remote.username);
      close();
      notify(`已登录为 ${remote.role === "super" ? "Super" : remote.username}`);
      return true;
    } catch (error) { notify(error.message); return false; }
  };
  const logout = async () => {
    if (dataMode === "live") await apiLogout().catch(() => undefined);
    setSignedIn(null);
    close();
    if (route.page === "users") go("pools");
    notify("已退出登录");
  };
  const log = (area, action, target, result) => {
    const time = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", hour12: false, timeStyle: "medium" }).format(new Date());
    const entry = { id: crypto.randomUUID(), actor: session.name, time, area, action, target, result };
    setAudit((entries) => [entry, ...entries]);
  };

  const queue = currentWorkflowQueue(cases, workflowOrder);

  const loadMoreHistory = async () => {
    if (!historyHasMore || !historyCursor || dataMode !== "live") return;
    try {
      const page = await apiLoadMoreHistory(signedIn ? "admin" : "public", historyCursor);
      setHistoryCursor(page.cursor);
      setHistoryHasMore(page.hasMore);
      setHistoryVersion((version) => version + 1);
    } catch (error) { notify(error.message); }
  };

  const expedite = async (run) => {
    const key = workflowKey(run);
    const children = queuedChildren(cases, run);
    if (!canManage || dataMode !== "live" || cancellations[key]) return;
    const updated = moveWorkflow(cases, workflowOrder, key);
    if (!updated) return;
    let response;
    try { response = await apiMoveWorkflowUp(run, queueVersion); } catch (error) { notify(error.message); return; }
    if (response?.status !== "PENDING" && response?.status !== "APPLIED") {
      notify(`控制面返回 ${response?.status || "未知状态"}`);
      return;
    }
    const positions = new Map(updated.cases.map((item) => [item.id, item.position]));
    const receipt = { from: updated.from, to: updated.to, entries: children.map((item) => ({ ...item, newPosition: positions.get(item.id) })) };
    setCases(updated.cases);
    setWorkflowOrder(updated.order);
    if (response.queue_version_after != null) setQueueVersion(response.queue_version_after);
    setPriorities((previous) => ({ ...previous, [key]: receipt }));
    log("workflow", "Workflow 插队", `${run.repo} #${run.id} · 尝试 ${run.attempt}`, "控制面已受理 MOVE_UP");
    open({ type: "priority-receipt", runKey: workflowKey(run) });
  };

  const cancelRun = async (run) => {
    if (!canManage || dataMode !== "live" || cancellations[workflowKey(run)]) return;
    const affected = cases.filter((item) => item.run === workflowKey(run) && ["排队中", "运行中", "准备中", "清理中"].includes(item.status));
    if (!affected.length) return;
    try {
      const cancellation = await apiCancelWorkflow(run);
      const update = (fields) => setCancellations((previous) => ({...previous, [workflowKey(run)]: {...previous[workflowKey(run)], ...fields}}));
      update({platform: cancellation?.status || "已提交取消", done: ["COMPLETED", "FAILED"].includes(cancellation?.status), release: "等待控制面回执", workers: {Blue: "等待受理", Yellow: "等待受理"}});
      open({type: "cancel-receipt", runKey: workflowKey(run)});
      notify("取消请求已提交到控制面");
    } catch (error) { notify(error.message); }
  };

  const publishConfig = async (target, action, addedNode) => {
    if (!canManage || dataMode !== "live" || config.pending) return;
    const devices = target.group?.match(/\d+/g)?.map(Number);
    const scope = target.group ? "device_group" : target.node ? "node" : "pool";
    const operation = addedNode
      ? {op: "ADD_NODE", pool: addedNode.pool, value: addedNode.value}
      : action === "暂停" || action === "恢复"
      ? {op: action === "暂停" ? "PAUSE" : "RESUME", scope, pool: target.pool, ...(target.node ? {host_id: target.node} : {}), ...(devices ? {devices} : {})}
      : action === "排空"
        ? {op: "DRAIN", scope, pool: target.pool, ...(target.node ? {host_id: target.node} : {}), ...(devices ? {devices} : {})}
        : {op: target.group ? "DELETE_DEVICE_GROUP" : target.node ? "DELETE_NODE" : "DELETE_POOL", pool: target.pool, ...(target.node ? {host_id: target.node} : {}), ...(devices ? {devices} : {})};
    setConfig((previous) => ({...previous, pending: target.worker}));
    close();
    try {
      const preview = await clusterPreview(config.desired, operation);
      if (preview.blocking_reasons?.length) {
        setConfig((previous) => ({...previous, pending: null}));
        notify(`控制面拒绝${action}：${preview.blocking_reasons.join("、")}`);
        return;
      }
      const sync = await clusterPublish(config.desired, operation, preview.preview_hash);
      setConfig((previous) => ({...previous, desired: sync.revision, pending: null}));
      log("cluster", action, target.label, "控制面已发布配置");
      notify(`配置 r${sync.revision} 已提交，等待 Worker 应用`);
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
      log("cluster", "回滚配置", `r${targetRevision}`, "控制面已发布新版本");
      notify(`已基于 r${targetRevision} 发布配置 r${sync.revision}`);
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
      account = {name: issued.username, userId: issued.user_id, role: "管理员", key: issued.secret_once, created: new Date().toLocaleString("zh-CN")};
    } catch (error) { notify(error.message); return; }
    setAccounts((previous) => [...previous, account]);
    log("users", "新增管理员", name, "已创建");
    open({ type: "user-key", name });
  };

  const deleteUser = async (name) => {
    if (!isSuper || !accounts.some((account) => account.name === name && account.role === "管理员")) return;
    const account = accounts.find((item) => item.name === name);
    if (dataMode === "live" && account?.userId) {
      try { await apiDeleteUser(account.userId); } catch (error) { notify(error.message); return; }
    }
    setAccounts((previous) => previous.filter((account) => account.name !== name));
    log("users", "删除管理员", name, "账号及会话已撤销");
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
      log("users", "重置管理员密钥", name, "旧会话已撤销");
      open({type: "user-key", name});
    } catch (error) { notify(error.message); }
  };

  const viewProps = { nodes, clusterPools, clusterHistory, cases, config, queueVersion, historyHasMore, loadMoreHistory, rollbackCluster, canManage, open, go, audit, live: dataMode === "live" };
  const item = modal?.caseId && cases.find(({ id }) => id === modal.caseId);
  const run = modal?.runKey && runs.find((run) => workflowKey(run) === modal.runKey);
  const historicalRun = modal?.historyId && history.find((run) => workflowKey(run) === modal.runKey);
  const historicalCase = historicalRun && historicalRun.cases.find(({ historyId }) => historyId === modal.historyId);
  const user = modal?.name && accounts.find(({ name }) => name === modal.name);
  const dialogProps = { ...viewProps, close };

  return <>
    <Header page={route.page} open={open} session={session} theme={theme} setTheme={setTheme} logout={logout} live={dataMode === "live"} />
    {route.page === "pools" && <PoolsView {...viewProps} />}
    {route.page === "models" && <ModelsView key={route.model} {...viewProps} model={route.model} cancellations={cancellations} priorities={priorities} queue={queue} />}
    {route.page === "cluster" && <ClusterView {...viewProps} />}
    {route.page === "users" && (isSuper
      ? <UsersView accounts={accounts} open={open} audit={audit} />
      : <main className="standard-view"><h1>用户管理</h1><p className="subtle">此页面需要 Super 登录。</p>
        {!canManage && <button className="button" onClick={() => open({ type: "login" })}>登录</button>}</main>)}

    {modal?.type === "login" && <LoginForm close={close} login={login} accounts={accounts} />}
    {modal?.type === "case" && item && <CaseDialog item={item} {...dialogProps} />}
    {modal?.type === "history" && historicalCase && <HistoryDialog run={historicalRun} item={historicalCase} close={close} />}
    {modal?.type === "resource" && <ResourceDialog target={modal.target} {...dialogProps} />}
    {modal?.type === "cancel" && canManage && <CancelDialog run={run} cases={cases} close={close} confirm={() => cancelRun(run)} />}
    {modal?.type === "cancel-receipt" && run && cancellations[workflowKey(run)] && <CancellationReceipt run={run} receipt={cancellations[workflowKey(run)]} close={close} />}
    {modal?.type === "expedite" && canManage && run && <ExpediteDialog run={run} cases={queuedChildren(cases, run)} position={queue.indexOf(workflowKey(run)) + 1} close={close} confirm={() => expedite(run)} />}
    {modal?.type === "priority-receipt" && run && priorities[workflowKey(run)] && <PriorityReceipt run={run}
      receipt={priorities[workflowKey(run)]} close={close} />}
    {modal?.type === "config" && canManage && <ConfigDialog {...dialogProps} target={modal.target} action={modal.action}
      confirm={() => publishConfig(modal.target, modal.action)} />}
    {modal?.type === "add-node" && canManage && <AddNodeDialog worker={modal.worker} nodes={nodes} close={close}
      save={(node) => publishConfig({ id: poolId(node.worker, node.pool), worker: node.worker, label: node.id }, "新增", node)} />}
    {modal?.type === "new-user" && isSuper && <NewUserDialog accounts={accounts} create={createUser} close={close} />}
    {modal?.type === "user-key" && isSuper && user && user.key && <UserKeyDialog account={user} close={close} />}
    {modal?.type === "reset-user" && isSuper && user && <ResetUserKeyDialog account={user} close={close} confirm={() => resetUserKey(user.name)} />}
    {modal?.type === "delete-user" && isSuper && user && <DeleteUserDialog name={user.name} close={close} confirm={() => deleteUser(user.name)} />}
    <div className="toast-container" aria-live="polite" aria-atomic="true">{message && <div className="toast">{message}</div>}</div>
  </>;
}
