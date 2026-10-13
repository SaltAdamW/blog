((root) => {
  function createConfig(settings, posts) {
    if (!Array.isArray(posts) || !posts.length) {
      throw new Error("The article inventory is empty or invalid.");
    }
    const names = new Set();
    const sources = new Set();
    const files = posts.map((post) => {
      const { slug, source, title } = post;
      if (typeof slug !== "string" || !/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(slug) || names.has(slug)) {
        throw new Error("The article inventory contains an invalid or duplicate slug.");
      }
      if (typeof source !== "string" || !/^[a-zA-Z0-9][a-zA-Z0-9_-]*\.md$/.test(source) || sources.has(source)) {
        throw new Error("The article inventory contains an invalid or duplicate Markdown path.");
      }
      if (typeof title !== "string" || !title.trim() || /[\r\n]/.test(title)) {
        throw new Error("The article inventory contains an invalid title.");
      }
      names.add(slug);
      sources.add(source);
      const escapedTitle = title.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      return {
        name: slug,
        label: title,
        file: source,
        format: "raw",
        preview_path: `posts/${slug}/`,
        fields: [{
          name: "body",
          label: "正文",
          widget: "markdown",
          modes: ["raw"],
          sanitize_preview: true,
          pattern: [`^# ${escapedTitle}\\r?\\n`, "请保留与文章清单一致的一级标题。"],
        }],
      };
    });
    return {
      ...settings,
      load_config_file: false,
      collections: [{ name: "articles", label: "文章", publish: false, files }],
    };
  }

  const api = { createConfig };
  root.BlogEditorConfig = api;
  if (typeof module !== "undefined") module.exports = api;
})(globalThis);
