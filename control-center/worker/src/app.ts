import {authorize} from "./middleware/access";
import {quotaError, requireWriteQuota} from "./middleware/quota";
import {type Context, type Env, HttpError, Router} from "./router";
import {authRoutes} from "./routes/auth";
import {boardRoutes} from "./routes/board";
import {cancellationRoutes} from "./routes/cancellations";
import {clusterRoutes} from "./routes/cluster";
import {commandRoutes} from "./routes/commands";
import {userRoutes} from "./routes/users";
import {workerRoutes} from "./routes/worker";
import {trackCancellations} from "./services/cancellations";
import {capacity, retention} from "./services/retention";
import type {SqlStore} from "./storage/sqlStore";
import {sanitizePublic} from "./utils/mask";
import {PublicCache} from "./utils/microCache";

export const router = new Router();
boardRoutes(router);
cancellationRoutes(router);
commandRoutes(router);
clusterRoutes(router);
authRoutes(router);
userRoutes(router);
workerRoutes(router);

const publicCache = new PublicCache();
const publicHeaders = {"Cache-Control": "public, max-age=10", "Content-Type": "application/json"};
const privateCache = "private, no-store";

interface MaintenanceTicks {
  capacity: number;
  cancel: number;
  retention: number;
}
const ticksByStore = new WeakMap<SqlStore, MaintenanceTicks>();
function ticksFor(store: SqlStore): MaintenanceTicks {
  let state = ticksByStore.get(store);
  if (!state) {
    state = {capacity: 0, cancel: 0, retention: 0};
    ticksByStore.set(store, state);
  }
  return state;
}

export interface HandleOptions {
  clientIp?: string;
  now?: number;
  scheduleBackground?: (promise: Promise<unknown>) => void;
}

export async function runMaintenance(kind: "cancellations" | "retention", env: Env, now = Math.floor(Date.now() / 1000)): Promise<void> {
  const context: Context = {
    request: new Request("https://internal.invalid/"),
    url: new URL("https://internal.invalid/"),
    env,
    params: {},
    clientIp: "system",
    now,
  };
  try {
    if (kind === "cancellations") {
      await trackCancellations(context);
    } else {
      await requireWriteQuota(context);
      await retention(context);
    }
  } catch (error) {
    await quotaError(context, error);
    console.warn("Control center maintenance deferred");
  }
}

export async function handleRequest(request: Request, env: Env, options: HandleOptions = {}): Promise<Response> {
  const url = new URL(request.url);
  const isApi = url.pathname.startsWith("/api/");
  if (!isApi && env.ASSETS) {
    const asset = await env.ASSETS.fetch(request);
    const hasExtension = /\.[A-Za-z0-9]{1,10}$/.test(url.pathname);
    if (asset.status !== 404 || hasExtension || !request.headers.get("accept")?.includes("text/html") || !["GET", "HEAD"].includes(request.method)) {
      return asset;
    }
    const indexUrl = new URL(request.url);
    indexUrl.pathname = "/index.html";
    indexUrl.search = "";
    return env.ASSETS.fetch(new Request(indexUrl, request));
  }
  const isPublic = url.pathname.startsWith("/api/v1/public/");
  const key = url.href;
  const now = options.now ?? Math.floor(Date.now() / 1000);
  const context: Context = {
    request,
    env,
    url,
    params: {},
    clientIp: options.clientIp ?? env.__clientIp ?? "unknown",
    now,
  };
  try {
    if (isPublic) {
      if (request.method !== "GET") throw new HttpError(405, "READ_ONLY", "Public API is read-only", {Allow: "GET"});
      const cached = publicCache.get(key);
      if (cached !== undefined) return new Response(cached, {headers: publicHeaders});
    }
    await authorize(context);
    const mutating = !isPublic && ["POST", "DELETE", "PATCH", "PUT"].includes(request.method);
    if (mutating) await requireWriteQuota(context);
    const response = await router.dispatch(context);
    const schedule = options.scheduleBackground;
    if (schedule && response.ok) {
      const ticks = ticksFor(env.DB);
      const lazyCancelRoute = url.pathname === "/api/v1/worker/heartbeat" || url.pathname.startsWith("/api/v1/admin/cancellations");
      if (lazyCancelRoute && context.now >= ticks.cancel) {
        ticks.cancel = context.now + 30;
        schedule(trackCancellations(context).catch(() => undefined));
      }
      if (mutating && context.now >= ticks.retention) {
        ticks.retention = context.now + 3600;
        schedule(requireWriteQuota(context).then(() => retention(context)).catch(() => undefined));
      }
      if (mutating && context.now >= ticks.capacity) {
        ticks.capacity = context.now + 60;
        schedule(capacity(context).catch(async error => { await quotaError(context, error); }));
      }
    }
    if (isPublic && response.ok) {
      const json = JSON.stringify(sanitizePublic(await response.json()));
      publicCache.put(key, json);
      return new Response(json, {status: response.status, headers: publicHeaders});
    }
    const headers = new Headers(response.headers);
    headers.set("Cache-Control", privateCache);
    return new Response(response.body, {status: response.status, headers});
  } catch (caught) {
    const error = await quotaError(context, caught);
    const expected = error instanceof HttpError;
    const headers = new Headers(expected ? error.headers : undefined);
    headers.set("Cache-Control", privateCache);
    return Response.json(
      expected ? {code: error.code, message: error.message} : {code: "INTERNAL_ERROR", message: "Internal server error"},
      {status: expected ? error.status : 500, headers},
    );
  }
}
