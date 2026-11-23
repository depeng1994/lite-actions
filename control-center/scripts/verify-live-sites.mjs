import process from "node:process";

const originRaw = (process.env.SITES_ORIGIN || process.argv[2] || "").trim();
if (!originRaw) {
  throw new Error("SITES_ORIGIN (or argv[2]) is required, e.g. https://your-site.example");
}

let originUrl;
try {
  originUrl = new URL(originRaw);
} catch {
  throw new Error(`Invalid SITES_ORIGIN URL: ${originRaw}`);
}
if (!["http:", "https:"].includes(originUrl.protocol)) {
  throw new Error(`SITES_ORIGIN must use http or https: ${originRaw}`);
}
const base = originUrl.origin;

async function checkHtml(pathname) {
  const res = await fetch(`${base}${pathname}`, {
    headers: { Accept: "text/html,application/xhtml+xml" },
    redirect: "follow",
  });
  const ct = res.headers.get("content-type") || "";
  const body = await res.text();
  if (res.status !== 200 || !ct.includes("text/html") || !body.includes("<div id=\"root\"")) {
    throw new Error(`Expected 200 SPA HTML for ${pathname}, got status=${res.status} content-type=${ct}`);
  }
  return { pathname, status: res.status, contentType: ct };
}

async function checkJson(pathname, expectedStatus) {
  const res = await fetch(`${base}${pathname}`, {
    headers: { Accept: "application/json" },
  });
  const ct = res.headers.get("content-type") || "";
  const text = await res.text();
  if (res.status !== expectedStatus || !ct.includes("application/json")) {
    throw new Error(
      `Expected ${expectedStatus} JSON for ${pathname}, got status=${res.status} content-type=${ct} body=${text.slice(0, 200)}`,
    );
  }
  return { pathname, status: res.status, json: JSON.parse(text) };
}

async function checkAdminLogin() {
  const username = (process.env.VERIFY_ADMIN_USERNAME || "").trim();
  const secret = (process.env.VERIFY_ADMIN_SECRET || "").trim();
  if (!username || !secret) {
    return { skipped: true };
  }
  const loginRes = await fetch(`${base}/api/v1/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ username, secret }),
  });
  if (loginRes.status !== 200) {
    throw new Error(`Admin login failed with status=${loginRes.status}: ${(await loginRes.text()).slice(0, 200)}`);
  }
  const setCookie = loginRes.headers.get("set-cookie") || "";
  const cookiePair = setCookie.split(";")[0];
  if (!cookiePair.includes("=")) {
    throw new Error("Admin login did not return a session cookie");
  }
  const adminRes = await fetch(`${base}/api/v1/admin/overview`, {
    headers: { Cookie: cookiePair, Accept: "application/json" },
  });
  if (adminRes.status !== 200) {
    throw new Error(`Authenticated /api/v1/admin/overview failed with status=${adminRes.status}`);
  }
  await fetch(`${base}/api/v1/auth/logout`, {
    method: "POST",
    headers: { Cookie: cookiePair, Accept: "application/json" },
  });
  return { skipped: false, authenticatedOverviewStatus: adminRes.status };
}

const results = {
  origin: base,
  rootHtml: await checkHtml("/"),
  deepLinkModels: await checkHtml("/models"),
  deepLinkCluster: await checkHtml("/cluster"),
  overviewApi: await checkJson("/api/v1/public/overview", 200),
  workflowsApi: await checkJson("/api/v1/public/workflows?history_limit=0", 200),
  missingApi: await checkJson("/api/v1/does-not-exist", 404),
  adminAuth: await checkAdminLogin(),
};

console.log(
  JSON.stringify(
    {
      ok: true,
      origin: results.origin,
      spaChecks: [results.rootHtml.pathname, results.deepLinkModels.pathname, results.deepLinkCluster.pathname],
      overviewWorkers: (results.overviewApi.json.workers || []).map((w) => ({
        worker: w.worker_id || w.worker,
        offline: w.offline,
        applied_rev: w.applied_rev,
      })),
      missingApiStatus: results.missingApi.status,
      adminAuth: results.adminAuth,
    },
    null,
    2,
  ),
);
