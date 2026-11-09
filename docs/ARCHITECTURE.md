# 架构

## 四层

### 入口层(四信道, 同一身份)
飞书(ws 长连接, 图片/文件/审批卡片) · 小智音箱(WS-MCP) · 网页聊天流 · CLI/MCP。
所有信道进入同一个 `on_chat`, 语义完全一致。

### Router 大脑层(`router_brain.py`)
1. **确定性快速路径**(零 LLM): 记忆写入/任务前缀/续聊/例程/复盘/邮件/文件/天气/笔记/搜索等 26 类正则
2. **LLM 分类**: A 纯对话 / B 单任务 / C 拆解(≤5 子任务), 双档模型链
3. **批处理聚合**: 连发消息 8 秒窗口合并成一条(`batch_router.py`)
4. **语义任务检索**: "刚才那个XX怎么样了"按内容匹配任务
5. **技能重放**: 命中技能卡 → 按模板带参重派

### Worker 层(`codex_worker.py` + `light_workers.py`)
领取任务 → (审批闸: 高危词命中则挂起等拍板) → opencode 公共任务分流(失败降级 codex) → 执行 → 独立总结 → 完成推送(飞书/ntfy/TTS/小智队列) + 后续建议。

### 记忆与运维层
`memory_engine.py`(FTS5+LIKE 中文检索) · `reme_worker.py`(ReMe 三层+冲突消解) · `routines.py`(定时例程) · `skills_memo.py`(技能卡) · `watchdog.py`(自愈) · `approval.py`(审批) · `quota_gate`(Lab 额度编排, 可选)。

## 关键设计决策

| 决策 | 理由 |
|---|---|
| 中文检索用 LIKE 不用 FTS5 | FTS5 默认分词器不切中文, 实测 MATCH 命中 0 条 |
| 轻工人答案不过 LLM 总结 | 已是人话, 再总结串味+延迟 |
| curl 子进程而非 requests 调 LLM | Windows schannel TLS 兼容性 |
| 事件回调里不做耗时调用 | 飞书 ws 心跳会被饿死断连(实测教训) |
| 完成推送全通道 | 任务从哪派不重要, 结果任何端都可达 |
