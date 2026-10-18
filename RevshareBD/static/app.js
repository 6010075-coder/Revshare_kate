const syncApplicationOpenLink = (input) => {
  const link = input.closest(".application-url-control")?.querySelector("[data-application-open]");
  if (!link) return;
  const value = input.value.trim();
  const valid = /^https?:\/\//i.test(value);
  link.href = valid ? value : "#";
  link.classList.toggle("is-disabled", !valid);
  link.setAttribute("aria-disabled", String(!valid));
  link.tabIndex = valid ? 0 : -1;
};

const csrfTokenMeta = document.querySelector('meta[name="csrf-token"]');
const csrfToken = csrfTokenMeta ? csrfTokenMeta.getAttribute("content") : "";

document.addEventListener("submit", (event) => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement)) return;
  if (!csrfToken) return;
  if (form.querySelector('input[name="csrf_token"]')) return;
  const input = document.createElement("input");
  input.type = "hidden";
  input.name = "csrf_token";
  input.value = csrfToken;
  form.appendChild(input);
});

// Optional native confirm() prompt for destructive actions
// (e.g. deleting a bot draft). Forms opt in via data-confirm="…".
document.addEventListener("submit", (event) => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement)) return;
  const confirmMessage = form.dataset.confirm;
  if (confirmMessage && !window.confirm(confirmMessage)) {
    event.preventDefault();
  }
});

// Universal submit-button lock + trailing ⏳ spinner.
// Registered AFTER the data-confirm listener above, so by the time we run
// event.defaultPrevented already reflects whether the user cancelled the
// confirm dialog. If they cancelled, we bail out and leave the button
// alone. Otherwise we lock the submit button that triggered the submit
// (or the first eligible one in the form) and stash its original text on
// data-original-text so we can restore it on page-state restoration.
document.addEventListener("submit", (event) => {
  if (event.defaultPrevented) return;
  const form = event.target;
  if (!(form instanceof HTMLFormElement)) return;
  const button =
    event.submitter instanceof HTMLButtonElement
      ? event.submitter
      : form.querySelector('button[type="submit"]:not([data-no-lock])');
  if (!button || button.disabled || button.dataset.originalText !== undefined) return;
  button.dataset.originalText = button.textContent;
  button.disabled = true;
  button.textContent = `${button.textContent} ⏳`;
});

const resetLockedButtons = () => {
  document.querySelectorAll("button[data-original-text]").forEach((button) => {
    button.textContent = button.dataset.originalText;
    button.disabled = false;
    delete button.dataset.originalText;
  });
};

// Back-button / bfcache restore, and returning to the tab after a failed
// submit (network error), both bring a stale "⏳" button back into view;
// in those cases restore the original label and re-enable it.
window.addEventListener("pageshow", (event) => {
  if (event.persisted) resetLockedButtons();
});
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) resetLockedButtons();
});

const originalFetch = window.fetch;
window.fetch = function patchedFetch(input, init = {}) {
  const method = String(init.method || (input instanceof Request ? input.method : "GET")).toUpperCase();
  if (!csrfToken || method === "GET" || method === "HEAD") {
    return originalFetch.call(this, input, init);
  }
  const headers = new Headers(init.headers || (input instanceof Request ? input.headers : undefined));
  if (!headers.has("X-CSRF-Token")) headers.set("X-CSRF-Token", csrfToken);
  return originalFetch.call(this, input, { ...init, headers });
};

document.querySelectorAll("[data-application-url]").forEach((input) => {
  syncApplicationOpenLink(input);
  input.addEventListener("input", () => syncApplicationOpenLink(input));
});

document.querySelectorAll("[data-add]").forEach((button) => {
  button.addEventListener("click", () => {
    const list = document.querySelector(`[data-repeat="${button.dataset.add}"]`);
    const row = list.querySelector(".repeat-row");
    if (!list || !row) return;
    const copy = row.cloneNode(true);
    copy.querySelectorAll("input").forEach((input) => {
      input.value = "";
    });
    copy.querySelectorAll("select").forEach((select) => {
      select.selectedIndex = 0;
    });
    list.appendChild(copy);
    copy.querySelectorAll("[data-application-url]").forEach((input) => {
      syncApplicationOpenLink(input);
      input.addEventListener("input", () => syncApplicationOpenLink(input));
    });
  });
});

document.querySelectorAll("[data-own-schedule]").forEach((checkbox) => {
  const block = document.querySelector("[data-schedule-block]");
  const sync = () => {
    if (!block) return;
    const own = checkbox.checked;
    block.classList.toggle("locked", !own);
    block.querySelectorAll("input").forEach((input) => {
      if (input.type === "hidden") return;
      input.disabled = !own;
    });
  };
  checkbox.addEventListener("change", sync);
  sync();
});

document.querySelectorAll("[data-auto-submit]").forEach((control) => {
  control.addEventListener("change", () => {
    control.closest("form")?.requestSubmit();
  });
});

document.querySelectorAll("[data-search-submit]").forEach((input) => {
  let timer;
  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = window.setTimeout(() => input.closest("form")?.requestSubmit(), 400);
  });
  input.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    clearTimeout(timer);
    event.preventDefault();
    input.closest("form")?.requestSubmit();
  });
});

document.querySelectorAll("[data-language]").forEach((select) => {
  select.addEventListener("change", () => {
    const next = select.dataset.next || "/";
    window.location.href = `/language/${select.value}?next=${encodeURIComponent(next)}`;
  });
});

document.querySelectorAll("[data-tabs]").forEach((nav) => {
  const buttons = nav.querySelectorAll("[data-tab]");
  const openTab = (name) => {
    buttons.forEach((item) => item.classList.toggle("active", item.dataset.tab === name));
    document.querySelectorAll("[data-panel]").forEach((panel) => {
      panel.classList.toggle("active", panel.dataset.panel === name);
    });
    const url = new URL(window.location.href);
    url.searchParams.set("tab", name);
    window.history.replaceState({}, "", url);
  };
  buttons.forEach((button) => {
    button.addEventListener("click", () => openTab(button.dataset.tab));
  });
  document.querySelectorAll("[data-open-tab]").forEach((control) => {
    control.addEventListener("click", () => openTab(control.dataset.openTab));
  });
});

document.querySelectorAll("[data-jump]").forEach((select) => {
  select.addEventListener("change", () => {
    window.location.href = select.value;
  });
});

document.querySelectorAll("[data-accrual-form]").forEach((form) => {
  const items = [...form.querySelectorAll("[data-accrual-item]")];
  const selectAll = form.querySelector("[data-accrual-select-all]");
  const submit = form.querySelector("[data-accrual-submit]");
  const counter = form.querySelector("[data-accrual-selected-count]");
  if (!items.length || !submit) return;

  const sync = () => {
    const selected = items.filter((item) => item.checked);
    submit.disabled = selected.length === 0;
    if (selectAll) {
      selectAll.checked = selected.length === items.length;
      selectAll.indeterminate =
        selected.length > 0 && selected.length < items.length;
    }
    if (counter) {
      counter.hidden = selected.length === 0;
      counter.textContent = (counter.dataset.label || "").replace(
        "__COUNT__",
        String(selected.length),
      );
    }
  };

  if (selectAll) {
    selectAll.addEventListener("change", () => {
      items.forEach((item) => {
        item.checked = selectAll.checked;
      });
      sync();
    });
  }
  items.forEach((item) => item.addEventListener("change", sync));
  sync();
});

document.addEventListener("click", (event) => {
  const button = event.target.closest(".icon-remove");
  if (!button) return;
  const list = button.closest("[data-repeat]");
  const rows = list.querySelectorAll(".repeat-row");
  if (rows.length === 1) {
    button.closest(".repeat-row").querySelectorAll("input").forEach((input) => {
      input.value = "";
    });
    return;
  }
  button.closest(".repeat-row").remove();
});

// "Reset to default" button for the invite_telegram textarea. Clearing
// the value is the explicit signal to the server that the partner has no
// per-language override and the i18n default should be used.
document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-invite-telegram-reset]");
  if (!button) return;
  const confirmMessage = button.dataset.confirmReset;
  if (confirmMessage && !window.confirm(confirmMessage)) {
    return;
  }
  const input = document.querySelector("[data-invite-telegram-input]");
  if (input instanceof HTMLTextAreaElement) {
    input.value = "";
    input.focus();
  }
});

// Live preview of the resolved notification date inside the schedule editor.
// The day number is the day of the month that follows the reporting period;
// we mirror the server-side weekend rollover so the admin sees the actual
// send date before they hit "Save changes".
const schedulePreviewCache = new WeakMap();
const daysInMonth = (year, month) => new Date(year, month, 0).getDate();
const computeSendDate = (preview, day) => {
  const year = Number(preview.dataset.scheduleYear);
  const month = Number(preview.dataset.scheduleMonth); // month after the reporting period
  const clamped = Math.min(28, Math.max(1, Math.floor(day) || 1));
  const lastDay = daysInMonth(year, month);
  const safeDay = Math.min(clamped, lastDay);
  const base = new Date(year, month - 1, safeDay);
  const shifted = base.getDay() === 6 ? new Date(base.getTime() + 86400000)
    : base.getDay() === 0 ? new Date(base.getTime() + 86400000)
    : base;
  return shifted;
};
const formatSendDate = (date, locale) => {
  try {
    return new Intl.DateTimeFormat(locale || document.documentElement.lang || "ru-RU", {
      weekday: "short",
      day: "numeric",
      month: "short",
    }).format(date);
  } catch (error) {
    return date.toISOString().slice(0, 10);
  }
};
document.querySelectorAll("[data-schedule-preview]").forEach((preview) => {
  const rows = preview.querySelectorAll("[data-schedule-row]");
  const locale = preview.dataset.scheduleLocale || "ru-RU";
  rows.forEach((row) => {
    const input = row.querySelector("[data-day-input]");
    const chip = row.querySelector("[data-send-date]");
    if (!(input instanceof HTMLInputElement) || !chip) return;
    const refresh = () => {
      const value = computeSendDate(preview, Number(input.value));
      chip.textContent = formatSendDate(value, locale);
    };
    input.addEventListener("input", refresh);
    input.addEventListener("change", refresh);
    schedulePreviewCache.set(row, { input, chip, refresh });
  });
});

// Master on/off switch for the schedule. Flipping it shows/hides the
// "auto-send is off" banner and dims the schedule preview so the admin
// sees at a glance that nothing will fire from the schedule.
// Manual sends from the partner card are unaffected — they go through a
// separate code path that does not consult this flag.
document.querySelectorAll("[data-schedule-master]").forEach((block) => {
  const input = block.querySelector("[data-schedule-master-input]");
  const banner = block.querySelector("[data-schedule-master-banner]");
  const preview = document.querySelector("[data-schedule-preview]");
  if (!(input instanceof HTMLInputElement)) return;
  const sync = () => {
    const on = input.checked;
    if (banner instanceof HTMLElement) banner.hidden = on;
    if (preview instanceof HTMLElement) {
      preview.classList.toggle("is-master-off", !on);
      preview.dataset.scheduleMasterOff = on ? "0" : "1";
    }
  };
  input.addEventListener("change", sync);
  sync();
});


// Copy partner Telegram invite link to clipboard. We try the async Clipboard
// API first and fall back to document.execCommand so it still works on older
// browsers and over plain http (where navigator.clipboard is blocked).
document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-copy-invite-link]");
  if (!button) return;
  const targetId = button.dataset.target;
  const input = targetId ? document.getElementById(targetId) : null;
  if (!input) return;
  const value = input.value;
  const restore = button.textContent;
  const ok = async () => {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(value);
      } else {
        input.select();
        input.setSelectionRange(0, value.length);
        if (!document.execCommand("copy")) throw new Error("execCommand failed");
      }
      button.textContent = button.dataset.copiedLabel || "Copied";
    } catch (error) {
      console.error("Failed to copy invite link", error);
      button.textContent = "Copy failed";
    } finally {
      window.setTimeout(() => { button.textContent = restore; }, 1800);
    }
  };
  await ok();
});
