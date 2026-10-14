(function () {
  "use strict";

  function fieldValue(form, name) {
    const field = form.elements.namedItem(name);
    return field && typeof field.value === "string" ? field.value : "";
  }

  function parseFactorTable(selector, attribute, build) {
    const table = {};
    document.querySelectorAll(selector).forEach(function (element) {
      const value = element.dataset[attribute];
      if (value) {
        build(table, value);
      }
    });
    return table;
  }

  function parseColonEntry(value) {
    const parts = value.split(":");
    if (parts.length < 2) {
      throw new Error("Pricing configuration is incomplete.");
    }
    return parts;
  }

  function readConfig(form) {
    const complexity = parseFactorTable(
      "[data-scopequote-complexity-factor]",
      "scopequoteComplexityFactor",
      function (table, value) {
        const parts = parseColonEntry(value);
        if (parts.length < 3) {
          throw new Error("Complexity configuration is incomplete.");
        }
        table[parts[0]] = { effort: parts[1], risk: parts[2] };
      },
    );
    const urgency = parseFactorTable(
      "[data-scopequote-urgency-factor]",
      "scopequoteUrgencyFactor",
      function (table, value) {
        const parts = parseColonEntry(value);
        table[parts[0]] = parts[1];
      },
    );
    const rates = parseFactorTable(
      "[data-scopequote-rate-factor]",
      "scopequoteRateFactor",
      function (table, value) {
        const separator = value.indexOf("=");
        const keyParts = value.slice(0, separator).split(":");
        if (separator < 1 || keyParts.length < 2) {
          throw new Error("Rate configuration is incomplete.");
        }
        if (!table[keyParts[0]]) {
          table[keyParts[0]] = {};
        }
        table[keyParts[0]][keyParts[1]] = value.slice(separator + 1);
      },
    );
    return {
      version: form.dataset.scopequotePricingVersion,
      hoursPerDeliveryDay: form.dataset.scopequoteHoursPerDeliveryDay,
      includedRevisionRounds: form.dataset.scopequoteIncludedRevisionRounds,
      additionalRevisionHours: form.dataset.scopequoteAdditionalRevisionHours,
      minimumMultiplier: form.dataset.scopequoteMinimumMultiplier,
      maximumMultiplier: form.dataset.scopequoteMaximumMultiplier,
      feeQuantum: form.dataset.scopequoteFeeQuantum,
      hourlyEquivalentQuantum: form.dataset.scopequoteHourlyEquivalentQuantum,
      complexity,
      urgency,
      rates,
    };
  }

  function inputValues(form) {
    return {
      delivery_days: fieldValue(form, "delivery_days"),
      complexity: fieldValue(form, "complexity"),
      revision_rounds: fieldValue(form, "revision_rounds"),
      urgency: fieldValue(form, "urgency"),
      experience: fieldValue(form, "experience"),
      currency: fieldValue(form, "currency"),
    };
  }

  function hasServerResult(form) {
    return form.dataset.scopequoteHasServerResult === "true";
  }

  function matchingElements(selector, roots) {
    const elements = [];
    const seen = new Set();
    const selectors = selector.split(",").map(function (part) { return part.trim(); });
    const add = function (matches) {
      matches.forEach(function (element) {
        if (!seen.has(element)) {
          seen.add(element);
          elements.push(element);
        }
      });
    };
    selectors.forEach(function (part) {
      if (document.querySelectorAll) {
        add(document.querySelectorAll(part));
      }
      roots.forEach(function (root) {
        if (root && root.querySelectorAll) {
          add(root.querySelectorAll(part));
        }
      });
    });
    return elements;
  }

  function markStale(form, preview) {
    if (!hasServerResult(form) || preview.dataset.scopequotePreviewState === "stale") {
      return;
    }
    preview.dataset.scopequotePreviewState = "stale";
    matchingElements("[data-scopequote-stale-status]", [preview, form]).forEach(function (status) {
      status.textContent = "Inputs changed. Submit to refresh the server result.";
      status.dataset.state = "stale";
      status.hidden = false;
    });
    matchingElements(
      "[data-scopequote-result-region], [data-scopequote-factor-ledger], [data-scopequote-scope-suggestions], [data-scopequote-guest-actions]",
      [form, preview],
    ).forEach(function (region) {
      region.setAttribute("data-scopequote-stale", "true");
      region.classList.add("is-stale");
    });
    matchingElements("[data-scopequote-downstream-action]", [form, preview]).forEach(function (action) {
      action.setAttribute("aria-disabled", "true");
      action.setAttribute("tabindex", "-1");
      action.classList.add("is-disabled");
      action.disabled = true;
      if (action.querySelectorAll) {
        action.querySelectorAll("button[type=\"submit\"], input[type=\"submit\"]").forEach(function (control) {
          control.disabled = true;
        });
      }
    });
  }

  function updatePreview(form, preview, api) {
    const status = preview.querySelector("[data-scopequote-preview-status]");
    const values = inputValues(form);
    const stale = preview.dataset.scopequotePreviewState === "stale";
    if (Object.values(values).some(function (value) { return value === ""; })) {
      if (status && !stale) {
        status.textContent = "Complete the pricing fields to see a live preview.";
        status.dataset.state = "idle";
      }
      return;
    }
    try {
      const result = api.calculateFromConfig(readConfig(form), values);
      preview.querySelectorAll("[data-scopequote-preview-value]").forEach(function (element) {
        const key = element.dataset.scopequotePreviewValue;
        if (Object.prototype.hasOwnProperty.call(result, key)) {
          element.textContent = result[key];
        }
      });
      const currency = preview.querySelector('[data-scopequote-preview-value="currency"]');
      if (currency) {
        currency.textContent = values.currency;
      }
      preview.dataset.scopequotePreviewState = stale ? "stale" : "live";
      if (status && !stale) {
        status.textContent = "Live preview. The server verifies this estimate on submit.";
        status.dataset.state = "ready";
      }
    } catch (error) {
      preview.dataset.scopequotePreviewState = stale ? "stale" : "server";
      if (status && !stale) {
        status.textContent = "Live preview unavailable. The server result remains authoritative.";
        status.dataset.state = "error";
      }
    }
  }

  function initialiseEstimator() {
    const api = window.ScopequotePricingPreview;
    const form = document.querySelector("[data-scopequote-pricing]");
    if (!api || !form) {
      return;
    }
    const preview = document.querySelector("[data-scopequote-preview]");
    if (!preview) {
      return;
    }
    form.addEventListener("input", function () {
      markStale(form, preview);
      updatePreview(form, preview, api);
    });
    form.addEventListener("change", function () {
      markStale(form, preview);
      updatePreview(form, preview, api);
    });
    form.querySelectorAll("[data-scopequote-duration-preset]").forEach(function (button) {
      button.addEventListener("click", function () {
        const field = form.elements.namedItem("delivery_days");
        if (!field) {
          return;
        }
        field.value = button.dataset.scopequoteDurationPreset;
        field.dispatchEvent(new Event("input", { bubbles: true }));
        field.focus();
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initialiseEstimator);
  } else {
    initialiseEstimator();
  }
})();
