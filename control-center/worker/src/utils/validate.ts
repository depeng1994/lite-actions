import {HttpError} from "../router";

export function invalid(message: string): never {
  throw new HttpError(400, "INVALID_INPUT", message);
}
export function object(value: unknown, field: string): Record<string, unknown> {
  if (!value || Array.isArray(value) || typeof value !== "object") invalid(field);
  return value as Record<string, unknown>;
}
export function array(value: unknown, field: string, max = 256): unknown[] {
  if (!Array.isArray(value) || value.length > max) invalid(field);
  return value;
}
export function integer(value: unknown, field: string, min = 0, max = Number.MAX_SAFE_INTEGER): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < min || value > max) invalid(field);
  return value;
}
export function string(value: unknown, field: string, max: number, pattern?: RegExp): string {
  if (typeof value !== "string" || new TextEncoder().encode(value).length > max || (pattern && !pattern.test(value))) {
    invalid(field);
  }
  return value;
}
export function member<T extends string>(value: unknown, field: string, allowed: readonly T[]): T {
  if (!allowed.includes(value as T)) invalid(field);
  return value as T;
}
export function nullableInt(value: unknown, field: string, min = 0): number | null {
  return value == null ? null : integer(value, field, min);
}
export function nullableString(value: unknown, field: string, max: number, pattern?: RegExp): string | null {
  return value == null ? null : string(value, field, max, pattern);
}
export const REPO = /^[A-Za-z0-9][A-Za-z0-9_.-]*\/[A-Za-z0-9][A-Za-z0-9_.-]*$/;
export const RUN_ID = /^[A-Za-z0-9_-]{1,128}$/;
export const POOL = /^[A-Za-z0-9_.-]{0,64}$/;
export const CASE_ID = /^case_[0-9]{3}_[a-z][a-z0-9_]{0,79}$/;
export const TID = /^[a-f0-9]{12}$/;
export const TEST_WORKER_ID = /^(blue|yellow)-test-id[0-9]{1,20}a[0-9]{1,4}$/;
export const SHA40 = /^[0-9a-f]{40}$/;
export const PHASES = ["QUEUED", "RESERVED", "STAGING", "RUNNING", "CLEANING"] as const;
export const CASE_KINDS = ["npu_smoke", "train", "worker_probe", "test_worker_deploy", "test_worker_cleanup"] as const;
export const STATUSES = ["PASS", "FAIL", "TIMED_OUT", "CANCELLED", "INFRA_ERROR", "RESOURCE_TIMEOUT", "NOT_RUN"] as const;

export function isTestWorkerId(value: unknown): value is string {
  return typeof value === "string" && TEST_WORKER_ID.test(value);
}

export function workerZoneOf(value: string): "blue" | "yellow" {
  if (value === "blue" || value === "yellow") return value;
  const match = TEST_WORKER_ID.exec(value);
  if (match) return match[1] as "blue" | "yellow";
  invalid("worker_id");
}

export function hostId(value: unknown, field = "host_id") {
  const host = string(value, field, 21, /^\d{1,3}(?:\.\d{1,3}){3}:\d{1,5}$/);
  const [ip, port] = host.split(":");
  if (+port < 1 || +port > 65535 || String(+port) !== port
    || ip.split(".").some(octet => +octet > 255 || String(+octet) !== octet)) invalid(field);
  return host;
}
export function devices(value: unknown, field = "devices"): number[] {
  const ids = array(value, field, 256).map(id => integer(id, field, 0, 255));
  if (!ids.length || new Set(ids).size !== ids.length) invalid(field);
  return ids.sort((a, b) => a - b);
}
