import type {SqlRunResult, SqlRows, SqlStatement, SqlStore} from "../storage/sqlStore";

const rawStatements = new WeakMap<SqlStatement, D1PreparedStatement>();
const storeCache = new WeakMap<D1Database, SqlStore>();

class D1SqlStatement implements SqlStatement {
  constructor(private readonly stmt: D1PreparedStatement) {
    rawStatements.set(this, stmt);
  }

  bind(...values: unknown[]): SqlStatement {
    return new D1SqlStatement(this.stmt.bind(...values));
  }

  first<T = unknown>(column?: string): Promise<T | null> {
    return column === undefined ? this.stmt.first<T>() : this.stmt.first<T>(column);
  }

  async all<T = Record<string, unknown>>(): Promise<SqlRows<T>> {
    const result = await this.stmt.all<T>();
    return {results: result.results, meta: result.meta ? {size_after: result.meta.size_after} : undefined};
  }

  async run<T = unknown>(): Promise<SqlRunResult & {results?: T[]}> {
    const result = await this.stmt.run<T>();
    return {
      meta: {
        changes: result.meta?.changes ?? 0,
        last_row_id: result.meta?.last_row_id,
        size_after: result.meta?.size_after,
      },
      results: result.results,
    };
  }
}

export function createD1Store(db: D1Database): SqlStore {
  const existing = storeCache.get(db);
  if (existing) return existing;
  const store: SqlStore = {
    prepare(query: string): SqlStatement {
      return new D1SqlStatement(db.prepare(query));
    },
    async batch<T = unknown>(statements: readonly SqlStatement[]): Promise<Array<SqlRunResult & {results?: T[]}>> {
      const native = statements.map(statement => {
        const inner = rawStatements.get(statement);
        if (!inner) throw new TypeError("Foreign SqlStatement passed to D1 store");
        return inner;
      });
      const results = await db.batch<T>(native);
      return results.map(result => ({
        meta: {
          changes: result.meta?.changes ?? 0,
          last_row_id: result.meta?.last_row_id,
          size_after: result.meta?.size_after,
        },
        results: result.results,
      }));
    },
    async dbSizeBytes(): Promise<number> {
      const response = await db.prepare("SELECT 1").all();
      return response.meta?.size_after ?? Number.NaN;
    },
  };
  storeCache.set(db, store);
  return store;
}
