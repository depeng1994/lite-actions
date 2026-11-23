import {body, type Router} from "../router";
import {cluster, clusterEdit} from "../services/cluster";

export function clusterRoutes(router: Router) {
  for (const access of ["public", "admin"]) {
    router.add("GET", `/api/v1/${access}/cluster`, async context => Response.json(await cluster(context)));
  }
  for (const mode of ["preview","publish","rollback"] as const) {
    router.add("POST", "/api/v1/admin/cluster/" + mode, async context =>
      Response.json(await clusterEdit(context, await body(context), mode)));
  }
}
