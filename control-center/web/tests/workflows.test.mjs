import assert from "node:assert/strict";
import {test} from "node:test";
import {now, workflowKey} from "../src/formal-data.js";
import {currentWorkflowQueue, groupActiveRuns, moveWorkflow, summarizeRun} from "../src/workflows.js";
import {resourceHealth} from "../src/resource-health.js";

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
