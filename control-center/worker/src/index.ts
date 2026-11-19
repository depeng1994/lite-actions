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
const privateCache = "private, no-store";

export default {
  async scheduled(controller: ScheduledController, env: Env): Promise<void> {
    if (controller.cron === "* * * * *") {
      await trackCancellations({
        request: new Request("https://internal.invalid/"), url: new URL("https://internal.invalid/"),
        env, params: {}, now: Math.floor(controller.scheduledTime / 1000),
      });
    }
  },
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
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
      const response = await router.dispatch(context);
      if (isPublic && response.ok) {
        const json = JSON.stringify(sanitizePublic(await response.json()));
        publicCache.put(key, json);
        return new Response(json, {status: response.status, headers: publicHeaders});
      }
      const headers = new Headers(response.headers);
      headers.set("Cache-Control", privateCache);
      return new Response(response.body, {status: response.status, headers});
    } catch (error) {
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
