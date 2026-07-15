// @vitest-environment jsdom
/**
 * On-prem / production nginx API base URL resolution tests.
 */
import { describe, expect, it } from "vitest";

import { resolveOnPremiseApiBaseUrl } from "@/lib/api-config";

describe("resolveOnPremiseApiBaseUrl", () => {
  it("uses same-origin relative /api when served on default port 80", () => {
    expect(resolveOnPremiseApiBaseUrl("192.168.50.130", "http:", "")).toBe("");
  });

  it("uses same-origin relative /api when served on explicit port 80", () => {
    expect(resolveOnPremiseApiBaseUrl("192.168.50.130", "http:", "80")).toBe("");
  });

  it("uses same-origin relative /api when served on port 443", () => {
    expect(resolveOnPremiseApiBaseUrl("drowai.lab.local", "https:", "443")).toBe("");
  });

  it("targets backend :8000 when dev UI runs on :5000", () => {
    expect(resolveOnPremiseApiBaseUrl("192.168.50.130", "http:", "5000")).toBe(
      "http://192.168.50.130:8000",
    );
  });

  it("targets backend :8000 when dev UI runs on :3000", () => {
    expect(resolveOnPremiseApiBaseUrl("10.0.0.5", "http:", "3000")).toBe("http://10.0.0.5:8000");
  });

  it("preserves non-standard UI port for split-port deployments", () => {
    expect(resolveOnPremiseApiBaseUrl("192.168.50.130", "http:", "8081")).toBe(
      "http://192.168.50.130:8081",
    );
  });
});
