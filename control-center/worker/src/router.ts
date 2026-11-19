import type { WorkerId, Role } from "./types/api";

export interface Env {
  DB: D1Database;
  ASSETS?: Fetcher;
  SUPER_SECRET?: string;
  SUPER_USERNAME?: string;
  KEY_PEPPER?: string;
  WORKER_BLUE_KEY_HASH?: string;
  WORKER_YELLOW_KEY_HASH?: string;
  DEV_INSECURE_COOKIE?: string;
  GITHUB_APP_ID?: string;
  GITHUB_APP_PRIVATE_KEY?: string;
  GITCODE_CANCEL_TOKEN?: string;
  D1_LIMIT_BYTES?: string;
}

export interface Principal {
  username: string;
  role: Role;
  session_id: string;
}
export interface Context {
  request: Request;
  env: Env;
  params: Record<string, string>;
  url: URL;
  now: number;
  /** Resolved by the runtime adapter. Core services never trust proxy headers. */
  clientIp?: string;
  principal?: Principal;
  workerId?: WorkerId;
}
export type Handler = (context: Context) => Response | Promise<Response>;

export class HttpError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message = code,
    readonly headers?: HeadersInit,
  ) {
    super(message);
  }
}

export async function body(context: Context): Promise<Record<string, unknown>> {
  const limit = context.workerId ? 1999 : 128 * 1024;
  const reader = context.request.body?.getReader();
  const decoder = new TextDecoder("utf-8", {fatal: true, ignoreBOM: false});
  let size = 0;
  let text = "";
  try {
    if (!reader) throw new Error();
    while (true) {
      const {value, done} = await reader.read();
      if (done) break;
      size += value.length;
      if (size > limit) {
        await reader.cancel();
        throw new HttpError(413, "BODY_TOO_LARGE");
      }
      text += decoder.decode(value, {stream: true});
    }
    const result = JSON.parse(text + decoder.decode());
    if (result && typeof result === "object" && !Array.isArray(result)) return result;
  } catch (error) {
    if (error instanceof HttpError) throw error;
  }
  throw new HttpError(400, "INVALID_JSON");
}

export class Router {
  private routes: {method: string; pattern: RegExp; keys: string[]; handler: Handler}[] = [];

  add(method: string, path: string, handler: Handler) {
    const keys: string[] = [];
    const pattern = path.split("/").map(segment => {
      if (segment.startsWith(":")) {
        keys.push(segment.slice(1));
        return "([^/]+)";
      }
      return segment.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    }).join("/");
    this.routes.push({method, pattern: new RegExp(`^${pattern}$`), keys, handler});
  }

  async dispatch(context: Context): Promise<Response> {
    for (const route of this.routes) {
      if (context.request.method !== route.method) continue;
      const match = route.pattern.exec(context.url.pathname);
      if (!match) continue;
      try {
        context.params = Object.fromEntries(route.keys.map((key, i) => [key, decodeURIComponent(match[i + 1])]));
      } catch {
        throw new HttpError(400, "INVALID_PATH");
      }
      return route.handler(context);
    }
    throw new HttpError(404, "NOT_FOUND");
  }
}
