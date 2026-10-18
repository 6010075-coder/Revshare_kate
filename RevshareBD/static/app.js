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
