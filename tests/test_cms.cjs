const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const { createConfig } = require("../admin/editor-config.js");

const root = path.resolve(__dirname, "..");
const settings = JSON.parse(fs.readFileSync(path.join(root, "admin/config.json"), "utf8"));
const posts = JSON.parse(fs.readFileSync(path.join(root, "posts.json"), "utf8"));

test("the live inventory exposes every registered article without migrating its source", () => {
  const config = createConfig(settings, posts);
  assert.deepEqual(config.backend, { name: "github", repo: "SaltAdamW/blog", branch: "main", auth_methods: ["token"] });
  assert.equal(config.publish_mode, "editorial_workflow");
  assert.equal(config.load_config_file, false);
  const [collection] = config.collections;
  assert.equal(collection.publish, false);
  assert.deepEqual(collection.files.map((file) => file.file), posts.map((post) => post.source));
  for (const [index, file] of collection.files.entries()) {
    assert.equal(file.format, "raw");
    assert.equal(file.name, posts[index].slug);
    assert.equal(file.preview_path, `posts/${posts[index].slug}/`);
    const [field] = file.fields;
    assert.deepEqual(field.modes, ["raw"]);
    assert.equal(field.sanitize_preview, true);
    assert.match(fs.readFileSync(path.join(root, file.file), "utf8"), new RegExp(field.pattern[0]));
    assert.doesNotMatch("# Another title\n\nBody", new RegExp(field.pattern[0]));
  }
});

test("a newly published article is editable without regenerating CMS configuration", () => {
  const newPost = { slug: "next-article", source: "next-article.md", title: "Next (article) [v2]." };
  const original = JSON.stringify(settings);
  const config = createConfig(settings, [...posts, newPost]);
  const file = config.collections[0].files.at(-1);
  assert.equal(file.file, newPost.source);
  assert.match(`# ${newPost.title}\n\nBody`, new RegExp(file.fields[0].pattern[0]));
  assert.doesNotMatch("# Next article v2.\n\nBody", new RegExp(file.fields[0].pattern[0]));
  assert.equal(JSON.stringify(settings), original);
});

test("invalid, duplicate, and escaping article identities fail before initializing the editor", () => {
  for (const inventory of [
    [], null, {}, [posts[0], posts[0]],
    [{ ...posts[0], source: "../secret.md" }],
    [{ ...posts[0], source: "https://example.com/article.md" }],
    [{ ...posts[0], source: "tools/private.md" }],
    [{ ...posts[0], slug: "../outside" }],
    [{ ...posts[0], title: "Title\nMore" }],
    [posts[0], { ...posts[0], slug: "another-slug" }],
  ]) {
    assert.throws(() => createConfig(settings, inventory));
  }
});

test("bootstrap pins the downloaded editor and never embeds or manages credentials", () => {
  const bootstrap = fs.readFileSync(path.join(root, "admin/editor.js"), "utf8");
  assert.match(bootstrap, /@sveltia\/cms@0\.235\.1\//);
  assert.match(bootstrap, /integrity: "sha384-[A-Za-z0-9+/=]+"/);
  assert.doesNotMatch(bootstrap, /@latest|localStorage|github_pat_|ghp_/);
  assert.match(bootstrap, /cache: "no-store"/);
  const html = fs.readFileSync(path.join(root, "admin/index.html"), "utf8");
  assert.match(html, /noindex, nofollow/);
  for (const [, asset] of html.matchAll(/(?:src|href)="([^"]+)"/g)) {
    assert.equal(fs.existsSync(path.resolve(root, "admin", asset)), true, asset);
  }
});

async function runBootstrap({ secure = true, status = 200, downloadFails = false } = {}) {
  const elements = Object.fromEntries(["editor-loading", "editor-status", "editor-retry"].map((id) => [id, {
    hidden: id === "editor-retry",
    addEventListener(_event, handler) { this.handler = handler; },
  }]));
  const calls = [];
  const scripts = [];
  const initialized = [];
  const sandbox = {
    URL,
    BlogEditorConfig: { createConfig },
    window: { isSecureContext: secure, location: { href: "https://example.com/blog/admin/" } },
    document: {
      getElementById: (id) => elements[id],
      createElement: () => ({ remove() { this.removed = true; } }),
      head: {
        append(script) {
          scripts.push(script);
          if (downloadFails) script.onerror();
          else {
            sandbox.window.CMS = sandbox.CMS = { init: async (input) => initialized.push(input) };
            script.onload();
          }
        },
      },
    },
    fetch: async (url, options) => {
      calls.push({ url: url.href, options });
      return { ok: status === 200, status, json: async () => url.pathname.endsWith("posts.json") ? posts : settings };
    },
  };
  vm.runInNewContext(fs.readFileSync(path.join(root, "admin/editor.js"), "utf8"), sandbox);
  await new Promise((resolve) => setImmediate(resolve));
  return { elements, calls, scripts, initialized };
}

test("bootstrap loads the inventory beneath a project subpath and initializes the real configuration", async () => {
  const result = await runBootstrap();
  assert.deepEqual(result.calls.map((call) => call.url), [
    "https://example.com/blog/admin/config.json", "https://example.com/blog/posts.json",
  ]);
  assert.equal(result.initialized[0].config.collections[0].files.length, posts.length);
  assert.equal(result.elements["editor-loading"].hidden, true);
  assert.equal(result.elements["editor-retry"].hidden, true);
});

test("configuration, CDN, and insecure-context failures retain an actionable loading state", async () => {
  for (const options of [{ status: 404 }, { downloadFails: true }, { secure: false }]) {
    const result = await runBootstrap(options);
    assert.equal(result.initialized.length, 0);
    assert.equal(result.elements["editor-retry"].hidden, false);
    assert.notEqual(result.elements["editor-loading"].hidden, true);
    assert.match(result.elements["editor-status"].textContent, /could not|HTTPS/);
  }
});
