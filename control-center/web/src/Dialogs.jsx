import { ArrowRight, ArrowUp, Check, Copy, Key, Pause, ShieldCheck, StopCircle, Warning } from "@phosphor-icons/react";
import { useEffect, useRef, useState } from "react";
import { clock, dateFormatter, flagFor, impactOf, now, plannedTime, runs, workflowKey } from "./formal-data.js";
import { Details, DialogActions, DialogClose, ResourceStatus, Status, Tag } from "./Views.jsx";
import { lastSeenDateTime, resourceHealth } from "./resource-health.js";

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

export function LoginForm({ close, login, accounts }) {
  const [name, setName] = useState("");
  const [key, setKey] = useState("");
  const [error, setError] = useState("");
  return <Dialog title="登录" subtitle="使用用户名和网站登录密钥体验管理操作。" close={close}>
    <form onSubmit={async (event) => {
      event.preventDefault();
      if (!(await login(name.trim(), key))) setError("用户名或登录密钥有误。");
    }}>
      <div className="form-fields">
        <label>用户名<input required autoComplete="username" value={name} onChange={(event) => setName(event.target.value)} placeholder="输入用户名" /></label>
        <label>网站登录密钥<input required autoComplete="current-password" value={key} onChange={(event) => setKey(event.target.value)} type="password" placeholder="输入登录密钥" /></label>
        {error && <p role="alert" className="form-error">{error}</p>}
      </div>
      {accounts.length > 0 && <div className="live-login">
        <p>可用账号</p>
        {accounts.map((account) =>
          <button key={account.name} className="button small" type="button" onClick={() => {
            setName(account.name);
            setKey(account.key);
            setError("");
          }}><Key size={16} />{account.role === "Super" ? "Super" : "普通管理员"}</button>)}
      </div>}
      <DialogActions close={close}><button className="button primary" type="submit">登录</button></DialogActions>
    </form>
  </Dialog>;
}

function RunDetails({ run }) {
  return <Details entries={[
    ["平台", run.platform], ["仓库", `${run.repo} · ${run.owner}`], ["PR", run.pr ? `#${run.pr}` : "—"],
    ["PR 作者", run.author ? `@${run.author}` : "—"], ["触发来源", run.trigger], ["触发者", `@${run.actor}`],
    ["提交", <code>{run.commit}</code>], ["Workflow", `${run.action} #${run.id} · 尝试 ${run.attempt}`],
  ]} />;
}

export function CaseDialog({ item, nodes, config, close, go }) {
  const run = runs.find((run) => workflowKey(run) === item.run);
  const node = nodes.find(({ id }) => id === item.node);
  const isQueued = item.status === "排队中";
  const elapsed = item.started != null ? (item.finished ?? now) - item.started : null;
  const planned = plannedTime(item, config, nodes);
  return <Dialog drawer title={item.name} subtitle={`${run.action} #${run.id} · ${run.repo}`} close={close}>
    <div className="drawer-body">
      <Status value={item.status} />
      <h3>Workflow</h3><RunDetails run={run} />
      <h3>资源与排期</h3>
      <Details entries={[
        ["申请资源", `${item.worker} / ${item.pool} · ${item.nodes} 节点 · ${item.cards} 卡`],
        ["实际分配", node ? `${node.worker} / ${node.pool} · ${node.id}` : "等待分配"],
        ["节点 IP / 端口", node && <code>{node.ip}:{node.port}</code>], ["设备 ID", node && item.groups.join("、")],
        [isQueued ? "预计开始" : "开始时间", clock(isQueued ? planned : item.started)],
        ["已用时长", elapsed == null ? "—" : `${elapsed} 分钟`],
        ["预计总时长", item.estimate ? `${item.estimate} 分钟 · ${item.samples} 个成功样本` : "暂无估算 · 0 个样本"],
        ...(item.estimate && elapsed > item.estimate ? [["预计状态", "已超过预计"]] : []),
        ...(isQueued ? [["队列顺位", `第 ${item.position} 位`]] : []),
      ]} />
    </div>
    <div className="dialog-actions">
      <button className="button" onClick={() => { close(); go("models", run.model); }}>查看模型仓任务 <ArrowRight size={16} /></button>

    </div>
  </Dialog>;
}

export function HistoryDialog({ run, item, close }) {
  return <Dialog drawer title={item.name} subtitle={`历史用例 · Workflow #${run.id}`} close={close}>
    <div className="drawer-body">
      <Status value={item.status} />
      <h3>Workflow</h3><RunDetails run={run} />
      <h3>执行明细</h3>
      <Details entries={[
        ["开始时间", dateFormatter.format(item.start)], ["结束时间", dateFormatter.format(item.end)],
        ["耗时", `${item.elapsed} 分钟`], ["数据来源", item.archived ? "控制面冷存储" : "控制面热存储"],
        ["实际分配", `${item.worker} / ${item.pool} · ${item.node.id}`],
        ["节点 IP / 端口", <code>{item.node.ip}:{item.node.port}</code>], ["设备 ID", item.groups.join("、")],
      ]} />
    </div>
  </Dialog>;
}

export function CancelDialog({ run, cases, close, confirm }) {
  const active = cases.filter((item) => item.run === workflowKey(run) && ["运行中", "准备中", "排队中", "清理中"].includes(item.status));
  return <Dialog title="取消 GitHub Workflow" subtitle={`${run.repo} · ${run.action} #${run.id} · 尝试 ${run.attempt}`} close={close}>
    <div className="dialog-body">
      <div className="callout warning"><Warning size={20} /><div><strong>取消整次运行</strong><p>影响 GitHub Actions 作业及这次运行在各 Worker 上的用例。运行中的用例会先清理，再释放设备。</p></div></div>
      <h3>影响范围</h3>
      <ul className="impact-list"><li><span>GitHub Actions</span><strong>整次运行</strong></li>
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
        <li><span>GitHub Actions 结论</span><Tag tone={receipt.platform === "已取消" ? "teal" : "amber"}>{receipt.platform}</Tag></li>
        {Object.entries(receipt.workers).map(([worker, status]) => <li key={worker}><span>{worker} 停止进度</span><Tag tone={status === "已停止" ? "teal" : "amber"}>{status}</Tag></li>)}
        <li><span>设备释放</span><Tag tone={receipt.done ? "teal" : "amber"}>{receipt.release}</Tag></li>
      </ul>
    </div>
    <DialogActions close={close} />
  </Dialog>;
}

function PriorityCases({ cases, receipt = false }) {
  return <ul className="impact-list">{cases.map((item) => <li key={item.id}>
    <span>{item.worker} / {item.pool} · {item.name} · {item.cards}P</span>
    <strong>{receipt ? `顺位 ${item.position} → ${item.newPosition}` : `当前顺位 ${item.position}`}</strong>
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
  return <Dialog title="Workflow 插队回执" subtitle={priorityTitle(run)} close={close}>
    <div className="dialog-body">
      <div className="callout"><Check size={20} /><strong>Workflow 序号 {receipt.from} → {receipt.to}</strong></div>
      <PriorityCases cases={receipt.entries} receipt />
    </div>
    <DialogActions close={close} />
  </Dialog>;
}

function Impact({ impact, target }) {
  return <ul className="impact-list">
    <li><span>影响设备</span><strong>{impact.affectedNodes.length} 节点 · {target.group ? 8 : impact.affectedNodes.reduce((count, node) => count + node.cards, 0)} 卡</strong></li>
    {impact.affectedNodes.map((node) => <li key={node.id}><span>{node.id}</span><code>{node.ip}:{node.port}</code></li>)}
    <li><span>排队用例</span><strong>{impact.queue.length} 个</strong></li>
    <li><span>活动租约</span><strong>{impact.leases.length} 个</strong></li>
  </ul>;
}

export function ConfigDialog({ target, action, cases, nodes, config, close, confirm }) {
  const impact = impactOf(target, cases, nodes);
  const unknown = impact.affectedNodes.some((node) => node.unknown.some((group) => !target.group || group === target.group));
  const blocked = action === "删除" && (impact.leases.length > 0 || impact.queue.length > 0 || unknown);
  const ref = impact.affectedNodes.find((node) => node.id === target.node);
  const sibling = target.group && ref?.groups.find((group) => group !== target.group);
  return <Dialog title={`${action}${target.type}`} subtitle={`${target.worker} / ${target.pool} · ${target.label}`} close={close}>
    <div className="dialog-body">
      {action === "暂停" && <div className="callout warning"><Pause size={20} /><div>
        <strong>Worker 确认应用后暂停新租约</strong>
        <p>{target.group && ref.cards > 8 ? `设备组 ${target.group} 和相交组合（含整台 16P）暂停发放新租约。设备组 ${sibling} 的候选资格沿用当前状态。` : "该资源暂停发放新租约。"} 已运行的用例继续执行。</p>
      </div></div>}
      {action === "恢复" && <div className="callout"><Check size={20} /><p>Worker 应用新版本后恢复资源候选资格。现有隔离状态继续生效。</p></div>}
      {action === "排空" && <div className="callout warning"><Warning size={20} /><p>先停止发放新租约，等待现有租约和排队引用处理完成，再删除配置。</p></div>}
      {blocked && <div className="callout warning"><Warning size={20} /><p>仍有{[impact.leases.length && "活动租约", impact.queue.length && "排队引用", unknown && "未知占用"].filter(Boolean).join("、")}。排空完成后可删除。</p></div>}
      <h3>影响预览</h3><Impact impact={impact} target={target} />
      <p className="subtle">发布版本 r{config.desired + 1} · 等待 Worker 回执</p>
    </div>
    <DialogActions close={close}><button disabled={blocked} className={`button ${action === "删除" ? "danger" : "primary"}`} onClick={confirm}>确认{action}</button></DialogActions>
  </Dialog>;
}

export function ResourceDialog({ target, cases, nodes, config, close, go }) {
  const impact = impactOf(target, cases, nodes);
  const node = impact.affectedNodes.find((item) => item.id === target.node);
  const state = node ? flagFor(config, node, target.group) : config.flags[target.id];
  const health = resourceHealth(impact.affectedNodes, target.group);
  return <Dialog drawer title={target.label} subtitle={`${target.worker} / ${target.pool}`} close={close}>
    <div className="drawer-body">
      <ResourceStatus health={health} configStatus={state}
        unknown={Boolean(target.group && node?.unknown.includes(target.group))} />
      <h3>配置</h3>
      <Details entries={[
        ["节点 IP / 端口", node && <code>{node.ip}:{node.port}</code>], ["设备 ID", target.group ?? node?.groups.join("、")],
        ["容器绑定", node?.binding], ["当前 / 期望版本", `r${config.applied[target.worker]} / r${config.desired}`],
        ["最近在线时间", lastSeenDateTime(health.lastSeen)],
      ]} />
      <h3>资源占用</h3><Impact impact={impact} target={target} />
    </div>
    <div className="dialog-actions"><button className="button" onClick={() => { close(); go("cluster"); }}>查看集群管理 <ArrowRight size={16} /></button></div>
  </Dialog>;
}

function exampleAddress(nodes) {
  return Array.from({ length: 140 }, (_, index) => `203.0.113.${index + 80}`).find((ip) => !nodes.some((node) => node.ip === ip));
}

export function AddNodeDialog({ worker, nodes, close, save }) {
  const [error, setError] = useState("");
  return <Dialog title="新增集群资产" subtitle="登记节点、设备拓扑和容器绑定。" close={close}>
    <form onSubmit={(event) => {
      event.preventDefault();
      const raw = Object.fromEntries(new FormData(event.currentTarget));
      const pool = raw.pool.trim().toUpperCase();
      const id = raw.id.trim();
      const ip = raw.ip.trim();
      const binding = raw.binding.trim();
      if (!/^[a-z][a-z0-9-]*$/.test(id) || !/^[A-Z][A-Z0-9-]*$/.test(pool) || !binding) {
        setError("请输入有效的 Pool、节点名和容器绑定。");
        return;
      }
      if (!/^(192\.0\.2|198\.51\.100|203\.0\.113)\.(?:[1-9][0-9]{0,2})$/.test(ip) || Number(ip.split(".").at(-1)) > 254) {
        setError("请输入有效的节点 IP，例如 203.0.113.80。");
        return;
      }
      if (nodes.some((node) => node.id === id || node.ip === ip)) {
        setError("节点名称和 IP 必须唯一。");
        return;
      }
      const cards = Number(raw.cards);
      save({ worker: raw.worker, id, ip, port: Number(raw.port), pool, binding, cards, groups: cards === 16 ? ["0–7", "8–15"] : ["0–7"], unknown: [] });
    }}>
      <div className="form-fields form-grid">
        <label>Worker<select name="worker" defaultValue={worker}><option>Blue</option><option>Yellow</option></select></label>
        <label>Pool<input name="pool" required defaultValue={worker === "Blue" ? "A3" : "A5"} /></label>
        <label>节点名<input name="id" required placeholder="blue-a3-02" /></label>
        <label>设备拓扑<select name="cards"><option value="8">8P · 0–7</option><option value="16">16P · 0–7、8–15</option></select></label>
        <label>节点 IP<input name="ip" required defaultValue={exampleAddress(nodes)} /></label>
        <label>端口<input name="port" required type="number" min="1" max="65535" defaultValue="22" /></label>
        <label className="span-two">容器绑定<input name="binding" required placeholder="blue-a3-runner-02" /></label>
        {error && <p role="alert" className="form-error span-two">{error}</p>}
      </div>
      <DialogActions close={close}><button className="button primary" type="submit">发布配置</button></DialogActions>
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
  return <Dialog title="管理员登录密钥" subtitle={`${account.name} · 普通管理员`} close={close}>
    <div className="dialog-body">
      <div className="key-box"><code>{account.key}</code><button className="button small" onClick={async () => {
        try { await navigator.clipboard.writeText(account.key); setCopied(true); } catch { setCopyError(true); }
      }}><Copy size={16} />{copied ? "已复制" : "复制"}</button></div>
      {copyError && <p role="status" className="subtle">可选中密钥后手动复制。</p>}
    </div>
    <DialogActions close={close} />
  </Dialog>;
}

export function DeleteUserDialog({ name, close, confirm }) {
  return <Dialog title="删除管理员" subtitle={name} close={close}>
    <div className="dialog-body"><div className="callout warning"><Warning size={20} /><p>删除后账号及其会话立即撤销。操作审计会保留。</p></div></div>
    <DialogActions close={close}><button className="button danger" onClick={confirm}>确认删除</button></DialogActions>
  </Dialog>;
}
