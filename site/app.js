/* Progressive enhancement: content and commands remain available without JS. */
(() => {
  "use strict";
  const tablist = document.querySelector(".setup-tabs");
  const tabs = [...tablist.querySelectorAll('[role="tab"]')];
  const panels = tabs.map((tab) =>
    document.getElementById(tab.getAttribute("aria-controls")),
  );
  function activate(index, moveFocus = false) {
    tabs.forEach((tab, i) => {
      tab.setAttribute("aria-selected", String(i === index));
      tab.tabIndex = i === index ? 0 : -1;
      panels[i].hidden = i !== index;
    });
    if (moveFocus) tabs[index].focus();
  }
  panels.forEach((panel, i) => {
    panel.setAttribute("role", "tabpanel");
    panel.setAttribute("aria-labelledby", tabs[i].id);
    panel.tabIndex = 0;
  });
  tabs.forEach((tab, i) => {
    tab.addEventListener("click", () => activate(i));
    tab.addEventListener("keydown", (event) => {
      let next;
      if (event.key === "ArrowRight") next = (i + 1) % tabs.length;
      if (event.key === "ArrowLeft") next = (i - 1 + tabs.length) % tabs.length;
      if (event.key === "Home") next = 0;
      if (event.key === "End") next = tabs.length - 1;
      if (next !== undefined) {
        event.preventDefault();
        activate(next, true);
      }
    });
  });
  activate(0);
  tablist.hidden = false;

  const status = document.querySelector(".copy-status");
  let statusTimer;
  const feedback = (message) => {
    clearTimeout(statusTimer);
    status.textContent = message;
    statusTimer = setTimeout(() => {
      status.textContent = "";
    }, 7000);
  };
  document.querySelectorAll("[data-copy]").forEach((button) => {
    button.hidden = false;
    button.addEventListener("click", async () => {
      const code = document.getElementById(button.dataset.copy);
      const command = code.textContent.trim();
      try {
        if (!navigator.clipboard?.writeText)
          throw new Error("Clipboard unavailable");
        await navigator.clipboard.writeText(command);
        feedback(
          button.dataset.copySuccess || (button.dataset.copy === "install-command"
            ? "Commands copied. Replace yourname before running."
            : "Inspection commands copied."),
        );
      } catch {
        // Clipboard access may be blocked by browser permissions or an insecure origin.
        const range = document.createRange();
        range.selectNodeContents(code);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        code.closest("pre").focus();
        feedback(
          "Clipboard unavailable. Commands selected — use your device’s copy action.",
        );
      }
    });
  });
})();

/* Ship's map: a console around static cards. Every card, step and entry stays in the
   HTML; this adds the card panel, assimilation progress, the guided tour and the flow cycle. */
(() => {
  "use strict";
  const section = document.querySelector("#system");
  if (!section) return;
  const art = section.querySelector(".map-art");
  const nodes = [...section.querySelectorAll(".map-node[data-part]")];
  const cards = new Map(
    [...section.querySelectorAll(".part-card[data-part]")].map((card) => [card.dataset.part, card]),
  );
  const panel = section.querySelector("#map-card");
  const count = section.querySelector("#map-count");
  const countLabel = section.querySelector("#map-count-label");
  const ring = section.querySelector(".ring-fill");
  const announce = section.querySelector(".map-announce");
  const tourButton = section.querySelector("#map-tour");
  const resetButton = section.querySelector("#map-reset");
  const hud = section.querySelector(".map-hud-text");
  const steps = [...section.querySelectorAll(".flow-steps > li")];
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
  const total = cards.size;
  const key = "borg-map-assimilated-v1";
  const name = (part) => cards.get(part)?.querySelector("h4").textContent.trim() || "Stardate log";
  const readout = (part) =>
    cards.get(part)?.querySelector(".part-readout").textContent.replace(/\s+/g, " ").trim() || "";
  let seen = new Set();
  try {
    seen = new Set(JSON.parse(localStorage.getItem(key) || "[]").filter((part) => cards.has(part)));
  } catch {
    seen = new Set();
  }
  let tour = -1;
  let cycle = 0;
  let timer = 0;

  const cubes = (part) => (art ? [...art.querySelectorAll(`.cube[data-part="${part}"]`)] : []);
  function save() {
    try {
      localStorage.setItem(key, JSON.stringify([...seen]));
    } catch {
      /* Progress is optional; private browsing may refuse storage. */
    }
  }
  function progress() {
    const done = seen.size;
    count.textContent = `${done} of ${total}`;
    countLabel.textContent = done === total ? "parts assimilated. The collective is complete." : "parts assimilated";
    ring.style.strokeDasharray = `${(done / total) * 100} 100`;
    section.classList.toggle("is-complete", done === total);
    resetButton.hidden = done === 0;
    for (const [part, card] of cards) {
      const assimilated = seen.has(part);
      cubes(part).forEach((cube) => cube.classList.toggle("is-assimilated", assimilated));
      nodes.filter((node) => node.dataset.part === part)
        .forEach((node) => node.classList.toggle("is-assimilated", assimilated));
      let badge = card.querySelector(".part-state");
      if (assimilated && !badge) {
        badge = document.createElement("p");
        badge.className = "part-state";
        badge.textContent = "Assimilated";
        card.append(badge);
      } else if (!assimilated && badge) badge.remove();
    }
  }
  function assimilate(parts) {
    const fresh = parts.filter((part) => cards.has(part) && !seen.has(part));
    fresh.forEach((part) => seen.add(part));
    if (fresh.length) {
      save();
      progress();
    }
    return fresh;
  }
  function highlight(part, className) {
    art?.querySelectorAll(`.${className}`).forEach((cube) => cube.classList.remove(className));
    if (part) cubes(part).forEach((cube) => cube.classList.add(className));
  }
  function button(label, onClick, primary = false) {
    const control = document.createElement("button");
    control.type = "button";
    control.className = primary ? "map-button primary" : "map-button";
    control.textContent = label;
    control.addEventListener("click", onClick);
    return control;
  }
  function nextUnseen(after) {
    const order = [...cards.keys()];
    const start = order.indexOf(after);
    for (let i = 1; i <= order.length; i++) {
      const part = order[(start + i) % order.length];
      if (!seen.has(part)) return part;
    }
    return null;
  }
  function say(message) {
    announce.textContent = "";
    requestAnimationFrame(() => {
      announce.textContent = message;
    });
  }
  function reveal() {
    const box = panel.getBoundingClientRect();
    if (box.top < 0 || box.top > innerHeight * 0.7)
      panel.scrollIntoView({ block: "start", behavior: reduced.matches ? "auto" : "smooth" });
  }
  function open(part, { fromMap = true } = {}) {
    const card = cards.get(part);
    if (!card) return;
    exitTour(false);
    const copy = card.cloneNode(true);
    copy.removeAttribute("id");
    copy.removeAttribute("role");
    copy.querySelectorAll("[id]").forEach((element) => element.removeAttribute("id"));
    copy.removeAttribute("aria-labelledby");
    copy.querySelector(".part-state")?.remove();
    const heading = copy.querySelector("h4");
    const title = document.createElement("h3");
    title.id = "map-panel-title";
    title.textContent = heading.textContent;
    heading.replaceWith(title);
    const fresh = assimilate([part]);
    const next = nextUnseen(part);
    const actions = document.createElement("div");
    actions.className = "map-card-nav";
    if (next) actions.append(button(`Next: ${name(next)}`, () => open(next, { fromMap: false }), true));
    else {
      const log = document.createElement("a");
      log.className = "map-button primary";
      log.href = "./assets/stardate/";
      log.textContent = "Every part seen. Read the Stardate log";
      actions.append(log);
    }
    actions.append(button("All cards", () => document.querySelector(`#part-${part}`).scrollIntoView({ behavior: reduced.matches ? "auto" : "smooth" })));
    panel.replaceChildren(copy, actions);
    nodes.forEach((node) => node.classList.toggle("is-selected", node.dataset.part === part));
    nodes.forEach((node) => (node.dataset.part === part ? node.setAttribute("aria-current", "true") : node.removeAttribute("aria-current")));
    highlight(part, "is-selected");
    const status = fresh.length
      ? `${name(part)} assimilated. ${seen.size} of ${total}.${seen.size === total ? " The collective is complete." : ""}`
      : `${name(part)}. Already assimilated.`;
    say(status);
    if (!fromMap) panel.focus({ preventScroll: true });
    reveal();
  }
  function showStep(index) {
    const step = steps[index];
    const parts = step.dataset.parts.split(" ").filter((part) => cards.has(part));
    section.dataset.flow = String(index + 1);
    steps.forEach((item, i) => item.classList.toggle("is-active", i === index));
    art?.querySelectorAll(".cube.is-step").forEach((cube) => cube.classList.remove("is-step"));
    parts.forEach((part) => cubes(part).forEach((cube) => cube.classList.add("is-step")));
    return parts;
  }
  function renderTour() {
    const step = steps[tour];
    const parts = showStep(tour);
    const fresh = assimilate(parts);
    const eyebrow = document.createElement("p");
    eyebrow.className = "eyebrow";
    eyebrow.textContent = `GUIDED TOUR · STOP ${tour + 1} OF ${steps.length}`;
    const title = document.createElement("h3");
    title.id = "map-panel-title";
    title.textContent = step.querySelector("h4").textContent;
    const text = document.createElement("p");
    text.textContent = step.querySelector("p").textContent;
    const list = document.createElement("ul");
    list.className = "tour-parts";
    for (const part of parts) {
      const item = document.createElement("li");
      const label = document.createElement("b");
      label.textContent = name(part);
      const lede = document.createElement("span");
      lede.textContent = cards.get(part).querySelector(".part-lede").textContent + " ";
      const value = document.createElement("span");
      value.className = "readout";
      value.textContent = readout(part);
      item.append(label, lede, value);
      list.append(item);
    }
    const actions = document.createElement("div");
    actions.className = "map-card-nav";
    if (tour > 0) actions.append(button("Back", () => { tour--; renderTour(); }));
    if (tour < steps.length - 1) actions.append(button("Next stop", () => { tour++; renderTour(); }, true));
    else actions.append(button("Finish the tour", () => exitTour(true), true));
    actions.append(button("Exit", () => exitTour(true)));
    panel.replaceChildren(eyebrow, title, text, list, actions);
    say(`Stop ${tour + 1} of ${steps.length}: ${title.textContent}${fresh.length ? ` ${fresh.length} new part${fresh.length > 1 ? "s" : ""} assimilated, ${seen.size} of ${total}.` : ""}`);
    panel.focus({ preventScroll: true });
    reveal();
  }
  function startTour(index = 0) {
    stopCycle();
    tour = index;
    tourButton.hidden = true;
    renderTour();
  }
  function exitTour(restore) {
    if (tour < 0) return;
    tour = -1;
    tourButton.hidden = false;
    steps.forEach((item) => item.classList.remove("is-active"));
    delete section.dataset.flow;
    if (restore) {
      panel.replaceChildren(...intro.map((node) => node.cloneNode(true)));
      say("Tour closed.");
      tourButton.focus();
    }
    syncCycle();
  }
  function stopCycle() {
    clearInterval(timer);
    timer = 0;
  }
  function syncCycle() {
    const running = section.dataset.motion === "running" && tour < 0;
    if (running && !timer) {
      showStep(cycle);
      timer = setInterval(() => {
        cycle = (cycle + 1) % steps.length;
        showStep(cycle);
      }, 4200);
    } else if (!running && timer) {
      stopCycle();
    }
    if (!running && tour < 0) {
      delete section.dataset.flow;
      steps.forEach((item) => item.classList.remove("is-active"));
      art?.querySelectorAll(".cube.is-step").forEach((cube) => cube.classList.remove("is-step"));
    }
  }

  const intro = [...panel.childNodes];
  section.classList.add("game-on");
  for (const node of nodes) {
    const part = node.dataset.part;
    if (part === "log") continue;
    node.addEventListener("click", (event) => {
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
      event.preventDefault();
      open(part);
    });
    const scan = () => {
      highlight(part, "is-hover");
      if (hud) hud.textContent = `${name(part).toUpperCase()} · ${readout(part)}`;
    };
    const clear = () => {
      highlight(null, "is-hover");
      if (hud) hud.textContent = "Hover or focus a cube to scan it.";
    };
    node.addEventListener("pointerenter", scan);
    node.addEventListener("focus", scan);
    node.addEventListener("pointerleave", clear);
    node.addEventListener("blur", clear);
  }
  steps.forEach((step, index) => {
    const heading = step.querySelector("h4");
    const control = document.createElement("button");
    control.type = "button";
    control.className = "flow-step-button";
    control.textContent = heading.textContent;
    control.setAttribute("aria-label", `Tour stop ${index + 1}: ${heading.textContent}`);
    control.addEventListener("click", () => startTour(index));
    heading.replaceChildren(control);
  });
  tourButton.hidden = false;
  tourButton.addEventListener("click", () => startTour(0));
  resetButton.addEventListener("click", () => {
    seen.clear();
    save();
    progress();
    highlight(null, "is-selected");
    nodes.forEach((node) => {
      node.classList.remove("is-selected");
      node.removeAttribute("aria-current");
    });
    if (tour < 0) panel.replaceChildren(...intro.map((node) => node.cloneNode(true)));
    say("Progress reset. Every part is unassimilated again.");
  });
  section.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && tour >= 0) exitTour(true);
  });
  new MutationObserver(syncCycle).observe(section, { attributes: true, attributeFilter: ["data-motion"] });
  progress();
  syncCycle();
})();
