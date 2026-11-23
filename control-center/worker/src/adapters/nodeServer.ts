import {existsSync, statSync} from "node:fs";
import {readFile} from "node:fs/promises";
import {createServer, type IncomingMessage, type Server, type ServerResponse} from "node:http";
import path from "node:path";
import {fileURLToPath} from "node:url";
import {NodeSqliteStore} from "./nodeSqlite.ts";
import type {StaticAssets} from "../storage/sqlStore.ts";

export type NodeFetchHandler = (
  request: Request,
  env: Record<string, unknown>,
  options?: {clientIp?: string; scheduleBackground?: (promise: Promise<unknown>) => void},
) => Response | Promise<Response>;

const mime = new Map([
  [".html", "text/html; charset=utf-8"], [".js", "text/javascript; charset=utf-8"],
  [".css", "text/css; charset=utf-8"], [".json", "application/json"],
  [".svg", "image/svg+xml"], [".png", "image/png"], [".woff2", "font/woff2"],
]);

function safeAsset(root: string, pathname: string): string | null {
  let decoded: string;
  try {
    decoded = decodeURIComponent(pathname);
  } catch {
    return null;
  }
  const relative = decoded.replace(/^\/+/, "");
  const candidate = path.resolve(root, relative || "index.html");
  const base = path.resolve(root) + path.sep;
  return candidate === path.resolve(root) || candidate.startsWith(base) ? candidate : null;
}

async function assetResponse(root: string, pathname: string, method: string): Promise<Response | null> {
  const asset = safeAsset(root, pathname);
  if (!asset || !existsSync(asset) || !statSync(asset).isFile()) return null;
  const body = method === "HEAD" ? null : await readFile(asset);
  return new Response(body, {headers: {
    "Content-Type": mime.get(path.extname(asset)) ?? "application/octet-stream",
    "Cache-Control": path.basename(asset) === "index.html" ? "no-cache" : "public, max-age=31536000, immutable",
  }});
}

export function createStaticDirAssets(staticDir: string): StaticAssets {
  const root = path.resolve(staticDir);
  return {
    async fetch(request: Request): Promise<Response> {
      if (!["GET", "HEAD"].includes(request.method)) return new Response("Method Not Allowed", {status: 405});
      const url = new URL(request.url);
      const found = await assetResponse(root, url.pathname, request.method);
      return found ?? new Response("Not Found", {status: 404});
    },
  };
}

async function toRequest(request: IncomingMessage, body: Buffer): Promise<Request> {
  const protocol = request.headers["x-forwarded-proto"] === "https" ? "https" : "http";
  const url = `${protocol}://${request.headers.host ?? "127.0.0.1"}${request.url ?? "/"}`;
  const headers = new Headers();
  for (const [key, value] of Object.entries(request.headers)) {
    if (Array.isArray(value)) value.forEach(item => headers.append(key, item));
    else if (value != null) headers.set(key, value);
  }
  return new Request(url, {method: request.method, headers,
    body: ["GET", "HEAD"].includes(request.method ?? "GET") ? undefined : new Uint8Array(body)});
}

async function writeResponse(response: Response, output: ServerResponse) {
  output.statusCode = response.status;
  response.headers.forEach((value, key) => output.setHeader(key, value));
  output.end(response.body ? Buffer.from(await response.arrayBuffer()) : undefined);
}

function clientIp(request: IncomingMessage, env: Record<string, unknown>): string {
  if (env.TRUST_PROXY_HEADERS === "1") {
    const forwarded = request.headers["x-forwarded-for"];
    const candidate = Array.isArray(forwarded) ? forwarded[0] : forwarded?.split(",")[0]?.trim();
    if (candidate && /^[0-9a-f:.]+$/i.test(candidate)) return candidate;
    const real = request.headers["x-real-ip"];
    if (typeof real === "string" && /^[0-9a-f:.]+$/i.test(real.trim())) return real.trim();
  }
  return request.socket.remoteAddress ?? "unknown";
}

export function startNodeServer(options: {
  handler: NodeFetchHandler;
  dbPath: string;
  staticDir?: string;
  port?: number;
  host?: string;
  env?: Record<string, unknown>;
  maintenance?: (kind: "cancellations" | "retention", env: Record<string, unknown>) => Promise<void>;
  enableTimers?: boolean;
}): {server: Server; store: NodeSqliteStore; ready: Promise<void>; flushBackground: () => Promise<void>; close: () => Promise<void>} {
  const store = new NodeSqliteStore(options.dbPath);
  const staticAssets = options.staticDir ? createStaticDirAssets(options.staticDir) : undefined;
  const staticDir = options.staticDir ? path.resolve(options.staticDir) : null;
  const pending = new Set<Promise<unknown>>();
  const scheduleBackground = (promise: Promise<unknown>) => {
    const tracked = Promise.resolve(promise).catch(() => undefined).finally(() => pending.delete(tracked));
    pending.add(tracked);
  };

  const server = createServer(async (incoming, output) => {
    try {
      const chunks: Buffer[] = [];
      for await (const chunk of incoming) chunks.push(Buffer.from(chunk));
      const request = await toRequest(incoming, Buffer.concat(chunks));
      const url = new URL(request.url);
      const resolvedIp = clientIp(incoming, options.env ?? {});
      const requestEnv: Record<string, unknown> = {
        ...options.env,
        DB: store,
        ...(staticAssets ? {ASSETS: staticAssets} : {}),
        __clientIp: resolvedIp,
      };
      let response: Response | null = null;
      if (staticDir && !url.pathname.startsWith("/api/") && ["GET", "HEAD"].includes(request.method)) {
        response = await assetResponse(staticDir, url.pathname, request.method);
        const hasExtension = /\.[A-Za-z0-9]{1,10}$/.test(url.pathname);
        if (!response && !hasExtension && request.headers.get("accept")?.includes("text/html")) {
          response = await assetResponse(staticDir, "/index.html", request.method);
        }
      }
      response ??= await options.handler(request, requestEnv, {clientIp: resolvedIp, scheduleBackground});
      await writeResponse(response, output);
    } catch (error) {
      output.statusCode = 500;
      output.setHeader("Content-Type", "application/json");
      output.end(JSON.stringify({code: "INTERNAL_ERROR", message: String(error)}));
    }
  });

  const timers: ReturnType<typeof setInterval>[] = [];
  if (options.enableTimers && options.maintenance) {
    const run = options.maintenance;
    const baseEnv = {...options.env, DB: store};
    timers.push(setInterval(() => scheduleBackground(run("cancellations", baseEnv)), 60_000));
    timers.push(setInterval(() => scheduleBackground(run("retention", baseEnv)), 86_400_000));
    for (const timer of timers) timer.unref();
  }

  const ready = new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(options.port ?? 8788, options.host ?? "127.0.0.1", () => {
      server.removeListener("error", reject);
      resolve();
    });
  });

  const flushBackground = async () => {
    while (pending.size) await Promise.all([...pending]);
  };

  return {
    server,
    store,
    ready,
    flushBackground,
    close: async () => {
      for (const timer of timers) clearInterval(timer);
      await flushBackground();
      await new Promise<void>(resolve => server.close(() => resolve()));
      store.close();
    },
  };
}

export function localPath(relative: string): string {
  return path.resolve(path.dirname(fileURLToPath(import.meta.url)), relative);
}
