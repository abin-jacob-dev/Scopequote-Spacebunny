const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const {
  accountEmail,
  assertRunId,
  registerIdentity,
} = require("../../playwright/identity");

const runIdError =
  "E2E_RUN_ID must be 1-64 lowercase ASCII letters or digits separated by single hyphens.";
const validRunIdBoundaries = ["a", "a".repeat(64)];
const invalidRunIds = [
  " round-5-run",
  "round-5-run ",
  "\tround-5-run",
  "round-5-run\t",
  "\nround-5-run",
  "round-5-run\n",
  "Round-5-run",
  "round_5_run",
  "",
  "-leading",
  "trailing-",
  "double--hyphen",
  "a".repeat(65),
];

test("generates and registers a strict run-scoped identity", () => {
  const runRoot = fs.mkdtempSync(path.join(os.tmpdir(), "scopequote-identity-"));
  const registry = path.join(runRoot, "emails.log");
  try {
    const email = accountEmail({
      runId: "round4-run",
      projectName: "chromium-desktop",
      scenario: "retry-regression",
      attempt: 0,
    });
    assert.equal(
      email,
      "e2e-account-round4-run-chromium-desktop-retry-regression-retry-1@example.test",
    );
    registerIdentity(registry, email, "round4-run");
    assert.deepEqual(fs.readFileSync(registry, "utf8").trim().split("\n"), [email]);
  } finally {
    fs.rmSync(runRoot, { force: true, recursive: true });
  }
});

test("accepts the exact 1- and 64-character run-ID boundaries", () => {
  for (const runId of validRunIdBoundaries) {
    assert.doesNotThrow(() => assertRunId(runId));
  }
});

test("rejects empty and whitespace-padded run IDs without normalization", () => {
  for (const runId of [
    "",
    " round-5-run",
    "round-5-run ",
    "\tround-5-run",
    "round-5-run\t",
    "\nround-5-run",
    "round-5-run\n",
  ]) {
    assert.throws(() => assertRunId(runId), { message: runIdError });
  }
});

test("rejects run IDs outside the strict grammar before generating identities", () => {
  for (const runId of invalidRunIds) {
    assert.throws(
      () => accountEmail({ runId, projectName: "chromium-desktop" }),
      { message: runIdError },
    );
  }
});

test("registration rejects run IDs outside the strict grammar", () => {
  const runRoot = fs.mkdtempSync(path.join(os.tmpdir(), "scopequote-registration-"));
  const registry = path.join(runRoot, "emails.log");
  try {
    for (const runId of invalidRunIds) {
      const email = `e2e-account-${runId}-chromium-desktop-signup-retry-1@example.test`;
      assert.throws(() => registerIdentity(registry, email, runId), {
        message: runIdError,
      });
    }
  } finally {
    fs.rmSync(runRoot, { force: true, recursive: true });
  }
});
