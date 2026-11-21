import {DatabaseSync} from "node:sqlite";
import type {SqlRunResult, SqlRows, SqlStatement, SqlStore} from "../storage/sqlStore";

/**
 * Node 24 adapter for the runtime-neutral SqlStore boundary.
 *
 * The adapter deliberately keeps the same async surface as D1. SQLite calls
 * are synchronous inside a request, while callers can continue to await the
 * boundary without changing business services.
 */
export class NodeSqliteStatement implements SqlStatement {
  private values: unknown[] = [];
  private readonly database: DatabaseSync;
  private readonly sql: string;

  constructor(database: DatabaseSync, sql: string) {
    this.database = database;
    this.sql = sql;
  }

  bind(...values: unknown[]): SqlStatement {
    this.values = values;
    return this;
  }

  first<T = unknown>(column?: string): Promise<T | null> {
    const raw = this.database.prepare(this.sql).get(...(this.values as any[])) as Record<string, unknown> | undefined;
    const row = raw ? {...raw} : undefined;
    if (!row) return Promise.resolve(null);
    return Promise.resolve((column ? row[column] : row) as T);
  }

  all<T = Record<string, unknown>>(): Promise<SqlRows<T>> {
    const rows = this.database.prepare(this.sql).all(...(this.values as any[])).map(row => ({...row})) as T[];
    return Promise.resolve({results: rows});
  }

  run<T = unknown>(): Promise<SqlRunResult & {results?: T[]}> {
    const statement = this.database.prepare(this.sql);
    const returning = /\bRETURNING\b/i.test(this.sql);
    const returnsRows = /^\s*(SELECT|PRAGMA)\b/i.test(this.sql) || returning;
    if (returnsRows) {
      const results = statement.all(...(this.values as any[])).map(row => ({...row})) as T[];
      return Promise.resolve({meta: {changes: returning ? results.length : 0}, results});
    }
    const result = statement.run(...(this.values as any[]));
    return Promise.resolve({meta: {
      changes: Number(result.changes),
      last_row_id: Number(result.lastInsertRowid),
    }});
  }
}

export class NodeSqliteStore implements SqlStore {
  readonly database: DatabaseSync;

  constructor(path: string) {
    this.database = new DatabaseSync(path);
    this.database.exec("PRAGMA journal_mode = WAL; PRAGMA busy_timeout = 5000; PRAGMA foreign_keys = ON;");
  }

  prepare(query: string): SqlStatement {
    return new NodeSqliteStatement(this.database, query);
  }

  async batch<T = unknown>(statements: readonly SqlStatement[]): Promise<Array<SqlRunResult & {results?: T[]}>> {
    this.database.exec("BEGIN IMMEDIATE");
    try {
      const results: Array<SqlRunResult & {results?: T[]}> = [];
      for (const statement of statements) results.push(await statement.run<T>());
      this.database.exec("COMMIT");
      return results;
    } catch (error) {
      this.database.exec("ROLLBACK");
      throw error;
    }
  }

  async dbSizeBytes(): Promise<number> {
    const pageCount = Number((this.database.prepare("PRAGMA page_count").get() as {page_count: number}).page_count);
    const pageSize = Number((this.database.prepare("PRAGMA page_size").get() as {page_size: number}).page_size);
    return pageCount * pageSize;
  }

  close(): void {
    this.database.close();
  }
}

export function createNodeSqliteStore(path: string): NodeSqliteStore {
  return new NodeSqliteStore(path);
}
