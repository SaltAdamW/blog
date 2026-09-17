# Adam‘s blog

在线阅读：<https://saltadamw.github.io/blog/>

## 文章

《开源 Agent 沙箱的设计：如何复用环境，又让任务彼此独立》

从一次任务开始前的准备工作出发，讨论开源沙箱如何保存运行状态、建立独立分支，以及组织存储、回收、预热池与编排。资料核对日期为 2026 年 9 月 17 日。

- `article.md`：完整 Markdown 正文。
- `source-manifest.json`：29 项一手资料及取材版本。
- `index.html`：静态阅读页面。
- `assets/`：样式、交互和本地资源。

## 更新页面

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python build.py
```

直接打开 `index.html` 即可预览。页面不依赖服务端，也不会向第三方请求字体或脚本。禁用 JavaScript 后，正文、目录和来源链接仍可阅读。

GitHub Pages 从 `main` 分支根目录发布。正文变化后，重新生成并一并提交 `index.html`。

修改一级或二级标题后，可运行 `.venv/bin/python tools/fetch-heading-font.py` 更新标题字体子集；此维护步骤会访问 Google Fonts。普通构建和在线阅读不需要该网络请求。

## 第三方资源

- 图标来自 Lucide，许可保留于 `assets/icons/LICENSE`。
- 标题字体为 Noto Serif SC 的页面用字子集，采用 SIL Open Font License，许可保留于 `assets/fonts/OFL.txt`。
- 作者头像来自公开的 GitHub 账号 `SaltAdamW`。
