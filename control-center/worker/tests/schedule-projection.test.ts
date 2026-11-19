import {expect, test} from "vitest";
import {estimate, projectSchedule} from "../src/services/schedule";

test("median uses at most the newest ten reliable samples", () => {
  expect(estimate([], 100)).toEqual({estimated_sec: null, sample_count: 0, estimate_status: "NO_ESTIMATE"});
  expect(estimate([11, 5, 7, 9, 3], 100)).toEqual({estimated_sec: 7, sample_count: 5, estimate_status: "ESTIMATED"});
  expect(estimate([12,11,10,9,8,7,6,5,4,3,2,1], 100, 90)).toEqual({
    estimated_sec: 7.5, sample_count: 10, estimate_status: "EXCEEDED",
  });
});

test("uses the worker's queue order and entire candidate combinations", () => {
  const active = {state: "LEASED" as const, release_at: 20, uncertain: true, paused: false, quarantined: false, offline: false};
  const idle = {...active, state: "CONFIRMED_IDLE" as const, release_at: 10, uncertain: false};
  const external = {...idle, state: "UNKNOWN_BUSY" as const};
  const results = projectSchedule([
    [{task_uid: "A", estimated_sec: 8, options: [[0,1]]},
      {task_uid: "B", estimated_sec: null, options: [[0]]},
      {task_uid: "C", estimated_sec: 5, options: [[0]]}],
    [{task_uid: "D", estimated_sec: 5, options: [[2]]}],
  ], [idle, active, external], 10);
  expect(results).toEqual([
    {projection: {task_uid: "A", projection_status: "SCHEDULED", est_start: 20, est_end: 28, uncertain_release: true}, groupIndexes: [0,1]},
    {projection: {task_uid: "B", projection_status: "NO_ESTIMATE"}, groupIndexes: []},
    {projection: {task_uid: "C", projection_status: "NO_ESTIMATE"}, groupIndexes: []},
    {projection: {task_uid: "D", projection_status: "WAITING_EXTERNAL"}, groupIndexes: []},
  ]);
});
