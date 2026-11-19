import {hmac} from "./crypto";
import type {NodeIdentity} from "../types/api";

const ipv4 = /\b(\d{1,3})\.\d{1,3}\.\d{1,3}\.(\d{1,3})\b/g;
const ipv4Host = /^(\d{1,3})\.\d{1,3}\.\d{1,3}\.(\d{1,3})(:\d{1,5})?$/;
const prohibited = /^(?:host_?id|node_?ip|.*ssh|container(?:_?id)?|.*fingerprint|last_?20_?lines|diagnostics?|log_?locator|.*logs?|.*tokens?|.*secrets?|.*passwords?|key_?hash|.*private_?key|credentials?|task_key)$/i;

export function maskHostId(host: string) {
  const parsed = ipv4Host.exec(host);
  return parsed ? `${parsed[1]}.xxx.xxx.${parsed[2]}${parsed[3] ?? ""}` : "masked";
}

export async function nodeIdentity(pepper: string, host: string, admin: boolean): Promise<NodeIdentity> {
  const node_ref = "n_" + (await hmac(pepper, host)).slice(0, 12);
  const display_host_id = admin ? host : maskHostId(host);
  return {node_ref, display_host_id, ...(admin ? {host_id: host} : {})};
}

export function sanitizePublic(value: unknown): unknown {
  if (typeof value === "string") return value.replace(ipv4, "$1.xxx.xxx.$2");
  if (Array.isArray(value)) return value.map(sanitizePublic);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).filter(([key]) => !prohibited.test(key)).map(
      ([key, val]) => [key.replace(ipv4, "$1.xxx.xxx.$2"), sanitizePublic(val)],
    ));
  }
  return value;
}
