# Adam‘s blog

在线阅读：<https://saltadamw.github.io/blog/>

## 文章

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
```

直接打开 `index.html` 即可预览全部页面。页面不依赖服务端，也不会向第三方请求字体或脚本。禁用 JavaScript 后，文章列表、归档、正文、目录和来源链接仍可阅读；搜索和主题切换需要 JavaScript。

GitHub Pages 从 `main` 分支根目录发布。正文或清单变化后，重新生成并一并提交首页、归档、关于页、文章页和 RSS。

## 新增文章

1. 新增 Markdown 文件，第一行使用 `# 文章标题`。
2. 在 `posts.json` 追加条目。`slug` 使用不重复的小写英文路径；`title` 必须与 Markdown 一级标题一致；填写 `date`、`category`、`tags`、`description`、`deck`、`word_count` 和 `source`。
3. `source_manifest`、`closing` 和 `toc_labels` 可选。`toc_labels` 省略时使用正文二级标题；日期采用 `YYYY-MM-DD`，首页和归档按日期倒序排列。
4. 运行构建和检查，提交新增正文、清单及所有生成页面。不要给新文章设置 `legacy_home`，该字段仅用于兼容最初的单篇文章首页链接。

修改一级或二级标题后，可运行 `.venv/bin/python tools/fetch-heading-font.py` 更新标题字体子集；此维护步骤会访问 Google Fonts。普通构建和在线阅读不需要该网络请求。

## 第三方资源

- 图标来自 Lucide，许可保留于 `assets/icons/LICENSE`。
- 标题字体为 Noto Serif SC 的页面用字子集，采用 SIL Open Font License，许可保留于 `assets/fonts/OFL.txt`。
- 作者头像来自公开的 GitHub 账号 `SaltAdamW`。
