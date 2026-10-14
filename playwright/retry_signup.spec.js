const { readFileSync } = require("node:fs");
const { test, expect } = require("@playwright/test");
const { accountEmail, registerIdentity } = require("./identity");

const password = "Retry verification password 47!A";

function registeredIdentities() {
  const registry = process.env.E2E_IDENTITY_REGISTRY;
  if (!registry) {
    throw new Error("E2E_IDENTITY_REGISTRY must be set by the retry configuration.");
  }
  return readFileSync(registry, "utf8").trim().split("\n");
}

test("retries account signup with a fresh run-scoped identity", async ({ page }) => {
  const attempt = test.info().retry;
  const expectedEmails = JSON.parse(process.env.E2E_RETRY_EXPECTED_EMAILS);
  expect(attempt).toBeLessThanOrEqual(1);

  const email = accountEmail({
    runId: process.env.E2E_RUN_ID,
    projectName: test.info().project.name,
    scenario: "retry-regression",
    attempt,
  });
  expect(email).toBe(expectedEmails[attempt]);
  registerIdentity(process.env.E2E_IDENTITY_REGISTRY, email);

  await page.goto("/accounts/signup/");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByLabel("Password confirmation").fill(password);
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page).toHaveURL(/\/accounts\/verify\/$/);

  if (attempt === 0) {
    console.log(`retry attempt 0 failed after account creation: ${email}`);
    throw new Error("intentional retry attempt 0 failure after account creation");
  }

  expect(registeredIdentities()).toEqual(expectedEmails);
  console.log(`retry attempt 1 passed after fresh signup: ${email}`);
});
