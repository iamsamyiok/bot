# 工人配置

每个工人是 `light_workers.py` / `codex_worker.py` 里的一个函数, 路由层按语义自动派发, 无需注册。

## codex / opencode(编码双引擎)
- codex: 涉密/个人信息任务, 需 `npm i -g @openai/codex && codex login`
- opencode: 公共任务免费 LLM, 需 `npm i -g opencode-ai && opencode auth login`; 模型链在 bot.json `workers.opencode_models`
- 分流规则: 消息含密码/账号/银行等涉密词或为续聊/看图 → codex; 其余 → opencode

## ufo(GUI)
Windows 桌面操作, 依赖 [UFO2](https://github.com/microsoft/UFO)。

## search / weather
search 用任意搜索 API(配置 `BUTLER_SEARCH_CLI`); weather 免 key(wttr.in+Open-Meteo)。

## mail
[agently-cli](https://npmjs.com/package/agently-cli) 登录 agent.qq.com 信箱。

## files
阿里云盘: [aliyunpan](https://github.com/tickstep/aliyunpan) CLI 登录 + 可选 ad.py 封装(`BUTLER_ALIYUN_AD`); 本地检索 [Everything](https://www.voidtools.com/) + es.exe(`BUTLER_SEARCH_CLI`)。

## review / weekly
零配置, 从任务账本聚合。

## reme(可选)
独立部署 [ReMe](https://github.com/agentscope-ai/ReMe) → 2333 端口, 配 LLM_API_KEY 后 auto_memory/auto_dream 生效。
