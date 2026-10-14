const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const pricingPreview = require("../../static/js/pricing-preview.js");
const fixture = JSON.parse(
  fs.readFileSync(
    path.join(__dirname, "../../pricing/fixtures/pricing_cases.json"),
    "utf8",
  ),
);

const config = {
  version: "pricing-v1",
  hoursPerDeliveryDay: "4",
  includedRevisionRounds: "2",
  additionalRevisionHours: "1.5",
  minimumMultiplier: "0.88",
  maximumMultiplier: "1.12",
  feeQuantum: "5",
  hourlyEquivalentQuantum: "1",
  complexity: {
    straightforward: { effort: "0.90", risk: "0.05" },
    standard: { effort: "1.00", risk: "0.10" },
    complex: { effort: "1.35", risk: "0.15" },
    advanced: { effort: "1.80", risk: "0.20" },
  },
  urgency: {
    standard: "1.00",
    priority: "1.18",
    rush: "1.35",
  },
  rates: {
    developing: { USD: "35", EUR: "35", GBP: "30" },
    intermediate: { USD: "60", EUR: "55", GBP: "50" },
    experienced: { USD: "90", EUR: "80", GBP: "75" },
    specialist: { USD: "125", EUR: "110", GBP: "105" },
  },
};

test("calculates every canonical pricing fixture with exact display values", () => {
  for (const testCase of fixture.cases) {
    if (testCase.expected.error) {
      assert.throws(
        () => pricingPreview.calculateFromConfig(config, testCase.input),
        { message: testCase.expected.error },
        testCase.id,
      );
      continue;
    }
    const result = pricingPreview.calculateFromConfig(config, testCase.input);
    for (const field of [
      "effective_hours",
      "minimum",
      "recommended",
      "maximum",
      "hourly_equivalent",
    ]) {
      assert.equal(result[field], testCase.expected[field], `${testCase.id}:${field}`);
    }
    assert.deepEqual(
      result.factors.map((factor) => factor.id),
      testCase.factor_ids,
    );
  }
});

test("accepts the server-rendered snake_case and factor-table configuration", () => {
  const serverConfig = {
    version: fixture.pricing_config_version,
    hours_per_delivery_day: "4",
    included_revision_rounds: "2",
    additional_revision_hours: "1.5",
    minimum_multiplier: "0.88",
    maximum_multiplier: "1.12",
    fee_quantum: "5",
    hourly_equivalent_quantum: "1",
    complexity_factors: [
      { key: "straightforward", effort: "0.90", risk: "0.05" },
      { key: "standard", effort: "1.00", risk: "0.10" },
      { key: "complex", effort: "1.35", risk: "0.15" },
      { key: "advanced", effort: "1.80", risk: "0.20" },
    ],
    urgency_factors: [
      { key: "standard", value: "1.00" },
      { key: "priority", value: "1.18" },
      { key: "rush", value: "1.35" },
    ],
    rate_factors: [
      { experience: "developing", currency: "USD", value: "35" },
      { experience: "developing", currency: "EUR", value: "35" },
      { experience: "developing", currency: "GBP", value: "30" },
      { experience: "intermediate", currency: "USD", value: "60" },
      { experience: "intermediate", currency: "EUR", value: "55" },
      { experience: "intermediate", currency: "GBP", value: "50" },
      { experience: "experienced", currency: "USD", value: "90" },
      { experience: "experienced", currency: "EUR", value: "80" },
      { experience: "experienced", currency: "GBP", value: "75" },
      { experience: "specialist", currency: "USD", value: "125" },
      { experience: "specialist", currency: "EUR", value: "110" },
      { experience: "specialist", currency: "GBP", value: "105" },
    ],
  };
  const inputs = fixture.cases[0].input;
  const expected = fixture.cases[0].expected;
  const result = pricingPreview.calculateFromConfig(serverConfig, inputs);
  assert.equal(result.recommended, expected.recommended);
  assert.equal(result.effective_hours, expected.effective_hours);
});

test("rejects unsafe or incomplete inputs instead of guessing", () => {
  assert.throws(
    () => pricingPreview.calculateFromConfig(config, { delivery_days: "1.5" }),
    /integer/i,
  );
  assert.throws(
    () => pricingPreview.calculateFromConfig(config, { delivery_days: "Infinity" }),
    /integer/i,
  );
  assert.throws(
    () => pricingPreview.calculateFromConfig(config, {
      delivery_days: "5",
      revision_rounds: "0",
      complexity: "invented",
      urgency: "standard",
      experience: "intermediate",
      currency: "USD",
    }),
    /complexity/i,
  );
  assert.throws(
    () => pricingPreview.calculateFromConfig({ ...config, feeQuantum: undefined }, {
      delivery_days: "5",
      complexity: "standard",
      revision_rounds: "0",
      urgency: "standard",
      experience: "intermediate",
      currency: "USD",
    }),
    /configuration/i,
  );
});

test("rejects pre-rounded arbitrary Number configuration values", () => {
  assert.throws(
    () => pricingPreview.calculateFromConfig({ ...config, feeQuantum: 5.1 }, {
      delivery_days: "5",
      revision_rounds: "0",
      complexity: "standard",
      urgency: "standard",
      experience: "intermediate",
      currency: "USD",
    }),
    /exact decimal/i,
  );
  assert.doesNotThrow(() => pricingPreview.calculateFromConfig({ ...config, feeQuantum: 5 }, {
    delivery_days: "5",
    revision_rounds: "0",
    complexity: "standard",
    urgency: "standard",
    experience: "intermediate",
    currency: "USD",
  }));
});

test("derives factor explanation copy from the supplied configuration", () => {
  const customConfig = {
    ...config,
    hoursPerDeliveryDay: "6",
    includedRevisionRounds: "3",
  };
  const result = pricingPreview.calculateFromConfig(customConfig, {
    delivery_days: "5",
    complexity: "standard",
    revision_rounds: "3",
    urgency: "standard",
    experience: "intermediate",
    currency: "USD",
  });

  assert.match(result.factors[0].label, /6 focused hours per delivery day/);
  assert.match(result.factors[3].label, /3 included rounds/);
});

test("publishes the pure API on the browser global and exports CommonJS", () => {
  assert.equal(typeof pricingPreview.calculateFromConfig, "function");
  assert.equal(typeof globalThis.ScopequotePricingPreview, "undefined");
  const source = fs.readFileSync(
    path.join(__dirname, "../../static/js/pricing-preview.js"),
    "utf8",
  );
  assert.doesNotMatch(source, /fetch\s*\(|XMLHttpRequest|axios/i);
  assert.match(source, /ScopequotePricingPreview/);
});
