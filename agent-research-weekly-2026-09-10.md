# Research 周报：2026 年 9 月 4 日至 9 月 10 日

本期选读并购尽调、缓存压缩，以及多 Agent 上下文和行为评测。简讯补入托管 harness、调用方身份、工具训练数据与沙箱安全披露。

## Harvey：多 Agent 做不好，先检查谁在分工

原文：[Post-Training RLM Agents for End-to-End M&A Diligence](https://www.harvey.ai/blog/post-training-rlm-agents-for-m-and-a-diligence) · 2026-09-08

并购尽调可能要检查数千份文件。风险线索散落在不同材料里，一份看起来完整的报告，可能只覆盖了容易搜到的部分。Harvey 在自己的 LAB Diligence 合成任务中观察到，基线 Agent 往往选择性地搜索和阅读，大量材料没有进入分析。

Harvey 与 Baseten 把资料室放进可以用代码查询的环境，再由根 Agent 分派范围明确的阅读任务。子 Agent 各读一部分，根 Agent 汇总发现。这样组织之后，值得追问的是：团队表现主要取决于谁读材料，还是谁决定怎么读？

在 30 个留出资料室的模型组合对照中，固定子 Agent、更换根模型时，最好与最差结果平均相差约 38 个百分点；固定根模型、更换子 Agent 时，差距约为 8 个百分点。这个差距提示，在这些配置里，分工者有值得单独优化的空间。

随后，他们固定子 Agent，对 Qwen3.5-122B-A10B 根模型做强化学习。在另一组 50 个留出资料室上，评分条目通过率从 29.9% 升到 63.0%。根 Agent 学到的不只是多派任务，还包括边接收阅读结果、边逐步写报告。训练后子 Agent 调用量也增加了，所以这不是等费用条件下的能力比较。

更多委派层级却没有顺势带来收益。在报告给出的 Qwen 对照中，让子 Agent 使用代码工具并继续委派之后，14 个资料室有 10 个得分下降，平均评分条目通过率降低 19 个百分点，还出现了读过材料却没有交付报告的情况。这里同时改变了子 Agent 的工具能力和委派方式，不能推广成所有递归结构都无效。

对大量材料分析任务，这篇研究给出的排查顺序很实用：先看遗漏了什么、分工是否覆盖这些材料、结果有没有进入最终交付，再决定是否升级阅读模型或增加层级。数字来自作者自建的合成法律任务和评分体系；评分条目通过率也不等于整项尽调任务的成功率。

## DeepSeek：长上下文的成本，还在缓存的存储和恢复里

原文：[DeepSeek-V4.1-Flash 技术报告，固定版本 PDF](https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash/resolve/53e70b17d9acb8e0c423f0e4fe5702f25562b63d/DeepSeek_V41_Tech_Report.pdf) · 模型发布于 2026-09-10

重点阅读：第 9 至 11 页、第 19 至 20 页。发布日期参见[官方发布说明](https://www.deepseek.com/en/news/deepseek-v4-1-flash/)。

Agent 连续调用工具时，输入里会不断累积代码、日志和已有对话。KV cache 保存模型处理上下文后的中间状态，让后续请求有机会复用已经完成的计算。历史越长，这些状态的保存、搬运和恢复也越贵。DeepSeek 这份报告没有只讨论怎样减少注意力计算，还拆开了缓存的不同生命周期。

用于访问全局上下文的缓存，可以在一些层之间共享。CSA2 把“复用 KV”和“复用检索到的位置”分开：有的层生成新缓存和索引，有的层共享缓存但重新选位置，有的层连已选位置也复用。这样既减少重复存储，又不强迫所有层永远读取同一批历史。配合 FP4 缓存，作者报告全局 KV 降到每 token 约 890 字节，约为 DeepSeek-V4-Flash 对应部分的四分之一。

局部滑动窗口的缓存有另一种访问规律。它在活跃会话中可能很快被复用，但没有必要与全局前缀一起长时间保存。报告中的部署把这部分状态放入短期内存池；如果全局前缀仍在、局部状态已经丢失，就重放末尾一个窗口，近似恢复局部状态，而不是完整重算所有跨层依赖。

“近似”是这项取舍的关键。重建结果并非与完整前向计算数学等价，作者在所测设置中观察到质量损失很小，但仍把缓存恢复边界列为后续压力测试对象。用少量重新计算换长期存储空间，需要接受并验证这类误差。

移除持久缓存中的局部窗口状态，再叠加全局 KV 压缩，报告给出的持久 KV 占用约为前代的八分之一。这里有两个不同的比较：全局 KV 约四分之一，持久 KV 约八分之一。它们都不是整台机器总内存或推理账单的缩减比例。对长任务服务，这篇报告提供的观察角度是：哪些状态值得长期保留，哪些只需短期保留，丢失之后恢复要付出什么代价。

## LangChain：子 Agent 要不要继承主管的上下文

原文：[Organizing Context in a Multi-Agent Harness](https://www.langchain.com/blog/organizing-context-in-a-multi-agent-harness) · 2026-09-08

主管已经查完日志、定位到函数，再让一个空上下文的 worker 去实现修复，可能把调查又做一遍。但把全部对话交给 reviewer，也可能让它沿着主管的判断检查，遗漏其他解释。子 Agent 的工作不同，合适的上下文起点也不同。

Deep Agents 将这一区别显式做成两种模式。`isolated` 只接收任务说明，从新上下文开始；`fork` 接收主管的状态与会话历史，移除末尾的委派工具调用，再加入自己的任务。完成后，主管接收最终结果，不把子 Agent 的全部中间过程搬回来。

继续已有调查的实现工作，可以利用继承的证据，减少重复读取；需要独立判断的审查，或能独立研究的问题，则未必需要主管历史。继承上下文也有缓存复用的机会，但不应预设一定更便宜：要看 worker 是否真需要那些材料，以及前缀缓存是否命中。文章提供的是分工时选择信息边界的方法，没有证明某种模式对所有任务更好。

## Google：总分降了，怎样找到 harness 的具体退步

原文：[The Anatomy of Harness Engineering: How to Evaluate, Iterate, and Guard AI Coding Agents](https://developers.googleblog.com/the-anatomy-of-harness-engineering-how-to-evaluate-iterate-and-guard-ai-coding-agents/) · 2026-09-09

一次端到端评测下降，可能是模型误解了要求，也可能只是修改构建文件后忘了运行验证。只有总分，开发者很难知道应该改提示、工具定义还是流程。

Google 的工程文章建议从真实失败中提取可观察行为：面对含糊要求是否澄清，修改后是否执行相应检查，文档是否给出仓库原始链接。断言关注工具调用、文件变化和执行证据，让一次流程修改能够对应到具体回归，而不是只看最后那段回答写得像不像完成。

简单任务可以严格检查必要动作，复杂任务则不能锁死工具顺序，否则另一条正确路径也会被判失败。模型执行有随机性，文章建议批量观察通过率，而非用单次运行决定成败。行为评测负责定位和防回归，端到端评测仍负责确认任务是否完成；两者不能互相替代。

## 本周简讯

### OpenAI Agents API：托管 harness，与执行环境分开选择

原文：[Introducing the Agents API](https://openai.com/index/introducing-the-agents-api/) · 2026-09-10，public beta

团队搭建长任务 Agent 时，除了工具和业务规则，还得维护上下文压缩、工具发现与子 Agent 协调。Agents API 将 Codex harness 作为托管服务提供，包含这些执行机制；代码运行环境可以选 OpenAI 托管沙箱、自有基础设施或合作方环境。

这让“谁维护 Agent 循环”和“任务在哪里运行”成为两个选择，但并不自动解决业务授权、工具质量和任务验收。公告中的客户提速与降本数字是客户陈述，不是统一条件下的对照实验；本条仅介绍发布范围，不据此判断迁移收益。

### LangChain Connections：同一个 Agent，替谁操作

原文：[Connections: Managed Credentials and Per-Caller Identity for Managed Deep Agents](https://www.langchain.com/blog/connections-managed-credentials-and-per-caller-identity-for-managed-deep-agents) · 2026-09-09

多人共用一个 Agent 时，共享服务账号会掩盖实际调用者，也可能让查询看到本不属于该用户的数据。Connections 将凭据所有者与凭据类型分开：Agent 或用户可以各自持有静态秘密或 OAuth 授权，工具运行时再按调用者解析凭据。缺少授权时暂停任务，完成授权后继续。

凭据由工作区管理，轮换无需重新打包项目；按用户解析也使查询范围和写操作身份随调用者变化。不过，用户身份不等于每项动作已经获批，业务审批与最小权限仍需单独设计。公告对应 Managed Deep Agents 的预发布能力，不能当作所有 LangChain 部署的默认行为。

### ToolGrad：先跑通工具链，再生成训练问题

原文：[ToolGrad: Efficient Tool-Use Dataset Generation with Textual “Gradients”](https://research.google/blog/toolgrad-efficient-tool-use-dataset-generation-with-textual-gradients/) · 2026-09-10，研究介绍

先编一个用户问题，再搜索能回答它的 API 路径，会产生大量失败尝试。ToolGrad 反过来扩展可执行的工具链：提出候选调用、并行执行、依据执行报告选择下一步，再更新与这条链对应的用户问题和回答。训练样本先有实际可用的执行依据，再补自然语言任务。

Google 的介绍还报告了用这批数据微调模型后的函数调用评测。需要分清两个环节：数据生成阶段的执行通过率，不等于模型部署后面对真实请求的完成率；合成问题能覆盖多少真实需求，也仍需另外检查。本周是博客介绍，不据此认定方法首次提出于本周。

### DSH 安全披露：沙箱内的任务不应能改自己的权限

原文：[CVE-2026-82533: DeepSeek Harness AI Agent Sandbox Escape](https://www.ox.security/blog/cve-2026-82533-deepseek-harness-ai-agent-sandbox-escape/) · 原始披露为 2026-09-08

OX Research 报告的漏洞发生在文件限制与本地管理 API 之间：任务虽然受文件写入约束，仍可访问 loopback；管理接口缺少鉴权，并依赖客户端可控制的 `Host` 头判断信任，导致任务能接触修改自身策略的接口。防守上需要检查的不只是文件路径，还包括任务能否访问控制它的管理面。

按披露者的记录，受影响范围为 `0.1.1-rc.2` 及更早，修复版本为 `0.1.2-alpha.1`，修复于 8 月 27 日发布、8 月 30 日复测通过。9 月 16 日的转载不应被当作首次披露，也不能据此声称后续版本仍受同一漏洞影响。此处仅核对披露材料，未运行攻击验证。

## 更多原文

以下仅核对题名与提交日期，尚未精读，不据此转述实验结论。

- 2026-09-04：[Train What You Deploy: Token-Faithful Post-Training of a Production Coding](https://arxiv.org/abs/2609.04678)。训练与部署的一致性；题名按原页面保留。
- 2026-09-09：[AgentAudit: An Open, Extensible Framework for Full-Lifecycle Trust Evaluation of AI Agents](https://arxiv.org/abs/2609.09875)。Agent 全生命周期信任评估。
