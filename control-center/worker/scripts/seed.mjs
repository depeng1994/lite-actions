import {createHash} from "node:crypto";
import {readFileSync, mkdtempSync, rmSync, writeFileSync, readdirSync} from "node:fs";
import {tmpdir} from "node:os";
import {join} from "node:path";
import {spawnSync} from "node:child_process";

const mode = process.argv[2];
if (!["--local", "--remote", "--sqlite"].includes(mode)) throw new Error("Specify --local, --remote, or --sqlite <path>");
const config = JSON.stringify(JSON.parse(readFileSync(new URL("../../../config/pools.json", import.meta.url), "utf8")));
const sha = createHash("sha256").update(config).digest("hex");
const now = Math.floor(Date.now() / 1000);
const sql = value => "'" + value.replaceAll("'", "''") + "'";
if (mode === "--sqlite") {
  const dbPath = process.argv[3];
  if (!dbPath) throw new Error("Specify --sqlite <path>");
  const {DatabaseSync} = await import("node:sqlite");
  const db = new DatabaseSync(dbPath);
  db.exec("PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000; PRAGMA foreign_keys=ON;");
  for (const file of readdirSync(new URL("../migrations/", import.meta.url)).filter(name => name.endsWith(".sql")).sort()) {
    db.exec(readFileSync(new URL(`../migrations/${file}`, import.meta.url), "utf8"));
  }
  db.exec("BEGIN IMMEDIATE");
  try {
    db.prepare(`INSERT INTO pools_configs(revision,sha256,config_json,created_by,created_at)
      SELECT 1,?,?,?,? WHERE NOT EXISTS (SELECT 1 FROM pools_configs)`).run(sha, config, "bootstrap", now);
    db.prepare(`INSERT INTO commands(cmd_id,worker_id,cmd_type,payload_json,status,created_by,created_at)
      SELECT ?,?,'SYNC_CONFIG',?,'PENDING','bootstrap',? WHERE changes()=1`).run("cmd_seed_blue", "blue", JSON.stringify({revision: 1, sha256: sha}), now);
    db.prepare(`INSERT INTO commands(cmd_id,worker_id,cmd_type,payload_json,status,created_by,created_at)
      SELECT ?,?,'SYNC_CONFIG',?,'PENDING','bootstrap',? WHERE changes()=1`).run("cmd_seed_yellow", "yellow", JSON.stringify({revision: 1, sha256: sha}), now);
    db.exec("COMMIT");
  } catch (error) {
    db.exec("ROLLBACK");
    throw error;
  } finally { db.close(); }
  console.log(`Seeded SQLite database ${dbPath}`);
  process.exit(0);
}
const dir = mkdtempSync(join(tmpdir(), "control-center-seed-"));
const file = join(dir, "seed.sql");
try {
  writeFileSync(file, `
    INSERT INTO pools_configs(revision,sha256,config_json,created_by,created_at)
      SELECT 1,${sql(sha)},${sql(config)},'bootstrap',${now} WHERE NOT EXISTS (SELECT 1 FROM pools_configs);
    INSERT INTO commands(cmd_id,worker_id,cmd_type,payload_json,status,created_by,created_at)
      SELECT 'cmd_seed_'||value,value,'SYNC_CONFIG',json_object('revision',1,'sha256',${sql(sha)}),
        'PENDING','bootstrap',${now} FROM json_each('["blue","yellow"]') WHERE changes()=1;
  `, {mode: 0o600});
  const result = spawnSync("npx", ["--no-install", "wrangler", "d1", "execute", "DB", mode, "--file", file, "--yes"], {stdio: "inherit"});
  if (result.error) throw result.error;
  process.exitCode = result.status ?? 1;
} finally {
  rmSync(dir, {recursive: true, force: true});
}
