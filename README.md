# Adam‘s blog

在线阅读：<https://saltadamw.github.io/blog/>

## 文章

### 技术长文

- [Jev：解耦 LLM 的理解和生成](https://saltadamw.github.io/blog/posts/jev-understanding-and-generation/) · [Markdown 原稿](longform-jev-understanding-and-generation.md) · [原文来源](longform-jev-understanding-and-generation.sources.json)

2026 年 9 月 20 日更新：按“是什么、特点、方法、应用、效果”介绍 Jev，展开动态候选读出、CUA 操作循环与证据核查，包含三张机制图和 21 项原文。官方披露与开源方法分开说明，效果来自公开实验报告，未独立复现。

### Research 周报

Research 周报按周独立选编材料，不预设专题。多轮开放搜索后沿项目、作者和引用补扫，内容不限于固定信源入口。每条附原文链接与发布日期，每期分别保存 Markdown 正文和公开来源清单，入口由 `posts.json` 维护。

选题范围不包含医学、医疗、病理、基因及生物医学应用；正文、简讯和原文索引均按此筛选，不为凑篇数补入范围外材料。

| 周期 | 在线阅读 | Markdown 原稿 |
| --- | --- | --- |
| 2026-08-21 至 08-27 | [第一期](https://saltadamw.github.io/blog/posts/weekly-agent-research-2026-08-27/) | [原稿](agent-research-weekly-2026-08-27.md) |
| 2026-08-28 至 09-03 | [第二期](https://saltadamw.github.io/blog/posts/weekly-agent-research-2026-09-03/) | [原稿](agent-research-weekly-2026-09-03.md) |
| 2026-09-04 至 09-10 | [第三期](https://saltadamw.github.io/blog/posts/weekly-agent-research-2026-09-10/) | [原稿](agent-research-weekly-2026-09-10.md) |
| 2026-09-11 至 09-17 | [第四期](https://saltadamw.github.io/blog/posts/weekly-agent-research-2026-09-17/) | [原稿](agent-research-weekly-2026-09-17.md) |

四期于 2026 年 9 月 17 日补编并扩充，列表日期为各期窗口结束日。9 月 18 日在第四期补入 Jev 与共享模型自修改研究，沿用原链接。截至此次更新，共保留 28 条选读或简讯，另附 7 条待精读原文。覆盖 Agent、harness、工具与协议、记忆、训练、评测、安全和应用实践，不声称穷尽当周所有材料。

来源清单使用 `review_status` 与 `reading_scope` 区分已核读的正文段落和仅核对题名、日期的材料。正文中的“更多原文”不作为实验结论依据。9 月 18 日新增两条经过另一次模型调用的事实复核；此前条目保留各自核读范围，所有条目均未进行实验复现。

9 月 18 日在第四期将 MLPerf 与 Jev 浏览器集成两条替换为已确认的图文版本：每条附一张机制图，手机使用单独排版，并保留原文链接、PNG 与可编辑 draw.io 图源。此次改写由作者重新核读并自检，不把此前的独立复核记录当作新正文的独立复核。其他条目与周报链接保持不变。

《开源 Agent 沙箱的设计：如何复用环境，又让任务彼此独立》

从一次任务开始前的准备工作出发，讨论开源沙箱如何保存运行状态、建立独立分支，以及组织存储、回收、预热池与编排。资料核对日期为 2026 年 9 月 17 日。

- `posts.json`：统一文章清单，维护标题、日期、分类、摘要与正文路径。
- `article.md`：现有长文的完整 Markdown 正文，保留原下载地址。
- `source-manifest.json`：29 项一手资料及取材版本。
- `index.html`：博客首页，展示全部文章，支持搜索。
- `posts/<slug>/index.html`：每篇文章的独立阅读页。
- `archive/index.html`：按年份汇总的文章归档。
- `about/index.html`：关于作者。
- `feed.xml`：RSS 订阅。
- `assets/`：样式、交互和本地资源。

## 更新页面

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python build.py
.venv/bin/python -m unittest discover -s tests -v
node --test tests/test_analytics.cjs
```

直接打开 `index.html` 即可预览全部页面。字体和脚本均为本地资源。正式站点会向第三方统计接口发送一次请求；本地预览不计数。禁用 JavaScript 后，文章列表、归档、正文、目录和来源链接仍可阅读；搜索、主题切换和访问统计需要 JavaScript。

## 访问统计

2026 年 9 月 18 日接入不蒜子（`busuanzi.cc`）JSON 接口。首页首屏简介下方显示本站访问量，文章日期旁显示本篇浏览量；其他页面页脚保留本站访问量。数据由服务端保存，并非浏览器本地累加；刷新会增加 PV，不代表独立读者人数，历史访问无法补算。本站数值按域名聚合，同域名下若有其他页面接入同一服务，也会计入。

`assets/analytics.js` 只在正式域名的 `/blog/` 页面运行，按 canonical 地址计数，合并目录链接与 `index.html`，丢弃查询参数、片段和来源页；不发送 Cookie，不加载第三方脚本。统计方仍可见 IP 和浏览器信息。尊重 DNT/GPC；失败或超时显示「暂不可用」，不伪造零值、不自动重试。无 JavaScript、本地预览不显示数字。服务故障、拦截器或禁用追踪会造成漏计，因此数字只用于大致观察，不作为审计或计费依据。

## 发布

GitHub Pages 从 `main` 分支根目录发布。正文或清单变化后，重新生成并一并提交首页、归档、关于页、文章页和 RSS。

本地生成、推送 GitHub 和 Pages 上线是三个不同状态。交付时需确认远端提交与本地一致、对应提交的 Pages 部署成功，并回读线上四期正文、原稿和来源清单；只给出本地文件链接不算发布完成。

## 自动更新

2026 年 9 月 18 日已在运行主机启用 systemd 用户定时器：每周一 09:00（Asia/Shanghai）执行多轮搜索、核读、生成、推送和 Pages 回读，默认覆盖此前七个完整自然日；每日 21:30 恢复未完成的发布，并重试到期的待补证材料。触发时间有最多 60 秒抖动，依赖主机在线。

检索不依赖固定信源表，每次进行 3 至 5 轮开放搜索，默认精读预算为 24 篇。未核实、抓取失败和预算内未处理的候选会保留，不为了凑数发布。每日补证不直接生成一篇新的周报。9 月 18 日已在 systemd 环境实际完成一次补稿发布与线上回读；首次每周定时触发计划为 9 月 21 日，并非已经执行。

## 新增文章

1. 新增 Markdown 文件，第一行使用 `# 文章标题`。
2. 在 `posts.json` 追加条目。`slug` 使用不重复的小写英文路径；`title` 必须与 Markdown 一级标题一致；填写 `date`、`category`、`tags`、`description`、`deck`、`word_count` 和 `source`。
3. `source_manifest`、`closing` 和 `toc_labels` 可选。`toc_labels` 省略时使用正文二级标题；日期采用 `YYYY-MM-DD`，首页和归档按日期倒序排列。
4. 运行构建和检查，提交新增正文、清单及所有生成页面。不要给新文章设置 `legacy_home`，该字段仅用于兼容最初的单篇文章首页链接。

条目机制图在 Markdown 中以独立图片段落引用仓库内 `assets/` 路径。对应 `posts.json` 条目的 `figures` 以此路径为键，登记 `width`、`height`、手机版路径 `mobile` 及其 `mobile_width`、`mobile_height`、可编辑图源 `diagram`、图注 `caption`。构建时生成带尺寸和替代文字的 `<picture>`，原稿仍能在仓库中直接显示图片；未登记图片不自动转换。不把绘图引擎或原文缓存部署到站点。

修改一级或二级标题后，可运行 `.venv/bin/python tools/fetch-heading-font.py` 更新标题字体子集；此维护步骤会访问 Google Fonts。普通构建和在线阅读不需要该网络请求。

## 第三方资源

- 图标来自 Lucide，许可保留于 `assets/icons/LICENSE`。
- 标题字体为 Noto Serif SC 的页面用字子集，采用 SIL Open Font License，许可保留于 `assets/fonts/OFL.txt`。
- 作者头像来自公开的 GitHub 账号 `SaltAdamW`。
