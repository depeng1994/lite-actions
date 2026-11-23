import {sha256} from "./crypto";
import type {RunIdentity} from "../types/api";

export function taskKey(identity: RunIdentity, caseId: string, dispatchId: string | null): string {
  const prefix = identity.provider === "gitcode" ? "gitcode:" : "";
  const dispatch = dispatchId ? ":" + dispatchId : "";
  return `${prefix}${identity.repo}:${identity.run_id}:${identity.attempt}${dispatch}:${caseId}`;
}

export async function taskIdentity(identity: RunIdentity, caseId: string, dispatchId: string | null) {
  const key = taskKey(identity, caseId, dispatchId);
  const hash = await sha256(key);
  return {task_uid: "t_" + hash.slice(0, 32), short_tid: hash.slice(0, 12), task_key: key};
}
export function runUrl(identity: RunIdentity): string {
  const base = identity.provider === "github" ? "https://github.com" : "https://gitcode.com";
  const repo = identity.repo.split("/").map(encodeURIComponent).join("/");
  return `${base}/${repo}/actions/runs/${encodeURIComponent(identity.run_id)}`;
}
