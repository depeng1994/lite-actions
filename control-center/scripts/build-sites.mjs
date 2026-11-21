#!/usr/bin/env node
import {cp, mkdir, readFile, rm, writeFile} from "node:fs/promises";
import path from "node:path";
import {fileURLToPath} from "node:url";
import {execFileSync} from "node:child_process";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const web = path.join(root, "control-center", "web");
const output = path.join(root, "control-center", "dist");
const projectId = process.env.SITES_PROJECT_ID || "appgprj_6af5418eb3dc81919102f6a354ee22a8";

await rm(output, {recursive: true, force: true});
execFileSync("npm", ["run", "build"], {cwd: web, stdio: "inherit"});
await mkdir(output, {recursive: true});
await cp(path.join(web, "dist"), output, {recursive: true});

const manifestPath = path.join(output, ".openai", "hosting.json");
const manifest = JSON.parse(await readFile(manifestPath, "utf8"));
manifest.project_id = projectId;
manifest.d1 = manifest.d1 ?? null;
manifest.r2 = null;
await writeFile(manifestPath, JSON.stringify(manifest, null, 2) + "\n");

for (const required of [
  path.join(output, "client", "index.html"),
  path.join(output, "server", "index.js"),
  manifestPath,
]) await readFile(required);

const server = await readFile(path.join(output, "server", "index.js"), "utf8");
if (server.includes("workers.dev") || server.includes("chatgpt.site")) {
  throw new Error("Sites server bundle must receive CONTROL_CENTER_API_ORIGIN at runtime");
}
if (manifest.r2 !== null) throw new Error("Sites package must not configure R2");
console.log(`Built ChatGPT Sites package at ${path.relative(root, output)}`);
