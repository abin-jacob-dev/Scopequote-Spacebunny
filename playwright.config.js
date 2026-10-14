const { defineConfig, devices } = require("@playwright/test");
const { randomBytes } = require("node:crypto");
const {
  existsSync,
  lstatSync,
  mkdirSync,
  realpathSync,
  writeFileSync,
} = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { assertRunId } = require("./playwright/identity");

const localPython = path.join(process.cwd(), ".venv/bin/python");
const python =
  process.env.E2E_PYTHON || (existsSync(localPython) ? localPython : "python");
process.env.E2E_PYTHON = python;
const baseURL = process.env.E2E_BASE_URL || "http://127.0.0.1:8000";
const basePort = new URL(baseURL).port || "8000";
const e2eDatabaseName = process.env.E2E_DATABASE_NAME?.trim();
const e2eRunId =
  process.env.E2E_RUN_ID ?? randomBytes(12).toString("hex");
assertRunId(e2eRunId);

if (!e2eDatabaseName) {
  throw new Error("E2E_DATABASE_NAME must name a dedicated PostgreSQL database.");
}
if (["postgres", "template0", "template1"].includes(e2eDatabaseName.toLowerCase())) {
  throw new Error("E2E_DATABASE_NAME must identify a dedicated PostgreSQL database.");
}
if (process.env.E2E_ALLOW_DESTRUCTIVE_RESET !== "1") {
  throw new Error("E2E_ALLOW_DESTRUCTIVE_RESET=1 is required for E2E tests.");
}
if (process.env.DATABASE_URL) {
  throw new Error("DATABASE_URL is not supported for E2E tests.");
}
if (process.env.SCOPEQUOTE_ENV && process.env.SCOPEQUOTE_ENV !== "test") {
  throw new Error("E2E tests require SCOPEQUOTE_ENV=test.");
}
if (
  process.env.E2E_ALLOW_REUSE_SERVER &&
  !new Set(["0", "1"]).has(process.env.E2E_ALLOW_REUSE_SERVER)
) {
  throw new Error("E2E_ALLOW_REUSE_SERVER must be 0 or 1.");
}

const projectRoot = realpathSync(path.resolve(process.cwd()));
const tempBase = realpathSync(path.resolve(os.tmpdir()));
const filesystemRoot = path.parse(tempBase).root;
function isWithin(candidate, parent) {
  const relative = path.relative(parent, candidate);
  return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
}
function assertSafeExistingPath(target, label) {
  if (!existsSync(target)) {
    return;
  }
  const absolute = path.resolve(target);
  if (lstatSync(absolute).isSymbolicLink() || realpathSync(absolute) !== absolute) {
    throw new Error(`${label} must not traverse a symlink.`);
  }
}
if (
  tempBase === filesystemRoot ||
  isWithin(projectRoot, tempBase) ||
  isWithin(tempBase, projectRoot)
) {
  throw new Error("E2E temporary roots must be outside the filesystem and project roots.");
}

const e2eBase = path.join(tempBase, "scopequote-e2e");
assertSafeExistingPath(e2eBase, "E2E temporary base");
mkdirSync(e2eBase, { recursive: true });
assertSafeExistingPath(e2eBase, "E2E temporary base");
const runRoot = path.join(e2eBase, e2eRunId);
assertSafeExistingPath(runRoot, "E2E run root");
function guardedPath(name, leaf) {
  const defaultPath = path.join(runRoot, leaf);
  const configuredPath = process.env[name];
  if (configuredPath && path.resolve(configuredPath) !== path.resolve(defaultPath)) {
    throw new Error(`${name} must equal the guarded E2E run path.`);
  }
  assertSafeExistingPath(defaultPath, name);
  return path.resolve(defaultPath);
}

const mediaRoot = guardedPath("E2E_MEDIA_ROOT", "media");
const staticRoot = guardedPath("E2E_STATIC_ROOT", "static");
const outputDir = guardedPath("E2E_OUTPUT_DIR", "playwright");
const identityRoot = guardedPath("E2E_IDENTITY_ROOT", "identities");
const identityRegistry = path.join(identityRoot, "emails.log");
const runRootPath = path.resolve(runRoot);
for (const root of [mediaRoot, staticRoot, outputDir, identityRoot]) {
  assertSafeExistingPath(root, path.basename(root));
  mkdirSync(root, { recursive: true });
  assertSafeExistingPath(root, path.basename(root));
  writeFileSync(path.join(root, ".scopequote-e2e-root"), `${e2eRunId}\n`);
}
assertSafeExistingPath(identityRegistry, "E2E identity registry");
if (!existsSync(identityRegistry)) {
  writeFileSync(identityRegistry, "", { encoding: "utf8", mode: 0o600 });
}

const ownerEmail = process.env.E2E_USER_EMAIL || "e2e-owner@example.test";
const defaults = {
  SCOPEQUOTE_ENV: "test",
  DEBUG: "false",
  SECRET_KEY: randomBytes(32).toString("base64url"),
  SHARE_TOKEN_ENCRYPTION_KEY: randomBytes(32).toString("base64"),
  SHARE_TOKEN_ENCRYPTION_KEY_VERSION: "v1",
  ALLOWED_HOSTS: "127.0.0.1,localhost",
  CSRF_TRUSTED_ORIGINS: new URL(baseURL).origin,
  POSTGRES_DB: e2eDatabaseName,
  POSTGRES_USER: process.env.POSTGRES_USER || "postgres",
  POSTGRES_PASSWORD: process.env.POSTGRES_PASSWORD || "",
  POSTGRES_HOST: process.env.POSTGRES_HOST || "127.0.0.1",
  POSTGRES_PORT: process.env.POSTGRES_PORT || "5432",
  MEDIA_ROOT: mediaRoot,
  STATIC_ROOT: staticRoot,
  EMAIL_BACKEND: "django.core.mail.backends.locmem.EmailBackend",
  USE_WHITENOISE: "true",
  SESSION_COOKIE_SECURE: "false",
  CSRF_COOKIE_SECURE: "false",
  CSRF_COOKIE_HTTPONLY: "false",
  TRUST_PROXY_HEADERS: "false",
  SECURE_PROXY_SSL_HEADER: "",
  SECURE_SSL_REDIRECT: "false",
  SECURE_HSTS_SECONDS: "0",
  SECURE_HSTS_INCLUDE_SUBDOMAINS: "false",
  SECURE_HSTS_PRELOAD: "false",
  E2E_DATABASE_NAME: e2eDatabaseName,
  E2E_ALLOW_DESTRUCTIVE_RESET: "1",
  E2E_ALLOW_REUSE_SERVER: process.env.E2E_ALLOW_REUSE_SERVER || "0",
  E2E_RUN_ID: e2eRunId,
  E2E_RUN_ROOT: runRootPath,
  E2E_MEDIA_ROOT: mediaRoot,
  E2E_STATIC_ROOT: staticRoot,
  E2E_OUTPUT_DIR: outputDir,
  E2E_IDENTITY_ROOT: identityRoot,
  E2E_IDENTITY_REGISTRY: identityRegistry,
  E2E_USER_EMAIL: ownerEmail,
  E2E_USER_PASSWORD: `E2E-${randomBytes(18).toString("base64url")}-Aa!`,
  E2E_ALLOWED_USER_EMAILS: ownerEmail,
  E2E_QUOTE_TITLE: "E2E publication fixture",
  E2E_CLIENT_IPS: "127.0.0.1,::1",
};
for (const [name, value] of Object.entries(defaults)) {
  if (process.env[name] === undefined) {
    process.env[name] = value;
  }
}
process.env.POSTGRES_DB = e2eDatabaseName;
process.env.E2E_DATABASE_NAME = e2eDatabaseName;
process.env.E2E_ALLOW_DESTRUCTIVE_RESET = "1";
process.env.E2E_RUN_ID = e2eRunId;
process.env.E2E_RUN_ROOT = runRootPath;
process.env.E2E_MEDIA_ROOT = mediaRoot;
process.env.E2E_STATIC_ROOT = staticRoot;
process.env.E2E_OUTPUT_DIR = outputDir;
process.env.E2E_IDENTITY_ROOT = identityRoot;
process.env.E2E_IDENTITY_REGISTRY = identityRegistry;
process.env.E2E_USER_EMAIL = ownerEmail;
process.env.E2E_QUOTE_TITLE = "E2E publication fixture";
process.env.E2E_CLIENT_IPS = "127.0.0.1,::1";
process.env.E2E_ALLOWED_USER_EMAILS = ownerEmail;
process.env.MEDIA_ROOT = mediaRoot;
process.env.STATIC_ROOT = staticRoot;

const serverCommand =
  process.env.E2E_WEB_SERVER_COMMAND ||
  `${python} manage.py migrate --noinput && ${python} manage.py collectstatic --noinput && ${python} -m gunicorn.app.wsgiapp scopequote.wsgi:application --bind 127.0.0.1:${basePort}`;

const browserChannelName =
  process.env.E2E_BROWSER_CHANNEL ||
  (existsSync("/usr/bin/google-chrome") ? "chrome" : "");
const browserChannel = browserChannelName
  ? { channel: browserChannelName }
  : {};

const webServerConfig = {
  command: serverCommand,
  url: `${baseURL.replace(/\/$/, "")}/health/`,
  reuseExistingServer: false,
  timeout: 120000,
};
if (process.env.E2E_ALLOW_REUSE_SERVER === "1") {
  webServerConfig.reuseExistingServer = true;
}

module.exports = defineConfig({
  testDir: "./playwright/tests",
  outputDir,
  fullyParallel: false,
  workers: 1,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  timeout: 30000,
  expect: { timeout: 5000 },
  reporter: [["list"]],
  globalSetup: "./scripts/e2e_setup.js",
  globalTeardown: "./scripts/e2e_teardown.js",
  use: {
    baseURL,
    trace: "off",
    screenshot: "off",
    video: "off",
  },
  projects: [
    {
      name: "chromium-desktop",
      use: { ...devices["Desktop Chrome"], ...browserChannel },
    },
    {
      name: "chromium-mobile",
      use: { ...devices["Pixel 5"], ...browserChannel },
    },
    {
      name: "chromium-reduced-motion",
      use: {
        ...devices["Desktop Chrome"],
        ...browserChannel,
        reducedMotion: "reduce",
      },
    },
  ],
  webServer:
    process.env.E2E_SKIP_WEBSERVER === "1" ? undefined : webServerConfig,
});
