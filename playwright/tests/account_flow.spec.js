const { test, expect } = require("@playwright/test");
const { randomBytes } = require("node:crypto");
const { accountEmail, registerIdentity } = require("../identity");

function accountCredentials(projectName, attempt = 0, scenario = "signup") {
  return {
    email: accountEmail({
      runId: process.env.E2E_RUN_ID,
      projectName,
      scenario,
      attempt,
    }),
    password: `${randomBytes(18).toString("base64url")}-Aa!`,
  };
}

async function createGuestEstimate(page) {
  await page.goto("/estimator/");
  await page.getByLabel("Project description").fill(
    "A client portal with account access, secure service requests, clear project communication, and a documented release process.",
  );
  await page.getByLabel("Expected duration").fill("5");
  await page.getByLabel("Revision rounds").fill("0");
  await page.getByLabel("Complexity").selectOption("standard");
  await page.getByLabel("Urgency").selectOption("standard");
  await page.getByLabel("Experience").selectOption("intermediate");
  await page.getByLabel("Currency").selectOption("USD");
  await page.getByRole("button", { name: "Calculate estimate" }).click();
  await page.getByRole("button", { name: "Create guest quotation" }).click();
  await expect(page.getByText("Guest quotation draft created.", { exact: true })).toBeVisible();
}

test("signs up and converts a guest estimate into account history", async ({ page }) => {
  const credentials = accountCredentials(test.info().project.name);
  registerIdentity(process.env.E2E_IDENTITY_REGISTRY, credentials.email);
  await createGuestEstimate(page);

  await page.goto("/accounts/signup/");
  await page.getByLabel("Email").fill(credentials.email);
  await page.getByLabel("Password", { exact: true }).fill(credentials.password);
  await page.getByLabel("Password confirmation").fill(credentials.password);
  await page.getByRole("button", { name: "Create account" }).click();

  await expect(page.getByText("Your guest quotation is now saved to your account.")).toBeVisible();
  await page.goto("/dashboard/");
  await expect(page.getByRole("heading", { name: "Your work, in the open." })).toBeVisible();
  await expect(page.getByRole("heading", { name: "1 saved quote" })).toBeVisible();
  await page.getByRole("link", { name: "Untitled quote" }).click();
  await expect(page.getByText("A client portal with account access", { exact: false })).toBeVisible();
});

test("supports keyboard navigation to the main content", async ({ page }) => {
  await page.goto("/");
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "Skip to content" })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/#main-content$/);
});

test.describe("mobile estimator", () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test("uses a single-column estimator layout", async ({ page }) => {
    await page.goto("/estimator/");
    const columns = await page.locator(".split-layout").evaluate((element) =>
      getComputedStyle(element).gridTemplateColumns,
    );

    expect(columns.split(/\s+/)).toHaveLength(1);
    await expect(page.locator(".mobile-summary-toggle")).toBeVisible();
  });
});

test.describe("reduced motion", () => {
  test.use({ reducedMotion: "reduce" });

  test("removes nonessential motion", async ({ page }) => {
    await page.goto("/");
    const duration = await page.locator(".button").first().evaluate((element) => {
      const value = getComputedStyle(element).transitionDuration;
      return Math.max(...value.split(",").map((part) => Number.parseFloat(part) || 0));
    });

    expect(duration).toBeLessThanOrEqual(0.01);
  });
});
