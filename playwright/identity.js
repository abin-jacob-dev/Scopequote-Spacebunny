const { appendFileSync } = require("node:fs");

const RUN_ID_ERROR =
  "E2E_RUN_ID must be 1-64 lowercase ASCII letters or digits separated by single hyphens.";
const RUN_ID_MIN_LENGTH = 1;
const RUN_ID_MAX_LENGTH = 64;
const RUN_ID_PATTERN = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;

function assertRunId(runId) {
  if (
    typeof runId !== "string" ||
    runId.length < RUN_ID_MIN_LENGTH ||
    runId.length > RUN_ID_MAX_LENGTH ||
    !RUN_ID_PATTERN.test(runId)
  ) {
    throw new Error(RUN_ID_ERROR);
  }
}

function accountEmail({ runId, projectName, scenario = "signup", attempt = 0 }) {
  assertRunId(runId);
  if (!/^[a-z0-9-]+$/.test(projectName)) {
    throw new Error("E2E project name must be a safe slug.");
  }
  if (!/^[a-z0-9-]+$/.test(scenario)) {
    throw new Error("E2E identity scenario must be a safe slug.");
  }
  if (!Number.isInteger(attempt) || attempt < 0) {
    throw new Error("E2E retry attempt must be a non-negative integer.");
  }
  return `e2e-account-${runId}-${projectName}-${scenario}-retry-${attempt + 1}@example.test`;
}

function registerIdentity(registryPath, email, runId = process.env.E2E_RUN_ID) {
  assertRunId(runId);
  if (!registryPath) {
    throw new Error("E2E_IDENTITY_REGISTRY must be set by the Playwright configuration.");
  }
  const expectedPrefix = `e2e-account-${runId}-`;
  const identityPattern = new RegExp(
    `^${expectedPrefix}[a-z0-9]+(?:-[a-z0-9]+)*@example\\.test$`,
  );
  if (!identityPattern.test(email)) {
    throw new Error("E2E identity must use the guarded example.test format.");
  }
  appendFileSync(registryPath, `${email}\n`, { encoding: "utf8", mode: 0o600 });
}

module.exports = { accountEmail, assertRunId, registerIdentity };
