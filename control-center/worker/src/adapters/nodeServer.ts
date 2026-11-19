import {createReadStream, existsSync, statSync} from "node:fs";
import {readFile} from "node:fs/promises";
import {createServer, type IncomingMessage, type Server, type ServerResponse} from "node:http";
import path from "node:path";
import {fileURLToPath} from "node:url";
import {NodeSqliteStore} from "./nodeSqlite.ts";

export type NodeFetchHandler = (request: Request, env: Record<string, unknown>) => Response | Promise<Response>;

const mime = new Map([
  [".html", "text/html; charset=utf-8"], [".js", "text/javascript; charset=utf-8"],
  [".css", "text/css; charset=utf-8"], [".json", "application/json"],
  [".svg", "image/svg+xml"], [".png", "image/png"], [".woff2", "font/woff2"],
]);

function safeAsset(root: string, pathname: string): string | null {
  const relative = decodeURIComponent(pathname).replace(/^\/+/, "");
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

async function toRequest(request: IncomingMessage, body: Buffer, clientIp: string): Promise<Request> {
  const protocol = request.headers["x-forwarded-proto"] === "https" ? "https" : "http";
  const url = `${protocol}://${request.headers.host ?? "127.0.0.1"}${request.url ?? "/"}`;
  const headers = new Headers();
  for (const [key, value] of Object.entries(request.headers)) {
    if (Array.isArray(value)) value.forEach(item => headers.append(key, item));
    else if (value != null) headers.set(key, value);
  }
  headers.set("CF-Connecting-IP", clientIp);
  return new Request(url, {method: request.method, headers,
    body: ["GET", "HEAD"].includes(request.method ?? "GET") ? undefined : body});
}

async function writeResponse(response: Response, output: ServerResponse) {
  output.statusCode = response.status;
  response.headers.forEach((value, key) => output.setHeader(key, value));
  output.end(response.body ? Buffer.from(await response.arrayBuffer()) : undefined);
}

export function startNodeServer(options: {
  handler: NodeFetchHandler;
  dbPath: string;
  staticDir?: string;
  port?: number;
  host?: string;
  env?: Record<string, unknown>;
}): {server: Server; ready: Promise<void>; close: () => Promise<void>} {
  const store = new NodeSqliteStore(options.dbPath);
  const staticDir = options.staticDir ? path.resolve(options.staticDir) : null;
  const server = createServer(async (incoming, output) => {
    try {
      const chunks: Buffer[] = [];
      for await (const chunk of incoming) chunks.push(Buffer.from(chunk));
      const request = await toRequest(incoming, Buffer.concat(chunks), incoming.socket.remoteAddress ?? "unknown");
      const url = new URL(request.url);
      let response: Response | null = null;
      if (staticDir && !url.pathname.startsWith("/api/") && ["GET", "HEAD"].includes(request.method)) {
        response = await assetResponse(staticDir, url.pathname, request.method);
        if (!response && request.headers.get("accept")?.includes("text/html")) response = await assetResponse(staticDir, "/index.html", request.method);
      }
      response ??= await options.handler(request, {...options.env, DB: store});
      await writeResponse(response, output);
    } catch (error) {
      output.statusCode = 500;
      output.setHeader("Content-Type", "application/json");
      output.end(JSON.stringify({code: "INTERNAL_ERROR", message: String(error)}));
    }
  });
  const ready = new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(options.port ?? 8788, options.host ?? "127.0.0.1", () => { server.removeListener("error", reject); resolve(); });
  });
  return {server, ready, close: async () => { store.close(); await new Promise<void>(resolve => server.close(() => resolve())); }};
}

export function localPath(relative: string): string {
  return path.resolve(path.dirname(fileURLToPath(import.meta.url)), relative);
}
