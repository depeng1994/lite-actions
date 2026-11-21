#!/usr/bin/env node
import { copyFileSync, existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const dist = path.join(root, "dist");
const index = path.join(dist, "client", "index.html");
const hosting = path.join(root, ".openai", "hosting.json");

for (const file of [index, hosting]) {
  if (!existsSync(file)) throw new Error("Missing Sites build input: " + file);
}

mkdirSync(path.join(dist, "server"), { recursive: true });
mkdirSync(path.join(dist, ".openai"), { recursive: true });
copyFileSync(hosting, path.join(dist, ".openai", "hosting.json"));

const client = path.join(dist, "client");
const assets = {};
const contentType = (file) => ({
  ".css": "text/css; charset=utf-8", ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
  ".json": "application/json", ".svg": "image/svg+xml", ".woff2": "font/woff2", ".png": "image/png",
}[path.extname(file)] || "application/octet-stream");
for (const relative of ["index.html", ...readdirSync(path.join(client, "assets"), {recursive: true}).map(file => path.join("assets", file))]) {
  const normalized = relative.split(path.sep).join("/");
  const full = path.join(client, normalized);
  if (existsSync(full)) assets[`/${normalized}`] = {
    body: readFileSync(full).toString("base64"), type: contentType(full),
  };
}
const server = `const ASSETS = ${JSON.stringify(assets)};
const decode = (value) => Uint8Array.from(atob(value), (char) => char.charCodeAt(0));
const assetResponse = (key) => {
  const asset = ASSETS[key];
  if (!asset) return null;
  return new Response(decode(asset.body), {headers: {"Content-Type": asset.type, "Cache-Control": key === "/index.html" ? "no-cache" : "public, max-age=31536000, immutable"}});
};
export default {async fetch(request, env) {
  const url = new URL(request.url);
  if (url.pathname === "/api/v1" || url.pathname.startsWith("/api/v1/")) {
    const origin = typeof env.CONTROL_CENTER_API_ORIGIN === "string"
      ? env.CONTROL_CENTER_API_ORIGIN.replace(/\\/$/, "") : "";
    if (!origin) return Response.json({code: "CONTROL_CENTER_API_NOT_CONFIGURED"}, {status: 503});
    const target = new URL(origin + url.pathname + url.search);
    const headers = new Headers(request.headers);
    headers.delete("host");
    return fetch(new Request(target, {method: request.method, headers, body: ["GET", "HEAD"].includes(request.method) ? undefined : request.body, redirect: "manual"}));
  }
  return assetResponse(url.pathname) || assetResponse("/index.html") || new Response("Not found", {status: 404});
}};`;
writeFileSync(path.join(dist, "server", "index.js"), server);

console.log("Prepared server-backed Sites build: dist/server/index.js and dist/.openai/hosting.json");
