const { execFileSync } = require("node:child_process");
const path = require("node:path");

module.exports = function teardown() {
  const python = process.env.E2E_PYTHON || "python";
  if (process.env.E2E_SKIP_SEED === "1") {
    return;
  }
  execFileSync(
    python,
    ["-c", "from scripts.e2e_seed import cleanup; cleanup()"],
    {
      cwd: path.resolve(__dirname, ".."),
      env: process.env,
      stdio: "inherit",
    },
  );
};
