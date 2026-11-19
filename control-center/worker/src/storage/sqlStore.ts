/** Runtime-neutral SQL primitives used by the application boundary.
 *
 * D1 and a local SQLite adapter can both satisfy these structural contracts.
 * Business services do not need to import either provider's concrete types.
 */
export interface SqlRunResult {
  meta: {changes: number; last_row_id?: number; size_after?: number};
}

export interface SqlRows<T> {
  results: T[];
  meta?: {size_after?: number};
}

export interface SqlStatement {
  bind(...values: unknown[]): SqlStatement;
  first<T = unknown>(column?: string): Promise<T | null>;
  all<T = Record<string, unknown>>(): Promise<SqlRows<T>>;
  run<T = unknown>(): Promise<SqlRunResult & {results?: T[]}>;
}

export interface SqlStore {
  prepare(query: string): SqlStatement;
  batch<T = unknown>(statements: readonly SqlStatement[]): Promise<Array<SqlRunResult & {results?: T[]}>>;
  dbSizeBytes(): Promise<number>;
}

/** Normalize BLOB values returned by D1, SQLite adapters, or test doubles. */
export function toArrayBuffer(value: unknown): ArrayBuffer {
  if (value instanceof ArrayBuffer) return value;
  if (ArrayBuffer.isView(value)) {
    return value.buffer.slice(value.byteOffset, value.byteOffset + value.byteLength) as ArrayBuffer;
  }
  if (Array.isArray(value) && value.every(byte => Number.isInteger(byte) && byte >= 0 && byte <= 255)) {
    return Uint8Array.from(value).buffer;
  }
  throw new TypeError("Unsupported BLOB value");
}
