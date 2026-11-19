import {DatabaseSync, type StatementSync} from "node:sqlite";
import type {SqlRunResult, SqlRows, SqlStatement, SqlStore} from "../storage/sqlStore";

type Row = Record<string, unknown>;

class NodeSqlStatement implements SqlStatement {
  private readonly database: DatabaseSync;
  private readonly query: string;
  private readonly values: unknown[];

  constructor(database: DatabaseSync, query: string, values: unknown[] = []) {
    this.database = database;
    this.query = query;
    this.values = values;
  }

  bind(...values: unknown[]): SqlStatement {
    return new NodeSqlStatement(this.database, this.query, values);
  }

  private statement(): StatementSync {
    return this.database.prepare(this.query);
  }

  private execute<T>(action: (statement: StatementSync, values: any[]) => T): T {
    return action(this.statement(), this.values as any[]);
  }

  first<T = unknown>(column?: string): Promise<T | null> {
    const row = this.execute((statement, values) => statement.get(...values) as Row | undefined);
    if (!row) return Promise.resolve(null);
    return Promise.resolve((column ? row[column] : row) as T);
  }

  all<T = Row>(): Promise<SqlRows<T>> {
    const results = this.execute((statement, values) => statement.all(...values) as T[]);
    return Promise.resolve({results});
  }

  run<T = unknown>(): Promise<SqlRunResult & {results?: T[]}> {
    const result = this.execute((statement, values) => statement.run(...values));
    return Promise.resolve({meta: {changes: Number(result.changes), last_row_id: Number(result.lastInsertRowid)}});
  }

  runSync() {
    return this.execute((statement, values) => statement.run(...values));
  }
}

export class NodeSqliteStore implements SqlStore {
  readonly database: DatabaseSync;

  constructor(path: string) {
    this.database = new DatabaseSync(path);
    this.database.exec("PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000; PRAGMA foreign_keys=ON;");
  }

  prepare(query: string): SqlStatement {
    return new NodeSqlStatement(this.database, query);
  }

  async batch(statements: readonly SqlStatement[]) {
    this.database.exec("BEGIN IMMEDIATE");
    try {
      const results = statements.map(statement => {
        if (!(statement instanceof NodeSqlStatement)) throw new TypeError("NodeSqliteStore.batch received another runtime statement");
        const result = statement.runSync();
        return {meta: {changes: Number(result.changes), last_row_id: Number(result.lastInsertRowid)}};
      });
      this.database.exec("COMMIT");
      return results;
    } catch (error) {
      this.database.exec("ROLLBACK");
      throw error;
    }
  }

  async dbSizeBytes() {
    const pageCount = this.database.prepare("PRAGMA page_count").get() as {page_count: number};
    const pageSize = this.database.prepare("PRAGMA page_size").get() as {page_size: number};
    return Number(pageCount.page_count) * Number(pageSize.page_size);
  }

  close() {
    this.database.close();
  }
}

export function createNodeSqliteStore(path: string): NodeSqliteStore {
  return new NodeSqliteStore(path);
}
