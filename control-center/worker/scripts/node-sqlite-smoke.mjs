import assert from "node:assert/strict";
import {mkdtempSync, rmSync} from "node:fs";
import {tmpdir} from "node:os";
import path from "node:path";
import {createNodeSqliteStore} from "../src/adapters/nodeSqlite.ts";

const directory = mkdtempSync(path.join(tmpdir(), "lite-actions-node-sqlite-"));
try {
  const store = createNodeSqliteStore(path.join(directory, "control-center.sqlite"));
  await store.batch([
    store.prepare("CREATE TABLE records(id INTEGER PRIMARY KEY, value TEXT)"),
    store.prepare("INSERT INTO records(value) VALUES(?)").bind("formal"),
  ]);
  const row = await store.prepare("SELECT value FROM records WHERE id=?").bind(1).first("value");
  assert.equal(row, "formal");
  const updated = await store.prepare("UPDATE records SET value=? WHERE id=? RETURNING id")
    .bind("updated", 1).run();
  assert.equal(updated.meta.changes, 1);
  assert.deepEqual(updated.results, [{id: 1}]);
  assert.ok((await store.dbSizeBytes()) > 0);
  await assert.rejects(() => store.batch([
    store.prepare("INSERT INTO records(value) VALUES(?)").bind("rollback"),
    store.prepare("INSERT INTO missing(value) VALUES(?)").bind("rollback"),
  ]));
  assert.equal(await store.prepare("SELECT COUNT(*) AS count FROM records").first("count"), 1);
  store.close();
} finally {
  rmSync(directory, {recursive: true, force: true});
}
console.log("node:sqlite adapter smoke passed");
