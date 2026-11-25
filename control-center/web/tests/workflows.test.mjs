import assert from "node:assert/strict";
import {test} from "node:test";
import {beijingClock, displayNodeHost, now, plannedTime, timelineLabel, workflowKey} from "../src/formal-data.js";
import {commandStatusLabel, commandStatusTone} from "../src/command-status.js";
import {resourceInventories} from "../src/control-api.js";
import {computeElapsedMinutes, currentWorkflowQueue, groupActiveRuns, moveWorkflow, summarizeRun, workflowResultLabel} from "../src/workflows.js";
import {lastSeenDateTime, resourceHealth} from "../src/resource-health.js";
import {measuredRange, rangeDuration, timelineSecondAt, timelineTicks} from "../src/timeline-viewport.js";

const runA = {id: "run-a", platform: "GitHub", repo: "org/model", attempt: 1, action: "Smoke", model: "vllm-ascend"};
const runB = {id: "run-b", platform: "GitCode", repo: "org/model", attempt: 1, action: "Smoke", model: "vllm-ascend"};
const runC = {id: "run-c", platform: "GitHub", repo: "org/model", attempt: 1, action: "Smoke", model: "vllm-ascend"};
const keyA = workflowKey(runA);
const keyB = workflowKey(runB);
const keyC = workflowKey(runC);

function queuedCases() {
  return [
    {id: "a-blue", run: keyA, status: "排队中", worker: "Blue", pool: "A3", position: 1, cards: 8, kind: "npu_smoke"},
    {id: "b-blue", run: keyB, status: "排队中", worker: "Blue", pool: "A3", position: 2, cards: 8, kind: "npu_smoke"},
    {id: "c-blue", run: keyC, status: "排队中", worker: "Blue", pool: "A3", position: 3, cards: 8, kind: "npu_smoke"},
  ];
}

test("Workflow totals count measured allocations and exclude unleased cases", () => {
  const cases = [
    {id: "a", run: keyA, status: "运行中", worker: "Blue", pool: "A3", cards: 8, groups: ["0–7"], node: "node-a", started: Math.max(0, now - 100), node: "node-a"},
    {id: "b", run: keyA, status: "排队中", worker: "Yellow", pool: "A5", cards: 16, groups: [], started: null},
  ];
  const active = groupActiveRuns(cases, [{id: "node-a", worker: "Blue", pool: "A3"}], [runA]);
  const summary = summarizeRun(active[0]);
  assert.equal(summary.maxCards, 16);
  assert.equal(summary.active, true);
  assert.equal(summary.pools.join(" · "), "Blue/A3 · Yellow/A5");
  assert.equal(summary.cardHours > 0, true);
});

test("historical Workflow totals fall back to case identity without allocation", () => {
  const summary = summarizeRun({cases: [
    {historyId: "history-1", status: "成功", worker: "Blue", pool: "a3_shared_blue", cards: 8, allocatedCards: 0},
  ]});
  assert.deepEqual(summary.pools, ["Blue/a3_shared_blue"]);
  assert.equal(summary.result, "成功");
});

test("historical Workflow totals use persisted backend start and card-hour summary", () => {
  const summary = summarizeRun({
    startedAt: 1_000_000,
    finishedAt: 1_002_000,
    cardHours: 0.59,
    cardHoursPartial: false,
    cases: [{historyId: "history-2", status: "成功", worker: "Blue", pool: "a3_shared_blue", cards: 16, allocatedCards: 16}],
  });
  assert.equal(summary.start, 1_000_000);
  assert.equal(summary.end, 1_002_000);
  assert.equal(summary.cardHours, 0.59);
});

test("a queued Workflow swaps with its predecessor without mutating source cases", () => {
  const cases = queuedCases();
  const order = [keyA, keyB, keyC];
  const moved = moveWorkflow(cases, order, keyC);
  assert.deepEqual(currentWorkflowQueue(moved.cases, moved.order), [keyA, keyC, keyB]);
  assert.deepEqual([moved.from, moved.to], [3, 2]);
  assert.deepEqual(cases.map(({id, position}) => [id, position]), [["a-blue", 1], ["b-blue", 2], ["c-blue", 3]]);
  assert.equal(moveWorkflow(moved.cases, moved.order, keyA), null);
});

test("moving ahead updates positions across a shared queue", () => {
  const cases = queuedCases().map((item) => ({...item, planned: item.id === "b-blue" ? 100 : 200}));
  const moved = moveWorkflow(cases, [keyA, keyB, keyC], keyB);
  assert.equal(moved.cases.find(({id}) => id === "b-blue").position, 1);
  assert.equal(moved.cases.find(({id}) => id === "a-blue").position, 2);
  assert.equal(moved.cases.find(({id}) => id === "a-blue").planned, null);
});

test("resource health distinguishes online, probe misses, offline and unknown", () => {
  const node = {groups: ["0–7"], probes: {"0–7": {lastSeen: "12:00:00", missed: 0}}};
  assert.equal(resourceHealth([node], "0–7").status, "在线");
  assert.equal(resourceHealth([{...node, probes: {"0–7": {lastSeen: "12:00:00", missed: 4}}}], "0–7").status, "探测异常 4/5");
  assert.deepEqual(resourceHealth([{...node, probes: {"0–7": {lastSeen: "12:00:00", missed: 5}}}], "0–7"), {status: "已离线", tone: "red", lastSeen: "12:00:00", missed: 5});
  assert.equal(resourceHealth([{groups: ["0–7"], probes: {}}], "0–7").status, "待探测");
});

test("Workflow results preserve individual outcomes", () => {
  const result = (...statuses) => summarizeRun({cases: statuses.map((status) => ({status, worker: "Blue", pool: "A3", cards: 8, allocatedCards: 0}))}).result;
  assert.equal(result("运行中", "排队中"), "排队中");
  assert.equal(result("成功", "成功"), "成功");
  assert.equal(result("成功", "失败"), "失败");
  assert.equal(result("成功", "已取消"), "失败");
});

test("dynamic Beijing clock, direct host display, full timestamp formatting, and projection status handling", () => {
  const sampleClock = beijingClock(new Date("2026-11-23T02:15:00Z")); // 10:15 UTC+8
  assert.equal(sampleClock.currentDate, "2026-11-23");
  assert.equal(sampleClock.now, 10 * 60 + 15);
  assert.equal(displayNodeHost({id: "node-ref-1", displayHost: "113.xxx.xxx.205:153"}), "113.xxx.xxx.205:153");
  assert.equal(lastSeenDateTime("2026-11-22 23:59:59"), "2026-11-22 23:59:59");
  assert.equal(
    plannedTime({planned: 600, projectionStatus: "WAITING_EXTERNAL", plannedNode: "n1", plannedGroups: ["0–7"]}, {flags: {}}, [{id: "n1", worker: "Blue", pool: "A3"}]),
    null,
  );
  assert.equal(
    plannedTime({planned: 600, projectionStatus: "SCHEDULED", plannedNode: "n1", plannedGroups: ["0–7"]}, {flags: {}}, [{id: "n1", worker: "Blue", pool: "A3"}]),
    600,
  );
});

test("applied and desired inventories each show one node when the admin responses use different identifiers", () => {
  const host = "192.0.2.1:22";
  const appliedGroup = {devices: [0, 1], state: "CONFIRMED_IDLE"};
  const removedGroup = {devices: [2, 3], state: "UNKNOWN_BUSY"};
  const addedGroup = {devices: [4, 5], occupancy_state: "UNKNOWN_BUSY"};
  const desiredGroup = {devices: appliedGroup.devices, occupancy_state: "CONFIRMED_IDLE"};
  const timeline = {workers: [{worker_id: "blue", pools: [{pool_name: "A3", nodes: [
    {node_ref: "n_shared", host_id: host, device_groups: [appliedGroup, removedGroup]},
    {node_ref: "n_removed", host_id: "192.0.2.2:22", device_groups: [appliedGroup]},
  ]}]}]};
  const cluster = {config: {A3: {owner: "blue", nodes: [
    {host_id: host, allocation_groups: [desiredGroup, addedGroup]},
    {host_id: "192.0.2.3:22", allocation_groups: [addedGroup]},
  ]}}};
  const {nodes, clusterNodes, nodesByRef} = resourceInventories(timeline, cluster);
  assert.deepEqual(nodes.map(({id, groups}) => [id, groups]), [["n_shared", ["0–1", "2–3"]], ["n_removed", ["0–1"]]]);
  assert.deepEqual(clusterNodes.map(({id, groups}) => [id, groups]), [["n_shared", ["0–1", "4–5"]], ["192.0.2.3:22", ["4–5"]]]);
  assert.deepEqual(clusterNodes[0].unknown, ["4–5"]);
  assert.deepEqual(clusterNodes[0].confirmedIdle, ["0–1"]);
  assert.deepEqual(nodes[0].confirmedIdle, ["0–1"]);
  assert.deepEqual(nodes[0].unknown, ["2–3"]);
  assert.equal(nodesByRef.get("n_shared"), nodes[0]);
});

test("cluster occupancy is joined by worker, pool and exact device membership", () => {
  const node_ref = "n_shared";
  const block = {started_at: 120, last_busy_at: 240, ended_at: null};
  const desired = {devices: [3, 1, 0]};
  const applied = {devices: [0, 1, 3], occupancy_blocks: [block]};
  const timeline = {workers: [{worker_id: "blue", pools: [
    {pool_name: "one", nodes: [{node_ref, device_groups: [applied]}]},
    {pool_name: "two", nodes: [{node_ref, device_groups: [applied]}]},
  ]}]};
  const cluster = {config: {
    one: {owner: "blue", nodes: [{node_ref, allocation_groups: [desired]}]},
    two: {owner: "blue", nodes: [{node_ref, allocation_groups: [{devices: [0, 2, 3]}]}]},
    three: {owner: "yellow", nodes: [{node_ref, allocation_groups: [desired]}]},
  }};
  assert.deepEqual(resourceInventories(timeline, cluster).clusterNodes.map((node) => node.occupancyBlocks["0–3"]),
    [[block], [], []]);
});

test("timeline ticks include Beijing dates across midnight and expose seconds when zoomed", () => {
  const midnight = Date.parse("2026-11-22T16:00:00Z") / 60_000;
  assert.equal(timelineLabel(midnight + 187), "11-23 03:07");
  assert.equal(timelineLabel(midnight - 1), "11-22 23:59");

  const overview = timelineTicks(midnight - 720, midnight + 720, 3000, 0, 3000);
  assert.equal(overview.withSeconds, false);
  assert.ok(overview.ticks.includes(midnight));
  const detail = timelineTicks(midnight - 720, midnight + 720, 12_000_000, 6_000_000, 1000);
  assert.equal(detail.withSeconds, true);
  assert.ok(detail.ticks.length > 1 && detail.ticks.length < 12);
  assert.equal(timelineLabel(detail.ticks[0], true), "11-23 00:00:00");
  assert.notEqual(timelineLabel(detail.ticks[0], true), timelineLabel(detail.ticks[1], true));
});

test("timeline selection measures a reverse drag across midnight at second precision", () => {
  const midnight = Date.parse("2026-11-22T16:00:00Z") / 60_000;
  const secondAt = (offset) => timelineSecondAt(offset, 1200, midnight - 1, midnight + 1);
  const range = measuredRange(secondAt(810), secondAt(450));
  assert.equal(timelineLabel(range.start / 60, true), "11-22 23:59:45");
  assert.equal(timelineLabel(range.end / 60, true), "11-23 00:00:21");
  assert.equal(rangeDuration(range.end - range.start), "36 秒");
});

test("summarizeRun prefers formal backend resultLabel/result even when cases are empty or incomplete", () => {
  const emptyFailedHistory = summarizeRun({result: "FAIL", resultLabel: "失败", sequence: -1, cases: []});
  assert.equal(emptyFailedHistory.active, false);
  assert.equal(emptyFailedHistory.result, "失败");

  const partialPassingCasesFailedRun = summarizeRun({
    result: "FAIL",
    resultLabel: "失败",
    cases: [{historyId: "h1", status: "成功", worker: "Blue", pool: "A3", cards: 8, allocatedCards: 8}],
  });
  assert.equal(partialPassingCasesFailedRun.active, false);
  assert.equal(partialPassingCasesFailedRun.result, "失败");

  const mixedQueuedFromBackend = summarizeRun({
    result: "QUEUED",
    cases: [{historyId: "h2", status: "成功", worker: "Blue", pool: "A3", cards: 8, allocatedCards: 8}],
  });
  assert.equal(mixedQueuedFromBackend.active, true);
  assert.equal(mixedQueuedFromBackend.result, "排队中");

  assert.equal(workflowResultLabel("PASS"), "成功");
  assert.equal(workflowResultLabel("FAIL"), "失败");
  assert.equal(workflowResultLabel("QUEUED"), "排队中");
});

test("command status helpers map PARTIAL_APPLIED and all protocol statuses consistently", () => {
  assert.equal(commandStatusLabel("PARTIAL_APPLIED"), "部分生效");
  assert.equal(commandStatusTone("PARTIAL_APPLIED"), "amber");
  assert.equal(commandStatusLabel("APPLIED"), "已应用");
  assert.equal(commandStatusTone("APPLIED"), "teal");
  assert.equal(commandStatusLabel("FAILED"), "失败");
  assert.equal(commandStatusTone("FAILED"), "red");
  assert.equal(commandStatusLabel("CONFLICT"), "冲突");
  assert.equal(commandStatusTone("CONFLICT"), "red");
  assert.equal(commandStatusLabel("NOOP"), "无变化");
  assert.equal(commandStatusTone("NOOP"), "gray");
});

test("test workflow keys and queue helpers isolate test runs from production expedite controls", () => {
  const prodKey = workflowKey({platform: "GitHub", repo: "depeng1994/lite-actions", id: "500", attempt: 1});
  const testKey = workflowKey({platform: "GitHub", repo: "depeng1994/lite-actions", id: "500", attempt: 1, isTest: true});
  assert.notEqual(prodKey, testKey);
  const queue = currentWorkflowQueue([
    {id: "prod-1", run: prodKey, status: "排队中", kind: "npu_smoke", isTest: false},
    {id: "test-1", run: testKey, status: "排队中", kind: "npu_smoke", isTest: true},
    {id: "deploy-1", run: testKey, status: "排队中", kind: "test_worker_deploy", isTest: false},
  ], [prodKey, testKey]);
  assert.deepEqual(queue, [prodKey]);
});

test("computeElapsedMinutes and groupActiveRuns compute rounded non-negative elapsed minutes across live, skewed, fractional, and cross-midnight tasks", () => {
  const lateStart = Date.parse("2026-11-25T13:15:00+08:00");
  const lateNowMs = lateStart + 100_000;
  assert.equal(computeElapsedMinutes({start: lateStart, end: null, started: (now + 75) % 1440, elapsed: null}, lateNowMs), 1.67);

  const grouped = groupActiveRuns(
    [
      {id: "live-later", run: keyA, status: "运行中", worker: "Blue", pool: "A3", cards: 8, groups: ["0–7"], node: "node-a", start: lateStart, end: null, started: (now + 75) % 1440, elapsed: null},
      {id: "legacy-fixture", run: keyA, status: "运行中", worker: "Blue", pool: "A3", cards: 8, groups: ["0–7"], node: "node-a", started: 10 * 60 + 10, elapsed: null},
    ],
    [{id: "node-a", worker: "Blue", pool: "A3"}],
    [runA],
    lateNowMs,
  );
  assert.equal(grouped[0].cases[0].elapsed, 1.67);
  assert.equal(grouped[0].cases[1].elapsed, 186);

  assert.equal(computeElapsedMinutes({start: lateStart + 30_000, end: null, elapsed: null}, lateStart), 0);

  const crossMidnightStart = Date.parse("2026-11-25T23:58:00+08:00");
  const crossMidnightNowMs = Date.parse("2026-11-26T00:02:00+08:00");
  assert.equal(computeElapsedMinutes({start: crossMidnightStart, end: null, started: 23 * 60 + 58, elapsed: null}, crossMidnightNowMs), 4);

  assert.equal(computeElapsedMinutes({start: lateStart, end: null, elapsed: null}, lateStart + 40_000), 0.67);
  assert.equal(computeElapsedMinutes({elapsed: -75.33333333333337}, lateNowMs), 0);
  assert.equal(computeElapsedMinutes({elapsed: 0.6666666666666288}, lateNowMs), 0.67);
  assert.equal(computeElapsedMinutes({start: null, started: null, elapsed: null}, lateNowMs), null);
});
