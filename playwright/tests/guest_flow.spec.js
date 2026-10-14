const { test, expect } = require("@playwright/test");
const { readFile } = require("node:fs/promises");

async function completeEstimate(page) {
  await page.goto("/estimator/");
  await page.getByLabel("Project description").fill(
    "A responsive marketing website with an editorial layout, content migration, analytics, and a tested handoff for the launch team.",
  );
  await page.getByLabel("Expected duration").fill("5");
  await page.getByLabel("Revision rounds").fill("1");
  await page.getByLabel("Complexity").selectOption("standard");
  await page.getByLabel("Urgency").selectOption("standard");
  await page.getByLabel("Experience").selectOption("intermediate");
  await page.getByLabel("Currency").selectOption("USD");
  await page.getByRole("button", { name: "Calculate estimate" }).click();
  await expect(
    page.locator('[data-scopequote-preview-value="recommended"]'),
  ).not.toHaveText("—");
}

test("calculates a guest estimate and downloads its PDF", async ({ page }) => {
  await completeEstimate(page);
  await page.getByRole("button", { name: "Create guest quotation" }).click();
  await expect(page.getByText("Guest quotation draft created.", { exact: true })).toBeVisible();

  const downloadStarted = page.waitForEvent("download");
  await page.getByRole("link", { name: "Download guest PDF" }).click();
  const download = await downloadStarted;
  const path = await download.path();
  const bytes = await readFile(path);

  expect(download.suggestedFilename()).toMatch(/\.pdf$/);
  expect(bytes.subarray(0, 5).toString()).toBe("%PDF-");
});
