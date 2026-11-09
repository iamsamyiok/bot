# 通道接入

## 飞书
1. [开放平台](https://open.feishu.cn) 创建企业自建应用
2. 权限: im:message(收发), im:resource(图片文件), 可选 im:card(审批卡片)
3. 事件订阅选"长连接"模式
4. 凭证填入 `config/bot.json`:
```json
{"keys": {"FEISHU_APP_ID": "cli_xxx", "FEISHU_APP_SECRET": "xxx"}}
```
5. 重启 → 飞书连接自动建立, 给机器人发消息即通

## 小智音箱
[xiaozhi.me](https://xiaozhi.me) 设备 → MCP 接入点填管家 WS 地址; 14 个工具自动上报(派任务/查进度/找任务/例程等)。

## 网页
启动即带 `http://127.0.0.1:9991` 聊天流。

## CLI
```bash
python butler_cli.py "任务：写一个xx工具"
python butler_cli.py 进度
```

## ntfy 手机推送(可选)
```json
{"keys": {"NTFY_TOPIC": "你的私有主题名"}}
```
