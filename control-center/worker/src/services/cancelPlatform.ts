import {type Env, HttpError} from "../router";
import type {Provider, RunIdentity} from "../types/api";
import {base64url} from "../utils/crypto";

const headers = {"Accept": "application/vnd.github+json", "User-Agent": "lite-actions-control-center",
  "X-GitHub-Api-Version": "2022-11-28"};
const GH = "https://api.github.com";
const GC = "https://api.gitcode.com/api/v8";
const installations = new Map<string, {id: number | null; until: number}>();
const installTokens = new Map<string, {token: string; until: number}>();

function der(tag: number, contents: number[]): number[] {
  const n = contents.length;
  const long = n > 255 ? [0x82, n >>> 8, n & 255] : n > 127 ? [0x81, n] : [n];
  return [tag, ...long, ...contents];
}

export async function appJwt(env: Env, now: number): Promise<string> {
  const pem = env.GITHUB_APP_PRIVATE_KEY?.replaceAll("\\n", "\n");
  if (!env.GITHUB_APP_ID || !pem) throw new HttpError(400, "CANCEL_NOT_CONFIGURED");
  let key: CryptoKey;
  try {
    let bytes = Array.from(atob(pem.replace(/-----[A-Z ]+-----|\s/g, "")), char => char.charCodeAt(0));
    if (pem.includes("BEGIN RSA PRIVATE KEY")) {
      const rsaOid = [0x30, 0x0d, 0x06, 0x09, 0x2a, 0x86, 0x48, 0x86, 0xf7, 0x0d, 0x01, 0x01, 0x01, 0x05, 0x00];
      bytes = der(0x30, [0x02, 0x01, 0, ...rsaOid, ...der(4, bytes)]);
    }
    key = await crypto.subtle.importKey("pkcs8", Uint8Array.from(bytes),
      {name: "RSASSA-PKCS1-v1_5", hash: "SHA-256"}, false, ["sign"]);
  } catch {
    throw new HttpError(503, "APP_KEY_INVALID");
  }
  const encoder = new TextEncoder();
  const encode = (data: unknown) => base64url(encoder.encode(JSON.stringify(data)));
  const payload = encode({alg: "RS256", typ: "JWT"}) + "." +
    encode({iat: now - 30, exp: now + 9 * 60, iss: env.GITHUB_APP_ID});
  const signature = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", key, encoder.encode(payload));
  return payload + "." + base64url(new Uint8Array(signature));
}

export function supportsCancel(env: Env, provider: Provider, repo: string) {
  if (provider === "gitcode") return Boolean(env.GITCODE_CANCEL_TOKEN);
  const row = installations.get(env.GITHUB_APP_ID + "/" + repo.toLowerCase());
  return Boolean(row?.id && row.until > Date.now() / 1000);
}
function remember<T>(cache: Map<string, T>, key: string, item: T) {
  if (cache.size >= 128 && !cache.has(key)) cache.delete(cache.keys().next().value!);
  cache.set(key, item);
}

async function platformRequest(url: string, init: RequestInit) {
  try {
    return await fetch(url, {...init, redirect: "manual", signal: AbortSignal.timeout(8000)});
  } catch {
    throw new HttpError(502, "PLATFORM_UNREACHABLE");
  }
}

function platformFailure(response: Response): never {
  throw new HttpError(502, "PLATFORM_HTTP_ERROR", "Platform returned HTTP " + response.status);
}

export async function installation(env: Env, repo: string, now: number, getJwt = () => appJwt(env, now)) {
  const key = env.GITHUB_APP_ID + "/" + repo.toLowerCase();
  const previous = installations.get(key);
  if (previous && previous.until > now) return previous.id;
  if (!env.GITHUB_APP_ID || !env.GITHUB_APP_PRIVATE_KEY) return null;
  const jwt = await getJwt();
  const response = await platformRequest(`${GH}/repos/${repo}/installation`, {headers: {...headers, Authorization: "Bearer " + jwt}});
  let id: number | null = null;
  if (response.status !== 404) {
    if (!response.ok) platformFailure(response);
    id = (await response.json<{id: number}>()).id;
    if (!Number.isSafeInteger(id) || id < 1) throw new HttpError(502, "INVALID_INSTALLATION");
  }
  remember(installations, key, {id, until: now + 600});
  return id;
}

async function installationToken(env: Env, repo: string, now: number) {
  let jwt: string | undefined;
  const getJwt = async () => jwt ??= await appJwt(env, now);
  const id = await installation(env, repo, now, getJwt);
  if (!id) throw new HttpError(400, "APP_NOT_INSTALLED");
  const key = env.GITHUB_APP_ID + "/" + id;
  const cached = installTokens.get(key);
  if (cached && cached.until > now) return cached.token;
  const response = await platformRequest(`${GH}/app/installations/${id}/access_tokens`, {
    method: "POST", headers: {...headers, Authorization: "Bearer " + await getJwt(), "Content-Type": "application/json"},
    body: JSON.stringify({permissions: {actions: "write"}}),
  });
  if (!response.ok) platformFailure(response);
  const result = await response.json<{token: string; expires_at: string}>();
  if (typeof result.token !== "string" || !result.token) throw new HttpError(502, "INSTALLATION_TOKEN_UNAVAILABLE");
  const expires = Date.parse(result.expires_at) / 1000;
  remember(installTokens, key, {token: result.token, until: Number.isFinite(expires) ? Math.min(now + 2700, expires - 60) : now});
  return result.token;
}
export async function workflowPlatform(env: Env, run: RunIdentity, now: number) {
  const {provider, repo, run_id} = run;
  const github = provider === "github";
  let auth: HeadersInit;
  if (github) {
    auth = {...headers, Authorization: "Bearer " + await installationToken(env, repo, now)};
  } else {
    if (!env.GITCODE_CANCEL_TOKEN) throw new HttpError(400, "CANCEL_NOT_CONFIGURED");
    auth = {"PRIVATE-TOKEN": env.GITCODE_CANCEL_TOKEN, Accept: "application/json"};
  }
  const url = `${github ? GH : GC}/repos/${repo}/actions/runs/${run_id}`;
  return {
    async state() {
      const response = await platformRequest(url, {headers: auth});
      if (!response.ok) platformFailure(response);
      const info = await response.json<{id?: number | string; workflow_run_id?: string; run_attempt?: number; attempt?: number; status?: string; conclusion?: string}>();
      const actualId = github ? info.id : info.workflow_run_id;
      const attempt = Number(github ? info.run_attempt : info.attempt);
      if (String(actualId) !== run_id || !Number.isSafeInteger(attempt) || attempt < 1 || !info.status) {
        throw new HttpError(502, "PLATFORM_RUN_UNVERIFIED");
      }
      const state = info.status.toLowerCase();
      const terminal = ["completed","failed","cancelled","canceled","ignored","stopped","success","succeeded"].includes(state);
      const active = ["queued","in_progress","waiting","pending","running","requested"].includes(state);
      if (!terminal && !active) throw new HttpError(502, "PLATFORM_RUN_UNVERIFIED");
      return {attempt, terminal, conclusion: info.conclusion ?? (terminal ? state : null)};
    },
    async stop(force = false) {
      const endpoint = github ? (force ? "/force-cancel" : "/cancel") : "/stop";
      const response = await platformRequest(url + endpoint, {method: "POST", headers: auth});
      if (!response.ok) platformFailure(response);
    },
  };
}
