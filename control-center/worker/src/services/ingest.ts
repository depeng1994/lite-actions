import {logicalModel} from "../modelMap";
import {type Context, HttpError} from "../router";
import {taskIdentity} from "../utils/taskIdentity";
import {
  array, CASE_ID, devices, hostId, integer, invalid, member, nullableInt, nullableString, object,
  PHASES, POOL, REPO, RUN_ID, STATUSES, string, TID,
} from "../utils/validate";

type Input = Record<string, unknown>;
type Statement = D1PreparedStatement;
type Cursor = {instance_id: string; last_seq: number; last_snapshot_id: string | null};
export const TASK_META = [
  "task_uid", "worker_id", "provider", "repo", "logical_model", "run_id", "attempt", "dispatch_id",
  "case_id", "test_id", "label", "kind", "target_pool", "req_nodes", "req_devices", "pr_number",
  "pr_author", "trigger_actor", "event_type", "workflow_file", "submitted_at", "workflow_name",
] as const;
const TASK_ACTIVE = [...TASK_META, "short_tid", "task_key", "phase", "allocation_json", "queued_at",
  "reserved_at", "staging_at", "running_at", "cleaning_at", "timing_reliable", "updated_at"];
const TIMES = ["queued_at", "reserved_at", "staging_at", "running_at", "cleaning_at"] as const;
const INSTANCE = /^[a-f0-9]{8}$/;
const SNAPSHOT_ID = /^[a-f0-9]{10}$/;

function timings(input: Input) {
  const times = input.tm == null ? {} : object(input.tm, "tm");
  return PHASES.map(phase => nullableInt(times[phase], phase));
}

function allocation(value: unknown) {
  return array(value ?? [], "allocation", 16).map(item => {
    const node = object(item, "allocation node");
    return {host_id: hostId(node.h), devices: devices(node.d)};
  });
}

async function task(context: Context, input: Input) {
  const provider = member(input.p, "provider", ["github", "gitcode"] as const);
  const repo = string(input.r, "repo", 128, REPO);
  const run_id = string(input.run, "run", 128, RUN_ID);
  const attempt = integer(input.att, "attempt", 1, 2**31 - 1);
  const dispatch_id = nullableString(input.did, "dispatch_id", 128, RUN_ID);
  const case_id = string(input.cid, "case_id", 89, CASE_ID);
  const identity = await taskIdentity({provider, repo, run_id, attempt}, case_id, dispatch_id);
  if (input.tid !== identity.short_tid) invalid("tid");
  const [nodes, count] = array(input.req, "req", 2);
  const times = timings(input);
  return {
    ...identity, worker_id: context.workerId, provider, repo, logical_model: logicalModel(provider, repo),
    run_id, attempt, dispatch_id, case_id, test_id: case_id.slice(9), label: string(input.lbl, "label", 115),
    kind: member(input.k, "kind", ["npu_smoke", "train", "worker_probe"] as const),
    target_pool: string(input.pool, "pool", 64, POOL), req_nodes: integer(nodes, "nodes", 0, 256),
    req_devices: integer(count, "devices_per_node", 0, 256), pr_number: nullableInt(input.pr, "pr", 1),
    pr_author: nullableString(input.pa, "pr_author", 40, /^[A-Za-z0-9_\[\]-]+$/),
    trigger_actor: nullableString(input.tr, "trigger_actor", 40, /^[A-Za-z0-9_\[\]-]+$/),
    event_type: nullableString(input.et, "event_type", 40), workflow_file: nullableString(input.wf, "workflow_file", 100),
    submitted_at: input.sub == null ? (times[0] ?? integer(input.ts, "ts")) : nullableInt(input.sub, "submitted_at"),
    workflow_name: nullableString(input.wn, "workflow_name", 64),
    phase: member(input.ph, "phase", PHASES), allocation_json: JSON.stringify(allocation(input.a)),
    queued_at: times[0] ?? integer(input.ts, "ts"), reserved_at: times[1], staging_at: times[2],
    running_at: times[3], cleaning_at: times[4], timing_reliable: integer(input.reliable ?? 0, "reliable", 0, 1),
    updated_at: context.now,
  };
}

function taskUpsert(db: D1Database, tasks: Awaited<ReturnType<typeof task>>[], snapshot: boolean): Statement {
  const columns = TASK_ACTIVE.join(",");
  const values = TASK_ACTIVE.map(column => `json_extract(value,'$.${column}')`).join(",");
  return db.prepare(`
    INSERT INTO tasks_active(${columns}) SELECT ${values} FROM json_each(?)
    WHERE NOT EXISTS (SELECT 1 FROM task_index WHERE task_uid=json_extract(value,'$.task_uid'))
    ON CONFLICT(task_uid) DO ${snapshot ? `UPDATE SET
      phase=excluded.phase,allocation_json=excluded.allocation_json,updated_at=excluded.updated_at,
      ${TIMES.map(column => `${column}=COALESCE(excluded.${column},tasks_active.${column})`).join(",")},
      timing_reliable=MAX(excluded.timing_reliable,tasks_active.timing_reliable),
      submitted_at=COALESCE(excluded.submitted_at,tasks_active.submitted_at),
      workflow_name=COALESCE(excluded.workflow_name,tasks_active.workflow_name)
      WHERE tasks_active.worker_id=excluded.worker_id` : "NOTHING"}
  `).bind(JSON.stringify(tasks));
}

function finish(context: Context, selection: string, parameters: unknown[], fields: {
  finished: number; status: string; exit: number | null; clean: number | null;
  alloc: ReturnType<typeof allocation> | null; timestamps?: ReturnType<typeof timings>; reconciled?: boolean;
}): Statement[] {
  const db = context.env.DB;
  const times = fields.timestamps ?? TIMES.map(() => null);
  const stamps = TIMES.map(column => `COALESCE(?, ${column})`);
  const stampValues = times;
  const where = `FROM tasks_active WHERE worker_id=? AND ${selection}`;
  const filter = [context.workerId, ...parameters];
  const finished = fields.finished;
  const duration = !fields.reconciled
    ? "CASE WHEN timing_reliable=1 AND ?>=COALESCE(?,running_at,?,reserved_at) THEN ?-COALESCE(?,running_at,?,reserved_at) END"
    : "NULL";
  return [
    db.prepare(`
      INSERT INTO task_index(${TASK_META.join(",")},finished_at,actual_allocation_json,status,exit_code,
        cleanup_verified,duration_sec,timing_reliable)
      SELECT ${TASK_META.join(",")},?,COALESCE(?,allocation_json),?,?,?,${duration},
        ${fields.reconciled ? "0" : "timing_reliable"} ${where}
      ON CONFLICT(task_uid) DO NOTHING
    `).bind(finished, fields.alloc ? JSON.stringify(fields.alloc) : null, fields.status, fields.exit, fields.clean,
      ...(fields.reconciled ? [] : [finished, times[3], times[1], finished, times[3], times[1]]), ...filter),
    db.prepare(`
      INSERT INTO task_details_hot(task_uid,finished_at,detail_json)
      SELECT task_uid,?,json_object(${TIMES.map((column, idx) => `'${column}',${stamps[idx]}`).join(",")},'reconciled',?)
      ${where} ON CONFLICT(task_uid) DO NOTHING
    `).bind(finished, ...stampValues, fields.reconciled ? 1 : 0, ...filter),
    ...(fields.status === "PASS" ? [db.prepare(`
      INSERT INTO duration_samples(logical_model,test_id,target_pool,req_nodes,req_devices,duration_sec,finished_at,task_uid)
      SELECT logical_model,test_id,target_pool,req_nodes,req_devices,duration_sec,finished_at,task_uid
      FROM task_index WHERE status='PASS' AND duration_sec IS NOT NULL
        AND task_uid IN (SELECT task_uid ${where})
      ON CONFLICT(task_uid) DO NOTHING
    `).bind(...filter)] : []),
    db.prepare(`DELETE FROM queue_orders WHERE task_uid IN (SELECT task_uid ${where})`).bind(...filter),
    db.prepare(`DELETE FROM tasks_active WHERE worker_id=? AND ${selection}`).bind(...filter),
  ];
}

function device(context: Context, input: Input) {
  const ts = nullableInt(input.ts, "probe time");
  const round = input.pr == null ? null : integer(input.pr, "probe round", 0);
  const visibility = input.v == null ? null : integer(input.v, "visibility", 0, 2);
  const failures = input.mc == null ? null : integer(input.mc, "probe failures", 0, 5);
  const online = nullableInt(input.lo, "last online probe");
  return {
    worker_id: context.workerId,
    pool_name: string(input.pool, "pool", 64, POOL),
    host_id: hostId(input.h),
    devices_json: JSON.stringify(devices(input.d)),
    occupancy_state: input.st == null ? null : member(input.st, "occupancy", ["idle", "busy", "probe_error"] as const),
    probe_at: ts === null ? null : Math.min(ts, context.now),
    quarantined: nullableInt(input.qu, "quarantined"),
    quarantine_task_uid: nullableString(input.qid, "quarantine task", 40, /^t_[a-f0-9]{32}$/),
    visibility_state: visibility == null ? null : (["online", "warn", "offline"] as const)[visibility],
    consecutive_failures: failures,
    last_online_at: online === null ? null : Math.min(online, context.now),
    last_probe_round: round,
    updated_at: context.now,
  };
}
type DeviceWrite = ReturnType<typeof device>;

function deviceUpsert(db: D1Database, rows: DeviceWrite[]): Statement {
  const columns = ["worker_id", "pool_name", "host_id", "devices_json", "occupancy_state", "probe_at",
    "quarantined", "quarantine_task_uid", "visibility_state", "consecutive_failures", "last_online_at", "last_probe_round", "updated_at"];
  return db.prepare(`
    INSERT INTO device_group_states(${columns.join(",")})
    SELECT ${columns.map(column => column === "quarantined"
      ? "COALESCE(json_extract(value,'$.quarantined'),0)"
      : column === "visibility_state" ? "COALESCE(json_extract(value,'$.visibility_state'),'pending')"
      : column === "consecutive_failures" || column === "last_probe_round" ? `COALESCE(json_extract(value,'$.'||'${column}'),0)`
      : `json_extract(value,'$.${column}')`).join(",")}
    FROM json_each(?) WHERE 1
    ON CONFLICT(worker_id,pool_name,host_id,devices_json) DO UPDATE SET
      occupancy_state=COALESCE(excluded.occupancy_state,device_group_states.occupancy_state),
      probe_at=COALESCE(excluded.probe_at,device_group_states.probe_at),
      quarantined=COALESCE((SELECT json_extract(value,'$.quarantined') FROM json_each(?) WHERE
        json_extract(value,'$.pool_name')=excluded.pool_name AND json_extract(value,'$.host_id')=excluded.host_id
        AND json_extract(value,'$.devices_json')=excluded.devices_json),device_group_states.quarantined),
      quarantine_task_uid=COALESCE(excluded.quarantine_task_uid,device_group_states.quarantine_task_uid),
      visibility_state=CASE WHEN COALESCE(excluded.last_probe_round,0)>device_group_states.last_probe_round
        THEN excluded.visibility_state ELSE device_group_states.visibility_state END,
      consecutive_failures=CASE WHEN COALESCE(excluded.last_probe_round,0)>device_group_states.last_probe_round
        THEN COALESCE(excluded.consecutive_failures,0) ELSE device_group_states.consecutive_failures END,
      last_online_at=CASE WHEN COALESCE(excluded.last_probe_round,0)>device_group_states.last_probe_round
        AND excluded.visibility_state='online' THEN MAX(COALESCE(device_group_states.last_online_at,0),COALESCE(excluded.last_online_at,0))
        ELSE device_group_states.last_online_at END,
      last_probe_round=MAX(device_group_states.last_probe_round,COALESCE(excluded.last_probe_round,0)),
      updated_at=excluded.updated_at
  `).bind(JSON.stringify(rows), JSON.stringify(rows));
}

function queuePage(context: Context, input: Input) {
  const pool = string(input.pool, "pool", 64, POOL);
  const kind = member(input.k, "kind", ["train", "npu_smoke"] as const);
  const version = integer(input.v, "queue_version", 1);
  const rev = integer(input.rev ?? 0, "revision");
  const rows = array(input.items, "queue items", 8).map(item => {
    const row = object(item, "queue row");
    const options = array(row.ix, "candidate allocations", 96).map(option =>
      array(option, "candidate groups", 16).map(idx => integer(idx, "group index", 0, 8191)));
    return {
      tid: string(row.tid, "tid", 12, TID), pos: integer(row.pos, "position"),
      options: JSON.stringify({rev, options, off: integer(row.off ?? 0, "candidate offset", 0, 8192)}),
    };
  });
  return {workerId: context.workerId, pool, kind, version, rows, now: context.now};
}

type QueueWrite = ReturnType<typeof queuePage>;
function queueUpsert(db: D1Database, pages: QueueWrite[]): Statement {
  return db.prepare(`
    INSERT INTO queue_orders(worker_id,pool_name,kind,task_uid,queue_pos,candidate_group_idxs_json,queue_ver,updated_at)
    SELECT t.worker_id,json_extract(p.value,'$.pool'),json_extract(p.value,'$.kind'),t.task_uid,
      json_extract(r.value,'$.pos'),json_extract(r.value,'$.options'),json_extract(p.value,'$.version'),json_extract(p.value,'$.now')
    FROM json_each(?) p JOIN json_each(p.value,'$.rows') r JOIN tasks_active t
      ON t.short_tid=json_extract(r.value,'$.tid') AND t.worker_id=json_extract(p.value,'$.workerId')
    WHERE t.phase='QUEUED'
    ON CONFLICT(worker_id,pool_name,kind,task_uid) DO UPDATE SET
      queue_pos=excluded.queue_pos,
      candidate_group_idxs_json=CASE WHEN json_extract(excluded.candidate_group_idxs_json,'$.off')=0
        THEN excluded.candidate_group_idxs_json ELSE json_set(queue_orders.candidate_group_idxs_json,'$.options',(
          SELECT json_group_array(json(value)) FROM (
            SELECT value FROM json_each(queue_orders.candidate_group_idxs_json,'$.options')
            UNION ALL SELECT value FROM json_each(excluded.candidate_group_idxs_json,'$.options')
          )
        )) END,
      queue_ver=excluded.queue_ver,updated_at=excluded.updated_at
    WHERE excluded.queue_ver>=queue_orders.queue_ver AND (
      json_extract(excluded.candidate_group_idxs_json,'$.off')=0 OR
      (excluded.queue_ver=queue_orders.queue_ver AND json_extract(excluded.candidate_group_idxs_json,'$.off')
        =json_array_length(queue_orders.candidate_group_idxs_json,'$.options'))
    )
  `).bind(JSON.stringify(pages));
}

async function eventWrites(context: Context, event: Input): Promise<Statement[]> {
  const db = context.env.DB;
  const worker = context.workerId;
  const ts = event.t === "IP" ? context.now : integer(event.ts, "ts");
  switch (event.t) {
    case "TU":
      return [taskUpsert(db, [await task(context, event)], false)];
    case "IP":
      return [deviceUpsert(db, [device(context, event)])];
    case "QP": {
      const page = queuePage(context, event);
      return [
        queueUpsert(db, [page]),
        ...(event.last === 1 ? [db.prepare(`
          DELETE FROM queue_orders WHERE worker_id=? AND pool_name=? AND kind=? AND queue_ver<?
        `).bind(worker, page.pool, page.kind, page.version)] : []),
      ];
    }
    case "TP": {
      const tid = string(event.tid, "tid", 12, TID);
      const phase = member(event.ph, "phase", PHASES);
      const times = timings(event);
      const idx = PHASES.indexOf(phase);
      times[idx] ??= ts;
      return [db.prepare(`
        UPDATE tasks_active SET phase=?,updated_at=?,allocation_json=CASE WHEN ?='QUEUED' THEN '[]' ELSE allocation_json END,
        ${TIMES.map(column => `${column}=COALESCE(?,${column})`).join(",")}
        WHERE worker_id=? AND short_tid=?
      `).bind(phase, context.now, phase, ...times, worker, tid)];
    }
    case "TL": {
      const tid = string(event.tid, "tid", 12, TID);
      const alloc = JSON.stringify(allocation(event.a));
      const part = integer(event.ap ?? 1, "allocation part", 1, 1024);
      return [db.prepare(`
        UPDATE tasks_active SET allocation_json=CASE WHEN ?=1 THEN ? ELSE (
          SELECT json_group_array(json(value)) FROM (
            SELECT value FROM json_each(allocation_json) UNION ALL SELECT value FROM json_each(?)
          )
        ) END,updated_at=? WHERE worker_id=? AND short_tid=?
      `).bind(part, alloc, alloc, context.now, worker, tid)];
    }
    case "TF": {
      const tid = string(event.tid, "tid", 12, TID);
      const status = member(event.st, "status", STATUSES);
      const alloc = event.a === undefined ? null : allocation(event.a);
      const clean = integer(event.cv, "cleanup verification", 0, 1);
      const quarantine = integer(event.qu ?? 0, "quarantine", 0, 1);
      return [
        ...(quarantine ? [db.prepare(`
          INSERT INTO device_group_states(worker_id,pool_name,host_id,devices_json,quarantined,quarantine_task_uid,updated_at)
          SELECT a.worker_id,a.target_pool,json_extract(value,'$.host_id'),json_extract(value,'$.devices'),
            1,a.task_uid,? FROM tasks_active a JOIN json_each(COALESCE(?,a.allocation_json))
          WHERE a.worker_id=? AND a.short_tid=?
          ON CONFLICT(worker_id,pool_name,host_id,devices_json) DO UPDATE SET
            quarantined=1,quarantine_task_uid=excluded.quarantine_task_uid,updated_at=excluded.updated_at
        `).bind(context.now, alloc ? JSON.stringify(alloc) : null, worker, tid)] : []),
        ...finish(context, "short_tid=?", [tid], {finished: ts, status, exit: nullableInt(event.ec, "exit code", -255),
          clean, alloc, timestamps: timings(event)}),
      ];
    }
    default:
      return invalid("event type");
  }
}

export async function assertInstance(context: Context, input: Input) {
  if (input.w !== context.workerId) throw new HttpError(403, "WORKER_MISMATCH");
  const instance = string(input.i, "instance", 8, INSTANCE);
  const worker = await context.env.DB.prepare("SELECT instance_id FROM workers WHERE worker_id=?")
    .bind(context.workerId).first<string>("instance_id");
  if (worker !== instance) throw new HttpError(409, "HEARTBEAT_REQUIRED");
  return instance;
}

async function cursor(context: Context, instance: string): Promise<Cursor> {
  return (await context.env.DB.prepare("SELECT * FROM worker_ingest_state WHERE worker_id=? AND instance_id=?")
    .bind(context.workerId, instance).first<Cursor>()) ?? {instance_id: instance, last_seq: 0, last_snapshot_id: null};
}

function advance(context: Context, instance: string, seq: number, sid: string | null) {
  return context.env.DB.prepare(`
    INSERT INTO worker_ingest_state(worker_id,instance_id,last_seq,updated_at,last_snapshot_id) VALUES (?,?,?,?,?)
    ON CONFLICT(worker_id) DO UPDATE SET instance_id=excluded.instance_id,last_seq=excluded.last_seq,
      updated_at=excluded.updated_at,last_snapshot_id=CASE WHEN excluded.instance_id=worker_ingest_state.instance_id
        THEN COALESCE(excluded.last_snapshot_id,worker_ingest_state.last_snapshot_id) ELSE excluded.last_snapshot_id END
  `).bind(context.workerId, instance, seq, context.now, sid);
}

function guard(context: Context, instance: string, seq: number, prior: Cursor): Statement {
  // A stale concurrent writer aborts the entire D1 transaction through the
  // NOT NULL constraint. Bind the expected cursor and current heartbeat instance.
  return context.env.DB.prepare(`
    INSERT INTO worker_event_dedup(worker_id,instance_id,seq,received_at)
    VALUES (?,?,CASE WHEN EXISTS(SELECT 1 FROM workers WHERE worker_id=? AND instance_id=?)
      AND COALESCE((SELECT last_seq FROM worker_ingest_state WHERE worker_id=? AND instance_id=?),0)=?
      AND COALESCE((SELECT last_snapshot_id FROM worker_ingest_state WHERE worker_id=? AND instance_id=?),'')=?
      THEN ? END,?)
  `).bind(context.workerId, instance, context.workerId, instance, context.workerId, instance, prior.last_seq,
    context.workerId, instance, prior.last_snapshot_id ?? "", seq, context.now);
}

async function guardedBatch(db: D1Database, statements: Statement[]) {
  try {
    await db.batch(statements);
  } catch (error) {
    if (/NOT NULL constraint failed: worker_event_dedup.seq|UNIQUE constraint failed: worker_event_dedup/.test(String(error))) {
      throw new HttpError(409, "CONCURRENT_INGEST");
    }
    throw error;
  }
}

export async function ingestEvents(context: Context, input: Input) {
  const instance = await assertInstance(context, input);
  const previous = await cursor(context, instance);
  const incoming = array(input.ev, "events", 6).map(value => object(value, "event"));
  const fresh = incoming.filter(event => integer(event.s, "sequence", 1) > previous.last_seq);
  let seq = previous.last_seq;
  for (const event of fresh) {
    if (event.s !== ++seq) {
      await context.env.DB.prepare("UPDATE workers SET need_snapshot=1 WHERE worker_id=? AND instance_id=? AND need_snapshot=0")
        .bind(context.workerId, instance).run();
      return {ok: true, need_snapshot: true, expected_seq: previous.last_seq + 1};
    }
  }
  if (fresh.length) {
    const db = context.env.DB;
    const writes = (await Promise.all(fresh.map(event => eventWrites(context, event)))).flat();
    if (writes.length > 46) invalid("too many event mutations in packet");
    await guardedBatch(db, [
      guard(context, instance, Number(fresh[0].s), previous),
      ...writes,
      db.prepare(`INSERT INTO worker_event_dedup(worker_id,instance_id,seq,received_at)
        SELECT ?,?,value,? FROM json_each(?)`).bind(context.workerId, instance, context.now,
        JSON.stringify(fresh.slice(1).map(event => event.s))),
      advance(context, instance, seq, null),
    ]);
  }
  return {ok: true, need_snapshot: false};
}

type SnapshotPart = {part_no: number; total_parts: number; seq: number; payload_json: string};
export async function ingestSnapshot(context: Context, input: Input) {
  const instance = await assertInstance(context, input);
  const sid = string(input.sid, "snapshot id", 10, SNAPSHOT_ID);
  const total = integer(input.n, "total parts", 1, 512);
  const part = integer(input.p, "part", 1, total);
  const seq = integer(input.seq, "sequence");
  const kind = member(input.kind, "kind", ["task", "device", "queue", "lease"] as const);
  const items = array(input.items, "items", 100);
  const db = context.env.DB;
  const previous = await cursor(context, instance);
  if (sid === previous.last_snapshot_id) return {ok: true, complete: true};
  if (seq < previous.last_seq) throw new HttpError(409, "STALE_SNAPSHOT");
  const payload = JSON.stringify({kind, items});
  const filter = [context.workerId, instance, sid];
  const staged = await db.batch<SnapshotPart>([
    db.prepare(`INSERT INTO snapshot_staging(worker_id,instance_id,snap_id,part_no,total_parts,seq,payload_json,created_at)
      VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(worker_id,instance_id,snap_id,part_no) DO NOTHING`)
      .bind(...filter, part, total, seq, payload, context.now),
    db.prepare("SELECT part_no,total_parts,seq,payload_json FROM snapshot_staging WHERE worker_id=? AND instance_id=? AND snap_id=? ORDER BY part_no")
      .bind(...filter),
  ]);
  const rows = staged[1].results;
  if (rows.some(row => row.total_parts !== total || row.seq !== seq ||
    (row.part_no === part && row.payload_json !== payload))) throw new HttpError(409, "SNAPSHOT_PART_CONFLICT");
  if (rows.length !== total) return {ok: true, complete: false};

  const tasks: Awaited<ReturnType<typeof task>>[] = [];
  const groups: DeviceWrite[] = [];
  const queues: QueueWrite[] = [];
  const leases = new Map<string, ReturnType<typeof allocation>>();
  for (const row of rows) {
    const frame = JSON.parse(row.payload_json) as {kind: string; items: unknown[]};
    for (const entry of frame.items) {
      const value = object(entry, "snapshot item");
      if (frame.kind === "task") tasks.push(await task(context, value));
      if (frame.kind === "device") groups.push(device(context, value));
      if (frame.kind === "lease") {
        const tid = string(value.tid, "tid", 12, TID);
        leases.set(tid, [...(leases.get(tid) ?? []), ...allocation(value.a)]);
      }
      if (frame.kind === "queue") queues.push(queuePage(context, value));
    }
  }
  for (const item of tasks) {
    if (leases.has(item.short_tid)) item.allocation_json = JSON.stringify(leases.get(item.short_tid));
  }
  const identities = JSON.stringify(tasks.map(item => item.task_uid));
  await guardedBatch(db, [
    guard(context, instance, -1, previous),
    ...finish(context, "task_uid NOT IN (SELECT value FROM json_each(?))", [identities],
      {finished: context.now, status: "INFRA_ERROR", exit: null, clean: null, alloc: null, reconciled: true}),
    taskUpsert(db, tasks, true),
    db.prepare("DELETE FROM queue_orders WHERE worker_id=?").bind(context.workerId),
    queueUpsert(db, queues),
    db.prepare("DELETE FROM device_group_states WHERE worker_id=?").bind(context.workerId),
    deviceUpsert(db, groups),
    advance(context, instance, seq, sid),
    db.prepare("UPDATE workers SET need_snapshot=0,updated_at=? WHERE worker_id=?").bind(context.now, context.workerId),
    db.prepare("DELETE FROM snapshot_staging WHERE worker_id=? AND instance_id=? AND seq<=?").bind(context.workerId, instance, seq),
    db.prepare("DELETE FROM worker_event_dedup WHERE worker_id=? AND instance_id=? AND seq=-1").bind(context.workerId, instance),
  ]);
  return {ok: true, complete: true};
}
