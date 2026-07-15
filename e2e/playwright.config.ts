/**
 * Playwright configuration for deterministic browser release journeys.
 *
 * Browser release tiers run serially against one backend/frontend stack at a
 * time. CI retains screenshots, video, and HTML only on failure. Network traces
 * stay disabled because Playwright persists unredacted authorization and cookie
 * headers in trace archives.
 */

const baseURL = process.env.BASE_URL ?? "http://localhost:5000";
const isCI = process.env.CI === "true";
const htmlReportRoot = process.env.PLAYWRIGHT_HTML_OUTPUT_DIR ?? "output/playwright-report";

export const tierSelectors = {
  prCore: "@pr-core",
  journey: "@journey",
  runtimeLocal: "@runtime-local",
} as const;

export default {
  metadata: { tierSelectors },
  testDir: "./tests",
  timeout: 30_000,
  forbidOnly: isCI,
  retries: 0,
  workers: 1,
  reporter: isCI
    ? [
        [
          "./reporters/secret-safe-artifacts.ts",
          { additionalOutputRoots: [htmlReportRoot] },
        ],
        ["line"],
        ["html", { outputFolder: htmlReportRoot, open: "never" }],
      ]
    : [["./reporters/secret-safe-artifacts.ts"], ["line"]],
  use: {
    baseURL,
    trace: "off",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: {
        browserName: "chromium",
      },
    },
    {
      name: "firefox",
      use: {
        browserName: "firefox",
      },
    },
    {
      name: "webkit",
      use: {
        browserName: "webkit",
      },
    },
  ],
  outputDir: "output/playwright",
};
