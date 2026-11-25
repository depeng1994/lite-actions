import { CalendarBlank, CaretDown, CaretRight, Pause, Play, Plus, Ruler, Trash, X } from "@phosphor-icons/react";
import { memo, useCallback, useEffect, useEffectEvent, useLayoutEffect, useMemo, useRef, useState } from "react";
import { displayNodeHost, flagFor, plannedTime, poolId, runs, statusTone, targetFor, timelineClock, timelineLabel, timelineRuns, workers, workflowKey } from "./formal-data.js";
import { commandStatusLabel, commandStatusTone } from "./command-status.js";
import { lastSeenDateTime, resourceHealth } from "./resource-health.js";
import { measuredRange, rangeDuration, timelineSecondAt, timelineTicks } from "./timeline-viewport.js";
import { computeElapsedMinutes } from "./workflows.js";

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
  return <span>{run.isTest && <><Tag tone="amber">TEST</Tag> </>}{run.pr ? `@${run.author} #${run.pr}` : `${run.trigger}触发`} <span className="middot">·</span> {run.action} #{run.id}</span>;
}

const rowHeight = { worker: 39, pool: 39, group: 54 };
const baseChartWidth = 3000;
const maxChartWidth = 12_288_000;
const plotInset = 64;
const timelineShortcuts = [
  ["w", "放大"], ["s", "缩小"], ["a", "左移"], ["d", "右移"],
];
const occupancyLabels = {unknown: "当前未知占用", leased: "当前实际租约", idle: "当前已确认空闲"};
const externalOccupancyLabel = "探测到外部占用";

function groupCardCount(node, group) {
  if (node.groupCards?.[group] != null) return node.groupCards[group];
  const bounds = String(group || "").match(/\d+/g)?.map(Number);
  if (bounds?.length === 2 && bounds[1] >= bounds[0]) return bounds[1] - bounds[0] + 1;
  return node.cards || 0;
}

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
        const hostLabel = displayNodeHost(node);
        for (const group of node.groups) {
          const occupancy = node.leased?.includes(group) ? "leased"
            : node.unknown.includes(group) ? "unknown"
            : node.confirmedIdle?.includes(group) ? "idle" : null;
          rows.push({ key: targetFor(node, group).id, worker, pool, node, group, occupancy, level: "group", label: `${hostLabel} 设备组 ${group}` });
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

function ResourceLabel({ row, folded, changeFold, config, selectScope, showResource }) {
  const { key, label, level, node, group, occupancy, worker, pool, height } = row;
  const GroupMark = folded[key] ? CaretRight : CaretDown;
  const flag = level === "group" ? flagFor(config, node, group) : config.flags[key];
  return <div className={`resource resource-${level} ${worker.toLowerCase()}`} style={{ height }}>
    {level === "group"
      ? <button className="tree-leaf" onClick={() => showResource(targetFor(node, group))}>
          <span className="resource-group-name" title={label}><span className="mono">{displayNodeHost(node)}</span> 设备组 {group}</span>
          <span className="resource-group-size subtle">（{groupCardCount(node, group)}P）</span>
          {occupancy && <span className={`resource-occupancy occupancy-${occupancy}`} title={occupancyLabels[occupancy]}>
            <span className="sr-only">{occupancyLabels[occupancy]}</span>
          </span>}
        </button>
      : <button className="tree-item" onClick={() => {
          changeFold(key);
          if (pool) selectScope(poolId(worker, pool));
        }} aria-expanded={!folded[key]}>
          <GroupMark size={16} weight="bold" />
          <strong className={level === "node" ? "mono" : undefined}>{label}</strong>
          {level === "worker" && <span>（Worker）</span>}
          {level === "pool" && <span>（Pool）</span>}
        </button>}
    {flag && <span className="resource-flag" title={flag}> {flag === "暂停" ? "暂停调度" : flag}</span>}
  </div>;
}

function OccupancyHistory({ row, clockState, showResource }) {
  const { timelineStart, timelineEnd } = clockState;
  const width = timelineEnd - timelineStart;
  return (row.node?.occupancyBlocks?.[row.group] || []).map((block) => {
    const start = Math.max(timelineStart, block.started_at / 60);
    const end = Math.min(timelineEnd, (block.ended_at ?? block.last_busy_at) / 60);
    if (start > end) return null;
    const lastBusy = timelineLabel(block.last_busy_at / 60, true);
    const ending = block.ended_at == null
      ? `最后确认占用 ${lastBusy}，释放时间未知`
      : `最后确认占用 ${lastBusy}，首次确认空闲 ${timelineLabel(block.ended_at / 60, true)}`;
    const title = `${row.label} · ${externalOccupancyLabel} · 首次发现 ${timelineLabel(block.started_at / 60, true)} · ${ending}`;
    return <button key={block.started_at} type="button" className="occupancy-history occupancy-unknown"
      style={{ left: `${(start - timelineStart) / width * 100}%`, width: `${(end - start) / width * 100}%`,
        top: row.top + row.height / 2 - 4 }}
      title={title} aria-label={title}
      onClick={() => showResource(targetFor(row.node, row.group), block.started_at)} />;
  });
}

const LeaseBar = memo(function LeaseBar({ item, run, rows, config, nodes, clockState, plotWidth, selected, onSelect }) {
  const { now: timelineNow, timelineStart, timelineEnd } = clockState;
  const marker = (minute) => `${(minute - timelineStart) / Math.max(1, timelineEnd - timelineStart) * 100}%`;
  const queued = item.status === "排队中";
  const actual = item.node && ["准备中", "运行中", "清理中"].includes(item.status);
  const ended = item.node && item.timelineFinished != null && ["成功", "失败", "已取消"].includes(item.status);
  const poolRows = rows.filter((row) => row.node && row.worker === item.worker && row.pool === item.pool);
  const resourceRowsList = queued
    ? poolRows.filter((row) => row.node.id === item.plannedNode && item.plannedGroups?.includes(row.group))
    : poolRows.filter((row) => item.allocations?.some((allocation) => row.node.id === allocation.node_ref
        && row.node.groupDeviceIds[row.group].some((id) => allocation.devices.includes(id))));
  if (!resourceRowsList.length || (!queued && !actual && !ended)) return null;
  const start = queued
    ? (plannedTime(item, config, nodes) == null ? null : item.timelinePlanned)
    : item.timelineStarted;
  if (start == null) return null;
  const projectedEnd = queued
    ? item.timelinePlannedEnd ?? (item.estimate ? start + item.estimate : null)
    : actual && item.estimate && item.timelineStarted != null && item.timelineStarted + item.estimate > timelineNow
      ? item.timelineStarted + item.estimate
      : null;
  const finish = ended ? item.timelineFinished : projectedEnd ?? timelineNow;
  const visibleStart = Math.max(start, timelineStart);
  const total = Math.min(finish, timelineEnd) - visibleStart;
  if (total <= 0) return null;
  const compact = total / (timelineEnd - timelineStart) * plotWidth < 84;
  const solid = queued ? 0 : Math.max(0, Math.min(100, (timelineNow - visibleStart) / total * 100));
  const elapsed = actual
    ? computeElapsedMinutes(
        item.start != null || item.started != null || item.elapsed != null
          ? item
          : { ...item, start: item.timelineStarted != null ? item.timelineStarted * 60000 : null },
        clockState.nowMs ?? Date.now(),
      )
    : null;
  const startLabel = timelineLabel(start);
  const finishLabel = timelineLabel(finish);
  const style = {
    "--actual": `${solid}%`,
    left: marker(visibleStart),
    width: marker(timelineStart + total),
  };
  const ariaLabel = `${run.repo} #${run.pr ?? run.id} · ${item.name} · ${item.status} · ${resourceRowsList.map((row) => `${displayNodeHost(row.node)} 设备组 ${row.group}`).join("、")}`;
  return resourceRowsList.map((row) => <button key={row.key} className={`lease ${item.worker.toLowerCase()} ${selected ? "lease-selected" : ""} ${ended ? "lease-ended" : ""} ${compact ? "lease-compact" : ""}`}
    aria-label={ariaLabel} title={`${ariaLabel}\n${startLabel} → ${finishLabel}`}
    data-midpoint={visibleStart + total / 2}
    aria-pressed={selected} aria-controls={selected ? "timeline-detail" : undefined}
    onClick={() => onSelect(item.id)} style={{ ...style, top: row.top + 6, height: row.height - 12 }}>
    {!queued && <span className="lease-actual" />}
    {projectedEnd && <span className="lease-planned" />}
    {!compact && <span className="lease-label">
      <strong>{item.name} · {item.cards}P</strong>
      <small>{run.repo} <span className="lease-author">· @{run.author ?? run.actor} #{run.pr ?? run.id}</span> / {run.action} #{run.id}</small>
      {actual && <small className="lease-meta">{item.status} · 已用 {elapsed ?? 0} 分钟 · {item.estimate ? `预计 ${item.estimate} 分钟（${item.samples} 样本）` : "暂无估算"}</small>}
    </span>}
    {!compact && start > timelineStart && <span className="lease-time lease-start">{startLabel}</span>}
    {actual && projectedEnd && <span className="lease-time lease-end">{finishLabel}</span>}
  </button>);
});

function InlineDetailSlot({ selected, children }) {
  const panel = useRef(null);
  useLayoutEffect(() => {
    const heading = panel.current?.querySelector("header");
    if (heading?.getBoundingClientRect().bottom > window.innerHeight) {
      heading.scrollIntoView({block: "nearest"});
    }
  }, [selected]);
  return <div className="inline-detail-slot" ref={panel}>{children}</div>;
}

export function PoolsView({ nodes, cases, timelineCases, config, go, live, renderTimelineDetail, renderResourceDetail }) {
  const [folded, setFolded] = useState({});
  const [queueScope, selectScope] = useState("");
  const [selected, setSelected] = useState(null);
  const [clockState, setClockState] = useState(() => timelineClock());
  const [zoom, setZoom] = useState(0);
  const [viewport, setViewport] = useState({ left: 0, width: 0 });
  const [measured, setMeasured] = useState(null);
  const [measuring, setMeasuring] = useState(null);
  const timelineScrollRef = useRef(null);
  const gesture = useRef(null);
  const suppressClick = useRef(false);
  const previousStart = useRef(null);
  const pointer = useRef(null);
  const zoomAnchor = useRef(null);
  useEffect(() => {
    const timer = setInterval(() => setClockState(timelineClock()), 30_000);
    return () => clearInterval(timer);
  }, []);
  useEffect(() => {
    setClockState(timelineClock());
  }, [cases, timelineCases]);
  const { dateLabel, now: timelineNow, timelineStart, timelineEnd } = clockState;
  const chartWidthAt = (level) => {
    const preferred = Math.max(baseChartWidth, viewport.width) * 2 ** level;
    return Math.min(maxChartWidth, Math.max(preferred, viewport.width, 2 * plotInset + 1));
  };
  const chartWidth = chartWidthAt(zoom);
  const plotWidth = chartWidth - 2 * plotInset;
  const pixelsPerMinute = plotWidth / (timelineEnd - timelineStart);
  const marker = (minute) => `${(minute - timelineStart) / (timelineEnd - timelineStart) * 100}%`;
  const {ticks, withSeconds} = timelineTicks(timelineStart, timelineEnd, plotWidth, viewport.left - plotInset, viewport.width);
  const range = measuring ?? measured;
  const rangeStart = range && Math.max(timelineStart * 60, range.start);
  const rangeEnd = range && Math.min(timelineEnd * 60, range.end);
  const duration = range && rangeDuration(range.end - range.start);
  const visibleLeft = viewport.left - plotInset;
  const visibleRight = visibleLeft + viewport.width;
  const bandLeft = Math.max(visibleLeft, (rangeStart / 60 - timelineStart) * pixelsPerMinute);
  const bandRight = Math.min(visibleRight, (rangeEnd / 60 - timelineStart) * pixelsPerMinute);
  const captionInset = Math.min(95, viewport.width / 2);
  const captionLeft = Math.max(visibleLeft + captionInset, Math.min(visibleRight - captionInset, (bandLeft + bandRight) / 2));
  const chartCases = timelineCases || cases;
  const runByKey = useMemo(() => new Map([...timelineRuns].reverse().map((run) => [workflowKey(run), run])), [chartCases]);
  const selectedItem = selected?.caseId && chartCases.find((item) => item.id === selected.caseId);
  const selectedRun = selectedItem && runByKey.get(selectedItem.run);
  const closeDetail = () => setSelected(null);
  const selectCase = useCallback((caseId) => setSelected({ caseId }), []);
  const showResource = (target, blockStart) => setSelected({ target, blockStart });
  const measureViewport = (scroll) => {
    const {scrollLeft, clientWidth} = scroll;
    setViewport((current) => current.left === scrollLeft && current.width === clientWidth
      ? current : {left: scrollLeft, width: clientWidth});
  };
  useLayoutEffect(() => {
    const scroll = timelineScrollRef.current;
    const observer = new ResizeObserver(() => measureViewport(scroll));
    observer.observe(scroll);
    return () => observer.disconnect();
  }, []);
  useLayoutEffect(() => {
    const scroll = timelineScrollRef.current;
    const anchor = zoomAnchor.current;
    const atRightEdge = scroll.scrollWidth - scroll.clientWidth - scroll.scrollLeft <= 2;
    if (anchor) {
      scroll.scrollLeft = plotInset + (anchor.minute - timelineStart) * pixelsPerMinute - anchor.offset;
      zoomAnchor.current = null;
    } else if (previousStart.current == null || (zoom <= 0 && atRightEdge)) {
      scroll.scrollLeft = scroll.scrollWidth;
    } else if (previousStart.current !== timelineStart) {
      scroll.scrollLeft += (previousStart.current - timelineStart) * pixelsPerMinute;
    }
    previousStart.current = timelineStart;
    measureViewport(scroll);
  }, [chartWidth, pixelsPerMinute, timelineStart, zoom]);

  const handleTimelineKey = useEffectEvent((event) => {
    if (event.defaultPrevented || event.isComposing || event.altKey || event.ctrlKey || event.metaKey
      || document.querySelector("dialog[open]") || event.target.closest?.("input, textarea, select, [contenteditable]")) return;
    const key = event.key.toLowerCase();
    if (key === "escape" && (gesture.current || measured || selected)) {
      if (gesture.current) {
        gesture.current = null;
        suppressClick.current = true;
        setMeasuring(null);
      } else if (measured) {
        setMeasured(null);
      } else {
        setSelected(null);
      }
      event.preventDefault();
      return;
    }
    if (!timelineShortcuts.some(([shortcut]) => shortcut === key)) return;
    event.preventDefault();
    moveTimeline(key, true);
  });
  useEffect(() => {
    const handleKey = (event) => handleTimelineKey(event);
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, []);

  function moveTimeline(key, usePointer = false) {
    if (gesture.current) return;
    const scroll = timelineScrollRef.current;
    if (key === "a" || key === "d") {
      scroll.scrollLeft += scroll.clientWidth * (key === "a" ? -0.3 : 0.3);
      return;
    }
    const next = zoom + (key === "w" ? 1 : -1);
    if (chartWidthAt(next) === chartWidth) return;
    const position = usePointer ? pointer.current : null;
    const bounds = scroll.getBoundingClientRect();
    const offset = position ? position.x - bounds.left : scroll.clientWidth / 2;
    const lease = position && document.elementFromPoint(position.x, position.y)?.closest(".lease");
    // The minimum hit area of a very short lease can be wider than its actual time span.
    const minute = lease && lease.offsetWidth < 16 ? Number(lease.dataset.midpoint)
      : timelineStart + (scroll.scrollLeft + offset - plotInset) / pixelsPerMinute;
    zoomAnchor.current = { offset, minute };
    setZoom(next);
  }

  function secondAt(clientX) {
    const scroll = timelineScrollRef.current;
    const offset = Math.max(0, Math.min(scroll.clientWidth, clientX - scroll.getBoundingClientRect().left));
    return timelineSecondAt(scroll.scrollLeft + offset - plotInset, plotWidth, timelineStart, timelineEnd);
  }

  function startMeasurement(event) {
    if (event.button !== 0 || !event.isPrimary || event.pointerType === "touch") return;
    suppressClick.current = false;
    gesture.current = {id: event.pointerId, x: event.clientX, start: secondAt(event.clientX), active: false};
    const owner = event.target.closest("button") || event.currentTarget;
    owner.setPointerCapture(event.pointerId);
  }

  function updateMeasurement(event) {
    const drag = gesture.current;
    if (drag?.id !== event.pointerId || (!drag.active && Math.abs(event.clientX - drag.x) < 4)) return;
    drag.active = true;
    setMeasuring(measuredRange(drag.start, secondAt(event.clientX)));
  }

  function finishMeasurement(event) {
    const drag = gesture.current;
    if (drag?.id !== event.pointerId) return;
    gesture.current = null;
    if (drag.active || Math.abs(event.clientX - drag.x) >= 4) {
      suppressClick.current = true;
      setMeasured(measuredRange(drag.start, secondAt(event.clientX)));
      setMeasuring(null);
    }
  }

  function cancelMeasurement(event) {
    if (gesture.current?.id !== event.pointerId) return;
    gesture.current = null;
    setMeasuring(null);
  }

  const rows = useMemo(() => resourceRows(nodes, folded), [nodes, folded]);
  const scopes = [...new Set([...nodes, ...cases].map((item) => poolId(item.worker, item.pool)))];
  useEffect(() => {
    if (!scopes.includes(queueScope)) selectScope(scopes[0] || "");
  }, [queueScope, scopes.join("|")]);
  const queue = cases.filter((item) => item.status === "排队中" && queueScope === poolId(item.worker, item.pool))
    .sort((a, b) => a.position - b.position);
  return <main className="pools-view">
    <section aria-labelledby="pools-title">
      <div className="pools-header">
        <div className="pools-heading">
          <h1 id="pools-title">Pools任务看板</h1>
          <div className="timeline-tools">
            <span className="timeline-measure-hint"><Ruler size={16} aria-hidden="true" />拖选时间轴测量耗时</span>
            <div className="timeline-shortcuts" role="group" aria-label="时间轴快捷键">
              {timelineShortcuts.map(([key, label]) => <button key={key} type="button" aria-keyshortcuts={key.toUpperCase()}
                title={key === "w" ? "将鼠标指向任务后按 W，可放大查看标签" : undefined}
                onClick={() => moveTimeline(key)}><kbd>{key.toUpperCase()}</kbd> {label}</button>)}
            </div>
          </div>
        </div>
        <div className="pools-meta">
          <div className="date-info">
            <span><CalendarBlank size={21} /> <time>{dateLabel}</time></span>
            <span>Asia/Shanghai (UTC+8)</span><span>近72小时</span><span>现在 {timelineLabel(timelineNow)}（{live ? "实时" : "等待连接"}）</span>
          </div>
          <ul className="legend" aria-label="时间轴图例">
            <li><i className="legend-actual" />实际租约</li>
            <li><i className="legend-planned" />预计使用</li>
            <li><i className="legend-occupancy legend-occupancy-history occupancy-unknown" />{externalOccupancyLabel}</li>
            <li><i className="legend-occupancy occupancy-unknown" />{occupancyLabels.unknown}</li>
            <li><i className="legend-occupancy occupancy-idle" />{occupancyLabels.idle}</li>
          </ul>
        </div>
      </div>
      <div className="timeline-board" aria-label="Blue 与 Yellow 资源时间轴">
        <div className="timeline-fixed">
          <div className="axis-label">资源 <span>（Worker / Pool / 节点 / 设备组）</span></div>
          <div className="resources">
            {rows.map((row) => <ResourceLabel key={row.key} row={row} config={config} folded={folded}
              selectScope={selectScope} showResource={showResource}
              changeFold={(key) => setFolded((previous) => ({ ...previous, [key]: !previous[key] }))} />)}
          </div>
        </div>
        <div className="timeline-scroll" ref={timelineScrollRef} onScroll={(event) => measureViewport(event.currentTarget)}
          onPointerMove={(event) => { pointer.current = { x: event.clientX, y: event.clientY }; }}
          onPointerLeave={() => { pointer.current = null; }}>
          <div className={`timeline${measuring ? " timeline-measuring" : ""}`} style={{ width: chartWidth, "--plot-inset": `${plotInset}px` }}
            onPointerDown={startMeasurement} onPointerMove={updateMeasurement} onPointerUp={finishMeasurement}
            onPointerCancel={cancelMeasurement} onLostPointerCapture={cancelMeasurement}
            onClickCapture={(event) => {
              if (!suppressClick.current || event.detail === 0) return;
              suppressClick.current = false;
              event.preventDefault();
              event.stopPropagation();
            }}>
            <div className="axis">
              <div className="chart-coordinates">
                {ticks.map((minute) => <span key={minute} className="axis-tick mono" style={{ left: marker(minute) }}>{timelineLabel(minute, withSeconds)}</span>)}
                <span className="now-label mono" style={{ left: marker(timelineNow) }}>现在 {timelineLabel(timelineNow)}</span>
              </div>
            </div>
            <div className="lanes">
              {rows.map((row) => <div key={row.key} className={`lane lane-${row.level} ${row.worker.toLowerCase()}`} style={{ height: row.height }} />)}
              <div className="chart-coordinates">
                {ticks.map((minute) => <i key={minute} className="grid-line" style={{ left: marker(minute) }} />)}
                {rows.map((row) => <OccupancyHistory key={row.key} row={row} clockState={clockState} showResource={showResource} />)}
                {rows.filter((row) => row.occupancy && row.occupancy !== "leased").map((row) => (
                  <button key={row.key} type="button" className={`occupancy-marker occupancy-${row.occupancy}`}
                    style={{ left: marker(timelineNow), top: row.top + row.height - 8 }}
                    aria-label={`${row.label} · ${occupancyLabels[row.occupancy]}`} title={occupancyLabels[row.occupancy]}
                    onClick={() => showResource(targetFor(row.node, row.group))} />
                ))}
                {chartCases.map((item) => {
                  const run = runByKey.get(item.run);
                  return run ? <LeaseBar key={item.id} item={item} run={run}
                    rows={rows} config={config} nodes={nodes} clockState={clockState} plotWidth={plotWidth}
                    selected={selected?.caseId === item.id} onSelect={selectCase} /> : null;
                })}
                <i className="now-line" style={{ left: marker(timelineNow) }} />
              </div>
            </div>
            {range && rangeStart <= rangeEnd && <div className="chart-coordinates timeline-measure-layer" aria-hidden="true">
              <div className="timeline-measure-band" style={{left: marker(rangeStart / 60), width: marker(timelineStart + (rangeEnd - rangeStart) / 60)}} />
              {bandRight >= bandLeft && <span className="timeline-measure-caption mono" style={{left: captionLeft}}>{duration}</span>}
            </div>}
          </div>
        </div>
      </div>
      {range && <div className="timeline-measure-readout">
        <div className="timeline-measure-values" role={measuring ? undefined : "status"}>
          <span><Ruler size={18} aria-hidden="true" />选区耗时 <strong>{duration}</strong></span>
          <span className="mono timeline-measure-times">{timelineLabel(range.start / 60, true)} → {timelineLabel(range.end / 60, true)}</span>
        </div>
        <button className="button small" type="button" onClick={() => setMeasured(null)} disabled={Boolean(measuring)}>清除选区</button>
      </div>}
    </section>
    {(selectedRun || selected?.target) && <InlineDetailSlot selected={selected}>
      {selectedRun
        ? renderTimelineDetail(selectedItem, selectedRun, closeDetail)
        : renderResourceDetail({ id: "timeline-detail", target: selected.target, blockStart: selected.blockStart, nodes, close: closeDetail })}
    </InlineDetailSlot>}
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
              const run = runs.find((candidate) => workflowKey(candidate) === item.run);
              if (!run) return null;
              const estimate = plannedTime(item, config, nodes) == null ? null : item.timelinePlanned;
              return <tr className={item.id === selected?.caseId ? "is-selected" : ""} key={item.id}>
                <td><button className="cell-title" onClick={() => selectCase(item.id)}>{item.name}</button><small>{run.repo} · {run.owner}</small></td>
                <td><RunIdentity run={run} /></td>
                <td>{item.worker} / {item.pool} · {item.cards}P</td>
                <td>{item.projectionStatus === "WAITING_EXTERNAL"
                  ? "等待外部释放"
                  : estimate == null ? "暂无估算" : <><span>约 {timelineLabel(estimate)}</span><small>排期预估</small></>}
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
  const own = state;
  const pending = config.pending;
  const paused = own === "暂停" || own === "排空中";
  const action = paused ? "恢复" : "暂停";
  const label = paused ? "恢复调度" : "暂停";
  const Icon = action === "暂停" ? Pause : Play;
  return <div className="cluster-actions">
    <button className="button small" disabled={Boolean(pending) || (!own && state != null)} onClick={() => open({ type: "config", target, action })}><Icon size={14} />{label}</button>
    <button className={`button small ${own === "排空中" ? "danger-subtle" : ""}`} disabled={Boolean(pending) || (!own && state != null)} onClick={() => open({ type: "config", target, action: own === "排空中" ? "删除" : "排空" })}>
      <Trash size={14} />{own === "排空中" ? "删除" : "排空删除"}
    </button>
  </div>;
}

export function ResourceStatus({ health, configStatus, unknown, quarantined = false }) {
  return <div className="resource-status">
    <Tag tone={health.tone}>{health.status}</Tag>
    {configStatus && <Tag tone="amber">{configStatus === "暂停" ? "暂停调度" : configStatus}</Tag>}
    {quarantined && <Tag tone="red">已隔离</Tag>}
    {unknown && <Tag tone="red">未知占用</Tag>}
  </div>;
}

function ClusterRow({ target, status, nodes, config, canManage, open, showResource, node, group }) {
  const health = resourceHealth(node ? [node] : nodes, group);
  const quarantined = Boolean(group ? node.quarantinedGroups?.includes(group) : node?.quarantinedGroups?.length);
  return <tr>
    <td><button className={`resource-link ${group ? "group-link" : node ? "node-link" : ""}`} onClick={() => showResource(target)}>
      {group ? <><span className="mono">{displayNodeHost(node)}</span> 设备组 {group}</> : node ? <span className="mono">{displayNodeHost(node)}</span> : `Pool ${target.pool}`} <CaretRight size={13} />
    </button></td>
    <td>{node ? <span className="mono">{displayNodeHost(node)}</span> : <span className="subtle">—</span>}</td>
    <td>{group ? `${groupCardCount(node, group)} 卡` : node ? `${node.cards} 卡 · ${node.groups.length} 组` : `${nodes.length} 节点 · ${nodes.reduce((sum, member) => sum + member.cards, 0)} 卡`}</td>
    <td><ResourceStatus health={health} configStatus={status} quarantined={quarantined} unknown={Boolean(group && node.unknown.includes(group))} /></td>
    <td className="subtle mono" title={group ? "该设备组上一次查到全部配置卡的时间" : "成员中最近一次查到配置卡的时间"}>{lastSeenDateTime(health.lastSeen)}</td>
    {canManage && <td><ClusterControls target={target} state={status} config={config} open={open} /></td>}
  </tr>;
}

function syncStatusBadge(workerInfo, appliedRevision, desiredRevision) {
  if (workerInfo?.status === "PARTIAL_APPLIED") {
    return { tone: commandStatusTone("PARTIAL_APPLIED"), label: `${commandStatusLabel("PARTIAL_APPLIED")}（存在不可达节点）` };
  }
  if (workerInfo?.status === "FAILED") {
    return { tone: commandStatusTone("FAILED"), label: "应用失败" };
  }
  if (appliedRevision != null && appliedRevision === desiredRevision) return { tone: "teal", label: "已同步" };
  return { tone: "amber", label: "等待 Worker 应用" };
}

export function ClusterView({ config, nodes, clusterPools = [], clusterHistory = [], rollbackCluster, canManage, open, audit, renderResourceDetail }) {
  const [worker, setWorker] = useState("Blue");
  const [selected, setSelected] = useState(null);
  const scoped = nodes.filter((node) => node.worker === worker);
  const pools = [...new Set([...scoped.map((node) => node.pool), ...clusterPools.filter((entry) => entry.worker === worker).map((entry) => entry.pool)])];
  const appliedRevision = config.applied[worker];
  const workerInfo = config.workerStatus?.[worker];
  const desiredRevision = workerInfo?.desired ?? config.desired;
  const syncBadge = syncStatusBadge(workerInfo, appliedRevision, desiredRevision);
  return <main className="standard-view">
    <div className="page-heading">
      <div><h1>集群管理</h1><p>查看 Worker、Pool、节点与设备组的配置及生效状态。</p></div>
      <button className={`button ${canManage ? "primary" : ""}`} disabled={Boolean(config.pending)}
        onClick={() => open(canManage ? { type: "add-node", worker } : { type: "login" })}>{canManage ? <><Plus size={17} weight="bold" />新增资产</> : "登录后管理"}</button>
    </div>
    <nav className="subnav" aria-label="Worker 页签">
      {workers.map((name) => {
        const info = config.workerStatus?.[name];
        const commitSha = info?.codeSha || "";
        const shortSha = commitSha ? commitSha.slice(0, 7) : "—";
        const commitTitle = commitSha
          ? `当前运行 Commit: ${commitSha}${info?.offline ? "（离线）" : ""}`
          : "暂无上报 Commit ID";
        return <button key={name} aria-current={name === worker ? "page" : undefined} onClick={() => {
          if (name === worker) return;
          setSelected(null);
          setWorker(name);
        }}>
          <i className={`worker-dot ${name.toLowerCase()}`} />
          <span>{name}</span>
          <code className={`worker-commit${!commitSha || info?.offline ? " is-muted" : ""}`} title={commitTitle}>
            {shortSha}
          </code>
        </button>;
      })}
    </nav>
    <div className="revision-bar">
      <strong>{worker}</strong><Tag tone={workerInfo?.offline ? "red" : "teal"}>{workerInfo?.offline ? "离线" : "在线"}</Tag>
      <span>当前版本 <code>{appliedRevision == null ? "—" : `r${appliedRevision}`}</code></span>
      <span>期望版本 <code>{desiredRevision == null ? "—" : `r${desiredRevision}`}</code></span>
      <Tag tone={syncBadge.tone}>{syncBadge.label}</Tag>
      <span className="revision-updated">{workerInfo?.status ? commandStatusLabel(workerInfo.status) : "等待心跳"}</span>
      {canManage && workerInfo?.error && <span className="subtle" title={workerInfo.error}>原因：{workerInfo.error}</span>}
    </div>
    <div className="table-scroll">
      <table className="data-table cluster-table">
        <thead><tr><th>资源</th><th>节点地址</th><th>容量</th><th>状态</th><th>最近在线时间</th>{canManage && <th>操作</th>}</tr></thead>
        <tbody>{pools.map((pool) => {
          const members = scoped.filter((node) => node.pool === pool);
          const target = { id: poolId(worker, pool), label: `${worker} / ${pool}`, worker, pool, type: "Pool" };
          return <ClusterPool key={pool} target={target} members={members} config={config} open={open} canManage={canManage} showResource={setSelected} />;
        })}
        {!pools.length && <tr><td className="empty" colSpan={canManage ? 6 : 5}>暂无已配置资源</td></tr>}
        </tbody>
      </table>
    </div>
    {selected && <InlineDetailSlot selected={selected}>
      {renderResourceDetail({ id: "cluster-detail", target: selected, nodes, close: () => setSelected(null) })}
    </InlineDetailSlot>}
    {canManage && <section className="audit-section" aria-label="历史配置版本">
      <h2>历史配置版本</h2>
      <div className="table-scroll"><table className="data-table">
        <thead><tr><th>版本</th><th>创建时间</th><th>操作账号</th><th>来源</th><th>操作</th></tr></thead>
        <tbody>{clusterHistory.map((revision) => <tr key={revision.revision}>
          <td className="mono">r{revision.revision}</td>
          <td className="mono">{revision.created_at ? new Date(revision.created_at * 1000).toLocaleString("zh-CN", {timeZone: "Asia/Shanghai"}) : "—"}</td>
          <td>{revision.created_by || "—"}</td><td>{revision.rollback_from_rev ? `回滚自 r${revision.rollback_from_rev}` : "发布"}</td>
          <td><button className="button small" disabled={Boolean(config.pending) || revision.revision === config.desired} onClick={() => rollbackCluster?.(revision.revision)}>回滚</button></td>
        </tr>)}
        {!clusterHistory.length && <tr><td className="empty" colSpan={5}>暂无历史配置版本</td></tr>}</tbody>
      </table></div>
    </section>}
    {canManage && <AuditList entries={audit.filter((entry) => entry.area === "cluster")} />}
  </main>;
}

function ClusterPool({ target, members, config, canManage, open, showResource }) {
  const properties = { config, canManage, open, showResource };
  return <>
    <ClusterRow {...properties} target={target} nodes={members} status={config.flags[target.id]} />
    {members.flatMap((node) => node.groups.length
      ? node.groups.map((group) => <ClusterRow key={`${node.id}/${group}`} {...properties} node={node} group={group}
          target={targetFor(node, group)} status={flagFor(config, node, group)} />)
      : [<ClusterRow key={node.id} {...properties} node={node} target={targetFor(node)} status={flagFor(config, node)} />])}
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
