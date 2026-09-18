const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const script = fs.readFileSync(path.join(__dirname, "../assets/analytics.js"), "utf8");
const canonical = "https://saltadamw.github.io/blog/posts/example/";

async function run(options = {}) {
  const nodes = ["busuanzi_site_pv", "busuanzi_page_pv"].map(viewCount => ({
    hidden: true,
    dataset: {viewCount},
    value: {textContent: "加载中"},
    querySelector() { return this.value; },
  }));
  const requests = [];
  const timers = new Map();
  const context = vm.createContext({
    document: {
      querySelectorAll: () => nodes,
      querySelector: () => ({href: options.canonical || canonical}),
    },
    location: {href: options.href || `${canonical}index.html?q=private#section-1`},
    navigator: options.navigator || {},
    window: {},
    URL, AbortController,
    setTimeout(callback, delay) { timers.set(1, callback); assert.equal(delay, 8000); return 1; },
    clearTimeout(id) { timers.delete(id); },
    fetch(url, config) {
      requests.push({url, config});
      if (options.timeout) return new Promise((resolve, reject) => config.signal.addEventListener("abort", () => reject(new Error("timeout"))));
      if (options.error) return Promise.reject(new Error("offline"));
      return Promise.resolve({
        ok: options.ok !== false,
        json: async () => {
          if (options.invalidJSON) throw new SyntaxError("invalid JSON");
          return options.data === undefined ? {busuanzi_site_pv: 1234, busuanzi_page_pv: "12"} : options.data;
        },
      });
    },
  });
  vm.runInContext(script, context);
  if (options.twice) vm.runInContext(script, context);
  if (options.timeout) timers.get(1)();
  await new Promise(resolve => setImmediate(resolve));
  return {nodes, requests, timers};
}

test("服务端数字用于展示，同次页面加载仅发送一次，不泄露搜索词或来源", async () => {
  const {nodes, requests, timers} = await run({twice: true});
  assert.equal(requests.length, 1);
  const {url, config} = requests[0];
  assert.equal(url, "https://cdn.busuanzi.cc/api.php");
  assert.equal(config.method, "POST");
  assert.equal(config.credentials, "omit");
  assert.equal(config.referrerPolicy, "no-referrer");
  assert.equal(config.cache, "no-store");
  assert.deepEqual(JSON.parse(config.body), {url: canonical, referrer: ""});
  assert.deepEqual(nodes.map(node => node.value.textContent), ["1,234 次", "12 次"]);
  assert.ok(nodes.every(node => !node.hidden));
  assert.equal(timers.size, 0);
});

test("重新加载会重新请求，固定地址与 index.html 使用相同统计键", async () => {
  const first = await run({href: canonical});
  const second = await run();
  assert.equal(first.requests.length + second.requests.length, 2);
  assert.equal(first.requests[0].config.body, second.requests[0].config.body);
});

test("本地预览、其他域名和非 canonical 页面不计数", async () => {
  for (const href of ["file:///root/blog/index.html", "http://localhost:8000/blog/", "https://example.com/blog/", "https://saltadamw.github.io/other/"]) {
    const {nodes, requests} = await run({href});
    assert.equal(requests.length, 0);
    assert.ok(nodes.every(node => node.hidden));
  }
});

test("正式首页和关于页均可计数", async () => {
  for (const suffix of ["", "about/"]) {
    const url = `https://saltadamw.github.io/blog/${suffix}`;
    const {requests} = await run({href: `${url}index.html`, canonical: url});
    assert.equal(JSON.parse(requests[0].config.body).url, url);
  }
});

test("DNT 与 GPC 禁止发送统计请求", async () => {
  for (const navigator of [{doNotTrack: "1"}, {globalPrivacyControl: true}]) {
    const {nodes, requests} = await run({navigator});
    assert.equal(requests.length, 0);
    assert.ok(nodes.every(node => node.value.textContent === "已停用"));
  }
});

test("网络失败、HTTP 错误、非法 JSON 和超时均不伪造零值也不重试", async () => {
  for (const options of [{error: true}, {ok: false}, {invalidJSON: true}, {timeout: true}]) {
    const {nodes, requests, timers} = await run(options);
    assert.equal(requests.length, 1);
    assert.ok(nodes.every(node => node.value.textContent === "暂不可用"));
    assert.equal(timers.size, 0);
  }
});

test("只接受非负安全整数，拒绝缺失字段、HTML、负数与溢出", async () => {
  for (const value of [undefined, null, true, [5], {}, -1, 1.2, Number.MAX_SAFE_INTEGER + 1, "1e3", "<img src=x onerror=alert(1)>"]) {
    const {nodes} = await run({data: {busuanzi_site_pv: 99, busuanzi_page_pv: value}});
    assert.ok(nodes.every(node => node.value.textContent === "暂不可用"));
  }
  const {nodes} = await run({data: {busuanzi_site_pv: 0, busuanzi_page_pv: 0}});
  assert.ok(nodes.every(node => node.value.textContent === "0 次"));
});
