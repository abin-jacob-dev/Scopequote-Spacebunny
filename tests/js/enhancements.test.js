const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

class FakeClassList {
  constructor() {
    this.values = new Set();
  }

  add(value) {
    this.values.add(value);
  }

  remove(value) {
    this.values.delete(value);
  }

  contains(value) {
    return this.values.has(value);
  }
}

function camelCase(value) {
  return value.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
}

function matches(element, selector) {
  const trimmed = selector.trim();
  const tagMatch = trimmed.match(/^([a-z0-9-]+)?(.*)$/i);
  const tag = tagMatch[1] || "";
  if (tag && element.tagName.toLowerCase() !== tag.toLowerCase()) {
    return false;
  }
  const remainder = tagMatch[2] || "";
  const attributePattern = /\[([^\]=]+)(?:=["']?([^\]"']*)["']?)?\]/g;
  let match;
  while ((match = attributePattern.exec(remainder))) {
    const name = match[1];
    const expected = match[2];
    const actual = element.getAttribute(name);
    if (actual === null || (expected !== undefined && actual !== expected)) {
      return false;
    }
  }
  return true;
}

class FakeElement {
  constructor(tagName, attributes = {}, dataset = {}) {
    this.tagName = tagName.toUpperCase();
    this.attributes = {};
    this.dataset = { ...dataset };
    this.children = [];
    this.parentElement = null;
    this.listeners = new Map();
    this.classList = new FakeClassList();
    this.textContent = "";
    this.value = "";
    this.type = "";
    this.hidden = false;
    this.disabled = false;
    this.href = "";
    this.tabIndex = 0;
    this.focused = false;
    for (const [name, value] of Object.entries(attributes)) {
      this.setAttribute(name, value);
    }
  }

  setAttribute(name, value) {
    const text = String(value);
    this.attributes[name] = text;
    if (name.startsWith("data-")) {
      this.dataset[camelCase(name.slice(5))] = text;
    }
  }

  getAttribute(name) {
    if (Object.prototype.hasOwnProperty.call(this.attributes, name)) {
      return this.attributes[name];
    }
    if (name.startsWith("data-")) {
      const key = camelCase(name.slice(5));
      if (Object.prototype.hasOwnProperty.call(this.dataset, key)) {
        return this.dataset[key];
      }
    }
    return null;
  }

  hasAttribute(name) {
    return this.getAttribute(name) !== null;
  }

  appendChild(child) {
    child.parentElement = this;
    this.children.push(child);
    return child;
  }

  descendants() {
    return this.children.flatMap((child) => [child, ...child.descendants()]);
  }

  querySelectorAll(selector) {
    return this.descendants().filter((element) => matches(element, selector));
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }

  matches(selector) {
    return matches(this, selector);
  }

  closest(selector) {
    let current = this;
    while (current) {
      if (current.matches(selector)) {
        return current;
      }
      current = current.parentElement;
    }
    return null;
  }

  addEventListener(type, listener) {
    const listeners = this.listeners.get(type) || [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }

  dispatchEvent(event) {
    const normalized = typeof event === "string" ? { type: event } : event;
    normalized.target = this;
    normalized.currentTarget = this;
    normalized.defaultPrevented = false;
    normalized.preventDefault = () => {
      normalized.defaultPrevented = true;
    };
    normalized.stopPropagation = () => {};
    for (const listener of this.listeners.get(normalized.type) || []) {
      listener(normalized);
    }
    return !normalized.defaultPrevented;
  }

  focus() {
    this.focused = true;
  }
}

function makeDocument(elements = []) {
  return {
    readyState: "complete",
    body: new FakeElement("body"),
    querySelectorAll(selector) {
      return elements.filter((element) => matches(element, selector));
    },
    querySelector(selector) {
      return elements.find((element) => matches(element, selector)) || null;
    },
  };
}

function makeEstimatorPage(hasServerResult) {
  const form = new FakeElement("form", { "data-scopequote-pricing": "" }, {
    scopequotePricingVersion: "pricing-v1",
    scopequoteHoursPerDeliveryDay: "4",
    scopequoteIncludedRevisionRounds: "2",
    scopequoteAdditionalRevisionHours: "1.5",
    scopequoteMinimumMultiplier: "0.88",
    scopequoteMaximumMultiplier: "1.12",
    scopequoteFeeQuantum: "5",
    scopequoteHourlyEquivalentQuantum: "1",
    scopequoteHasServerResult: hasServerResult ? "true" : "false",
  });
  const fields = {};
  for (const [name, value] of Object.entries({
    delivery_days: "5",
    complexity: "standard",
    revision_rounds: "0",
    urgency: "standard",
    experience: "intermediate",
    currency: "USD",
  })) {
    fields[name] = new FakeElement("input", {}, {});
    fields[name].value = value;
  }
  form.elements = { namedItem: (name) => fields[name] || null };
  const status = new FakeElement("p", {}, { scopequotePreviewStatus: "" });
  const staleStatus = new FakeElement("p", { hidden: "hidden" }, { scopequoteStaleStatus: "" });
  const values = {};
  for (const key of ["minimum", "recommended", "maximum", "hourly_equivalent", "currency"]) {
    values[key] = new FakeElement("span", {}, { scopequotePreviewValue: key });
  }
  const preview = new FakeElement(
    "aside",
    { "data-scopequote-preview": "", "data-scopequote-preview-state": hasServerResult ? "server" : "idle" },
    { scopequotePreview: "", scopequotePreviewState: hasServerResult ? "server" : "idle" },
  );
  preview.appendChild(status);
  preview.appendChild(staleStatus);
  Object.values(values).forEach((element) => preview.appendChild(element));
  const region = new FakeElement("section", { "data-scopequote-result-region": "" });
  const factorLedger = new FakeElement("div", { "data-scopequote-factor-ledger": "" });
  const scopeSuggestions = new FakeElement("aside", { "data-scopequote-scope-suggestions": "" });
  const guestActions = new FakeElement("section", { "data-scopequote-guest-actions": "" });
  const downstream = new FakeElement("a", { "data-scopequote-downstream-action": "", href: "/old" });
  guestActions.appendChild(downstream);
  region.appendChild(factorLedger);
  region.appendChild(scopeSuggestions);
  region.appendChild(guestActions);
  preview.appendChild(region);
  form.appendChild(new FakeElement("button", { "data-scopequote-duration-preset": "3" }));
  const config = [
    new FakeElement("span", { "data-scopequote-complexity-factor": "standard:1:0.1" }),
    new FakeElement("span", { "data-scopequote-urgency-factor": "standard:1" }),
    new FakeElement("span", { "data-scopequote-rate-factor": "intermediate:USD=60" }),
  ];
  const elements = [form, preview, ...config];
  const document = makeDocument(elements);
  const window = {
    ScopequotePricingPreview: {
      calculateFromConfig() {
        return {
          minimum: "100",
          recommended: "110",
          maximum: "120",
          hourly_equivalent: "22",
        };
      },
    },
    matchMedia() {
      return { matches: false };
    },
    addEventListener() {},
  };
  return { document, window, form, preview, staleStatus, factorLedger, scopeSuggestions, guestActions, downstream };
}

function runEstimator(page) {
  const source = fs.readFileSync(
    path.join(__dirname, "../../static/js/estimator.js"),
    "utf8",
  );
  vm.runInNewContext(source, { window: page.window, document: page.document, Event: class {} });
}

test("estimator marks server results and downstream exports stale after input changes", () => {
  const page = makeEstimatorPage(true);
  runEstimator(page);
  page.form.value = "changed";
  page.form.dispatchEvent("input");

  assert.equal(page.preview.dataset.scopequotePreviewState, "stale");
  assert.equal(page.staleStatus.hidden, false);
  assert.match(page.staleStatus.textContent, /submit to refresh/i);
  assert.equal(page.factorLedger.getAttribute("data-scopequote-stale"), "true");
  assert.equal(page.scopeSuggestions.getAttribute("data-scopequote-stale"), "true");
  assert.equal(page.guestActions.getAttribute("data-scopequote-stale"), "true");
  assert.equal(page.downstream.disabled, true);
  assert.equal(page.downstream.getAttribute("aria-disabled"), "true");
});

test("estimator does not disable exports when no server result exists", () => {
  const page = makeEstimatorPage(false);
  runEstimator(page);
  page.form.dispatchEvent("input");

  assert.notEqual(page.preview.dataset.scopequotePreviewState, "stale");
  assert.equal(page.downstream.disabled, false);
});

test("pending mutation forms set busy state and restore after a response page", () => {
  const form = new FakeElement("form", { "data-scopequote-pending": "" });
  const button = new FakeElement("button", { type: "submit" });
  const status = new FakeElement("p", { hidden: "hidden", "data-scopequote-pending-status": "" });
  form.appendChild(button);
  form.appendChild(status);
  const pageListeners = new Map();
  const window = {
    addEventListener(type, listener) {
      pageListeners.set(type, listener);
    },
    dispatchEvent(type) {
      const listener = pageListeners.get(type);
      if (listener) {
        listener({ type });
      }
    },
    matchMedia() {
      return { matches: false };
    },
    confirm() {
      return true;
    },
  };
  const document = makeDocument([form]);
  const source = fs.readFileSync(
    path.join(__dirname, "../../static/js/quote-actions.js"),
    "utf8",
  );
  vm.runInNewContext(source, { window, document, navigator: {} });
  form.dispatchEvent("submit");

  assert.equal(form.getAttribute("aria-busy"), "true");
  assert.equal(button.disabled, true);
  assert.equal(status.hidden, false);
  assert.match(status.textContent, /submitting/i);

  window.dispatchEvent("pageshow");
  assert.equal(form.getAttribute("aria-busy"), "false");
  assert.equal(button.disabled, false);
  assert.equal(status.hidden, true);
});

test("pending state preserves controls that were disabled before submit", () => {
  const form = new FakeElement("form", { "data-scopequote-pending": "" });
  const button = new FakeElement("button", { type: "submit" });
  button.disabled = true;
  const status = new FakeElement("p", { hidden: "hidden", "data-scopequote-pending-status": "" });
  form.appendChild(button);
  form.appendChild(status);
  const pageListeners = new Map();
  const window = {
    addEventListener(type, listener) {
      pageListeners.set(type, listener);
    },
    dispatchEvent(type) {
      pageListeners.get(type)?.({ type });
    },
    matchMedia() {
      return { matches: false };
    },
    confirm() {
      return true;
    },
  };
  const source = fs.readFileSync(
    path.join(__dirname, "../../static/js/quote-actions.js"),
    "utf8",
  );
  vm.runInNewContext(source, {
    window,
    document: makeDocument([form]),
    navigator: {},
  });

  form.dispatchEvent("submit");
  window.dispatchEvent("pageshow");

  assert.equal(button.disabled, true);
});
