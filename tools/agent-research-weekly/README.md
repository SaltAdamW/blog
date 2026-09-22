# Agent 研究周报

普通动态汇总只告诉读者发生了什么，无法解释哪些研究足以改变 Agent 的设计、训练和验证方式。
本工具多轮开放搜索并获取原文，让 Codex 选题、核读和单独复核事实，再生成中文周报、更新博客并验证线上内容。
Codex 和 DSH 相关的研究都是候选信源，不调用 DSH 作为执行 worker，也不搭建递归多 Agent。

## 交付范围

- 方法框架、实验发现、新发布和工程实践均可入选，不预设每期主题，也不强求每条都有 benchmark。
- 保留当周动态，同时检索 Agent 的历史场景经验：任务条件、实际尝试、反馈与调整、结果及迁移边界；成功、失败和未解决案例均可入选。
- RSS/Atom、网站索引、固定页面、Codex 联网搜索、JSON/JSONL 和手动 URL 输入。
- 获取失败会尝试备用链接、Hugging Face 镜像；仍失败时由 Codex 搜索同文档其他入口。
- arXiv 候选优先读取 HTML/PDF 全文，不能把摘要页当成完整技术报告。
- PDF 本地解析、有限页 OCR、完整块索引和按需补读，避免只读报告前半部分。
- 短摘录必须逐字存在于已读原文块；入选发现再经过一次独立的 Codex 事实复核调用。
- SQLite 跨周状态、内容 hash、同周幂等、进程锁、待补证重试、来源与尝试日志。
- 中文 Markdown 报告、旧文单列、重复发现去重、读者反馈和 systemd 用户定时器。

不包括邮件或群消息发送、模型训练和多用户服务。普通 `run` 只生成本地稿；`deliver` 按已授权配置发布到博客。医学相关应用不进入正文、简讯或原文索引。

## 运行

GitHub 源码位于 `SaltAdamW/blog` 的 `tools/agent-research-weekly/`，代码 Release 使用 `agent-weekly-vVERSION` 标签。
下载包只含源码和示例配置，不含本机任务、草稿或认证。首次使用先在工具目录执行 `cp config.example.json config.json`，再按环境修改配置；示例默认关闭自动发布。
可用 `python3 release/package.py --output OUTPUT_DIR` 生成源码白名单包及校验文件，不会打包本机 `config.json`、`var/` 或草稿目录。

要求 Linux/POSIX、Python 3.11+、已配置并登录的 Codex CLI。周报 Python 代码只用标准库；长文另需 `requirements-article.txt` 中的 Markdown 解析器，与博客版本一致。
PDF 使用 `pdftotext`；扫描 PDF 可选使用 `pdftoppm` 和 `tesseract`。无需安装整个 Python 工程即可运行：

```bash
cd /root/agent-research-weekly
python3 -m research_weekly doctor
python3 -m research_weekly doctor --smoke
python3 -m research_weekly init
python3 -m research_weekly run --no-discover --limit 3
```

`--smoke` 是真实模型调用，会消耗额度。`init` 幂等加入三份参照材料；不等于把它们自动选入周报。
`run --no-discover` 适合用固定材料校准标准。日常完整运行：

```bash
python3 -m research_weekly deliver
python3 -m research_weekly status
```

相同日期窗口、预算、检索配置和编辑规则已有报告时默认复用原稿；发布失败恢复同一提交，不重新生成一篇。单篇或无发现模式的样例不会阻止完整周报运行。
需要手动追加或恢复时用 `run --force`；已报道的发现仍去重。
`partial` 报告可以包含已核验内容，但会列出信源失败、待补证和预算内未处理完的候选，不能当作全网完整覆盖。

## 信源与覆盖

默认不配置固定入口。每次至少执行三轮：开放发现、沿项目与作者扩展、换词查漏；最多五轮。三轮后连续两轮成功且无新增候选、无待执行追查时可结束。新模型、概率决策、路由、浏览器操作等材料不必在标题中自称 Agent 或 harness。

每轮传递实际查询、失败备注、候选去向和待执行追查，按官方原始发布、代码实现、独立实测寻找互补证据；已查但未找到也保留缺口。查过不等于证据完整，同项目不等于重复。发现与精读仍分阶段运行，本轮精读中新提取的链接不会自动触发额外搜索。
除 Codex 内置搜索，每轮宿主通过 agent-reach 的 Exa 路由执行最多三条查询：一条轮换开放方向，一条延续追查（首轮用另一开放方向），一条历史场景经验查询。历史经验只限制截止日，不限起始日；方向由 `discovery.experience_topics` 轮换，设为空列表可关闭该检索与自动旧文准入，外部查询恢复最多两条。长文不额外追加这条查询，仍使用自身不限当周的研究方向。不配置站点白名单，所有结果仍须抓原文和审稿。需要 PATH 中的 `mcporter` 及已配置的 `exa.web_search_exa`；可用 `discovery.external_search=false` 显式关闭外部入口，内置搜索仍按配置寻找历史经验。失败记为覆盖缺口，不静默当作零结果。
外部入口固定、只读、不经 shell，单次最多 35 秒且计入整轮时间预算；模型仍不能调用本机 shell、MCP 或插件。原始搜索响应保存在运行目录的 `codex/external-*.json`，给模型的长摘要会注明截断。
发现提示将每轮内置查询限制在 4 至 8 条，实际次数仍以日志为准。若模型只交回过程说明而没有候选，程序额外调用一次无工具抽取，只能从已经留存的 Exa URL 中恢复线索，且该轮仍标为 `partial`。这不会把搜索摘要升级成可发布证据。

每轮最多返回 40 个候选，默认一次最多精读 24 篇、收录 24 条。这是运行预算，不是篇数要求。
96 次模型调用和 7200 秒是整轮预算，不保证读满 24 篇，也不是费用上限。
本轮发现的候选先按域名轮转进入最多 100 条的预选池，余位再填历史积压，避免新线索被旧队列挤出。Codex 在公告、实现、实测及负结果间分配精读预算；域名多样性不代表证据独立性，也不保证每项都入选。
用户通过 `--origin user-request` 提交的漏收材料优先进入精读，但仍需通过事实、范围和日期核查。已经公开的原文不重复收录。

每轮保存实际工具查询、返回候选、日期与领域过滤原因。审稿和最终去重另记去向；失败源和未处理队列保留，不把没读到写成无价值。
`var/reports/` 是内部审计报告；博客只发布核读通过的自然段、原文链接和公开来源清单，不外传提示、运行日志或原始缓存。
`--no-discover` 或 `--only` 输出明确标注的补充报告，不用这类样例证明本周覆盖范围。

### 历史经验的准入与学习

检索结果用 `track=current/experience` 区分动态与场景经验线索。历史经验候选以
`origin=experience-search` 持久化，不新增数据库结构；标记只说明为何核读，不能代替质量判断。
只有原文支持具体 Agent 任务中的实际尝试或观察，才可按 `category=practice` 入选。
旧公告、泛泛教程、设想和没有经历依据的最佳实践不能借此进入历史经验。
程序检查字段、类型、范围、引用和日期；是否真的有经历、推论是否成立仍由模型核读和事实复核判断，不声称机械校验证明语义准确。

经验复用现有记录：`problem` 保存任务与条件，`mechanism` 保存尝试与反馈，`conclusion`
保存实际观察，`implication` 保存有条件的借鉴，`limitations` 保存未知与适用边界。
原文未报告修复结果就明确未知，不把建议写成成功经验，也不要求必须有量化收益。
公开稿给旧案例标注“历史经验，非本周新作”，来源清单保留原日期、选读类型及历史标记；
无核实日期的材料可以留在本地待补证，不能公开为历史经验，未来日期仍被排除。
关闭历史检索不会删除已有候选或稿件；恢复已生成稿件仍按稿件的显式类型与证据校验。
候选从普通来源转入历史经验时重新核读，不沿用普通类型的审稿缓存。发现阶段最多携带最近100条
符合日期范围的既有候选，省略数量记入 `discovery_context_omitted`；全库与已报道记录仍用于去重。

长文从这些具体经验提出问题，并寻找同场景不同做法和反证；不自动创建任务、批准框架或发布。
“学习”指读者和后续研究借鉴经验，不代表训练模型、执行网上流程、自动安装技能或更新长期记忆。

## 加入材料

```bash
python3 -m research_weekly add 'https://example.org/research' \
  --title 'Agent 研究' --origin dsh --focus '关注协作训练和负结果'
python3 -m research_weekly import candidates.jsonl
python3 -m research_weekly fetch CANDIDATE_ID
python3 -m research_weekly run --no-discover --force --only CANDIDATE_ID
```

JSONL 每行一个对象；也接受 JSON 数组。例子中的地址是格式示意，不是内置真实信源。

```json
{"url":"https://example.org/report.pdf","title":"研究标题","origin":"dsh","published_at":"2026-09-17","focus":"多 Agent 对照实验","alternatives":["https://mirror.example.org/report.pdf"]}
```

`origin` 是采集渠道。多个渠道提交相同 URL 会归并，但不会被当作多个独立证据。
发布日期可省略；不要用采集时间填充发布日期。`alternatives` 必须是同一文档的其他全文入口。

## 调度

2026 年 9 月 18 日在原运行主机安装并启用了下面两组定时器，并通过用户服务执行过一次补稿发布。该历史安装记录不代表下载者的机器已安装、已触发或已上线；当前状态应以 `systemctl --user` 和发布台账为准。

默认每周一 09:00（Asia/Shanghai）生成周报；每日 21:30 先恢复未完成的发布，再重试已到期的待补证候选。
周一任务覆盖此前完整七天，不收录尚未结束的当天；只核实搜索日期不能代替原文发布日期。周报服务执行 `deliver`，等待 GitHub Pages 部署和线上文件回读结束。没有到期任务时，重试入口不会调用模型。配置生成和启用分开：

```bash
python3 -m research_weekly schedule
systemd-analyze --user verify var/schedule/*.service var/schedule/*.timer
# 以下命令才会启用周期性模型调用：
python3 -m research_weekly schedule --install
systemctl --user list-timers 'agent-research-weekly*'
journalctl --user -u agent-research-weekly.service
```

需要机器在线且用户 systemd manager 持续运行。用户退出后是否继续执行取决于该主机的 linger 配置；
工具不修改系统级 linger 或提权设置。`Persistent=true` 会补触发关机期间错过的计时器事件。
修改 `config.json` 的 `schedule` 后重新生成并审查；安装命令拒绝覆盖内容不同的已有 unit。

停用，不删除报告和证据：

```bash
systemctl --user disable --now agent-research-weekly.timer agent-research-weekly-retry.timer
```

## 编辑与反馈

### 发布前逐轮追问

定时周报在新推送前，由 Python 宿主分别调用追问者和回答者，按问题、替代方案、机制、数据和适用边界完成十轮真实问答。每轮传递上一轮回答，而不是让一次调用编造整份对话。高、中优先级问题逐项深化，最多做一次局部返修，再独立复核事实和解释。任何阶段失败都不能推送；原文核读和事实校验仍保留。

`deliver`、每日 `retry` 和手动 `publish` 共用这项门禁。也可以单独审稿，不发布：

```bash
python3 -m research_weekly review var/runs/RUN_ID/bundle.json
```

记录保存在该运行的 `questioning/INPUT_SHA256/`：`input.json` 为冻结原稿，`steps/` 为逐步响应和请求哈希，`qa-record.md`、`deepening-book.md`、`core-summary.md` 为研讨材料，`reviewed-bundle.json` 为返修后的待发布稿。原 `bundle.json` 不被覆盖。上述内容均留在私有运行目录，不随博客或代码包上传。

深化结果中的空 `replacement` 表示建议删除原段，记录中会明确标注，不代表省略整条深化。问题、候选方案、取舍、推荐、验证和下一步仍须完整；实际删改还要经过正文格式校验及事实、解释复核。

超时可重试并续跑已完成步骤；明确未通过的复核不会反复重新抽样。需要补证或人工返修时，用新的运行稿重新审查。修改原稿、证据、已完成记录或报告会使对应校验失效，不能沿用旧的发布许可。调用和上下文预算不是质量证明。

新增独立预算 `questioning_review.run_seconds=3600`、`max_calls=96`，不增加已有发现与精读的预算，不提供关闭门禁的配置。单期最多24条；宿主分配覆盖对象。涉及多个条目时，回答分别绑定条目 ID，并为每条提供该次实际读取材料中的引用或明确缺口，不能仅凭问题提到过条目就算覆盖；最终仍逐条核对事实。

预算内，追问者仍读取全文概览；超限时明确提供完整目录和本轮条目的完整正文，其他条目由对应轮次覆盖。回答者按问题读取最多三个条目的引用及相邻原文；必要时省略重复编辑元信息和未引用的邻接块，保留全部事实和日期引用块。既往问答中的引用可只传原文块定位，完整引文仍保存在逐轮记录中，问题、回答和缺口不缩写。

跟进仍超限时，可只携带最近两轮完整问答，并列出更早的问答编号；问题提炼（`issues`）仍全量读取问答。解释复查可按预算分批通读全部条目的完整正文，再汇总判断；所有批次及最终复查均须通过。单条必需上下文仍超预算时停止；其他步骤在上述调整后仍超限也停止，绝不截断正文。

服务超时相应增加至 13500 秒；更新已有服务时应先核对差异和活动状态，再安装新的受审 unit，不修改定时触发时间。已推送的旧版本仅恢复线上回读与后续历史登记；未推送且没有追问记录的旧提交转为 `needs_revision` 隔离，禁止恢复推送，需以新运行重新审稿。

内容复核不通过时，发布台账进入 `needs_revision`，定时恢复会跳过它，避免阻塞下一期。尚无已登记 commit，且 worktree 中也没有未登记提交的草稿，允许显式修改后重审，不限于 `needs_revision` 状态；旧版追问输入和问题记录保留，已经生成提交的稿件继续冻结。线上回读已通过但历史登记未完成时，`verified_pending_history` 可继续补登历史，不重复推送。网络或模型调用中断仍保留可续跑的状态。

此编排适用于定时周报及其补稿。独立长文入口保留现有框架、成稿确认，本版未把双角色编排接入其自动调用，不能把周报验收扩大为长文验收。

编辑规则在 `editorial.md`，预算、信源、主题、时区在 `config.json`。模型默认继承本机 Codex 配置，
可设置 `codex.model`，但不会自动换供应商。阅读报告后按发现 ID 反馈：

```bash
python3 -m research_weekly feedback FINDING_ID useful '对上下文隔离的边界解释有用'
python3 -m research_weekly feedback FINDING_ID correction '应区分同时间预算与同费用预算'
python3 -m research_weekly rejudge CANDIDATE_ID
```

最近 30 条反馈会进入后续审稿提示。不会自动修改编辑规则或模型权重。
原文 hash 与编辑规则 hash 均不变时复用审稿结果；规则变化会使旧审稿缓存失效，也可用 `rejudge` 手动重新审稿。

## 发布与恢复

`publication` 配置固定授权仓库、分支和站点，模型不能选择发布目标。博客检出必须干净且与远端一致；工具从远端提交创建独立工作树，只提交本期稿件、来源清单和生成页面。构建与站点测试通过后普通快进推送，不强推、不覆盖用户改动。

```bash
python3 -m research_weekly run --start 2026-09-11 --end 2026-09-17 --limit 4
python3 -m research_weekly publish var/runs/RUN_ID/bundle.json
python3 -m research_weekly deliver
```

`publish` 会等待对应提交的 Pages 成功，并对线上页面、原稿、来源清单、首页、归档、RSS、字体、样式与脚本逐项比对哈希。`deliver` 只在回读成功后将新增条目记入已报道历史；普通 `run` 的历史仍表示本地审计报告生成，并不代表网站发布。推送后失败再次执行相同命令，复用已生成提交恢复回读；远端出现无关提交时停止并保留现场。已完成发布重复执行不新增提交。

已有一期只追加未收录原文，原有段落保持不变。指定窗口必须与已有一期相同；定向补稿需要显式 `publish --allow-supplement`，不能用补稿代替完整检索验收。

失败详情见 `var/publications/RUN_ID/status.json`，构建输出见同目录 `checks.json`，提交差异见 `review.diff`。调度失败通过 systemd 的失败状态和 journal 查看，未配置邮件或群通知。

## 证据与恢复

所有运行数据默认在 `.gitignore` 排除的 `var/` 中，私有文件默认权限为 0600：

```text
var/state.sqlite3                  候选、审稿、已报道发现、反馈、运行状态
var/evidence/<sha256>/source.*     网页/PDF 快照
var/evidence/<sha256>/evidence.json 正文块、页号、获取路径和尝试记录
var/runs/<run-id>/                 每次模型提示、JSONL 事件、结果、失败及最终 manifest
var/runs/<run-id>/discovery-round-*.json 实际查询、候选与轮次结果
var/runs/<run-id>/bundle.json       核读后的待发布正文及本地证据，不公开整个文件
var/publications/<run-id>/status.json 提交、部署和线上回读状态
var/reports/<run-id>.md            不覆盖的历史报告
var/reports/latest.md              最近一次报告（重试可能是补充报告）
var/reports/latest-weekly.md       最近一次执行发现流程的周报
var/reports/latest-supplement.md   最近一次限定范围的补充报告
```

抓取失败不会被标记为低质量或删除；待补证记录至少冷却 `fetch.retry_hours` 后重试。
重试保留上一轮失败或主编补证要求，用于定向检查；不会把这些模型意见当作原文证据。
进程异常退出后系统锁自动释放，下一次运行把旧 `running` 标记为 `interrupted`，不会擅自重放外部发布。
模型失联、输出不合法或引用校验失败同样保留候选和原因。审稿有效但最终选题失败时，不记录为已发布。

获取路径按单路径时限、尝试次数、最大路由数和最大字节数限制。模型按调用次数、单次时间、上下文字符和输出大小限制。
这些是运行预算，**不是准确的费用上限**。实际 token 用量写入报告；供应商计费以其账单为准。
预算耗尽不意味着内容无价值；未处理候选保留到下一轮。

## 安全与限制

- 只读取公开 HTTP(S) 地址。DNS 固定至已验证的公网地址，每次重定向重新校验；拒绝本地/内网、非常规端口和 URL 凭据。
- 不继承网页 Cookie、HTTP 代理或任意鉴权头。镜像默认仅用于公开 Hugging Face 资料；Jina 需明确开启。
- Codex 审稿禁用 shell、MCP、插件、hooks、浏览器控制和子 Agent。仅发现/补证调用允许 web search。
  双角色追问由 Python 宿主交替调用独立模型步骤，不改变上述模型工具权限。
  不使用 `--dangerously-bypass-approvals-and-sandbox`。已有认证只供 Codex 使用，不复制到本仓库。
- 公开文章会发送给本机 Codex 所配置的模型路由。不要向本工具导入机密或需登录的资料。
- 文本块匹配及另一轮模型复核不能证明语义完全正确，更不能替代独立复现实验。高风险决策仍需人工核查。
- 本地哈希用于发现内容变化和防止版本错配，不提供针对同权限写入者的身份认证。能同时修改本机稿件、缓存和台账的人仍在信任边界内；这不是多租户审稿服务。
- OCR 只处理配置数量的页，报告会披露部分读取及识别风险。正文块索引超过预算时拒绝静默截断。
- 不保证搜索覆盖全网，也不保证第三方镜像与原站在任何时刻完全一致；保留原始/实际获取地址和文件 hash 以供追溯。
- 只有通过发布门禁的正文与公开来源清单发送到已授权博客；原文快照、提示、凭据和运行日志留在本地。版本库不保存凭据。

## 长文 Agent

周报可以发现值得深入的问题，但按摘要直接扩写会丢失原文条件，也无法在会话中断后可靠保留确认和前稿。
长文使用独立入口：搜索与精读交替，按原文外链和具体缺口继续追查；围绕用户选定的问题组织机制解释，不继承周报的时间窗口和多栏目格式。
长文是可选入口；仅使用周报不需要安装长文的 Python 依赖或写作 skill。启用长文前，在独立 Python 环境中运行 `python3 -m pip install -r requirements-article.txt`，安装与博客一致的 CommonMark 解析器。

解释标准还依赖用户显式安装的本机 `deep-tech-writing` skill。它不包含在源码发布包中，也不会由上述 pip 命令安装或由本工具自动下载。请从可信来源取得并审查该 skill，安装到当前用户的 `~/.codex/skills/deep-tech-writing/`，其中必须有 `SKILL.md`、`references/style-reference.md`、`references/draft.md` 和 `references/review.md` 四个文件。`start --skill /absolute/path/to/deep-tech-writing` 可以指定其他目录；`topics` 仍使用默认目录。新建文章会冻结这些规则，后续 `resume` 使用已保存的版本。

`python3 -m research_weekly doctor` 的 `optional_dependencies.article` 展示解析器是否可发现、默认 skill 路径和缺失文件；不导入长文模块、不读取规则内容、不调用模型。缺少这些可选依赖不会阻止周报运行；文件存在也不代表长文流程已验收。`doctor --smoke` 仍会执行显式请求的真实模型调用。

```bash
python3 -m research_weekly.article --help
python3 -m research_weekly.article topics --issue weekly-agent-research-2026-09-17
python3 -m research_weekly.article start ARTICLE_ID --topic '用户选定的主问题' --source 'ORIGINAL_URL'
python3 -m research_weekly.article resume ARTICLE_ID
python3 -m research_weekly.article status ARTICLE_ID
```

`start` 仅登记；`topics` 调用模型给出候选，但不替用户选择、不创建文章。`resume` 先研究并写框架，在 `awaiting_outline_approval` 停下。
`status` 返回目录、Markdown 路径和内容哈希。展示文件并收到用户对当前版本的明确确认后才能执行：

```bash
python3 -m research_weekly.article approve-outline ARTICLE_ID --hash OUTLINE_SHA256 --note '用户确认原话'
python3 -m research_weekly.article resume ARTICLE_ID
python3 -m research_weekly.article approve-draft ARTICLE_ID --hash DRAFT_SHA256 --note '用户确认原话'
python3 -m research_weekly.article export ARTICLE_ID --hash DRAFT_SHA256
```

框架确认后分节写作，逐节核查事实、通读审查解释；通过后停在 `awaiting_draft_approval`。成稿确认不等于授权上线，用户另行要求发布时再显式执行
`python3 -m research_weekly.article publish ARTICLE_ID --hash DRAFT_SHA256 --confirm`。
发布使用隔离工作树、当前博客构建与测试、普通推送、对应 Pages 部署与线上哈希回读；失败恢复同一提交。普通 `resume` 不会发布，也未接入任何定时任务。

补证和返修先登记，再 `resume`：

```bash
python3 -m research_weekly.article add-source ARTICLE_ID 'ORIGINAL_URL'
python3 -m research_weekly.article research ARTICLE_ID --question '需要补证的具体问题' --rounds 1 --max-sources 12
python3 -m research_weekly.article revise ARTICLE_ID --section SECTION_ID --feedback '只修该节的具体问题'
python3 -m research_weekly.article revise ARTICLE_ID --feedback '调整主问题或章节关系'
```

局部返修保留其他章节与框架确认；结构返修重新确认框架。补充原文会撤销成稿确认、重新审稿；阅读预算已满时先用 `research --max-sources` 扩充。
默认3轮、8篇原文、2轮自动返修；创建时可调1至5轮、1至20篇、0至3轮返修，补查累计最多10轮。每次运行另受配置中的调用次数、时长和上下文限制，不代表累计费用上限。
`--no-search` 只读指定公开原作。当前版本不接收私有文件，不自动刷新旧快照；新版本调研另建任务。医学排除沿用本项目要求。

全部状态位于 `var/articles/ARTICLE_ID/`，不写周报 SQLite 历史：`state.json` 记录阶段，`versions/` 保存规则、框架、章节和审稿，`evidence/` 保存原文，`runs/` 保存实际调用。
已确认导出包在 `delivery/HASH/`，只有正文与原文清单；`publication/HASH/` 记录发布状态。编辑规则在新建时冻结，后续修改 skill 不改变已有任务。
每份正文及快照都校验哈希；不能编辑旧文件继续使用旧确认，应通过返修生成新版本。脚本检查和模型审稿不能代替用户对文章的判断。

## 验证

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q research_weekly
python3 -m research_weekly doctor --smoke
ARTICLE_BLOG_CHECKOUT=/root/blog python3 -m unittest discover -s tests -p test_article.py -v
# 以下是真实模型及公开原文试跑，不确认、不发布测试文章：
PYTHONPATH=. python3 tests/smoke_article.py --topic '仅用于验收的主问题' --source 'ORIGINAL_URL'
```

单元测试使用明确标识的模拟 editor/transport，覆盖引用造假、晚页读取、镜像恢复、失败保留、去重、锁、重跑和调度。
真实模型与真实网络结果另在 `var/doctor/` 和 `var/runs/` 留证，不能把模拟测试通过写成真实模型验收。
长文真实试跑记录在 `var/article-smoke/`，从周报生成的候选在 `var/article-topics/`；均不会自动建正式文章或进入博客。
长文发布测试使用临时本地 Git 远端和模拟部署回读；显式设置 `ARTICLE_BLOG_CHECKOUT` 时另在临时克隆中运行真实博客构建、9项站点测试和7项访问统计测试，不改原检出、不联网发布。
