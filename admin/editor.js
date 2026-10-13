/* global CMS, BlogEditorConfig */
(() => {
  const bundle = {
    url: "https://cdn.jsdelivr.net/npm/@sveltia/cms@0.235.1/dist/sveltia-cms.js",
    integrity: "sha384-0buNFRS4cnwrZfClZMgBYxOTaefGGJltRCRnsKTgLl+q0sOGWcd8sgJpgwr1oKUl",
  };
  const loading = document.getElementById("editor-loading");
  const status = document.getElementById("editor-status");
  const retry = document.getElementById("editor-retry");
  let pending = false;

  window.CMS_MANUAL_INIT = true;

  function loadBundle() {
    if (window.CMS) return Promise.resolve();
    return new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = bundle.url;
      script.integrity = bundle.integrity;
      script.crossOrigin = "anonymous";
      script.onload = resolve;
      script.onerror = () => {
        script.remove();
        reject(new Error("The editor could not be downloaded. Check your connection and retry."));
      };
      document.head.append(script);
    });
  }

  async function readJSON(path) {
    const response = await fetch(new URL(path, window.location.href), { cache: "no-store" });
    if (!response.ok) throw new Error(`Editor data could not be loaded (${response.status}).`);
    return response.json();
  }

  async function start() {
    if (pending) return;
    pending = true;
    retry.hidden = true;
    status.textContent = "Loading editor...";
    try {
      if (!window.isSecureContext) {
        throw new Error("Open the editor over HTTPS or on localhost, not as a local file.");
      }
      const [settings, posts] = await Promise.all([readJSON("config.json"), readJSON("../posts.json")]);
      const config = BlogEditorConfig.createConfig(settings, posts);
      await loadBundle();
      await CMS.init({ config });
      loading.hidden = true;
    } catch (error) {
      status.textContent = error.message;
      retry.hidden = false;
    } finally {
      pending = false;
    }
  }

  retry.addEventListener("click", start);
  start();
})();
