import { ArrowRight, ArrowSquareOut, ArrowUp, Check, Copy, Pause, ShieldCheck, StopCircle, Warning } from "@phosphor-icons/react";
import { useEffect, useRef, useState } from "react";
import { beijingClock, clock, dateFormatter, dateTimeSecondFormatter, displayNodeHost, flagFor, impactOf, plannedTime, runs, workflowKey } from "./formal-data.js";
import { Details, DialogActions, DialogClose, ResourceStatus, Status, Tag } from "./Views.jsx";
import { lastSeenDateTime, resourceHealth } from "./resource-health.js";
import { clusterPreview, getTaskDetail } from "./control-api.js";
import { commandStatusLabel, commandStatusTone } from "./command-status.js";
import { actionsUrl } from "./workflows.js";

async function copyToClipboard(value) {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(value);
      return;
    } catch {
      // Fall through to the selection-based browser fallback.
    }
  }
  const input = document.createElement("textarea");
  input.value = value;
  input.setAttribute("readonly", "");
  input.style.position = "fixed";
  input.style.opacity = "0";
  document.body.appendChild(input);
  input.select();
  try {
    if (!document.execCommand("copy")) throw new Error("clipboard unavailable");
  } finally {
    input.remove();
  }
}

function CopyCommand({ value }) {
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState(false);
  return <div className="command-block">
    <div className="command-block-header"><span>在目标 Worker 主机执行</span><button className="button small" type="button" onClick={async () => {
      try {
        await copyToClipboard(value);
        setCopied(true);
        setError(false);
        window.setTimeout(() => setCopied(false), 1800);
      } catch {
        setCopied(false);
        setError(true);
      }
    }}><Copy size={15} />{copied ? "已复制" : "复制命令"}</button></div>
    <pre><code>{value}</code></pre>
    {error && <p role="status" className="subtle">浏览器未允许自动复制，请选中命令后手动复制。</p>}
  </div>;
}

export function Dialog({ title, subtitle, close, drawer = false, children }) {
  const element = useRef(null);
  useEffect(() => {
    const dialog = element.current;
    dialog.showModal();
    return () => dialog.close();
  }, []);
  return <dialog ref={element} className={`dialog ${drawer ? "drawer" : ""}`} aria-label={title}
    onCancel={(event) => { event.preventDefault(); close(); }}
    onClick={(event) => { if (event.target === event.currentTarget && !drawer) close(); }}>
    <div className="dialog-inner">
      <header className="dialog-header"><div><h2>{title}</h2>{subtitle && <p>{subtitle}</p>}</div><DialogClose close={close} /></header>
      {children}
    </div>
  </dialog>;
}

export function LoginForm({ close, login }) {
  const [name, setName] = useState("");
  const [key, setKey] = useState("");
  const [error, setError] = useState("");
  return <Dialog title="登录" subtitle="使用用户名和网站登录密钥执行管理操作。" close={close}>
    <form onSubmit={async (event) => {
      event.preventDefault();
      if (!(await login(name.trim(), key))) setError("用户名或登录密钥有误。");
    }}>
      <div className="form-fields">
        <label>用户名<input required autoComplete="username" value={name} onChange={(event) => setName(event.target.value)} placeholder="输入用户名" /></label>
        <label>网站登录密钥<input required autoComplete="current-password" value={key} onChange={(event) => setKey(event.target.value)} type="password" placeholder="输入登录密钥" /></label>
        {error && <p role="alert" className="form-error">{error}</p>}
      </div>
      <DialogActions close={close}><button className="button primary" type="submit">登录</button></DialogActions>
    </form>
  </Dialog>;
}

function RunDetails({ run, runUrl }) {
  const link = runUrl || actionsUrl(run);
  return <Details entries={[
    ["平台", run.platform], ["仓库", `${run.repo} · ${run.owner}`], ["PR", run.pr ? `#${run.pr}` : "—"],
    ["PR 作者", run.author ? `@${run.author}` : "—"], ["触发来源", run.trigger], ["触发者", `@${run.actor}`],
    ["Workflow", <a key="url" className="text-button" href={link} target="_blank" rel="noreferrer">{run.action} #{run.id} · 尝试 {run.attempt} <ArrowSquareOut size={13} /></a>],
  ]} />;
}

function formatUnixTime(seconds) {
  return seconds ? dateTimeSecondFormatter.format(seconds * 1000) : "—";
}

function useTaskDetail(access, taskUid, live) {
  const [detail, setDetail] = useState(null);
  useEffect(() => {
    if (!live || !taskUid) return undefined;
    let cancelled = false;
    getTaskDetail(access, taskUid).then((data) => { if (!cancelled) setDetail(data); }).catch(() => undefined);
    return () => { cancelled = true; };
  }, [access, taskUid, live]);
  return detail;
}

export function CaseDialog({ item, nodes, config, access = "public", live = false, close, go }) {
  const run = runs.find((candidate) => workflowKey(candidate) === item.run);
  const node = nodes.find(({ id }) => id === item.node);
  const detail = useTaskDetail(access, item.taskUid, live);
  const nowMinute = beijingClock().now;
  const isQueued = item.status === "排队中";
  const elapsed = detail?.duration_sec != null
    ? Number((detail.duration_sec / 60).toFixed(1))
    : item.started != null ? Math.max(0, Math.round((item.finished ?? nowMinute) - item.started)) : null;
  const planned = plannedTime(item, config, nodes);
  const allocHost = detail?.allocation?.[0]
    ? (detail.allocation[0].host_id || detail.allocation[0].display_host_id)
    : (node ? displayNodeHost(node) : null);
  return <Dialog drawer title={item.name} subtitle={`${run.action} #${run.id} · ${run.repo}`} close={close}>
    <div className="drawer-body">
      <Status value={item.status} />
      <h3>Workflow</h3><RunDetails run={run} runUrl={detail?.run_url || item.runUrl} />
      <h3>资源与排期</h3>
      <Details entries={[
        ["申请资源", `${item.worker} / ${item.pool} · ${item.nodes} 节点 · ${item.cards} 卡`],
        ["实际分配", allocHost ? `${item.worker} / ${item.pool} · ${allocHost}` : "等待分配"],
        ["设备 ID", detail?.allocation?.length ? detail.allocation.map((a) => a.devices.join(",")).join(" / ") : (node && item.groups.join("、")) || "—"],
        [isQueued ? "预计开始" : "开始时间", isQueued ? (item.projectionStatus === "WAITING_EXTERNAL" ? "等待外部释放" : clock(planned)) : (detail?.started_at ? formatUnixTime(detail.started_at) : clock(item.started))],
        ["已用时长", elapsed == null ? "—" : `${elapsed} 分钟`],
        ["预计总时长", item.estimate ? `${item.estimate} 分钟 · ${item.samples} 个成功样本` : "暂无估算 · 0 个样本"],
        ...(item.estimate && elapsed != null && elapsed > item.estimate ? [["预计状态", "已超过预计"]] : []),
        ...(isQueued ? [["队列顺位", `第 ${item.position} 位`]] : []),
      ]} />
      {detail && <>
        <h3>各阶段时间（北京时间）</h3>
        <Details entries={[
          ["入队", formatUnixTime(detail.queued_at)],
          ["租约", formatUnixTime(detail.reserved_at)],
          ["准备", formatUnixTime(detail.staging_at)],
          ["运行", formatUnixTime(detail.running_at)],
          ["清理", formatUnixTime(detail.cleaning_at)],
          ["数据层级", detail.storage_tier === "cold" ? "冷归档" : "热库"],
        ]} />
      </>}
    </div>
    <div className="dialog-actions">
      <button className="button" onClick={() => { close(); go("models", run.model); }}>查看代码仓任务 <ArrowRight size={16} /></button>
    </div>
  </Dialog>;
}

export function HistoryDialog({ run, item, access = "public", live = false, close }) {
  const detail = useTaskDetail(access, item.taskUid || item.historyId, live);
  const elapsed = detail?.duration_sec != null
    ? `${Number((detail.duration_sec / 60).toFixed(1))} 分钟`
    : item.elapsed == null ? "—" : `${item.elapsed} 分钟`;
  const start = detail?.started_at ? detail.started_at * 1000 : item.start;
  const end = detail?.finished_at ? detail.finished_at * 1000 : item.end;
  const allocHost = detail?.allocation?.[0]
    ? (detail.allocation[0].host_id || detail.allocation[0].display_host_id)
    : (item.allocation ? displayNodeHost(item.allocation) : null);
  const storageTier = detail?.storage_tier ?? item.storageTier;
  return <Dialog drawer title={item.name} subtitle={`历史用例 · Workflow #${run.id}`} close={close}>
    <div className="drawer-body">
      <Status value={item.status} />
      <h3>Workflow</h3><RunDetails run={run} runUrl={detail?.run_url || item.runUrl} />
      <h3>执行明细</h3>
      <Details entries={[
        ["开始时间", start ? dateFormatter.format(start) : "—"], ["结束时间", end ? dateFormatter.format(end) : "—"],
        ["耗时", elapsed],
        ["退出码", detail?.exit_code != null ? String(detail.exit_code) : "—"],
        ["清理校验", detail?.cleanup_verified == null ? "—" : (detail.cleanup_verified ? "已确认释放" : "未确认（隔离）")],
        ["数据层级", storageTier === "cold" ? "控制面冷归档" : "控制面热库"],
        ["实际分配", allocHost ? `${item.worker} / ${item.pool} · ${allocHost}` : "—"],
        ["设备 ID", detail?.allocation?.length ? detail.allocation.map((a) => a.devices.join(",")).join(" / ") : (item.groups.join("、") || "—")],
      ]} />
      {detail && <>
        <h3>各阶段时间（北京时间）</h3>
        <Details entries={[
          ["入队", formatUnixTime(detail.queued_at)],
          ["租约", formatUnixTime(detail.reserved_at)],
          ["准备", formatUnixTime(detail.staging_at)],
          ["运行", formatUnixTime(detail.running_at)],
          ["清理", formatUnixTime(detail.cleaning_at)],
          ["结束", formatUnixTime(detail.finished_at)],
        ]} />
      </>}
    </div>
  </Dialog>;
}

export function CancelDialog({ run, cases, close, confirm }) {
  const active = cases.filter((item) => item.run === workflowKey(run) && ["运行中", "准备中", "排队中", "清理中"].includes(item.status));
  return <Dialog title={`取消 ${run.platform} Workflow`} subtitle={`${run.repo} · ${run.action} #${run.id} · 尝试 ${run.attempt}`} close={close}>
    <div className="dialog-body">
      <div className="callout warning"><Warning size={20} /><div><strong>取消整次运行</strong><p>影响 {run.platform} Actions 作业及这次运行在各 Worker 上的用例。运行中的用例会先清理，再释放设备。</p></div></div>
      <h3>影响范围</h3>
      <ul className="impact-list"><li><span>{run.platform} Actions</span><strong>整次运行</strong></li>
        {active.map((item) => <li key={item.id}><span>{item.worker} / {item.pool} · {item.name} · {item.cards}P</span><Status value={item.status} /></li>)}
      </ul>
    </div>
    <DialogActions close={close}><button className="button danger" onClick={confirm}><StopCircle size={17} />确认取消</button></DialogActions>
  </Dialog>;
}

export function CancellationReceipt({ run, receipt, close }) {
  return <Dialog title="Workflow 取消进度" subtitle={`${run.repo} · ${run.action} #${run.id} · 尝试 ${run.attempt}`} close={close}>
    <div className="dialog-body" aria-live="polite">
      <div className="callout"><ShieldCheck size={20} /><strong>{receipt.done ? "取消已完成" : "取消单已提交"}</strong></div>
      <ul className="impact-list">
        <li><span>{run.platform} Actions 结论</span><Tag tone={receipt.done ? "teal" : "amber"}>{receipt.platform}</Tag></li>
        {Object.entries(receipt.workers).map(([worker, status]) => <li key={worker}><span>{worker} 停止进度</span><Tag tone={status === "已停止" ? "teal" : "amber"}>{status}</Tag></li>)}
        <li><span>设备释放</span><Tag tone={receipt.done ? "teal" : "amber"}>{receipt.release}</Tag></li>
      </ul>
    </div>
    <DialogActions close={close} />
  </Dialog>;
}

function PriorityCases({ cases }) {
  return <ul className="impact-list">{cases.map((item) => <li key={item.id}>
    <span>{item.worker} / {item.pool} · {item.name} · {item.cards}P</span>
    <strong>当前顺位 {item.position}</strong>
  </li>)}</ul>;
}

function priorityTitle(run) {
  return `${run.platform} · ${run.repo} · #${run.id} · 尝试 ${run.attempt}`;
}

export function ExpediteDialog({ run, cases, position, close, confirm }) {
  return <Dialog title="Workflow 插队" subtitle={priorityTitle(run)} close={close}>
    <div className="dialog-body">
      <div className="callout"><ArrowUp size={20} /><p>与前一位 Workflow 交换顺序。共享资源队列按新顺序更新，同一 Workflow 的子用例顺序保持不变，已拿到租约的用例继续执行。</p></div>
      <h3>Workflow 序号 {position} → {position - 1}</h3>
      <p className="subtle">{cases.length} 个排队子用例</p>
      <PriorityCases cases={cases} />
    </div>
    <DialogActions close={close}><button disabled={!cases.length || position < 2} className="button primary" onClick={confirm}>确认插队</button></DialogActions>
  </Dialog>;
}

export function PriorityReceipt({ run, receipt, close }) {
  const commands = receipt.commands || [];
  return <Dialog title="Workflow 插队回执" subtitle={priorityTitle(run)} close={close}>
    <div className="dialog-body" aria-live="polite">
      <div className="callout"><Check size={20} /><div>
        <strong>Workflow 序号 {receipt.from} → {receipt.to}</strong>
        <p className="subtle">操作 ID：<code>{receipt.opId}</code> · 整体状态：<Tag tone={commandStatusTone(receipt.status)}>{commandStatusLabel(receipt.status)}</Tag></p>
      </div></div>
      <h3>各端回执</h3>
      <ul className="impact-list">
        {commands.map((cmd) => <li key={cmd.cmd_id}>
          <span>{cmd.worker_id === "blue" ? "Blue" : "Yellow"} Worker{cmd.result?.pools?.length ? `（${cmd.result.pools.map((p) => `${p.p}/${p.k}: ${commandStatusLabel(p.st)}`).join("，")}）` : ""}</span>
          <Tag tone={commandStatusTone(cmd.status)}>{commandStatusLabel(cmd.status)}</Tag>
        </li>)}
        {!commands.length && <li><span>跨池队列顺位已更新</span><Tag tone={commandStatusTone(receipt.status)}>{commandStatusLabel(receipt.status)}</Tag></li>}
      </ul>
    </div>
    <DialogActions close={close} />
  </Dialog>;
}

function Impact({ impact, target }) {
  const groupCards = target.group
    ? impact.affectedNodes.reduce((sum, node) => sum + (node.groupCards?.[target.group] ?? 0), 0)
    : impact.affectedNodes.reduce((count, node) => count + node.cards, 0);
  return <ul className="impact-list">
    <li><span>影响设备</span><strong>{impact.affectedNodes.length} 节点 · {groupCards} 卡</strong></li>
    {impact.affectedNodes.map((node) => <li key={node.id}><span>{displayNodeHost(node)}</span><code>{node.cards} 卡</code></li>)}
    <li><span>排队用例</span><strong>{impact.queue.length} 个</strong></li>
    <li><span>活动租约</span><strong>{impact.leases.length} 个</strong></li>
  </ul>;
}

const blockingReasonLabel = (code) => ({
  RESOURCE_NOT_DRAINED: "目标尚未排空生效，请先执行排空并等待 Worker 回执",
  ACTIVE_LEASES_REMAINING: "仍有活动租约正在执行",
  QUEUED_TASKS_REMAINING: "仍有排队用例引用该资源",
  NODE_QUARANTINED: "存在未解除隔离的设备组，请先上主机运行 reconcile.py 处理",
}[code] || code);

export function buildClusterOperation(target, action) {
  const devices = target.group?.match(/\d+/g)?.map(Number);
  const scope = target.group ? "device_group" : target.node ? "node" : "pool";
  if (action === "暂停" || action === "恢复") {
    return {op: action === "暂停" ? "PAUSE" : "RESUME", scope, pool: target.pool, ...(target.node ? {host_id: target.node} : {}), ...(devices ? {devices} : {})};
  }
  if (action === "排空") {
    return {op: "DRAIN", scope, pool: target.pool, ...(target.node ? {host_id: target.node} : {}), ...(devices ? {devices} : {})};
  }
  return {op: target.group ? "DELETE_DEVICE_GROUP" : target.node ? "DELETE_NODE" : "DELETE_POOL", pool: target.pool, ...(target.node ? {host_id: target.node} : {}), ...(devices ? {devices} : {})};
}

export function ConfigDialog({ target, action, cases, nodes, config, live = false, close, confirm }) {
  const impact = impactOf(target, cases, nodes);
  const [serverPreview, setServerPreview] = useState(null);
  const [previewError, setPreviewError] = useState("");
  const [loadingPreview, setLoadingPreview] = useState(Boolean(live));

  useEffect(() => {
    if (!live || !config.desired) {
      setLoadingPreview(false);
      return undefined;
    }
    let cancelled = false;
    setLoadingPreview(true);
    setPreviewError("");
    clusterPreview(config.desired, buildClusterOperation(target, action))
      .then((data) => { if (!cancelled) { setServerPreview(data); setLoadingPreview(false); } })
      .catch((error) => { if (!cancelled) { setPreviewError(error.message); setLoadingPreview(false); } });
    return () => { cancelled = true; };
  }, [live, config.desired, target.id, action]);

  const blockingReasons = serverPreview?.blocking_reasons || [];
  const blocked = loadingPreview || Boolean(previewError) || blockingReasons.length > 0;
  const ref = impact.affectedNodes.find((node) => node.id === target.nodeId || node.id === target.node);
  const sibling = target.group && ref?.groups.find((group) => group !== target.group);
  return <Dialog title={`${action}${target.type}`} subtitle={`${target.worker} / ${target.pool} · ${target.label}`} close={close}>
    <div className="dialog-body">
      {action === "暂停" && <div className="callout warning"><Pause size={20} /><div>
        <strong>Worker 确认应用后暂停新租约</strong>
        <p>{target.group && ref && ref.cards > (ref.groupCards?.[target.group] ?? 0) ? `设备组 ${target.group} 和相交组合（含整机多组组合）暂停发放新租约。设备组 ${sibling} 的候选资格沿用当前状态。` : "该资源暂停发放新租约。"} 已运行的用例继续执行。</p>
      </div></div>}
      {action === "恢复" && <div className="callout"><Check size={20} /><p>Worker 应用新版本后恢复资源候选资格。现有隔离状态继续生效。</p></div>}
      {action === "排空" && <div className="callout warning"><Warning size={20} /><p>先停止发放新租约，唯一依赖该目标的排队用例将被取消并回报平台，等待现有租约自然结束后再执行删除。</p></div>}
      {loadingPreview && <p className="subtle">正在向控制面拉取影响预览…</p>}
      {previewError && <div className="callout warning"><Warning size={20} /><p>预览失败：{previewError}</p></div>}
      {blockingReasons.length > 0 && <div className="callout warning"><Warning size={20} /><div>
        <strong>当前无法{action}</strong>
        <p>{blockingReasons.map(blockingReasonLabel).join("；")}</p>
      </div></div>}
      <h3>影响预览</h3>
      {serverPreview ? <ul className="impact-list">
        <li><span>受影响设备组</span><strong>{serverPreview.affected_groups?.length ?? 0} 组</strong></li>
        <li><span>受影响跨机组合</span><strong>{serverPreview.affected_combinations?.length ?? 0} 个</strong></li>
        <li><span>待取消唯一依赖排队用例</span><strong>{serverPreview.drain_cancel_tasks?.length ?? 0} 个</strong></li>
        <li><span>活动租约</span><strong>{serverPreview.active_leases?.length ?? 0} 个</strong></li>
      </ul> : <Impact impact={impact} target={target} />}
      <p className="subtle">发布版本 r{config.desired + 1} · 等待 Worker 回执</p>
    </div>
    <DialogActions close={close}><button disabled={blocked} className={`button ${action === "删除" ? "danger" : "primary"}`} onClick={() => confirm(serverPreview?.preview_hash)}>确认{action}</button></DialogActions>
  </Dialog>;
}

export function ResourceDialog({ target, cases, nodes, config, close, go }) {
  const impact = impactOf(target, cases, nodes);
  const node = impact.affectedNodes.find((item) => item.id === target.nodeId || item.id === target.node);
  const state = node ? flagFor(config, node, target.group) : config.flags[target.id];
  const health = resourceHealth(impact.affectedNodes, target.group);
  const quarantined = Boolean(target.group ? node?.quarantinedGroups?.includes(target.group) : node?.quarantinedGroups?.length);
  return <Dialog drawer title={target.label} subtitle={`${target.worker} / ${target.pool}`} close={close}>
    <div className="drawer-body">
      <ResourceStatus health={health} configStatus={state} quarantined={quarantined}
        unknown={Boolean(target.group && node?.unknown.includes(target.group))} />
      <h3>配置</h3>
      <Details entries={[
        ["节点地址", node && <code>{displayNodeHost(node)}</code>], ["设备 ID", target.group ?? node?.groups.join("、")],
        ["容器绑定", node?.binding], ["当前 / 期望版本", `r${config.applied[target.worker] ?? "—"} / r${config.desired}`],
        ["最近在线时间", lastSeenDateTime(health.lastSeen)],
      ]} />
      <h3>资源占用</h3><Impact impact={impact} target={target} />
    </div>
    <div className="dialog-actions"><button className="button" onClick={() => { close(); go("cluster"); }}>查看集群管理 <ArrowRight size={16} /></button></div>
  </Dialog>;
}

export function AddNodeDialog({ worker, nodes, close, save }) {
  const [selectedWorker, setSelectedWorker] = useState(worker);
  const [raw, setRaw] = useState("");
  const [preview, setPreview] = useState(null);
  const [error, setError] = useState("");
  const workerId = selectedWorker.toLowerCase();
  // Resolve the deployed checkout from systemd so host layouts remain portable.
  const serviceName = `lite-actions-${workerId}.service`;
  const preflightCommand = `service="${serviceName}"
workdir="$(systemctl show "$service" -p WorkingDirectory --value)"
if [ -z "$workdir" ] || [ "$workdir" = "/" ] || [ "$workdir" = "-" ]; then
  router_path="$(systemctl show "$service" -p ExecStart --value | grep -oE '/[^ ;"]+/unified_router\\.py' | tail -n1)"
  workdir="$(dirname "$router_path")"
fi
if [ -z "$workdir" ]; then
  echo "无法从 $service 获取 Lite Actions 部署目录" >&2
  exit 1
fi
cd "$workdir"
python3 scripts/preflight.py --worker-id ${workerId} --export-node-snippet \\\n  --host-id "HOST_IP:PORT" \\\n  --ssh "SSH_TARGET" \\\n  --host-ssh "HOST_SSH_TARGET" \\\n  --node-ip "NODE_IP" \\\n  --nic "NETWORK_INTERFACE" \\\n  --devices "0-15" \\\n  --groups "0-7;8-15" \\\n  --container "CONTAINER_NAME_OR_ID"`;
  return <Dialog title="新增集群资产" subtitle="登记节点、设备拓扑和容器绑定。" close={close}>
    <form onSubmit={(event) => {
      event.preventDefault();
      const form = Object.fromEntries(new FormData(event.currentTarget));
      const pool = form.pool.trim();
      if (!/^[A-Za-z][A-Za-z0-9_\.-]*$/.test(pool)) {
        setError("请输入有效的 Pool 名称。");
        return;
      }
      if (!preview) {
        setError("请先解析 preflight.py --export-node-snippet JSON 片段。");
        return;
      }
      const id = preview.host_id;
      if (nodes.some((node) => node.hostId === id || node.displayHost === id || node.id === id)) {
        setError("该节点已存在。");
        return;
      }
      save({worker: form.worker, pool, value: preview, label: id});
    }}>
      <div className="form-fields form-grid">
        <label>Worker<select name="worker" value={selectedWorker} onChange={(event) => setSelectedWorker(event.target.value)}><option>Blue</option><option>Yellow</option></select></label>
        <label>Pool<input name="pool" required defaultValue={nodes.find((node) => node.worker === worker)?.pool || ""} /></label>
        <div className="span-two asset-instructions">
          <h3>操作说明</h3>
          <p>请先 SSH 登录目标 {selectedWorker} Worker 主机，再执行下方命令。命令通过 systemctl 获取 Lite Actions 实际部署目录，先将大写占位符替换为实际值。命令只读探查节点指纹、设备和容器绑定，不在网站或控制面 Worker 中执行。</p>
          <CopyCommand value={preflightCommand} />
          <p>执行完成后，将标准输出的完整 JSON 粘贴到下方文本框，点击「解析片段」核对节点摘要，再发布配置。</p>
        </div>
        <label className="span-two">preflight.py 导出片段<textarea name="snippet" required rows="9" value={raw} onChange={(event) => {
          setRaw(event.target.value); setPreview(null); setError("");
        }} placeholder="粘贴 --export-node-snippet 输出的 JSON" /></label>
        <div className="span-two form-inline-actions"><button className="button" type="button" onClick={() => {
          try {
            const parsed = JSON.parse(raw);
            const value = parsed.node || parsed;
            if (!value || typeof value !== "object" || !value.host_id || !value.ssh || !value.device_ids || !value.allocation_groups) throw new Error();
            setPreview(value); setError("");
          } catch { setPreview(null); setError("JSON 无法解析，或缺少 host_id、ssh、device_ids、allocation_groups 字段。"); }
        }}>解析片段</button>{preview && <span className="subtle">已解析 {preview.host_id} · {preview.device_ids.length} 卡</span>}</div>
        {error && <p role="alert" className="form-error span-two">{error}</p>}
      </div>
      <DialogActions close={close}><button className="button primary" type="submit" disabled={!preview}>发布配置</button></DialogActions>
    </form>
  </Dialog>;
}

export function NewUserDialog({ close, accounts, create }) {
  const [name, setName] = useState("");
  const [error, setError] = useState("");
  return <Dialog title="新增管理员" subtitle="系统为新账号生成网站登录密钥。" close={close}>
    <form onSubmit={(event) => {
      event.preventDefault();
      const username = name.trim();
      if (!/^[a-zA-Z][\w-]{1,29}$/.test(username)) return setError("请输入 2–30 位字母、数字、下划线或连字符，首位为字母。");
      if (accounts.some((account) => account.name === username)) return setError("该用户名已存在。");
      create(username);
    }}>
      <div className="form-fields">
        <label>用户名<input required value={name} onChange={(event) => setName(event.target.value)} placeholder="例如 ops-admin" /></label>
        {error && <p role="alert" className="form-error">{error}</p>}
      </div>
      <DialogActions close={close}><button className="button primary" type="submit">创建管理员</button></DialogActions>
    </form>
  </Dialog>;
}

export function UserKeyDialog({ account, close }) {
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState(false);
  return <Dialog title="一次性管理员登录密钥" subtitle={`${account.name} · 普通管理员`} close={close}>
    <div className="dialog-body">
      <div className="key-box"><code>{account.key}</code><button className="button small" onClick={async () => {
        try { await copyToClipboard(account.key); setCopied(true); } catch { setCopyError(true); }
      }}><Copy size={16} />{copied ? "已复制" : "复制"}</button></div>
      {copyError && <p role="status" className="subtle">可选中密钥后手动复制。</p>}
    </div>
    <DialogActions close={close} />
  </Dialog>;
}

export function ResetUserKeyDialog({ account, close, confirm }) {
  return <Dialog title="重置管理员密钥" subtitle={account.name} close={close}>
    <div className="dialog-body"><div className="callout warning"><Warning size={20} /><p>旧密钥和旧会话会立即失效。新密钥只在确认后显示一次。</p></div></div>
    <DialogActions close={close}><button className="button danger" onClick={confirm}>确认重置</button></DialogActions>
  </Dialog>;
}

export function DeleteUserDialog({ name, close, confirm }) {
  return <Dialog title="删除管理员" subtitle={name} close={close}>
    <div className="dialog-body"><div className="callout warning"><Warning size={20} /><p>删除后账号及其会话立即撤销。操作审计会保留。</p></div></div>
    <DialogActions close={close}><button className="button danger" onClick={confirm}>确认删除</button></DialogActions>
  </Dialog>;
}
