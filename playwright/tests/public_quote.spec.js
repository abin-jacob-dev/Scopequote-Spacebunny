const { test, expect } = require("@playwright/test");
const { execFileSync } = require("node:child_process");

const ownerEmail = process.env.E2E_USER_EMAIL || "e2e-owner@example.test";
const ownerPassword = process.env.E2E_USER_PASSWORD;
if (!ownerPassword) {
  throw new Error("E2E_USER_PASSWORD must be set by the Playwright configuration.");
}

function resetFixture() {
  if (process.env.E2E_SKIP_SEED === "1") {
    return;
  }
  execFileSync(
    process.env.E2E_PYTHON || "python",
    ["-c", "from scripts.e2e_seed import seed; seed()"],
    { cwd: process.cwd(), env: process.env, stdio: "inherit" },
  );
}

test.beforeEach(resetFixture);

async function signIn(page) {
  await page.goto("/accounts/login/");
  await page.getByLabel("Email").fill(ownerEmail);
  await page.getByLabel("Password", { exact: true }).fill(ownerPassword);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Your work, in the open." })).toBeVisible();
}

async function publishFixture(page) {
  await signIn(page);
  await page.goto("/quotes/");
  const draftRow = page
    .getByRole("row")
    .filter({ hasText: /publication fixture/i })
    .filter({ hasText: "Draft" })
    .first();
  await expect(draftRow).toBeVisible();
  await draftRow.getByRole("link", { name: /publication fixture/i }).click();
  await page.getByRole("link", { name: "Review and publish" }).click();
  await page.getByRole("button", { name: "Publish immutable quotation" }).click();
  await expect(page).toHaveURL(/\/quotes\/[0-9a-f-]+\/links\/$/);
  return page;
}

async function openPrivateLink(page, href) {
  const clientContext = await page.context().browser().newContext();
  const clientPage = await clientContext.newPage();
  await clientPage.goto(new URL(href, page.url()).toString());
  await clientPage.getByRole("button", { name: "Open secure quotation" }).click();
  await expect(clientPage.getByRole("heading", { name: /publication fixture/i })).toBeVisible();
  return { clientContext, clientPage };
}

test("publishes a quote, creates a private link, and accepts it as a client", async ({ page }) => {
  await publishFixture(page);
  await page.getByRole("button", { name: "Create private link" }).click();
  const privateLink = page.locator("a.private-link").first();
  await expect(privateLink).toBeVisible();
  const privateHref = await privateLink.getAttribute("href");
  const { clientContext, clientPage } = await openPrivateLink(page, privateHref);
  await clientPage.getByLabel("Accept quotation").check();
  await clientPage.getByRole("button", { name: "Send response" }).click();
  await expect(clientPage.getByText("Your response has been recorded.")).toBeVisible();
  await clientContext.close();
});

test("records a request-changes response without JavaScript", async ({ browser }) => {
  const context = await browser.newContext({ javaScriptEnabled: false });
  const page = await context.newPage();
  await publishFixture(page);
  const privateLink = page.locator("a.private-link").first();
  await expect(privateLink).toBeVisible();
  const href = await privateLink.getAttribute("href");
  const clientContext = await browser.newContext({ javaScriptEnabled: false });
  const clientPage = await clientContext.newPage();
  await clientPage.goto(new URL(href, page.url()).toString());
  await clientPage.getByRole("button", { name: "Open secure quotation" }).click();
  await clientPage.getByLabel("Request changes").check();
  await clientPage.getByLabel("Note").fill("Please adjust the scope.");
  await clientPage.getByRole("button", { name: "Send response" }).click();
  await expect(clientPage.getByText("Your response has been recorded.")).toBeVisible();
  await clientContext.close();
  await context.close();
});

test("reissues and revokes a private link without retaining an old capability", async ({ page }) => {
  await publishFixture(page);
  const firstLink = page.locator("a.private-link").first();
  const firstHref = await firstLink.getAttribute("href");
  await page.getByRole("button", { name: "Reissue link" }).first().click();
  const links = page.locator("a.private-link");
  await expect(links).toHaveCount(2);
  const hrefs = await links.evaluateAll((items) => items.map((item) => item.href));
  const replacementHref = hrefs.find((href) => href !== firstHref);
  expect(replacementHref).toBeTruthy();
  const oldContext = await page.context().browser().newContext();
  const oldPage = await oldContext.newPage();
  const oldResponse = await oldPage.goto(new URL(firstHref, page.url()).toString());
  expect(oldResponse.status()).toBe(404);
  await oldContext.close();
  const replacementRow = page.locator("li").filter({ hasText: replacementHref });
  page.once("dialog", (dialog) => dialog.accept());
  await replacementRow.getByRole("button", { name: "Revoke link" }).click();
  const revokedContext = await page.context().browser().newContext();
  const revokedPage = await revokedContext.newPage();
  const revokedResponse = await revokedPage.goto(new URL(replacementHref, page.url()).toString());
  expect(revokedResponse.status()).toBe(404);
  await revokedContext.close();
});

test("supports a true 320px viewport without horizontal overflow", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 800 });
  await page.goto("/estimator/");
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(1);
});

test("expiry makes a private link unavailable", async ({ page }) => {
  await publishFixture(page);
  const privateLink = page.locator("a.private-link").first();
  const href = await privateLink.getAttribute("href");
  execFileSync(
    process.env.E2E_PYTHON || "python",
    ["-c", "from scripts.e2e_seed import expire_fixture_links; expire_fixture_links()"],
    { cwd: process.cwd(), env: process.env, stdio: "inherit" },
  );
  const expiredContext = await page.context().browser().newContext();
  const expiredPage = await expiredContext.newPage();
  const response = await expiredPage.goto(new URL(href, page.url()).toString());
  expect(response.status()).toBe(404);
  await expiredContext.close();
});
