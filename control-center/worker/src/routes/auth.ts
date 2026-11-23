import {type Router, body, HttpError} from "../router";
import {cookie, login, principal} from "../services/auth";

export const USERNAME = /^[A-Za-z0-9._-]{3,32}$/;

export function authRoutes(router: Router) {
  router.add("POST", "/api/v1/auth/login", async context => {
    const {username, secret} = await body(context);
    if (typeof username !== "string" || !USERNAME.test(username) || typeof secret !== "string" || secret.length > 256) {
      throw new HttpError(400, "INVALID_CREDENTIALS_FORMAT");
    }
    return login(context, username.toLowerCase(), secret);
  });
  router.add("POST", "/api/v1/auth/logout", async context => {
    const actor = await principal(context);
    if (actor) {
      await context.env.DB.prepare("UPDATE sessions SET revoked_at=? WHERE session_id=?")
        .bind(context.now, actor.session_id).run();
    }
    return Response.json({authenticated: false}, {headers: {"Set-Cookie": cookie(context, "", 0)}});
  });
  router.add("GET", "/api/v1/auth/me", async context => {
    const actor = await principal(context);
    return Response.json(actor ? {authenticated: true, username: actor.username, role: actor.role} : {authenticated: false});
  });
}
