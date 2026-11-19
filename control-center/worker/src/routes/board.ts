import type {Router} from "../router";
import {modelRuns, models, overview, taskDetail, timeline, workflows} from "../services/board";

export function boardRoutes(router: Router) {
  for (const access of ["public", "admin"]) {
    const prefix = "/api/v1/" + access;
    for (const [path, read] of [
      ["/overview", overview], ["/timeline", timeline], ["/models", models],
      ["/models/:slug/runs", modelRuns], ["/workflows", workflows], ["/tasks/:taskUid", taskDetail],
    ] as const) {
      router.add("GET", prefix + path, async context => Response.json(await read(context)));
    }
  }
}
