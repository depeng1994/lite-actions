import {expect, test} from "vitest";
import {maskHostId, nodeIdentity, sanitizePublic} from "../src/utils/mask";

test("masks IPv4 ports and gives nodes with equal display addresses distinct keyed identities", async () => {
  expect(maskHostId("113.46.17.205:153")).toBe("113.xxx.xxx.205:153");
  expect(maskHostId("7.246.125.19:22")).toBe("7.xxx.xxx.19:22");
  expect(maskHostId("[2001:db8::1]:22")).toBe("masked");
  const first = await nodeIdentity("pepper", "113.46.17.205:153", false);
  const second = await nodeIdentity("pepper", "113.13.18.205:153", false);
  expect(first.display_host_id).toBe(second.display_host_id);
  expect(first.node_ref).toMatch(/^n_[a-f0-9]{12}$/);
  expect(first.node_ref).not.toBe(second.node_ref);
  expect(first).not.toHaveProperty("host_id");
});
test("removes nested sensitive fields and masks address strings", () => {
  expect(sanitizePublic({rows: [{host_id: "203.0.113.1:22", host_ssh: "private", api_token: "s", machine_fingerprint: "f",
    container_id: "c", diagnostics: "blocked", detail: "host=203.0.113.1:22"}]})).toEqual({
    rows: [{detail: "host=203.xxx.xxx.1:22"}],
  });
});
