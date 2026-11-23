export {};

declare global {
  interface Response {
    json<T = unknown>(): Promise<T>;
  }
}
