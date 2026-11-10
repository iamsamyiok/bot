# bot — 产品需求文档 (PRD v1.0)

> **Your self-hosted personal AI butler**
> 融合 Meta Muse 与 Grok Bot 的核心设计理念, 跑在你自己电脑上的个人 AI 智能体
> 版本: v1.0 | 日期: 2026-11-10 | 状态: 已实现(管家程序 v6.7, 7208行, 11工人)
> 仓库: github.com/iamsamyiok/bot

---

## 一、产品定位

**一句话**: 跑在你自己电脑上的个人 AI 智能体——飞书/音箱/网页/CLI 四入口说一句话, 它自动分配给对口工人执行, 结果主动推送回来, 记忆与文档自动沉淀成你的数字分身。

**与闭源竞品的本质区别**:

| | Muse (Meta) | Grok Bot (xAI) | bot (本项目) |
|---|---|---|---|
| 运行位置 | Meta 云电脑 | xAI 共享云电脑 | **你自己的电脑** |
| 数据归属 | Meta | xAI | **你** |
| 费用 | 订阅制 | 订阅制 | **免费模型即可全链跑通** |
| 模型选择 | 固定 Spark | 固定 Grok | **任意 OpenAI 兼容端点(含 Ollama 本地)** |
| 隐私 | Meta 政策约束 | xAI 政策约束 | **数据不出电脑** |
| 审批闸 | 有(核心卖点) | 有 | **有(飞书拍板卡片)** |
| 开源 | 否 | 否 | **MIT** |

**设计哲学**(来自 dsh-superagent): 占用最少的人类注意力, 给出最大的价值。每条消息、每个通知、每个设置项都要"价值 > 注意力成本"。

---

## 二、目标用户

- 有个人电脑(Windows/Mac/Linux)且希望 AI 智能体帮干活的用户
- 对数据隐私有要求, 不想把对话/记忆/文件放在厂商云端
- 想用免费模型跑通全链, 不想付订阅费
- 有飞书或微信生态, 希望在移动端随时指挥 AI

---

## 三、核心功能

### 3.1 入口层(四信道, 同一身份)

| 信道 | 能力 | 实现模块 |
|---|---|---|
| **飞书** | 文本/图片/文件收发、审批卡片按钮、完成推送、中断通知 | feishu_adapter.py (ws 长连接, 免公网) |
| **小智音箱** | 语音派任务(14 个 MCP 工具), 待播报队列 | xiaozhi_bridge.py (WS-MCP) |
| **网页** | 聊天流 UI, 任务点开看详情/续聊 | router_server.py (内置 web/index.html) |
| **CLI** | butler_cli.py 一行指挥 | butler_cli.py → /api/chat |

所有信道的语义/格式完全一致——用户在飞书说的话, 在音箱/网页/CLI 说效果相同。

### 3.2 Router 大脑(意图路由与任务分配)

**流程**: 用户消息 → 确定性快速路径(零 LLM, 毫秒级) → LLM 分类(fast 档) → 任务分配(worker 决策) → 执行

**确定性快速路径**(零 LLM, 毫秒级): 记忆写入(记住X)/进度查询(语义匹配)/例程创建(每天X点)/技能重放(再来一次X)。

**LLM 分类**(fast 档, 秒级):
- A 纯对话 → Router 直答(strong 档 GLM-5.2)
- B 单任务 → 派 1 个工人
- C 复杂目标 → 拆 ≤5 子任务并行

**Router 判断时注入的上下文**: 相关历史任务 top3 + RAG 知识 top2 + ReMe 数字分身 top2 + 用户画像 + 最近对话(滚动摘要) + 任务快照(运行中/排队)。

**批处理聚合**: 飞书连发消息 8 秒窗口合并成一条, 避免 3 条消息烧 3 次 LLM。

**质量验收与委派循环**(CEGIS 思想):
```
worker 完成 → LLM 强档质量验收(按原始诉求逐条评)
  → PASS → 汇总发给用户
  → FAIL(第1次) → 带反例修正指令 → 原 worker 续跑
  → FAIL(第2次) → 换引擎(codex↔opencode)重做
  → FAIL(第3次) → 停止循环, 如实报告: "⚠ 经3次执行仍未完全达标(原因), 以上是最后实际结果"
```

### 3.3 Worker 层(11 个专职工人)

| 工人 | 职责 | 引擎 | 分流规则 |
|---|---|---|---|
| **codex** | 私有任务/复杂编码(涉密专用) | codex CLI | 消息含密码/账号/银行/个人等涉密词 |
| **opencode** | 公共任务(免费 LLM) | opencode CLI, 3 模型降级链 | 非涉密的编码/分析/处理 |
| **ufo** | Windows GUI 桌面操作 | UFO2 | 打开应用/点击/截屏 |
| **search** | 联网搜索+摘要(8-15行中文) | unified-search 降级链 | "搜索X"/"查一下X" |
| **weather** | 天气(多城市+中文+时间词) | wttr.in + Open-Meteo | "X天气怎么样" |
| **note** | 笔记增删改查 | 本地 DB | "新笔记X"/"查笔记" |
| **reme** | 数字分身记忆 | ReMe API | "记住X"/"查记忆" |
| **research** | 深度调研出报告(标准交付) | codex + 交付模板 | "调研X"/"深度研究X" |
| **review** | 复盘提炼经验 | LLM+知识库 | "复盘今天/昨天" |
| **mail** | 收件箱/验证码(只读) | agent.qq.com IMAP | "查邮件"/"有没有验证码" |
| **files** | 云盘上传下载/本地找文件 | aliyunpan + Everything | "传云盘"/"找文件" |
| **weekly** | 周报聚合(7天) | LLM+ledger | "周报"/"这周做了什么" |

**opencode→codex 降级链**: opencode 三模型(glm-5.3-flash-free → deepseek-v4.1-flash-free → flint-40b-free)全失败 → 自动降级 codex 接管, 任务不断。

**公私分流**(用户指令): 消息含涉密词(密码/账号/银行/个人等15词)或续聊/看图 → codex; 其余 → opencode 免费链。

### 3.4 审批闸(高危动作先拍板)

16 类高危动作(发送邮件/转账/购买/删除文件/git push/重启服务等)自动拦截:
1. worker 挂起(等待期零进程零 token)
2. 飞书推交互卡片(✅确认执行 / ❌取消任务)
3. 用户点按钮(ws 回调, 无需公网) → worker 续跑或终止
4. 超时 1 小时 → 自动取消

### 3.5 记忆与知识层(数字分身)

**三层记忆**:
- **daily**(每日流水): 对话/任务结果自动沉淀
- **digest**(提炼): 画像(偏好/身份/环境)+知识(解决方案/教训)
- **procedure**(程序记忆): 教程/部署方法

**冲突消解**: 新记忆与已有记忆相似 → LLM 判定 ADD/UPDATE/NOOP

**成果沉淀**: 重任务产出 >300 字自动入 ReMe knowledge 卡(同主题合并去重+bilinks)

**技能化沉淀**: 同类任务成功 3 次 → LLM 自动提炼"技能卡"(模板+参数槽) → "再来一次上周那个周报"一句话重放

**语义检索**: "我刚才问的那个XX怎么样了" → 关键词+2-gram 匹配历史任务

### 3.6 自主性(定时例程+额度感知)

**定时例程**: "每天9点, 汇总GitHub趋势发飞书" 一句话创建 → routines.json → 每分钟检查触发 → 结果全通道推送

**额度闸**(可选): 智谱 coding plan 5h 窗口剩余额度感知
- ≥60% 全速 / 30-59% 只派高优 / <30% 挂起等重置 / 0% 转 PENDING / 查询失败 fallback 照跑

### 3.7 完成推送(全通道)

| 任务从哪派 | 用户在哪收到 |
|---|---|
| 飞书 | 飞书回推(结果+产物文件) |
| 网页 | 飞书补推(带来源标签) + 网页时间线 |
| 小智音箱 | 待播报队列(下次开口先播) + 飞书补推 + ntfy 手机 |
| 全来源 | ntfy 推手机(双保险) |

### 3.8 运维层

- **Watchdog**: 三服务(管家/Lab/ReMe)每分钟探活, 掉线自动拉起
- **需要人面板**: failed/interrupted 置顶, 健康状态不占位
- **UXL 用户体验流水**: 记录用户发送+看到的一切(月轮转, 脱敏), 含确认延迟/推送延迟统计
- **质量检查**: 轻工人输出过三道检查(实体完整性/实质性/失败透传), 不过自动重试→升级 codex
- **中断通知**: 服务重启导致任务中断 → 飞书告知用户可重试

---

## 四、数据与隐私

| 数据 | 存储 | 入库 |
|---|---|---|
| 对话/记忆 | ReMe workspace(本机) | 否 |
| 任务账本 | ledger.jsonl(本机) | 否 |
| 密钥 | config/bot.json(本机, gitignore) | 否 |
| UXL 体验流水 | state/uxl/(本机, 脱敏) | 否 |
| 用户文件 | 用户文档目录(本机) | 否 |

**承诺**: 数据不出电脑。你配置的 LLM 提供商能看到对话内容——涉密任务请配本地 Ollama 或指定 codex 私有引擎。

---

## 五、配置要求

| 项 | 必需 | 说明 |
|---|---|---|
| OS | Windows 10/11 推荐 | GUI 工人/Watchdog 深度绑定 Windows; Linux/Mac 核心链可跑 |
| Python | 3.11+ | |
| LLM | 任选免费档 | 硅基流动免费/OpenRouter free/魔搭(2000次/日)/本地 Ollama |
| 飞书应用 | 可选 | 开放平台创建+长连接, 凭证本地填 |
| 小智音箱 | 可选 | xiaozhi.me MCP |
| codex CLI | 可选 | 私有任务引擎 |
| GPU | **不需要** | 全链可在无 GPU 机器跑 |

---

## 六、模块清单(已实现, 24 文件, 7208 行, 286 函数)

```
butler.py              一键启动器(自检+端口探测)
router_server.py       HTTP 服务+聊天流 UI+API 端点
router_brain.py        Router 大脑(快速路径+LLM 分类+语义检索)
codex_worker.py        Worker 层(执行+审批闸+委派循环+降级链)
light_workers.py       轻工人(search/weather/note/reme/files/mail/review/weekly)
delegation.py          委派循环(质量验收+修正指令+引擎互换)
quality_gate.py        轻工人输出质量检查(Q1实体/Q2实质/Q3透传)
approval.py            审批闸(高危词判定+创建/拍板/等待)
batch_router.py        批处理聚合(8秒窗口)
memory_engine.py       FTS5+LIKE 双检索
reme_worker.py         ReMe 数字分身三层记忆
routines.py            定时例程(创建/列表/触发)
skills_memo.py         技能化沉淀(计数+提炼+重放)
uxlog.py               UXL 用户体验流水
_t2s.py                繁体→简体转换(200+映射)
model_router.py        Responsible API(用户自填 base_url/key/model)
feishu_adapter.py      飞书 ws 长连接+卡片
xiaozhi_bridge.py      小智音箱 WS-MCP
voice_io.py            TTS 语音播报
watchdog.py            三平台自愈
rag_client.py          RAG 三库接入(可选)
config.py              配置层(全路径+密钥集中)
daily_brief.py         每日简报预计算
butler_cli.py          CLI 入口
butler_mcp.py          MCP server 入口
```

---

## 七、Roadmap

- [x] 11 工人 / 审批闸 / 批处理路由 / Watchdog / 额度闸 / 委派循环 / 技能沉淀 / 语义检索 / UXL
- [ ] 更多通道(Telegram/Slack/Discord)
- [ ] 手机端(Mika 式本地执行)
- [ ] Web 设置页(替代手改 bot.json)
- [ ] 隔离执行舱(Docker, 出事故再做)
- [ ] 交互产物回传(需要时再做)
- [ ] 163 邮箱 IMAP 接入
- [ ] 更多搜索源(当前 exa→tavily, 可扩展 Perplexity/SearXNG)

---

## 八、仓库

- **代码**: github.com/iamsamyiok/bot (MIT)
- **文档**: README.md(双语) + docs/ARCHITECTURE.md + docs/WORKERS.md + docs/CHANNELS.md + docs/PRIVACY.md
- **测试**: 78+36 离线测试全绿
- **本机运行版**: 管家程序 v6.7 (Desktop/test3/agentic-os-server)
