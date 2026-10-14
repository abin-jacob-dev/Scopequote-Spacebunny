(function (root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  if (typeof window !== "undefined") {
    window.ScopequotePricingPreview = api;
  }
})(typeof window !== "undefined" ? window : this, function () {
  "use strict";

  function gcd(left, right) {
    let a = left < 0n ? -left : left;
    let b = right < 0n ? -right : right;
    while (b !== 0n) {
      const next = a % b;
      a = b;
      b = next;
    }
    return a;
  }

  function rational(numerator, denominator) {
    if (denominator === 0n) {
      throw new Error("A pricing denominator cannot be zero.");
    }
    let n = numerator;
    let d = denominator;
    if (d < 0n) {
      n = -n;
      d = -d;
    }
    const divisor = gcd(n, d) || 1n;
    return { n: n / divisor, d: d / divisor };
  }

  function fromDecimal(value, fieldName) {
    if (typeof value === "number" && !Number.isSafeInteger(value)) {
      throw new Error(`${fieldName} must be an exact decimal string or safe integer.`);
    }
    if (
      typeof value !== "string" &&
      typeof value !== "bigint" &&
      typeof value !== "number"
    ) {
      throw new Error(`${fieldName} must be an exact decimal.`);
    }
    const text = String(value).trim();
    if (!/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$/.test(text)) {
      throw new Error(`${fieldName} must be an exact decimal.`);
    }
    const sign = text.startsWith("-") ? -1n : 1n;
    const unsigned = text.replace(/^[+-]/, "");
    const parts = unsigned.split(".");
    const whole = parts[0] || "0";
    const fraction = parts[1] || "";
    const digits = BigInt(whole + fraction || "0");
    const denominator = 10n ** BigInt(fraction.length);
    return rational(digits * sign, denominator);
  }

  function fromInteger(value, fieldName) {
    if (typeof value === "bigint") {
      return value;
    }
    if (typeof value === "number" && !Number.isSafeInteger(value)) {
      throw new Error(`${fieldName} must be an integer.`);
    }
    if (typeof value !== "string" && typeof value !== "number") {
      throw new Error(`${fieldName} must be an integer.`);
    }
    const text = String(value).trim();
    if (!/^[+-]?\d+$/.test(text)) {
      throw new Error(`${fieldName} must be an integer.`);
    }
    return BigInt(text);
  }

  function add(left, right) {
    return rational(left.n * right.d + right.n * left.d, left.d * right.d);
  }

  function subtract(left, right) {
    return rational(left.n * right.d - right.n * left.d, left.d * right.d);
  }

  function multiply(left, right) {
    return rational(left.n * right.n, left.d * right.d);
  }

  function divide(left, right) {
    return rational(left.n * right.d, left.d * right.n);
  }

  function compare(left, right) {
    const difference = left.n * right.d - right.n * left.d;
    return difference < 0n ? -1 : difference > 0n ? 1 : 0;
  }

  function max(left, right) {
    return compare(left, right) >= 0 ? left : right;
  }

  function positive(value) {
    return value.n > 0n;
  }

  function roundHalfUp(value) {
    const sign = value.n < 0n ? -1n : 1n;
    const numerator = value.n < 0n ? -value.n : value.n;
    const whole = numerator / value.d;
    const remainder = numerator % value.d;
    return whole + (remainder * 2n >= value.d ? 1n : 0n) * sign;
  }

  function quantize(value, step) {
    if (!positive(step)) {
      throw new Error("A pricing quantum must be positive.");
    }
    return rational(roundHalfUp(divide(value, step)) * step.n, step.d);
  }

  function formatRational(value, decimals) {
    const scale = 10n ** BigInt(decimals);
    const rounded = roundHalfUp(multiply(value, rational(scale, 1n)));
    const sign = rounded < 0n ? "-" : "";
    const absolute = rounded < 0n ? -rounded : rounded;
    const text = absolute.toString();
    if (decimals === 0) {
      return `${sign}${text}`;
    }
    const padded = text.padStart(decimals + 1, "0");
    return `${sign}${padded.slice(0, -decimals)}.${padded.slice(-decimals)}`;
  }

  function formatTrimmed(value) {
    let denominator = value.d;
    let power = 1n;
    let places = 0;
    while (denominator % power !== 0n && places < 64) {
      power *= 10n;
      places += 1;
    }
    const text = formatRational(value, places);
    return text.replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, "");
  }

  function firstValue(source, camelName, snakeName) {
    if (source && Object.prototype.hasOwnProperty.call(source, camelName)) {
      return source[camelName];
    }
    if (source && Object.prototype.hasOwnProperty.call(source, snakeName)) {
      return source[snakeName];
    }
    return undefined;
  }

  function tableValue(table, key, aliases, fieldName) {
    const value = firstValue(table, key, key);
    if (value === undefined) {
      for (const alias of aliases || []) {
        const candidate = table && table[alias];
        if (candidate !== undefined) {
          return candidate;
        }
      }
      throw new Error(`${fieldName} is missing from the pricing configuration.`);
    }
    return value;
  }

  function normalizeFactorTables(config) {
    let complexityTable = config.complexity;
    if (complexityTable === undefined) {
      complexityTable = config.complexity_factors || config.complexityFactors;
    }
    if (complexityTable === undefined) {
      const efforts = config.complexity_multipliers || config.complexityMultipliers;
      const risks = config.complexity_risk_buffers || config.complexityRiskBuffers;
      if (efforts) {
        complexityTable = {};
        for (const key of Object.keys(efforts)) {
          complexityTable[key] = {
            effort: efforts[key],
            risk: risks ? risks[key] : undefined,
          };
        }
      }
    }
    if (Array.isArray(complexityTable)) {
      const converted = {};
      for (const factor of complexityTable) {
        if (!factor || !factor.key) {
          throw new Error("A complexity factor is missing its key.");
        }
        converted[factor.key] = {
          effort: firstValue(factor, "effort", "effort") ?? firstValue(factor, "effortMultiplier", "effort_multiplier"),
          risk: firstValue(factor, "risk", "risk") ?? firstValue(factor,
            "riskBuffer",
            "risk_buffer",
          ),
        };
      }
      complexityTable = converted;
    }
    if (!complexityTable || typeof complexityTable !== "object") {
      throw new Error("Complexity configuration is missing.");
    }

    let urgencyTable = config.urgency;
    if (urgencyTable === undefined) {
      urgencyTable = config.urgency_factors || config.urgencyFactors;
    }
    if (urgencyTable === undefined) {
      urgencyTable = config.urgency_multipliers || config.urgencyMultipliers;
    }
    if (Array.isArray(urgencyTable)) {
      const converted = {};
      for (const factor of urgencyTable) {
        if (!factor || !factor.key) {
          throw new Error("An urgency factor is missing its key.");
        }
        converted[factor.key] = firstValue(factor, "value", "value") ?? firstValue(
          factor,
          "multiplier",
          "multiplier",
        );
      }
      urgencyTable = converted;
    }
    if (!urgencyTable || typeof urgencyTable !== "object") {
      throw new Error("Urgency configuration is missing.");
    }

    let rateTable = config.rates;
    if (rateTable === undefined) {
      rateTable = config.experience_rates || config.experienceRates || config.rate_factors || config.rateFactors;
    }
    if (Array.isArray(rateTable)) {
      const converted = {};
      for (const factor of rateTable) {
        if (!factor || !factor.experience || !factor.currency) {
          throw new Error("A rate factor is missing its experience or currency.");
        }
        if (!converted[factor.experience]) {
          converted[factor.experience] = {};
        }
        converted[factor.experience][factor.currency] = firstValue(
          factor,
          "value",
          "value",
        ) ?? firstValue(factor, "rate", "rate");
      }
      rateTable = converted;
    }
    if (!rateTable || typeof rateTable !== "object") {
      throw new Error("Rate configuration is missing.");
    }
    return { complexityTable, urgencyTable, rateTable };
  }

  function normalizeConfig(config) {
    if (typeof config === "string") {
      try {
        config = JSON.parse(config);
      } catch (error) {
        throw new Error("A pricing configuration is required.");
      }
    }
    if (!config || typeof config !== "object") {
      throw new Error("A pricing configuration is required.");
    }
    const tables = normalizeFactorTables(config);
    return {
      version: String(firstValue(config, "version", "pricing_config_version") || "pricing-v1"),
      hoursPerDeliveryDay: fromDecimal(
        firstValue(config, "hoursPerDeliveryDay", "hours_per_delivery_day"),
        "configuration hoursPerDeliveryDay",
      ),
      includedRevisionRounds: rational(
        fromInteger(
          firstValue(config, "includedRevisionRounds", "included_revision_rounds"),
          "configuration includedRevisionRounds",
        ),
        1n,
      ),
      additionalRevisionHours: fromDecimal(
        firstValue(config, "additionalRevisionHours", "additional_revision_hours"),
        "configuration additionalRevisionHours",
      ),
      minimumMultiplier: fromDecimal(
        firstValue(config, "minimumMultiplier", "minimum_multiplier"),
        "configuration minimumMultiplier",
      ),
      maximumMultiplier: fromDecimal(
        firstValue(config, "maximumMultiplier", "maximum_multiplier"),
        "configuration maximumMultiplier",
      ),
      feeQuantum: fromDecimal(
        firstValue(config, "feeQuantum", "fee_quantum"),
        "configuration feeQuantum",
      ),
      hourlyEquivalentQuantum: fromDecimal(
        firstValue(config, "hourlyEquivalentQuantum", "hourly_equivalent_quantum"),
        "configuration hourlyEquivalentQuantum",
      ),
      complexity: tables.complexityTable,
      urgency: tables.urgencyTable,
      rates: tables.rateTable,
    };
  }

  function factorDecimal(table, key, fieldName) {
    const raw = tableValue(table, key, [], fieldName);
    if (raw && typeof raw === "object") {
      return fromDecimal(
        firstValue(raw, "effort", "effort") ??
          firstValue(raw, "effortMultiplier", "effort_multiplier") ??
          firstValue(raw, "value", "value") ??
          firstValue(raw, "multiplier", "multiplier"),
        fieldName,
      );
    }
    return fromDecimal(raw, fieldName);
  }

  function riskDecimal(table, key, fieldName) {
    const raw = tableValue(table, key, [], fieldName);
    if (raw && typeof raw === "object") {
      return fromDecimal(
        firstValue(raw, "risk", "risk") ??
          firstValue(raw, "riskBuffer", "risk_buffer"),
        fieldName,
      );
    }
    throw new Error(`${fieldName} is missing from the pricing configuration.`);
  }

  function rateDecimal(table, experience, currency, fieldName) {
    const row = table && table[experience];
    if (row && typeof row === "object") {
      const direct = firstValue(row, currency, currency);
      if (direct !== undefined) {
        return fromDecimal(direct, fieldName);
      }
    }
    const reverse = table && table[currency];
    if (reverse && typeof reverse === "object") {
      const direct = firstValue(reverse, experience, experience);
      if (direct !== undefined) {
        return fromDecimal(direct, fieldName);
      }
    }
    throw new Error(`${fieldName} is missing from the pricing configuration.`);
  }

  function inputValue(inputs, camelName, snakeName) {
    const value = firstValue(inputs, camelName, snakeName);
    if (value === undefined || value === null || value === "") {
      throw new Error(`${snakeName} is required.`);
    }
    return value;
  }

  function factorRow(id, label, value) {
    return { id, label, value };
  }

  function calculateFromConfig(config, inputs) {
    const normalizedConfig = normalizeConfig(config);
    const deliveryDays = fromInteger(
      inputValue(inputs, "deliveryDays", "delivery_days"),
      "delivery_days",
    );
    const revisionRounds = fromInteger(
      inputValue(inputs, "revisionRounds", "revision_rounds"),
      "revision_rounds",
    );
    if (deliveryDays === 0n) {
      throw new Error("effective_hours must be positive");
    }
    if (deliveryDays < 1n || deliveryDays > 60n) {
      throw new Error("delivery_days must be between 1 and 60.");
    }
    if (revisionRounds < 0n || revisionRounds > 10n) {
      throw new Error("revision_rounds must be between 0 and 10.");
    }
    const complexity = String(inputValue(inputs, "complexity", "complexity"));
    const urgency = String(inputValue(inputs, "urgency", "urgency"));
    const experience = String(inputValue(inputs, "experience", "experience"));
    const currency = String(inputValue(inputs, "currency", "currency"));
    const complexityFactor = factorDecimal(
      normalizedConfig.complexity,
      complexity,
      "complexity",
    );
    const riskBuffer = riskDecimal(
      normalizedConfig.complexity,
      complexity,
      "complexity risk",
    );
    const urgencyMultiplier = factorDecimal(
      normalizedConfig.urgency,
      urgency,
      "urgency",
    );
    const hourlyRate = rateDecimal(
      normalizedConfig.rates,
      experience,
      currency,
      "experience rate",
    );
    const baseHours = multiply(
      multiply(
        rational(deliveryDays, 1n),
        normalizedConfig.hoursPerDeliveryDay,
      ),
      complexityFactor,
    );
    const additionalRevisions = max(
      subtract(
        rational(revisionRounds, 1n),
        normalizedConfig.includedRevisionRounds,
      ),
      rational(0n, 1n),
    );
    const effectiveHours = add(
      baseHours,
      multiply(additionalRevisions, normalizedConfig.additionalRevisionHours),
    );
    if (!positive(effectiveHours)) {
      throw new Error("effective_hours must be positive");
    }
    const baseFee = multiply(effectiveHours, hourlyRate);
    const riskAdjustedFee = multiply(
      baseFee,
      add(rational(1n, 1n), riskBuffer),
    );
    const recommendedExact = multiply(riskAdjustedFee, urgencyMultiplier);
    const minimumExact = multiply(recommendedExact, normalizedConfig.minimumMultiplier);
    const maximumExact = multiply(recommendedExact, normalizedConfig.maximumMultiplier);
    const hourlyEquivalentExact = divide(recommendedExact, effectiveHours);
    const minimum = quantize(minimumExact, normalizedConfig.feeQuantum);
    const recommended = quantize(recommendedExact, normalizedConfig.feeQuantum);
    const maximum = quantize(maximumExact, normalizedConfig.feeQuantum);
    const hourlyEquivalent = quantize(
      hourlyEquivalentExact,
      normalizedConfig.hourlyEquivalentQuantum,
    );
    const additionalRevisionTime = multiply(
      additionalRevisions,
      normalizedConfig.additionalRevisionHours,
    );
    const factors = [
      factorRow(
        "effective_hours",
        `Effective hours (${formatTrimmed(normalizedConfig.hoursPerDeliveryDay)} focused hours per delivery day)`,
        formatRational(effectiveHours, 2),
      ),
      factorRow(
        "complexity_multiplier",
        "Complexity effort multiplier",
        formatTrimmed(complexityFactor),
      ),
      factorRow(
        "experience_rate",
        `${experience[0].toUpperCase()}${experience.slice(1)} reference rate (${currency})`,
        formatTrimmed(hourlyRate),
      ),
      factorRow(
        "revision_time",
        `Revision time (${formatTrimmed(normalizedConfig.includedRevisionRounds)} included rounds)`,
        positive(additionalRevisionTime) ? `+${formatTrimmed(additionalRevisionTime)}` : "0",
      ),
      factorRow(
        "risk_buffer",
        "Complexity risk buffer",
        `${formatTrimmed(multiply(riskBuffer, rational(100n, 1n)))}%`,
      ),
      factorRow(
        "urgency_multiplier",
        "Urgency multiplier",
        formatTrimmed(urgencyMultiplier),
      ),
      factorRow("minimum", "Minimum fee", formatTrimmed(minimum)),
      factorRow("recommended", "Recommended fee", formatTrimmed(recommended)),
      factorRow("maximum", "Maximum fee", formatTrimmed(maximum)),
      factorRow(
        "hourly_equivalent",
        "Hourly equivalent",
        formatTrimmed(hourlyEquivalent),
      ),
    ];
    return Object.freeze({
      effective_hours: formatRational(effectiveHours, 2),
      minimum: formatTrimmed(minimum),
      recommended: formatTrimmed(recommended),
      maximum: formatTrimmed(maximum),
      hourly_equivalent: formatTrimmed(hourlyEquivalent),
      pricing_config_version: normalizedConfig.version,
      factors: Object.freeze(factors),
    });
  }

  return { calculateFromConfig };
});
