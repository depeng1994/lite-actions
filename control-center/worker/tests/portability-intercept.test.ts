import {expect, test} from "vitest";
import {toArrayBuffer} from "../src/storage/sqlStore";

const sourceFiles = import.meta.glob("../src/**/*.ts", {eager: true, query: "?raw", import: "default"}) as Record<string, string>;

test("runtime lock-in scan rejects forbidden primitives outside the CF adapter", () => {
  const forbidden = [
    /crypto\.subtle\.timingSafeEqual/,
    /https?:\/\/[^\s"'`]*workers\.dev/,
  ];
  const findings: string[] = [];
  for (const [path, text] of Object.entries(sourceFiles)) {
    const relativePath = path.replace(/^\.\.\/src\//, "");
    for (const pattern of forbidden) if (pattern.test(text)) findings.push(`${relativePath}: ${pattern}`);
    // The Cloudflare entry point is an adapter boundary. Core services must
    // consume Context.clientIp instead of parsing provider headers themselves.
    if (relativePath !== "index.ts" && /CF-Connecting-IP/.test(text)) {
      findings.push(`${relativePath}: /CF-Connecting-IP/`);
    }
  }
  expect(findings).toEqual([]);
});

test("BLOB normalization is equivalent for ArrayBuffer, views, and byte arrays", () => {
  const bytes = [0, 1, 127, 255];
  expect([...new Uint8Array(toArrayBuffer(bytes))]).toEqual(bytes);
  const view = new Uint8Array(bytes).subarray(1, 3);
  expect([...new Uint8Array(toArrayBuffer(view))]).toEqual([1, 127]);
  expect([...new Uint8Array(toArrayBuffer(view.buffer))]).toEqual(bytes);
  expect(() => toArrayBuffer("not-a-blob")).toThrow(TypeError);
});
