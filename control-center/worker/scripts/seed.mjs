import {createHash} from "node:crypto";
import {readFileSync, readdirSync} from "node:fs";

if (process.argv[2] !== "--sqlite") throw new Error("Specify --sqlite <path>");
const dbPath = process.argv[3];
if (!dbPath) throw new Error("Specify --sqlite <path>");

const config = JSON.stringify(JSON.parse(readFileSync(new URL("../../../config/pools.json", import.meta.url), "utf8")));
const sha = createHash("sha256").update(config).digest("hex");
const now = Math.floor(Date.now() / 1000);
const {DatabaseSync} = await import("node:sqlite");
const db = new DatabaseSync(dbPath);
db.exec("PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000; PRAGMA foreign_keys=ON;");
db.exec("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at INTEGER NOT NULL)");
const migrationFiles = readdirSync(new URL("../migrations/", import.meta.url))
  .filter(name => name.endsWith(".sql") && !name.startsWith("._"))
  .sort();
const hasLegacySchema = Boolean(db.prepare("SELECT 1 AS ok FROM sqlite_schema WHERE type='table' AND name='workers'").get());
const hasMigrationRows = Boolean(db.prepare("SELECT 1 AS ok FROM schema_migrations LIMIT 1").get());
if (hasLegacySchema && !hasMigrationRows) {
  for (const file of migrationFiles) db.prepare("INSERT OR IGNORE INTO schema_migrations(name,applied_at) VALUES(?,?)").run(file, now);
}
for (const file of migrationFiles) {
  const applied = db.prepare("SELECT 1 AS ok FROM schema_migrations WHERE name=?").get(file);
  if (applied) continue;
  db.exec(readFileSync(new URL(`../migrations/${file}`, import.meta.url), "utf8"));
  db.prepare("INSERT INTO schema_migrations(name,applied_at) VALUES(?,?)").run(file, now);
}
db.exec("BEGIN IMMEDIATE");
try {
  db.prepare(`INSERT INTO pools_configs(revision,sha256,config_json,created_by,created_at)
    SELECT 1,?,?,?,? WHERE NOT EXISTS (SELECT 1 FROM pools_configs)`).run(sha, config, "bootstrap", now);
  for (const workerId of ["blue", "yellow"]) {
    db.prepare(`INSERT INTO commands(cmd_id,worker_id,cmd_type,payload_json,status,created_by,created_at)
      SELECT ?,?,'SYNC_CONFIG',?,'PENDING','bootstrap',? WHERE changes()=1`)
      .run(`cmd_seed_${workerId}`, workerId, JSON.stringify({revision: 1, sha256: sha}), now);
  }
  db.exec("COMMIT");
} catch (error) {
  db.exec("ROLLBACK");
  throw error;
} finally {
  db.close();
}
console.log(`Seeded SQLite database ${dbPath}`);
