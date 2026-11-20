import { CalendarBlank, CaretDown, CaretRight, Pause, Play, Plus, Trash, X } from "@phosphor-icons/react";
import { useEffect, useState } from "react";
import { clock, dateLabel, flagFor, now, plannedTime, poolId, runs, statusTone, targetFor, timelineEnd, timelineStart, workers, workflowKey } from "./formal-data.js";
import { lastSeenDateTime, resourceHealth } from "./resource-health.js";

export function Tag({ children, tone = "gray" }) {
  return <span className={`tag tag-${tone}`}>{children}</span>;
}

export function Status({ value }) {
  return <Tag tone={statusTone[value]}>{value}</Tag>;
}

export function Details({ entries }) {
  return <dl className="details">
    {entries.map(([name, value]) => <div key={name}><dt>{name}</dt><dd>{value ?? "—"}</dd></div>)}
  </dl>;
}

export function RunIdentity({ run }) {
  return <span>{run.pr ? `@${run.author} #${run.pr}` : `${run.trigger}触发`} <span className="middot">·</span> {run.action} #{run.id}</span>;
}

const ticks = Array.from({ length: 6 }, (_, index) => timelineStart + index * 30);
const rowHeight = { worker: 39, pool: 39, node: 39, group: 54 };
const marker = (minute) => `${(minute - timelineStart) / (timelineEnd - timelineStart) * 100}%`;

function resourceRows(nodes, folded) {
  const rows = [];
  for (const worker of workers) {
    const onWorker = nodes.filter((node) => node.worker === worker);
    rows.push({ key: worker, worker, level: "worker", label: worker });
    if (folded[worker]) continue;
    for (const pool of [...new Set(onWorker.map((node) => node.pool))]) {
      const key = poolId(worker, pool);
      rows.push({ key, worker, pool, level: "pool", label: pool });
      if (folded[key]) continue;
      for (const node of onWorker.filter((item) => item.pool === pool)) {
        rows.push({ key: node.id, worker, pool, node, level: "node", label: node.id });
        if (folded[node.id]) continue;
        for (const group of node.groups) {
          rows.push({ key: targetFor(node, group).id, worker, pool, node, group, level: "group", label: group });
        }
      }
    }
  }
  let top = 0;
  return rows.map((row) => {
    const height = rowHeight[row.level];
    const entry = { ...row, top, height };
    top += height;
    return entry;
  });
}

function ResourceLabel({ row, folded, changeFold, config, selectScope, open }) {
  const { key, label, level, node, group, worker, pool, height } = row;
  const GroupMark = folded[key] ? CaretRight : CaretDown;
  const flag = level === "group" ? flagFor(config, node, group) : config.flags[key];
  return <div className={`resource resource-${level} ${worker.toLowerCase()}`} style={{ height }}>
    {level === "group"
      ? <button className="tree-leaf" onClick={() => open({ type: "resource", target: targetFor(node, group) })}>
          设备组 {group} <span className="subtle">（{node.groupCards?.[group] ?? 8}P）</span>
        </button>
      : <button className="tree-item" onClick={() => {
          changeFold(key);
          if (pool) selectScope(poolId(worker, pool));
        }} aria-expanded={!folded[key]}>
          <GroupMark size={16} weight="bold" />
          <strong>{label}</strong>
          {level === "worker" && <span>（Worker）</span>}
          {level === "pool" && <span>（Pool）</span>}
          {level === "node" && <span className="node-address mono">({node.ip}:{node.port})</span>}
        </button>}
    {flag && <span className="resource-flag" title={flag}> {flag === "暂停" ? "暂停调度" : flag}</span>}
  </div>;
}

function LeaseBar({ item, run, rows, config, nodes, selected, onClick }) {
  const queued = item.status === "排队中";
  const actual = item.node && ["准备中", "运行中", "清理中"].includes(item.status);
  const ended = item.node && item.status === "已取消" && item.finished;
  const selectedGroup = queued ? item.plannedGroups : item.groups;
  const node = queued ? item.plannedNode : item.node;
  const resourceRows = rows.filter((row) => row.node?.id === node && selectedGroup?.includes(row.group));
  if (!resourceRows.length || (!queued && !actual && !ended)) return null;
  const start = queued ? plannedTime(item, config, nodes) : item.started;
  if (start == null) return null;
  const projectedEnd = queued ? start + item.estimate : actual && item.estimate && item.started + item.estimate > now ? item.started + item.estimate : null;
  const finish = ended ? item.finished : projectedEnd ?? now;
  const visibleStart = Math.max(start, timelineStart);
  const total = Math.min(finish, timelineEnd) - visibleStart;
  if (total <= 0) return null;
  const solid = queued ? 0 : Math.max(0, Math.min(100, (now - visibleStart) / total * 100));
  const startLabel = clock(start);
  const finishLabel = clock(finish);
  const style = {
    "--actual": `${solid}%`,
    left: marker(visibleStart),
    width: marker(timelineStart + total),
  };
  return resourceRows.map((row) => <button key={row.key} className={`lease ${item.worker.toLowerCase()} ${selected ? "lease-selected" : ""} ${item.status === "已取消" ? "lease-ended" : ""}`}
    aria-label={`${run.repo} #${run.pr ?? run.id} · ${item.name} · ${item.status} · ${row.node.id} 设备组 ${row.group}`}
    aria-pressed={selected} onClick={onClick} style={{ ...style, top: row.top + 6, height: row.height - 12 }}>
    {!queued && <span className="lease-actual" />}
    {projectedEnd && <span className="lease-planned" />}
    <span className="lease-label">
      <strong>{run.repo} <span className="lease-author">· @{run.author ?? run.actor} #{run.pr ?? run.id}</span></strong>
      <small>{run.action} #{run.id} / {item.name} · {item.cards}P</small>
      {actual && <small className="lease-meta">{item.status} · 已用 {now - item.started} 分钟 · {item.estimate ? `预计 ${item.estimate} 分钟（${item.samples} 样本）` : "暂无估算"}</small>}
    </span>
    {start > timelineStart && <span className="lease-time lease-start">{startLabel}</span>}
    {actual && projectedEnd && <span className="lease-time lease-end">{finishLabel}</span>}
  </button>);
}

export function PoolsView({ nodes, cases, config, open, go, live }) {
  const [folded, setFolded] = useState({});
  const [queueScope, selectScope] = useState("");
  const [selected, setSelected] = useState(null);
  const rows = resourceRows(nodes, folded);
  const scopes = [...new Set([...nodes, ...cases].map((item) => poolId(item.worker, item.pool)))];
  useEffect(() => {
    if (!scopes.includes(queueScope)) selectScope(scopes[0] || "");
  }, [queueScope, scopes.join("|")]);
  const queue = cases.filter((item) => item.status === "排队中" && queueScope === poolId(item.worker, item.pool))
    .sort((a, b) => a.position - b.position);
  const showCase = (item) => {
    setSelected(item.id);
    open({ type: "case", caseId: item.id });
  };
  return <main className="pools-view">
    <section aria-labelledby="pools-title">
      <div className="pools-header">
        <h1 id="pools-title">Pools任务看板</h1>
        <div className="pools-meta">
          <div className="date-info">
            <span><CalendarBlank size={21} /> <time>{dateLabel}</time></span>
            <span>Asia/Shanghai (UTC+8)</span><span>现在 {clock(now)}（{live ? "实时" : "等待连接"}）</span>
          </div>
          <ul className="legend" aria-label="时间轴图例">
            <li><i className="legend-actual" />实际租约</li>
            <li><i className="legend-planned" />预计使用</li>
            <li><i className="legend-unknown" />未知占用</li>
            <li><i className="legend-free" />已确认空闲</li>
          </ul>
        </div>
      </div>
      <div className="timeline-scroll">
        <div className="timeline" aria-label="Blue 与 Yellow 资源时间轴">
          <div className="axis-label">资源 <span>（Worker / Pool / 节点 / 设备组）</span></div>
          <div className="axis">
            <div className="chart-coordinates">
              {ticks.map((minute) => <span key={minute} className="axis-tick mono" style={{ left: marker(minute) }}>{clock(minute)}</span>)}
              <span className="now-label mono" style={{ left: marker(now) }}>现在 {clock(now)}</span>
            </div>
          </div>
          <div className="resources">
            {rows.map((row) => <ResourceLabel key={row.key} row={row} config={config} folded={folded}
              selectScope={selectScope} open={open}
              changeFold={(key) => setFolded((previous) => ({ ...previous, [key]: !previous[key] }))} />)}
          </div>
          <div className="lanes">
            {rows.map((row) => <div key={row.key} className={`lane lane-${row.level} ${row.worker.toLowerCase()}`} style={{ height: row.height }} />)}
            <div className="chart-coordinates">
              {ticks.map((minute) => <i key={minute} className="grid-line" style={{ left: marker(minute) }} />)}
              {rows.filter((row) => row.level === "group" && row.node.unknown.includes(row.group)).map((row) => (
                <button key={row.key} className="unknown-block" style={{ left: marker(timelineStart + 2), width: marker(now - 2), top: row.top + 11 }}
                  onClick={() => open({ type: "resource", target: targetFor(row.node, row.group) })}>未知占用</button>
              ))}
              {cases.map((item) => <LeaseBar key={item.id} item={item} run={runs.find((run) => workflowKey(run) === item.run)}
                rows={rows} config={config} nodes={nodes} selected={selected === item.id} onClick={() => showCase(item)} />)}
              <i className="now-line" style={{ left: marker(now) }} />
            </div>
          </div>
        </div>
      </div>
    </section>
    <section className="queue-section" aria-labelledby="queue-title">
      <div className="queue-heading">
        <div className="queue-heading-main"><h2 id="queue-title">排队用例</h2>
          <label className="scope-select"><span className="sr-only">排队资源范围</span>
            <select value={queueScope} onChange={(event) => selectScope(event.target.value)}>
              {scopes.map((scope) => <option value={scope} key={scope}>{scope.replace("/", " / ")}</option>)}
            </select><CaretDown size={12} weight="bold" />
          </label>
        </div>
        <button className="text-button" onClick={() => go("models")}>查看代码仓任务 <CaretRight size={16} weight="bold" /></button>
      </div>
      <div className="table-scroll">
        <table className="data-table queue-table">
          <thead><tr><th>用例 / 仓库</th><th>PR / Workflow</th><th>申请资源</th><th>预计开始</th></tr></thead>
          <tbody>
            {queue.map((item) => {
              const run = runs.find((run) => workflowKey(run) === item.run);
              const estimate = plannedTime(item, config, nodes);
              return <tr className={item.id === selected ? "is-selected" : ""} key={item.id}>
                <td><button className="cell-title" onClick={() => showCase(item)}>{item.name}</button><small>{run.repo} · {run.owner}</small></td>
                <td><RunIdentity run={run} /></td>
                <td>{item.worker} / {item.pool} · {item.cards}P</td>
                <td>{estimate == null ? "暂无估算" : <><span>约 {clock(estimate)}</span><small>排期预估</small></>}
                  <small>队列顺位 {item.position}</small>
                </td>
              </tr>;
            })}
            {!queue.length && <tr><td colSpan={4} className="empty">当前资源范围内没有排队用例</td></tr>}
          </tbody>
        </table>
      </div>
    </section>
  </main>;
}

function ClusterControls({ target, state, config, open }) {
  const own = config.flags[target.id];
  const pending = config.pending;
  const action = own === "暂停" || own === "排空中" ? "恢复" : "暂停";
  const Icon = action === "暂停" ? Pause : Play;
  return <div className="cluster-actions">
    <button className="button small" disabled={Boolean(pending) || (!own && state != null)} onClick={() => open({ type: "config", target, action })}><Icon size={14} />{action}</button>
    <button className={`button small ${own === "排空中" ? "danger-subtle" : ""}`} disabled={Boolean(pending) || (!own && state != null)} onClick={() => open({ type: "config", target, action: own === "排空中" ? "删除" : "排空" })}>
      <Trash size={14} />{own === "排空中" ? "删除" : "排空删除"}
    </button>
  </div>;
}

export function ResourceStatus({ health, configStatus, unknown }) {
  return <div className="resource-status">
    <Tag tone={health.tone}>{health.status}</Tag>
    {configStatus && <Tag tone="amber">{configStatus === "暂停" ? "暂停调度" : configStatus}</Tag>}
    {unknown && <Tag tone="red">未知占用</Tag>}
  </div>;
}

function ClusterRow({ target, status, nodes, config, canManage, open, node, group }) {
  const health = resourceHealth(node ? [node] : nodes, group);
  return <tr>
    <td><button className={`resource-link ${group ? "group-link" : node ? "node-link" : ""}`} onClick={() => open({ type: "resource", target })}>
      {group ? `设备组 ${group}` : node ? node.id : `Pool ${target.pool}`} <CaretRight size={13} />
    </button></td>
    <td>{node ? <span className="mono">{node.ip}:{node.port}</span> : <span className="subtle">—</span>}</td>
    <td>{group ? `${node.groupCards?.[group] ?? 8} 卡` : node ? `${node.cards} 卡 · ${node.groups.length} 组` : `${nodes.length} 节点 · ${nodes.reduce((sum, member) => sum + member.cards, 0)} 卡`}</td>
    <td><ResourceStatus health={health} configStatus={status} unknown={Boolean(group && node.unknown.includes(group))} /></td>
    <td className="subtle mono" title={group ? "该设备组上一次查到全部配置卡的时间" : "成员中最近一次查到配置卡的时间"}>{lastSeenDateTime(health.lastSeen)}</td>
    {canManage && <td><ClusterControls target={target} state={status} config={config} open={open} /></td>}
  </tr>;
}

export function ClusterView({ config, nodes, clusterPools = [], clusterHistory = [], rollbackCluster, canManage, open, audit }) {
  const [worker, setWorker] = useState("Blue");
  const scoped = nodes.filter((node) => node.worker === worker);
  const pools = [...new Set([...scoped.map((node) => node.pool), ...clusterPools.filter((entry) => entry.worker === worker).map((entry) => entry.pool)])];
  const appliedRevision = config.applied[worker];
  const desiredRevision = config.workerStatus?.[worker]?.desired ?? config.desired;
  return <main className="standard-view">
    <div className="page-heading">
      <div><h1>集群管理</h1><p>查看 Worker、Pool、节点与设备组的配置及生效状态。</p></div>
      <button className={`button ${canManage ? "primary" : ""}`} disabled={Boolean(config.pending)}
        onClick={() => open(canManage ? { type: "add-node", worker } : { type: "login" })}>{canManage ? <><Plus size={17} weight="bold" />新增资产</> : "登录后管理"}</button>
    </div>
    <nav className="subnav" aria-label="Worker 页签">
      {workers.map((name) => <button key={name} aria-current={name === worker ? "page" : undefined} onClick={() => setWorker(name)}>
        <i className={`worker-dot ${name.toLowerCase()}`} />{name}
      </button>)}
    </nav>
    <div className="revision-bar">
      <strong>{worker}</strong><Tag tone={config.workerStatus?.[worker]?.offline ? "red" : "teal"}>{config.workerStatus?.[worker]?.offline ? "离线" : "在线"}</Tag>
      <span>当前版本 <code>{appliedRevision == null ? "—" : `r${appliedRevision}`}</code></span>
      <span>期望版本 <code>{desiredRevision == null ? "—" : `r${desiredRevision}`}</code></span>
      <Tag tone={appliedRevision != null && appliedRevision === desiredRevision ? "teal" : "amber"}>{appliedRevision != null && appliedRevision === desiredRevision ? "已同步" : "等待 Worker 应用"}</Tag>
      <span className="revision-updated">{config.workerStatus?.[worker]?.status || "等待心跳"}</span>
    </div>
    <div className="table-scroll">
      <table className="data-table cluster-table">
        <thead><tr><th>资源</th><th>节点地址</th><th>容量</th><th>状态</th><th>最近在线时间</th>{canManage && <th>操作</th>}</tr></thead>
        <tbody>{pools.map((pool) => {
          const members = scoped.filter((node) => node.pool === pool);
          const target = { id: poolId(worker, pool), label: `${worker} / ${pool}`, worker, pool, type: "Pool" };
          return <ClusterPool key={pool} target={target} members={members} config={config} open={open} canManage={canManage} />;
        })}
        {!pools.length && <tr><td className="empty" colSpan={canManage ? 6 : 5}>暂无已配置资源</td></tr>}
        </tbody>
      </table>
    </div>
    {canManage && <section className="audit-section" aria-label="历史配置版本">
      <h2>历史配置版本</h2>
      <div className="table-scroll"><table className="data-table">
        <thead><tr><th>版本</th><th>创建时间</th><th>操作账号</th><th>来源</th><th>操作</th></tr></thead>
        <tbody>{clusterHistory.map((revision) => <tr key={revision.revision}>
          <td className="mono">r{revision.revision}</td>
          <td className="mono">{revision.created_at ? new Date(revision.created_at * 1000).toLocaleString("zh-CN") : "—"}</td>
          <td>{revision.created_by || "—"}</td><td>{revision.rollback_from_rev ? `回滚自 r${revision.rollback_from_rev}` : "发布"}</td>
          <td><button className="button small" disabled={Boolean(config.pending) || revision.revision === config.desired} onClick={() => rollbackCluster?.(revision.revision)}>回滚</button></td>
        </tr>)}
        {!clusterHistory.length && <tr><td className="empty" colSpan={5}>暂无历史配置版本</td></tr>}</tbody>
      </table></div>
    </section>}
    {canManage && <AuditList entries={audit.filter((entry) => entry.area === "cluster")} />}
  </main>;
}

function ClusterPool({ target, members, config, canManage, open }) {
  const properties = { config, canManage, open };
  return <>
    <ClusterRow {...properties} target={target} nodes={members} status={config.flags[target.id]} />
    {members.map((node) => <ClusterNode key={node.id} {...properties} node={node} />)}
  </>;
}

function ClusterNode({ node, config, canManage, open }) {
  const properties = { config, canManage, open, node };
  return <>
    <ClusterRow {...properties} target={targetFor(node)} status={flagFor(config, node)} />
    {node.groups.map((group) => <ClusterRow key={group} {...properties} group={group} target={targetFor(node, group)} status={flagFor(config, node, group)} />)}
  </>;
}

export function AuditList({ entries }) {
  return <section className="audit-section" aria-label="操作审计">
    <h2>操作审计</h2>
    <div className="table-scroll"><table className="data-table">
      <thead><tr><th>时间</th><th>操作账号</th><th>动作 / 对象</th><th>结果</th></tr></thead>
      <tbody>{entries.map((entry) => <tr key={entry.id}>
        <td className="mono">{entry.time}</td><td>{entry.actor}</td><td>{entry.action} · {entry.target}</td><td>{entry.result}</td>
      </tr>)}
      {!entries.length && <tr><td className="empty" colSpan={4}>暂无操作记录</td></tr>}</tbody>
    </table></div>
  </section>;
}

export function UsersView({ accounts, open, audit }) {
  return <main className="standard-view">
    <div className="page-heading">
      <div><h1>用户管理</h1><p>创建普通管理员、查看登录密钥或撤销账号和会话。</p></div>
      <button className="button primary" onClick={() => open({ type: "new-user" })}><Plus size={17} weight="bold" />新增管理员</button>
    </div>
    <div className="section-heading"><h2>普通管理员</h2><Tag tone="teal">Super</Tag></div>
    <div className="table-scroll"><table className="data-table users-table">
      <thead><tr><th>用户名</th><th>角色</th><th>创建时间</th><th>登录密钥</th><th>操作</th></tr></thead>
      <tbody>{accounts.filter(({ role }) => role !== "Super").map((account) => <tr key={account.name}>
        <td className="mono"><strong>{account.name}</strong></td><td>管理员</td><td className="mono">{account.created}</td>
        <td><span className="subtle">仅创建或重置时显示一次</span></td>
        <td><div className="button-row"><button className="button small" onClick={() => open({ type: "reset-user", name: account.name })}>重置密钥</button>
          <button className="button small danger-subtle" onClick={() => open({ type: "delete-user", name: account.name })}><Trash size={15} />删除</button></div></td>
      </tr>)}
      {accounts.every(({ role }) => role === "Super") && <tr><td className="empty" colSpan={5}>暂无普通管理员</td></tr>}</tbody>
    </table></div>
    <AuditList entries={audit.filter((entry) => entry.area === "users")} />
  </main>;
}

export function DialogActions({ close, children }) {
  return <div className="dialog-actions"><button className="button" type="button" onClick={close}>返回</button>{children}</div>;
}

export function DialogClose({ close }) {
  return <button type="button" className="icon-button" onClick={close} aria-label="关闭"><X size={20} /></button>;
}
