(function () {
  "use strict";

  function statusFor(element) {
    const container = element.closest("[data-scopequote-actions]");
    return container ? container.querySelector("[data-scopequote-status]") : null;
  }

  function setStatus(element, message, state) {
    const status = statusFor(element);
    if (!status) {
      return;
    }
    status.textContent = message;
    status.dataset.state = state || "success";
  }

  function fallbackCopy(value) {
    const field = document.createElement("textarea");
    field.value = value;
    field.setAttribute("readonly", "");
    field.style.position = "fixed";
    field.style.opacity = "0";
    document.body.appendChild(field);
    field.focus();
    field.select();
    let copied = false;
    try {
      copied = document.execCommand("copy");
    } catch (error) {
      copied = false;
    }
    field.remove();
    return copied;
  }

  function copyValue(value, source) {
    if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
      Promise.resolve()
        .then(function () {
          return navigator.clipboard.writeText(value);
        })
        .then(
          function () {
            setStatus(source, "Copied to clipboard.");
          },
          function () {
            const copied = fallbackCopy(value);
            setStatus(
              source,
              copied ? "Copied to clipboard." : "Copy the selected link manually.",
              copied ? "success" : "error",
            );
          },
        );
      return;
    }
    const copied = fallbackCopy(value);
    setStatus(
      source,
      copied ? "Copied to clipboard." : "Copy the selected link manually.",
      copied ? "success" : "error",
    );
  }

  function shareValue(button) {
    const url = button.dataset.scopequoteShareUrl || button.dataset.scopequoteShareValue;
    const title = button.dataset.scopequoteShareTitle || "Scopequote";
    const text = button.dataset.scopequoteShareText || "A Scopequote quotation";
    if (!url) {
      setStatus(button, "There is no link to share yet.", "error");
      return;
    }
    if (typeof navigator.share === "function") {
      Promise.resolve()
        .then(function () {
          return navigator.share({ title, text, url });
        })
        .then(
          function () {
            setStatus(button, "Share sheet opened.");
          },
          function (error) {
            if (error && error.name === "AbortError") {
              return;
            }
            copyValue(url, button);
          },
        );
      return;
    }
    copyValue(url, button);
  }

  function submitControls(form) {
    return form.querySelectorAll(
      'button[type="submit"], input[type="submit"], button:not([type])',
    );
  }

  function setPending(form, pending) {
    const active = form.dataset.scopequotePendingActive === "true";
    if (pending && !active) {
      submitControls(form).forEach(function (control) {
        control.dataset.scopequotePendingWasDisabled = control.disabled ? "true" : "false";
        control.disabled = true;
      });
      form.dataset.scopequotePendingActive = "true";
    } else if (!pending && active) {
      submitControls(form).forEach(function (control) {
        control.disabled = control.dataset.scopequotePendingWasDisabled === "true";
        delete control.dataset.scopequotePendingWasDisabled;
      });
      form.dataset.scopequotePendingActive = "false";
    }
    form.setAttribute("aria-busy", pending ? "true" : "false");
    const status = form.querySelector("[data-scopequote-pending-status]");
    if (status) {
      status.hidden = !pending;
      status.textContent = pending ? "Submitting…" : "";
    }
  }

  function initialisePending() {
    const forms = document.querySelectorAll("form[data-scopequote-pending]");
    forms.forEach(function (form) {
      form.setAttribute("aria-busy", "false");
      const status = form.querySelector("[data-scopequote-pending-status]");
      if (status) {
        status.hidden = true;
        status.textContent = "";
      }
      form.addEventListener("submit", function (event) {
        if (event.defaultPrevented) {
          return;
        }
        setPending(form, true);
      });
    });
    if (window.addEventListener) {
      window.addEventListener("pageshow", function () {
        forms.forEach(function (form) {
          setPending(form, false);
        });
      });
    }
  }

  function initialiseActions() {
    document.querySelectorAll("[data-scopequote-copy]").forEach(function (button) {
      button.hidden = false;
      button.addEventListener("click", function () {
        copyValue(button.dataset.scopequoteCopyValue || "", button);
      });
    });

    document.querySelectorAll("[data-scopequote-share]").forEach(function (button) {
      button.hidden = false;
      button.addEventListener("click", function () {
        shareValue(button);
      });
    });

    document.querySelectorAll("form[data-scopequote-confirm]").forEach(function (form) {
      form.addEventListener("submit", function (event) {
        if (event.defaultPrevented) {
          return;
        }
        const message = form.dataset.scopequoteConfirm;
        if (message && !window.confirm(message)) {
          event.preventDefault();
        }
      });
    });

    initialisePending();

    document.querySelectorAll("[data-scopequote-dismissible]").forEach(function (notice) {
      const button = notice.querySelector("[data-scopequote-dismiss]");
      if (!button) {
        return;
      }
      button.addEventListener("click", function () {
        notice.classList.add("notice-dismissed");
        notice.setAttribute("hidden", "hidden");
        const next = notice.nextElementSibling;
        if (next) {
          next.focus();
        }
      });
    });

    document.querySelectorAll("[data-scopequote-summary-toggle]").forEach(function (button) {
      if (window.matchMedia && window.matchMedia("(max-width: 900px)").matches) {
        button.hidden = false;
      }
      button.addEventListener("click", function () {
        const targetId = button.getAttribute("aria-controls");
        const target = targetId ? document.getElementById(targetId) : null;
        if (!target) {
          return;
        }
        const expanded = button.getAttribute("aria-expanded") === "true";
        button.setAttribute("aria-expanded", expanded ? "false" : "true");
        button.textContent = expanded ? "Show estimate summary" : "Hide estimate summary";
        target.hidden = expanded;
      });
    });

    document.querySelectorAll("[data-scopequote-focus-target]").forEach(function (target) {
      if (target.dataset.scopequoteFocusTarget === "true") {
        target.focus();
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initialiseActions);
  } else {
    initialiseActions();
  }
})();
