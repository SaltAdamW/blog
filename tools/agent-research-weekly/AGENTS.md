# 周报与长文协作约定

## 默认审稿

用户于 2026-09-22 明确要求：后续默认用 `strict-interviewer` 的追问方式 review。
处理周报、长文、正文返修或发布前审稿时，先读 [追问审稿约定](review-policy.md)，再读取已安装的 `strict-interviewer` skill。
该要求适用于内容审稿，不把代码检查替换成材料问答，也不授权本轮发布。

## 执行边界

保留 `editorial.md`、`deep-tech-writing` 及既有事实核查要求；追问补充技术解释与证据判断，不替代它们。
定时周报的 `deliver`、`retry` 和手动 `publish` 共用 `questioning.py` 发布前门禁：十轮真实问答、逐项深化、有限返修、事实与解释复查。没有通过记录不得新推送；不得用单次生成的虚构问答替代。
恢复旧版本已推送提交时仅允许完成回读；没有追问记录的旧版本待推送提交必须停止。已发表文章不因安装新版而自动改写。
长文的独立自动宿主尚未接入此编排，仍保留人工框架和成稿确认；不能把定时周报的接入声称为长文全流程也已自动化。

## 代码发布

代码在 `SaltAdamW/blog` 的 `tools/agent-research-weekly/` 独立发布，版本见 `VERSION`。
发布前运行 `python3 -m unittest discover -s tests -v` 和 `python3 -m compileall -q research_weekly`，检查打包白名单及解包后的测试。
禁止上传 `var/`、研究缓存、文章草稿、模型提示与响应、运行日志和凭据。公开包使用 `config.example.json`，其中自动发布默认关闭；本机配置不作为发布附件。

保留工作区既有改动，不修改已冻结文章版本或伪造用户确认；未获发布授权时不推送博客。
