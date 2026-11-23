import {createD1Store} from "./adapters/d1";
import {handleRequest, router, runMaintenance} from "./app";
import type {Env} from "./router";
import type {SqlStore, StaticAssets} from "./storage/sqlStore";

export {handleRequest, router, runMaintenance};

export interface WorkerEnv extends Omit<Env, "DB" | "ASSETS"> {
  DB: D1Database | SqlStore;
  ASSETS?: Fetcher | StaticAssets;
}

function adaptEnv(env: WorkerEnv): Env {
  const db = "dbSizeBytes" in env.DB && typeof env.DB.dbSizeBytes === "function"
    ? env.DB as SqlStore
    : createD1Store(env.DB as D1Database);
  return {...env, DB: db};
}

export default {
  async scheduled(controller: ScheduledController, env: WorkerEnv): Promise<void> {
    const adapted = adaptEnv(env);
    const now = Math.floor(controller.scheduledTime / 1000);
    if (controller.cron === "* * * * *") {
      await runMaintenance("cancellations", adapted, now);
    } else if (controller.cron === "0 2 * * *") {
      await runMaintenance("retention", adapted, now);
    }
  },
  async fetch(request: Request, env: WorkerEnv, execution?: ExecutionContext): Promise<Response> {
    const adapted = adaptEnv(env);
    const clientIp = env.__clientIp ?? request.headers.get("CF-Connecting-IP") ?? "unknown";
    return handleRequest(request, adapted, {
      clientIp,
      scheduleBackground: execution ? promise => execution.waitUntil(promise) : undefined,
    });
  },
} satisfies ExportedHandler<WorkerEnv>;
