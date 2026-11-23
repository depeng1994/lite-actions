import {body, type Router} from "../router";
import {cancellationDetail, requestCancellation} from "../services/cancellations";
import {models} from "../services/board";

export function cancellationRoutes(router: Router) {
  router.add("POST", "/api/v1/admin/cancellations",
    async context => Response.json(await requestCancellation(context, await body(context))));
  router.add("GET", "/api/v1/admin/cancellations/:cancelId",
    async context => Response.json(await cancellationDetail(context, context.params.cancelId)));
  router.add("GET", "/api/v1/admin/capabilities/cancel", async context => {
    const {models: tabs} = await models(context);
    const repos = tabs.flatMap(tab => tab.repos);
    return Response.json({repos});
  });
}
