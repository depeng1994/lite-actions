import {cp, mkdir, readdir, rm} from "node:fs/promises";
import path from "node:path";
import {fileURLToPath} from "node:url";

const workerRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const source = path.resolve(process.env.FRONTEND_DIST || path.join(workerRoot, "../../../lite-actions-control-center-demo/dist/client"));
const target = path.join(workerRoot, "public");

await mkdir(target, {recursive: true});
for (const entry of await readdir(target)) await rm(path.join(target, entry), {recursive: true, force: true});
for (const entry of await readdir(source)) await cp(path.join(source, entry), path.join(target, entry), {recursive: true});
console.log(`Synced frontend assets from ${source} to ${target}`);
