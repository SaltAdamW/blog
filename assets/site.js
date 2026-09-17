(() => {
  "use strict";
  const root = document.documentElement;
  root.classList.add("js");
  const media = window.matchMedia("(max-width: 980px)");
  const systemTheme = window.matchMedia("(prefers-color-scheme: dark)");
  const disclosure = document.querySelector(".toc-disclosure");
  const summary = disclosure.querySelector("summary");
  const themeButton = document.querySelector('[data-action="theme"]');
  const toast = document.querySelector(".toast");
  let savedTheme;
  let toastTimer;
  try { savedTheme = localStorage.getItem("adam-blog-theme"); } catch { /* 存储被禁用时，主题仍可在当前页面切换。 */ }

  const cloneIcon = (name) => document.querySelector(`#${name}-icon`).content.cloneNode(true);
  function setTheme(theme) {
    root.dataset.theme = theme;
    const dark = theme === "dark";
    const label = dark ? "切换浅色主题" : "切换深色主题";
    themeButton.setAttribute("aria-pressed", String(dark));
    themeButton.setAttribute("aria-label", label);
    themeButton.querySelector(".tooltip").textContent = label;
    themeButton.querySelector("svg").replaceWith(cloneIcon(dark ? "sun" : "moon"));
  }
  setTheme(savedTheme === "dark" || savedTheme === "light" ? savedTheme : systemTheme.matches ? "dark" : "light");
  themeButton.addEventListener("click", () => {
    savedTheme = root.dataset.theme === "dark" ? "light" : "dark";
    setTheme(savedTheme);
    try { localStorage.setItem("adam-blog-theme", savedTheme); } catch { /* 无持久存储时保留本次选择。 */ }
  });
  systemTheme.addEventListener("change", () => { if (!savedTheme) setTheme(systemTheme.matches ? "dark" : "light"); });

  function responsiveToc() {
    disclosure.open = !media.matches;
    summary.tabIndex = media.matches ? 0 : -1;
  }
  responsiveToc();
  media.addEventListener("change", responsiveToc);
  summary.addEventListener("click", (event) => { if (!media.matches) event.preventDefault(); });
  disclosure.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && media.matches && disclosure.open) {
      disclosure.open = false;
      summary.focus();
    }
  });
  disclosure.querySelectorAll("a").forEach((link) => link.addEventListener("click", () => {
    if (!media.matches) return;
    disclosure.open = false;
    const target = document.getElementById(link.hash.slice(1));
    if (target) {
      target.tabIndex = -1;
      target.focus({ preventScroll: true });
    }
  }));

  function notify(message) {
    clearTimeout(toastTimer);
    toast.textContent = message;
    toast.classList.add("is-visible");
    toastTimer = setTimeout(() => toast.classList.remove("is-visible"), 4000);
  }

  async function copy(text) {
    if (navigator.clipboard?.writeText) {
      try { await navigator.clipboard.writeText(text); return; } catch { /* 本地文件或受限权限使用下方回退。 */ }
    }
    const previous = document.activeElement;
    const field = document.createElement("textarea");
    field.value = text;
    field.style.cssText = "position:fixed;left:-9999px;top:0";
    field.setAttribute("readonly", "");
    document.body.append(field);
    field.select();
    const success = document.execCommand("copy");
    field.remove();
    previous?.focus({ preventScroll: true });
    if (!success) throw new Error("Clipboard unavailable");
  }

  document.querySelector('[data-action="copy-link"]').addEventListener("click", async () => {
    const canonical = document.querySelector('link[rel="canonical"]').href;
    try { await copy(canonical + location.hash); notify("文章链接已复制"); }
    catch { notify("未能访问剪贴板，请从地址栏复制链接"); }
  });

  document.querySelectorAll(".prose pre").forEach((pre) => {
    const code = pre.querySelector("code");
    if (!code) return;
    const wrapper = document.createElement("div");
    wrapper.className = "code-block";
    const bar = document.createElement("div");
    bar.className = "code-bar";
    const language = document.createElement("span");
    language.textContent = code.className.replace("language-", "").toUpperCase() || "TEXT";
    const button = document.createElement("button");
    button.className = "icon-button";
    button.type = "button";
    button.setAttribute("aria-label", "复制代码");
    const tip = document.createElement("span");
    tip.className = "tooltip";
    tip.setAttribute("role", "tooltip");
    tip.textContent = "复制代码";
    button.append(cloneIcon("copy"), tip);
    let resetTimer;
    button.addEventListener("click", async () => {
      try {
        await copy(code.textContent);
        clearTimeout(resetTimer);
        button.querySelector("svg").replaceWith(cloneIcon("check"));
        tip.textContent = "已复制";
        notify("代码已复制");
        resetTimer = setTimeout(() => { button.querySelector("svg").replaceWith(cloneIcon("copy")); tip.textContent = "复制代码"; }, 2000);
      } catch { notify("复制失败，请选中代码后复制"); }
    });
    bar.append(language, button);
    pre.before(wrapper);
    wrapper.append(bar, pre);
  });

  const sections = [...document.querySelectorAll(".prose h2")];
  const tocLinks = [...document.querySelectorAll(".article-toc a")];
  const article = document.querySelector(".prose");
  const fill = document.querySelector(".reading-fill");
  const progress = document.querySelector(".reading-track");
  const label = document.querySelector("#progress-label");
  const back = document.querySelector(".back-to-top");
  let queued = false;
  let positions = [];
  let articleStart = 0;
  let articleEnd = 0;
  let currentId;
  function measure() {
    positions = sections.map((section) => ({ id: section.id, y: section.getBoundingClientRect().top + scrollY }));
    articleStart = article.getBoundingClientRect().top + scrollY;
    articleEnd = articleStart + article.offsetHeight;
    update();
  }
  function update() {
    queued = false;
    const available = Math.max(1, articleEnd - articleStart - innerHeight);
    const fraction = Math.max(0, Math.min(1, (scrollY - articleStart) / available));
    const percentage = Math.round(fraction * 100);
    fill.style.transform = `scaleX(${fraction})`;
    progress.setAttribute("aria-valuenow", String(percentage));
    label.textContent = `${percentage}%`;
    back.classList.toggle("is-visible", scrollY > 800);
    let active = positions[0]?.id;
    for (const position of positions) {
      if (position.y > scrollY + (media.matches ? 168 : 142)) break;
      active = position.id;
    }
    if (active !== currentId) {
      currentId = active;
      tocLinks.forEach((link) => {
        if (link.dataset.section === active) link.setAttribute("aria-current", "location");
        else link.removeAttribute("aria-current");
      });
    }
  }
  window.addEventListener("scroll", () => { if (!queued) { queued = true; requestAnimationFrame(update); } }, { passive: true });
  window.addEventListener("resize", measure);
  new ResizeObserver(measure).observe(article);
  document.fonts.ready.then(measure);
  window.addEventListener("load", measure);
  measure();
})();
