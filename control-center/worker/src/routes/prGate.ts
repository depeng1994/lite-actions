import {body, type Router} from "../router";
import {
  createGitcodeIntent, getPrGateStatus, mergePrWithGate,
  reconcilePrGateIntents, registerPrGate, reportPrGate, updateGitcodeIntent,
} from "../services/prGate";

export function prGateRoutes(router: Router) {
  const prefix = "/api/v1/pr-gate";
  router.add("GET", prefix + "/status", async context => Response.json(await getPrGateStatus(context)));
  router.add("POST", prefix + "/register", async context =>
    Response.json(await registerPrGate(context, await body(context))));
  router.add("POST", prefix + "/report", async context =>
    Response.json(await reportPrGate(context, await body(context))));
  router.add("POST", prefix + "/intents", async context =>
    Response.json(await createGitcodeIntent(context, await body(context))));
  router.add("POST", prefix + "/intents/:intentId", async context =>
    Response.json(await updateGitcodeIntent(context, context.params.intentId, await body(context))));
  router.add("POST", prefix + "/reconcile", async context =>
    Response.json(await reconcilePrGateIntents(context)));
  router.add("POST", prefix + "/merge", async context =>
    Response.json(await mergePrWithGate(context, await body(context))));
}
