import { ArrowDown, ArrowSquareOut, ArrowUp, CaretUpDown, FunnelSimple, Minus, Plus } from "@phosphor-icons/react";
import { Fragment, useEffect, useLayoutEffect, useRef, useState } from "react";
import { clock, dateFormatter, history, plannedTime, poolId, repositories, workflowKey } from "./formal-data.js";
import { actionsUrl, groupActiveRuns, queuedChildren, summarizeRun, terminalStatuses } from "./workflows.js";
import { Status, Tag } from "./Views.jsx";

const pageSize = 50;
const cardHours = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 });

function WorkflowTime({ value }) {
  if (value == null) return <span className="subtle">—</span>;
  const [date, time] = dateFormatter.format(value).split(" ");
  return <time className="workflow-time mono" dateTime={new Date(value).toISOString()}>{date}<small>{time}</small></time>;
}

function CasePhase({ item, config, nodes }) {
  const queued = item.status === "排队中";
  const planned = queued ? plannedTime(item, config, nodes) : null;
  return <>
    <Status value={item.status} />
    {queued && <small>{planned == null ? "暂无估算" : `预计 ${clock(planned)}`} · 顺位 {item.position}</small>}
  </>;
}

function CaseDuration({ item }) {
  if (item.elapsed == null) return "—";
  if (terminalStatuses.has(item.status)) return `${item.elapsed} 分钟`;
  return <>已用 {item.elapsed} 分钟
    <small>{item.estimate ? `预计 ${item.estimate} 分钟 · ${item.samples} 个样本` : "暂无估算"}</small>
  </>;
}

function CasesTable({ run, config, nodes, open }) {
  return <table className="data-table cases-table" aria-label={`Workflow #${run.id} 的子用例`}>
    <thead><tr><th scope="col">用例</th><th scope="col">阶段</th><th scope="col">申请资源</th><th scope="col">实际分配／设备 ID</th><th scope="col">开始／结束</th><th scope="col">耗时</th></tr></thead>
    <tbody>{run.cases.map((item) => {
      const allocation = item.historyId ? item.node : item.allocation;
      return <tr key={item.historyId ?? item.id}>
        <td><button className="text-button case-detail" onClick={() => open(item.historyId
          ? { type: "history", runKey: workflowKey(run), historyId: item.historyId }
          : { type: "case", caseId: item.id })}>{item.name}</button>
          {item.archived && <small>归档</small>}</td>
        <td><CasePhase item={item} config={config} nodes={nodes} /></td>
        <td>{item.worker} / {item.pool} · {item.nodes} 节点 · {item.cards} 卡</td>
        <td>{allocation ? <><code>{allocation.ip}:{allocation.port}</code><small>{item.groups.join("、")}</small></> : <span className="subtle">—</span>}</td>
        <td className="case-times"><WorkflowTime value={item.start} /><span className="subtle"> / </span><WorkflowTime value={item.end} /></td>
        <td><CaseDuration item={item} /></td>
      </tr>;
    })}</tbody>
  </table>;
}

function WorkflowActions({ run, summary, sequence, canManage, open, cancellation, priority }) {
  const prompt = (type) => open(canManage ? { type, runKey: workflowKey(run) } : { type: "login" });
  const queued = queuedChildren(run.cases, run);
  return <div className="workflow-actions">
    {sequence > 0 && queued.length > 0 && !cancellation && <button className="button small" disabled={sequence === 1} onClick={() => prompt("expedite")} title={sequence === 1 ? "已在队首" : "与前一位 Workflow 交换顺序"}>{sequence === 1 ? "已在队首" : canManage ? "插队" : "登录后插队"}</button>}
    {priority && <button className="text-button small-text" onClick={() => open({ type: "priority-receipt", runKey: workflowKey(run) })}>插队回执</button>}
    {cancellation
      ? <button className="button small" onClick={() => open({ type: "cancel-receipt", runKey: workflowKey(run) })}>取消进度</button>
      : summary.active && run.cancelSupported && <button className="button small danger-subtle" onClick={() => prompt("cancel")}>{canManage ? "取消 Workflow" : "登录后取消"}</button>}
  </div>;
}

function filtersFor(filters, run, summary) {
  return (!filters.model || run.model === filters.model)
    && (!filters.repo || run.repo === filters.repo)
    && (!filters.platform || run.platform === filters.platform)
    && (!filters.pool || summary.pools.includes(filters.pool))
    && (!filters.status || (summary.active ? filters.status === "active" : filters.status === "history"));
}

const columns = [
  { key: "sequence", label: "队列序号", numeric: true, value: ({ sequence }) => sequence },
  { key: "submitted", label: "提交时间", date: true, value: ({ run }) => run.submitted },
  { key: "repo", label: "平台 / 仓库", value: ({ run }) => `${run.platform} ${run.repo} ${run.owner}` },
  { key: "pools", label: "资源", value: ({ summary }) => summary.pools.join(" · ") },
  { key: "maxCards", label: "最大卡数", numeric: true, value: ({ summary }) => summary.maxCards },
  { key: "actions", label: "Actions ID", value: ({ run }) => run.id },
  { key: "result", label: "调度结果", value: ({ summary }) => summary.result },
  { key: "actor", label: "触发人", value: ({ run }) => `@${run.actor}` },
  { key: "trigger", label: "触发来源", value: ({ run }) => [run.trigger, run.pr && `#${run.pr}`, run.author && `@${run.author}`].filter(Boolean).join(" ") },
  { key: "start", label: "开始时间", date: true, value: ({ summary }) => summary.start },
  { key: "end", label: "结束时间", date: true, value: ({ summary }) => summary.end },
  { key: "hours", label: "运行总卡时", numeric: true, value: ({ summary }) => summary.cardHours == null ? null : Number(summary.cardHours.toFixed(2)) },
];
const columnCount = columns.length + 1;

function normalize(value) {
  return String(value).toLowerCase().replace(/[\s#]/g, "");
}

function matchesColumn(entry, column, raw) {
  const query = raw?.trim();
  if (!query) return true;
  const value = column.value(entry);
  if (value == null) return query === "—" || query === "-";
  if (column.numeric) {
    const comparison = query.match(/^(>=|<=|>|<|=)?\s*(-?\d+(?:\.\d+)?)$/);
    if (!comparison) return false;
    const bound = Number(comparison[2]);
    switch (comparison[1]) {
      case ">": return value > bound;
      case ">=": return value >= bound;
      case "<": return value < bound;
      case "<=": return value <= bound;
      default: return value === bound;
    }
  }
  const label = column.date ? dateFormatter.format(value) : value;
  return normalize(label).includes(normalize(query));
}

function sortEntries(entries, sort) {
  if (!sort) return entries;
  const column = columns.find(({ key }) => key === sort.key);
  return [...entries].sort((a, b) => {
    const left = column.value(a);
    const right = column.value(b);
    if (left == null || right == null) return Number(left == null) - Number(right == null);
    const difference = typeof left === "number" ? left - right : left.localeCompare(right, "zh-CN", { numeric: true });
    return sort.direction === "ascending" ? difference : -difference;
  });
}

function SortHeader({ column, sort, change }) {
  const direction = sort?.key === column.key ? sort.direction : null;
  const Icon = direction === "ascending" ? ArrowUp : direction === "descending" ? ArrowDown : CaretUpDown;
  return <th scope="col" aria-sort={direction ?? undefined}>
    <button className="column-sort" onClick={() => change(column.key)} aria-label={`按${column.label}排序`} title="点击切换升序、降序和队列默认顺序">
      {column.label}<Icon size={12} weight={direction ? "bold" : "regular"} />
    </button>
  </th>;
}

export function ModelsView({ model, cases, nodes, config, cancellations, priorities, queue, historyHasMore, loadMoreHistory, canManage, open }) {
  const [filters, setFilters] = useState({ model, repo: "", platform: "", pool: "", status: "" });
  const [columnFilters, setColumnFilters] = useState({});
  const [showColumns, setShowColumns] = useState(false);
  const [sort, setSort] = useState(null);
  const [limit, setLimit] = useState(0);
  const [historyCollapsed, setHistoryCollapsed] = useState(false);
  const historyBoundary = useRef(null);
  const revealBoundary = useRef(false);
  const [expanded, setExpanded] = useState({});
  // A queue move returns the table to live queue order so the swap is visible.
  useEffect(() => setSort(null), [priorities]);
  useLayoutEffect(() => {
    if (!revealBoundary.current) return;
    revealBoundary.current = false;
    historyBoundary.current?.focus({ preventScroll: true });
    historyBoundary.current?.scrollIntoView({ block: "start", inline: "nearest" });
  }, [limit]);
  const updateFilter = (filter, value) => {
    setFilters((previous) => ({ ...previous, [filter]: value, ...(filter === "model" ? { repo: "" } : {}) }));
    if (filter === "status" && value === "history") setHistoryCollapsed(false);
    setLimit((loaded) => loaded || value === "history" ? pageSize : 0);
  };
  const updateColumn = (key, value) => {
    setColumnFilters((previous) => ({ ...previous, [key]: value }));
    setLimit((loaded) => loaded ? pageSize : 0);
  };
  const changeSort = (key) => setSort((previous) => {
    if (previous?.key !== key) return { key, direction: "ascending" };
    if (previous.direction === "ascending") return { key, direction: "descending" };
    return null;
  });

  const entries = [...groupActiveRuns(cases, nodes), ...history].map((run) => ({ run, summary: summarizeRun(run) }));
  const active = entries.filter(({ summary }) => summary.active)
    .map((entry) => ({ ...entry, sequence: queue.indexOf(workflowKey(entry.run)) + 1 }))
    .sort((a, b) => a.sequence - b.sequence || a.run.submitted - b.run.submitted);
  const finished = entries.filter(({ summary }) => !summary.active)
    .sort((a, b) => (b.summary.end ?? 0) - (a.summary.end ?? 0) || workflowKey(a.run).localeCompare(workflowKey(b.run)))
    .map((entry, index) => ({ ...entry, sequence: -index - 1 }));
  const matches = (entry) => filtersFor(filters, entry.run, entry.summary)
    && columns.every((column) => matchesColumn(entry, column, columnFilters[column.key]));
  const activeMatches = active.filter(matches);
  const historicalMatches = finished.filter(matches);
  const windowedHistory = sortEntries(historicalMatches, sort).slice(0, limit);
  const shownHistory = historyCollapsed ? [] : windowedHistory;
  const visible = sortEntries([...shownHistory.slice().reverse(), ...activeMatches], sort);
  const boundary = windowedHistory[Math.min(1, windowedHistory.length - 1)];
  const loadHistory = () => {
    revealBoundary.current = limit === 0 && !sort && activeMatches.length > 0;
    if (limit >= historicalMatches.length && historyHasMore) loadMoreHistory?.();
    setLimit((count) => count + pageSize);
  };
  const repos = [...new Set(repositories.filter(({ name }) => !filters.model || name === filters.model).flatMap(({ sources }) => sources))].sort();
  const pools = [...new Set([...entries.flatMap(({ summary }) => summary.pools), ...nodes.map(({ worker, pool }) => poolId(worker, pool))])].sort();
  const activeColumnFilters = Object.values(columnFilters).filter((value) => value.trim()).length;

  return <main className="standard-view">
    <div className="page-heading">
      <div><h1>代码仓任务看板</h1><p>按当前队列顺序查看 Workflow，展开可查看子用例和实际分配。</p></div>
    </div>
    <div className="workflow-filters" aria-label="Workflow 筛选">
      <label>模型<select value={filters.model} onChange={(event) => updateFilter("model", event.target.value)}>
        <option value="">全部模型</option>{repositories.map(({ name }) => <option key={name}>{name}</option>)}
      </select></label>
      <label>仓库<select value={filters.repo} onChange={(event) => updateFilter("repo", event.target.value)}>
        <option value="">全部仓库</option>{repos.map((repo) => <option key={repo}>{repo}</option>)}
      </select></label>
      <label>平台<select value={filters.platform} onChange={(event) => updateFilter("platform", event.target.value)}>
        <option value="">全部平台</option><option>GitHub</option><option>GitCode</option>
      </select></label>
      <label>资源<select value={filters.pool} onChange={(event) => updateFilter("pool", event.target.value)}>
        <option value="">全部资源</option>{pools.map((pool) => <option key={pool} value={pool}>{pool.replace("/", " / ")}</option>)}
      </select></label>
      <label>状态<select value={filters.status} onChange={(event) => updateFilter("status", event.target.value)}>
        <option value="">活动与历史</option><option value="active">仅活动</option><option value="history">仅历史</option>
      </select></label>
      <button className="button small" aria-expanded={showColumns} onClick={() => setShowColumns((open) => !open)}>
        <FunnelSimple size={16} />列筛选{activeColumnFilters > 0 && `（${activeColumnFilters}）`}
      </button>
    </div>
    <div className="workflow-count subtle">
      <div className="workflow-count-main">
        <span role="status">{activeMatches.length} 个活动 Workflow · 已显示 {shownHistory.length} / {historicalMatches.length} 个历史 Workflow</span>
        {windowedHistory.length > 0 && <button className="button small" aria-expanded={!historyCollapsed}
          onClick={() => setHistoryCollapsed((collapsed) => !collapsed)}>{historyCollapsed ? "展开历史 Workflow" : "折叠历史 Workflow"}</button>}
        {!historyCollapsed && (limit < historicalMatches.length || historyHasMore) && <button className="button small" onClick={loadHistory}>{limit ? "再加载" : "加载"} 50 个历史 Workflow</button>}
        {activeColumnFilters > 0 && <button className="text-button" onClick={() => { setColumnFilters({}); setLimit((loaded) => loaded ? pageSize : 0); }}>清除列筛选</button>}
      </div>
      <div className="workflow-count-main">{sort && <button className="text-button" onClick={() => setSort(null)}>恢复队列顺序</button>}<span>北京时间 (UTC+8)</span></div>
    </div>
    <div className="table-scroll workflow-table-scroll">
      <table className="data-table workflow-grid" aria-label="Workflow 活动与历史">
        <thead>
          <tr>{columns.map((column) => <SortHeader key={column.key} column={column} sort={sort} change={changeSort} />)}<th scope="col">操作</th></tr>
          {showColumns && <tr className="workflow-column-filters">{columns.map(({ key, label, numeric }) => <th key={key}>
            <input type="text" aria-label={`筛选${label}`} placeholder={numeric ? "如 >=8" : "筛选"} title={numeric ? "支持等值及 >、>=、<、<=" : `筛选${label}`} value={columnFilters[key] ?? ""}
              onChange={(event) => updateColumn(key, event.target.value)} />
          </th>)}<th /></tr>}
        </thead>
        <tbody>{visible.map((entry) => {
          const { run, summary, sequence } = entry;
          const key = workflowKey(run);
          const isOpen = Boolean(expanded[key]);
          const Expand = isOpen ? Minus : Plus;
          return <Fragment key={key}>
            <tr aria-label={`Workflow #${run.id} · 尝试 ${run.attempt}`} className={isOpen ? "run-expanded" : undefined}
              ref={entry === boundary ? historyBoundary : undefined} tabIndex={entry === boundary ? -1 : undefined}>
              <td><div className="workflow-sequence"><button className="expand-workflow" aria-label={`${isOpen ? "收起" : "展开"} Workflow #${run.id} 的子用例`} aria-expanded={isOpen}
                onClick={() => setExpanded((previous) => ({ ...previous, [key]: !isOpen }))}><Expand size={15} weight="bold" /></button>
                <span className={`mono ${sequence < 0 ? "subtle" : ""}`}>{sequence}</span>
              </div></td>
              <td><WorkflowTime value={run.submitted} /></td>
              <td><strong>{run.repo}</strong><small>{run.platform} · {run.owner}</small></td>
              <td className="workflow-pools">{summary.pools.map((pool) => <span key={pool}>{pool.replace("/", " / ")}</span>)}</td>
              <td>{summary.maxCards}</td>
              <td><a className="text-button run-id" href={actionsUrl(run)} target="_blank" rel="noreferrer" title={`平台 Actions 链接 · ${run.id}`}>
                {run.action}{run.number == null ? "" : ` #${run.number}`} <ArrowSquareOut size={13} />
              </a><small>尝试 {run.attempt}{run.number == null ? "" : ` · ID ${run.id}`}</small></td>
              <td title={summary.active ? "尚未产生最终结果，执行阶段见子用例" : undefined}><Status value={summary.result} /></td>
              <td>@{run.actor}</td><td>{run.trigger}{run.pr && <small>#{run.pr} · @{run.author}</small>}</td>
              <td><WorkflowTime value={summary.start} /></td>
              <td><WorkflowTime value={summary.end} /></td>
              <td title="Σ（各子用例实际分配卡数 × 实际执行分钟数）÷ 60，单位为卡·小时">
                {summary.cardHours == null ? <span className="subtle">—</span> : <>{cardHours.format(summary.cardHours)}{summary.active && <small>截至当前</small>}{summary.partialCardHours && <small>统计不完整</small>}</>}
              </td>
              <td><WorkflowActions run={run} summary={summary} sequence={sequence} canManage={canManage} open={open}
                cancellation={cancellations[key]} priority={priorities[key]} /></td>
            </tr>
            {isOpen && <tr className="expanded-cases"><td colSpan={columnCount}>
              <CasesTable run={run} nodes={nodes} config={config} open={open} />
            </td></tr>}
          </Fragment>;
        })}
        {!visible.length && <tr><td className="empty" colSpan={columnCount}>
          {historyCollapsed && windowedHistory.length ? "历史 Workflow 已折叠" : "当前筛选没有找到 Workflow"}
        </td></tr>}</tbody>
      </table>
    </div>
  </main>;
}
