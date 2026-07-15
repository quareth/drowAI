/** Isolated Playwright configuration for intentional artifact-policy probes. */

const htmlReportRoot = process.env.PLAYWRIGHT_HTML_OUTPUT_DIR ?? "../output/playwright-report";

export default {
  testDir: ".",
  timeout: 30_000,
  retries: 0,
  workers: 1,
  reporter: [
    ["../reporters/secret-safe-artifacts.ts", { additionalOutputRoots: [htmlReportRoot] }],
    ["line"],
    ["html", { outputFolder: htmlReportRoot, open: "never" }],
  ],
  use: {
    trace: "off",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
  outputDir: "../output/playwright-probes",
};
