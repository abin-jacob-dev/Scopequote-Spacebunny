const { defineConfig } = require("@playwright/test");
const { accountEmail } = require("./playwright/identity");
const baseConfig = require("./playwright.config");

const expectedEmails = [0, 1].map((attempt) =>
  accountEmail({
    runId: process.env.E2E_RUN_ID,
    projectName: "chromium-desktop",
    scenario: "retry-regression",
    attempt,
  }),
);
process.env.E2E_RETRY_EXPECTED_EMAILS = JSON.stringify(expectedEmails);
process.env.E2E_RETRY_SENTINEL_EMAIL = `e2e-account-${process.env.E2E_RUN_ID}-unregistered-sentinel@example.test`;

module.exports = defineConfig({
  ...baseConfig,
  testDir: "./playwright",
  testMatch: "retry_signup.spec.js",
  retries: 1,
  projects: [baseConfig.projects[0]],
  globalTeardown: "./scripts/e2e_retry_teardown.js",
});
