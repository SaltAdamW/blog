(() => {
  "use strict";
  const counters = [...document.querySelectorAll("[data-view-count]")];
  const canonical = document.querySelector('link[rel="canonical"]');
  if (!counters.length || !canonical || window.adamBlogVisitSent) return;

  const site = new URL("https://saltadamw.github.io/blog/");
  const page = new URL(canonical.href);
  const current = new URL(location.href);
  const normalize = path => path.replace(/\/index\.html$/, "/");
  if (current.origin !== site.origin || page.origin !== site.origin ||
      !page.pathname.startsWith(site.pathname) || normalize(current.pathname) !== page.pathname) return;
  page.search = "";
  page.hash = "";

  const show = text => counters.forEach(counter => {
    counter.hidden = false;
    counter.querySelector("[data-count-value]").textContent = text;
  });
  if (navigator.doNotTrack === "1" || window.doNotTrack === "1" || navigator.globalPrivacyControl === true) {
    show("已停用");
    return;
  }

  window.adamBlogVisitSent = true;
  show("加载中");
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 8000);
  // 只发送公开固定地址，不发送查询参数、来源页或 Cookie；失败不重试，以免重复计数。
  fetch("https://cdn.busuanzi.cc/api.php", {
    method: "POST",
    body: JSON.stringify({url: page.href, referrer: ""}),
    credentials: "omit",
    referrerPolicy: "no-referrer",
    cache: "no-store",
    signal: controller.signal,
  }).then(response => {
    if (!response.ok) throw new Error("Visit counter unavailable");
    return response.json();
  }).then(data => {
    const counts = counters.map(counter => {
      const value = data?.[counter.dataset.viewCount];
      const number = typeof value === "number" ? value : typeof value === "string" && /^\d+$/.test(value) ? Number(value) : NaN;
      if (!Number.isSafeInteger(number) || number < 0) throw new Error("Invalid visit count");
      return number;
    });
    counters.forEach((counter, index) => {
      counter.querySelector("[data-count-value]").textContent = `${counts[index].toLocaleString("zh-CN")} 次`;
    });
  }).catch(() => show("暂不可用")).finally(() => clearTimeout(timer));
})();
