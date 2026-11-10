# musebot — Your self-hosted personal AI butler — inspired by Muse & Grok Bot

**中文** | [English](README.en.md)

> 跑在你自己电脑上的个人 AI 管家:飞书 / 智能音箱 / 网页 / CLI 四入口,11 个专职工人执行任务,记忆与文档自动沉淀成你的数字分身,**免费模型即可跑通全链**。

开源版的 Muse / Grok Bot 思路 —— 但**不跑在厂商云上,不收订阅费,数据不出你的电脑**。

---

## 它能做什么

**🗣 一句话,任务开工**
"帮我调研一下数字分身开源项目" → Router 大脑判断意图 → 派对口工人执行 → 完成后飞书/手机/音箱三端主动推送结果。

**🧠 越用越懂你**
对话自动沉淀成三层记忆(当日流水 → 画像提炼 → 程序知识),冲突自动消解。"我喜欢什么样的工具?"它综合你的历史回答。

**🔧 技能自动沉淀**
同类任务成功 3 次,自动提炼成"技能卡"——下次"再来一次上周那个周报"一句话重放。

**🔐 高危动作先拍板**
发邮件 / 转账 / 删除等 16 类高危动作,worker 自动挂起,飞书推审批卡片,你点按钮才继续——等待期不占进程、不耗 token。

**⏰ 定时例程**
"每天9点,汇总GitHub趋势和惠州天气发给我" 一句话创建,到点自动执行推送。

**💰 额度感知(可选)**
接入 LLM 套餐额度查询,额度富余全速跑、见底自动转免费模型链。

**🖥 三平台自愈**
管家 / 附属服务掉线,Watchdog 每分钟探活自动拉起。

## 四个入口

| 入口 | 能力 |
|---|---|
| **飞书** | 文本/图片/文件收发、审批卡片按钮、完成推送 |
| **小智音箱** | 语音派任务(14 个 MCP 工具),待播报队列 |
| **网页** | 聊天流 UI,任务点开看详情/续聊 |
| **CLI** | `python butler_cli.py "任务"` 一行指挥 |

## 十一个工人

| 工人 | 干什么 | 引擎 |
|---|---|---|
| codex | 私有任务/复杂编码(涉密信息专用) | codex CLI |
| opencode | 公共任务(免费 LLM) | opencode CLI,失败自动降级 codex |
| ufo | Windows 桌面 GUI 操作 | UFO2 |
| search | 联网搜索+摘要 | 免费 API 降级链 |
| weather | 天气(中文播报) | wttr.in + Open-Meteo |
| note | 笔记增删改查 | 本地 DB |
| reme | 数字分身记忆 | ReMe |
| research | 深度调研出报告 | codex+交付模板 |
| review | 复盘提炼经验 | LLM+知识库 |
| mail | 收件箱/验证码 | agent 邮箱 |
| files | 云盘上传下载/本地找文件 | 阿里云盘 CLI+Everything |
| weekly | 周报聚合 | ledger 7 天 |

## 快速开始

```bash
# 1. 拉代码
git clone https://github.com/iamsamyiok/musebot.git && cd bot

# 2. (可选)配置模型与密钥 —— 不配也能跑核心链(免费模型档)
cp config/bot.example.json config/bot.json   # 编辑填入你的 base_url/key/model

# 3. 启动(自检+端口探测+自动开网页)
python butler.py
```

> **Responsible API 设计**:bot 不内置任何供应商。在 `config/bot.json` 里填你的
> `base_url` + `model` + `api_key_env`(支持本地 Ollama 的 `http://localhost:11434/v1/...`),
> 密钥只存在你本机的 bot.json(已 gitignore)。

详细接入(飞书应用创建/小智绑定/ReMe 记忆部署)见 [docs/CHANNELS.md](docs/CHANNELS.md)。

## 架构

```
入口(飞书│音箱│网页│CLI) — 同一身份四信道
        ↓
Router 大脑: 确定性快速路径 + LLM 双档分类 + 批处理聚合(连发8秒合并)
        ↓ 任务单
11 工人并行: 审批闸挂起拍板 / opencode→codex 降级 / 公私分流
        ↓
记忆层: ReMe 三层(daily/digest/procedure) + bilinks + 冲突消解 + 技能卡
        ↓
运维层: Watchdog 自愈 / 额度四档编排 / 需要人面板 / 全通道完成推送
```

详见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 配置要求

- Windows 10/11(推荐,GUI 工人/Watchdog 深度集成);Linux/macOS 跑核心链
- Python 3.11+
- LLM:任意 OpenAI 兼容端点(**免费档即可**,无 GPU 需求)

## 隐私边界

- 记忆、任务账本、配置全部在你本机(`~/.musebot/`)
- 密钥只存本地 bot.json,永不入库、永不上传
- 你配置的 LLM 提供商能看到你的对话内容——**涉密任务请配本地模型**或指定私有引擎(codex worker)

## Roadmap

- [x] 11 工人 / 审批闸 / 批处理路由 / Watchdog / 额度闸
- [ ] 更多通道(Telegram/Slack/Discord)
- [ ] 手机端(Mika 式本地执行)
- [ ] Web 设置页(替代手改 bot.json)

## License

[MIT](LICENSE) — 拿去用,改造自由,请遵守你所用 LLM 提供商的服务条款。
