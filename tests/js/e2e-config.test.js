const test = require("node:test");
const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const projectRoot = path.resolve(__dirname, "../..");
const runRootName = `config-guard-${process.pid}`;

function runConfig(overrides = {}) {
  const runId = `${runRootName}-${Math.random().toString(16).slice(2)}`;
  const childEnv = { ...process.env };
  for (const name of [
    "MEDIA_ROOT",
    "STATIC_ROOT",
    "E2E_MEDIA_ROOT",
    "E2E_STATIC_ROOT",
    "E2E_OUTPUT_DIR",
    "E2E_IDENTITY_ROOT",
    "E2E_IDENTITY_REGISTRY",
    "E2E_RUN_ROOT",
  ]) {
    delete childEnv[name];
  }
  Object.assign(childEnv, {
    E2E_DATABASE_NAME: "scopequote_e2e",
    E2E_ALLOW_DESTRUCTIVE_RESET: "1",
    E2E_RUN_ID: runId,
    E2E_PYTHON: path.join(projectRoot, ".venv/bin/python"),
    E2E_SKIP_WEBSERVER: "1",
  }, overrides);
  const effectiveRunId = Object.hasOwn(overrides, "E2E_RUN_ID")
    ? overrides.E2E_RUN_ID
    : runId;
  const script = [
    `require(${JSON.stringify(path.join(projectRoot, "playwright.config.js"))});`,
    "console.log(JSON.stringify({",
    "  mediaRoot: process.env.MEDIA_ROOT,",
    "  staticRoot: process.env.STATIC_ROOT,",
    "  e2eMediaRoot: process.env.E2E_MEDIA_ROOT,",
    "  e2eStaticRoot: process.env.E2E_STATIC_ROOT,",
    "}));",
  ].join("\n");
  const result = spawnSync(process.execPath, ["-e", script], {
    cwd: projectRoot,
    env: childEnv,
    encoding: "utf8",
  });
  const runRoot = path.join(
    os.tmpdir(),
    "scopequote-e2e",
    effectiveRunId || "invalid-empty-run-id",
  );
  return { result, runRoot };
}

function removeRunRoot(runRoot) {
  fs.rmSync(runRoot, { force: true, recursive: true });
}

test("overrides ambient media and static roots with guarded temporary roots", () => {
  const ambientMedia = path.join(projectRoot, "media");
  const ambientStatic = path.join(projectRoot, "staticfiles");
  const { result, runRoot } = runConfig({
    MEDIA_ROOT: ambientMedia,
    STATIC_ROOT: ambientStatic,
  });
  try {
    assert.equal(result.status, 0, result.stderr);
    const output = JSON.parse(result.stdout.trim());
    const expectedRoot = path.join(os.tmpdir(), "scopequote-e2e");
    assert.equal(path.dirname(output.mediaRoot), path.join(expectedRoot, path.basename(runRoot)));
    assert.equal(path.dirname(output.staticRoot), path.join(expectedRoot, path.basename(runRoot)));
    assert.equal(output.e2eMediaRoot, output.mediaRoot);
    assert.equal(output.e2eStaticRoot, output.staticRoot);
    assert.notEqual(output.mediaRoot, ambientMedia);
    assert.notEqual(output.staticRoot, ambientStatic);
  } finally {
    removeRunRoot(runRoot);
  }
});

test("rejects an empty run ID with the strict grammar error", () => {
  const { result } = runConfig({ E2E_RUN_ID: "" });
  assert.notEqual(result.status, 0);
  assert.match(
    result.stderr,
    /E2E_RUN_ID must be 1-64 lowercase ASCII letters or digits separated by single hyphens\./,
  );
});


test("refuses repository and root filesystem E2E roots", () => {
  for (const unsafeRoot of [projectRoot, path.parse(projectRoot).root]) {
    const { result, runRoot } = runConfig({ E2E_STATIC_ROOT: unsafeRoot });
    try {
      assert.notEqual(result.status, 0);
      assert.match(result.stderr, /E2E_STATIC_ROOT/);
    } finally {
      removeRunRoot(runRoot);
    }
  }
});

test("rejects run IDs outside the strict grammar before creating storage", () => {
  for (const invalidRunId of [
    "",
    " round-5-run",
    "round-5-run ",
    "\tround-5-run",
    "round-5-run\t",
    "\nround-5-run",
    "round-5-run\n",
    "Round-5-run",
    "round_5_run",
    "-leading",
    "trailing-",
    "double--hyphen",
    "a".repeat(65),
  ]) {
    const { result, runRoot } = runConfig({ E2E_RUN_ID: invalidRunId });
    try {
      assert.notEqual(result.status, 0);
      assert.match(
        result.stderr,
        /E2E_RUN_ID must be 1-64 lowercase ASCII letters or digits separated by single hyphens\./,
      );
      assert.equal(fs.existsSync(runRoot), false);
    } finally {
      removeRunRoot(runRoot);
    }
  }
});

test("accepts the exact 1- and 64-character run-ID boundaries", () => {
  for (const runId of ["a", "a".repeat(64)]) {
    const { result, runRoot } = runConfig({ E2E_RUN_ID: runId });
    try {
      assert.equal(result.status, 0, result.stderr);
      assert.equal(fs.existsSync(runRoot), true);
    } finally {
      removeRunRoot(runRoot);
    }
  }
});
