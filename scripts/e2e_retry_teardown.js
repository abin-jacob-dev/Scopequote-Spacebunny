const { execFileSync } = require("node:child_process");
const fs = require("node:fs");

function runPython(python, source, ...args) {
  execFileSync(python, ["-c", source, ...args], {
    cwd: process.cwd(),
    env: {
      ...process.env,
      DJANGO_SETTINGS_MODULE:
        process.env.DJANGO_SETTINGS_MODULE || "scopequote.settings",
    },
    stdio: "inherit",
  });
}

module.exports = function teardown() {
  if (process.env.E2E_SKIP_SEED === "1") {
    throw new Error("Retry verification requires E2E seeding and exact teardown.");
  }

  const python = process.env.E2E_PYTHON || "python";
  const expectedEmails = JSON.parse(process.env.E2E_RETRY_EXPECTED_EMAILS);
  const sentinelEmail = process.env.E2E_RETRY_SENTINEL_EMAIL;
  const registry = process.env.E2E_IDENTITY_REGISTRY;
  const actualEmails = fs.readFileSync(registry, "utf8").trim().split("\n");

  if (
    expectedEmails.length !== 2 ||
    expectedEmails.includes(sentinelEmail) ||
    JSON.stringify(actualEmails) !== JSON.stringify(expectedEmails)
  ) {
    throw new Error("Retry teardown requires exactly both registered retry identities.");
  }

  const serializedEmails = JSON.stringify(expectedEmails);
  try {
    runPython(
      python,
      [
        "import django",
        "import json, sys",
        "django.setup()",
        "from accounts.models import User",
        "emails = json.loads(sys.argv[1])",
        "sentinel = sys.argv[2]",
        "User._base_manager.get_or_create(email=sentinel, defaults={'is_active': True})",
        "actual = set(User._base_manager.filter(email__in=emails).values_list('email', flat=True))",
        "if actual != set(emails):",
        "    raise RuntimeError(f'retry identities missing before teardown: {sorted(set(emails) - actual)}')",
        "if not User._base_manager.filter(email=sentinel).exists():",
        "    raise RuntimeError('retry sentinel missing before teardown')",
        "print('retry teardown before: identities=2 sentinel=present')",
      ].join("\n"),
      serializedEmails,
      sentinelEmail,
    );
    runPython(python, "from scripts.e2e_seed import cleanup; cleanup()");
    runPython(
      python,
      [
        "import django",
        "import json, sys",
        "django.setup()",
        "from accounts.models import User",
        "emails = json.loads(sys.argv[1])",
        "sentinel = sys.argv[2]",
        "remaining = set(User._base_manager.filter(email__in=emails).values_list('email', flat=True))",
        "if remaining:",
        "    raise RuntimeError(f'retry identities remain after teardown: {sorted(remaining)}')",
        "if not User._base_manager.filter(email=sentinel).exists():",
        "    raise RuntimeError('retry sentinel was removed by non-exact teardown')",
        "print('retry teardown after: identities=0 sentinel=present')",
      ].join("\n"),
      serializedEmails,
      sentinelEmail,
    );
  } finally {
    runPython(
      python,
      [
        "import django",
        "import sys",
        "django.setup()",
        "from accounts.models import User",
        "User._base_manager.filter(email=sys.argv[1]).delete()",
      ].join("\n"),
      sentinelEmail,
    );
  }
};
