/** Probes retained Playwright artifacts for credential leakage after an auth failure. */

import { expect, test } from "@playwright/test";

const enabled = process.env.E2E_ARTIFACT_POLICY_PROBE === "true";

test("authenticated failure artifact probe", async ({ context, page }) => {
  test.skip(!enabled, "Run only from the secret-safe artifact contract.");

  const probeUrl = requiredEnvironment("E2E_ARTIFACT_PROBE_URL");
  const token = requiredEnvironment("E2E_ARTIFACT_PROBE_TOKEN");
  const cookie = requiredEnvironment("E2E_ARTIFACT_PROBE_COOKIE");
  const password = requiredEnvironment("E2E_ARTIFACT_PROBE_PASSWORD");
  const origin = new URL(probeUrl).origin;

  await context.addCookies([{ name: "session", value: cookie, url: origin }]);
  await page.goto(probeUrl);
  await page.evaluate(
    ({ accessToken, secretPassword }) => {
      window.localStorage.setItem("access_token", accessToken);
      const input = document.createElement("input");
      input.type = "password";
      input.value = secretPassword;
      document.body.append(input);
    },
    { accessToken: token, secretPassword: password },
  );
  await page.request.get(probeUrl, {
    headers: { Authorization: `Bearer ${token}` },
  });

  expect(
    false,
    `password: ${password}; Authorization: Bearer ${token}; cookie: ${cookie}`,
  ).toBe(true);
});

function requiredEnvironment(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(`Missing required artifact probe environment: ${name}`);
  }
  return value;
}
