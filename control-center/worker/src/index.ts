import {quotaError, requireWriteQuota} from "./middleware/quota";
import {capacity, retention} from "./services/retention";
import {cancellationRoutes} from "./routes/cancellations";
import {trackCancellations} from "./services/cancellations";
import {authorize} from "./middleware/access";
import {authRoutes} from "./routes/auth";
import {userRoutes} from "./routes/users";
import {workerRoutes} from "./routes/worker";
import {boardRoutes} from "./routes/board";
import {clusterRoutes} from "./routes/cluster";
import {commandRoutes} from "./routes/commands";
import {type Context, type Env, HttpError, Router} from "./router";
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
let nextCapacityProbe = 0;
const privateCache = "private, no-store";

export default {
  async scheduled(controller: ScheduledController, env: Env): Promise<void> {
    const context: Context = {
      request: new Request("https://internal.invalid/"), url: new URL("https://internal.invalid/"),
      env, params: {}, now: Math.floor(controller.scheduledTime / 1000),
    };
    try {
      if (controller.cron === "* * * * *") {
        await trackCancellations(context);
      } else if (controller.cron === "0 2 * * *") {
        await requireWriteQuota(context);
        await retention(context);
      }
    } catch (error) {
      await quotaError(context, error);
      console.warn("Control center maintenance deferred");
    }
  },
  async fetch(request: Request, env: Env, execution?: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    const isApi = url.pathname.startsWith("/api/");
    if (!isApi && env.ASSETS) {
      const asset = await env.ASSETS.fetch(request);
      if (asset.status !== 404 || !request.headers.get("accept")?.includes("text/html") || !["GET", "HEAD"].includes(request.method)) {
        return asset;
      }
      const indexUrl = new URL(request.url);
      indexUrl.pathname = "/index.html";
      indexUrl.search = "";
      return env.ASSETS.fetch(new Request(indexUrl, request));
    }
    const isPublic = url.pathname.startsWith("/api/v1/public/");
    const key = url.href;
    const context: Context = {request, env, url, params: {}, now: Math.floor(Date.now() / 1000)};
    try {
      if (isPublic) {
        if (request.method !== "GET") throw new HttpError(405, "READ_ONLY", "Public API is read-only", {Allow: "GET"});
        const cached = publicCache.get(key);
        if (cached !== undefined) return new Response(cached, {headers: publicHeaders});
      }
      await authorize(context);
      const mutating = !isPublic && ["POST","DELETE","PATCH","PUT"].includes(request.method);
      if (mutating) await requireWriteQuota(context);
      const response = await router.dispatch(context);
      // Sample write traffic at most once per isolate per minute. The daily
      // trigger is the fallback when no writes reach this isolate.
      if (mutating && execution && response.ok && context.now >= nextCapacityProbe) {
        nextCapacityProbe = context.now + 60;
        execution.waitUntil(capacity(context).catch(async error => { await quotaError(context, error); }));
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
  },
} satisfies ExportedHandler<Env>;
