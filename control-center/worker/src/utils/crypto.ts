const encoder = new TextEncoder();

export function base64url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}
export function randomSecret(prefix = ""): string {
  return prefix + base64url(crypto.getRandomValues(new Uint8Array(32)));
}
export function hex(bytes: ArrayBuffer): string {
  return Array.from(new Uint8Array(bytes), byte => byte.toString(16).padStart(2, "0")).join("");
}
export async function sha256(value: string | ArrayBuffer): Promise<string> {
  return hex(await crypto.subtle.digest("SHA-256", typeof value === "string" ? encoder.encode(value) : value));
}
export async function hmac(pepper: string, message: string): Promise<string> {
  const key = await crypto.subtle.importKey("raw", encoder.encode(pepper), {name: "HMAC", hash: "SHA-256"}, false, ["sign"]);
  return hex(await crypto.subtle.sign("HMAC", key, encoder.encode(message)));
}
export function safeHashEqual(left: string, right: string | undefined): boolean {
  const valid = typeof right === "string" && /^[a-f0-9]{64}$/.test(right);
  const compare = valid ? right : "0".repeat(64);
  return crypto.subtle.timingSafeEqual(encoder.encode(left), encoder.encode(compare)) && valid;
}
