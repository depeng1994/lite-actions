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
  assert.ok((await store.dbSizeBytes()) > 0);
  store.close();
} finally {
  rmSync(directory, {recursive: true, force: true});
}
console.log("node:sqlite adapter smoke passed");
