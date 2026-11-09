# -*- coding: utf-8 -*-
"""小智盒子桥接 — 连接 xiaozhi.me 的 MCP WebSocket 端点, 让小智音箱语音调管家

协议: xiaozhi.me 的 /mcp/?token=xxx 是一个 MCP over WebSocket 端点。
管家作为 MCP server 通过 WS 连上去, 上报工具列表;
用户对小智说话时, 小智 LLM 选工具 → 云端通过 WS 反向调管家 → 结果回传 → 小智语音播报。

管家暴露的工具:
  butler_chat    — 快速问答(Router直答, 秒回)
  butler_run_task — 派耗时任务(异步, 完成后推手机ntfy+飞书)
  butler_status  — 查任务进度
  butler_search  — 全局记忆检索
  butler_remind  — 设一次性提醒
  butler_brief   — 获取今日简报(定时预计算, 秒回)

安全: token 即密码, 不要泄露。
"""
import asyncio
import json
import os
import sys
import time
import threading
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

_cfg = {}
_status = {'connected': False, 'error': '', 'started': '', 'tools_served': 0}


def load_cfg():
    try:
        return json.load(open(os.path.join(BASE, 'config.json'), encoding='utf-8'))
    except Exception:
        return {}


def save_cfg(d):
    os.makedirs(os.path.join(BASE, 'state'), exist_ok=True)
    cur = load_cfg()
    cur.update(d)
    json.dump(cur, open(os.path.join(BASE, 'config.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)


def status():
    cfg = load_cfg().get('xiaozhi', {})
    return {'configured': bool(cfg.get('ws_url')),
            'connected': _status['connected'],
            'error': _status['error'],
            'started': _status['started'],
            'tools': _status['tools_served']}


# ---------- HTTP → 管家 ----------
def _http(path, body=None, method='GET', timeout=120):
    req = urllib.request.Request('http://127.0.0.1:9991' + path,
                                 method=method)
    if body is not None:
        req.add_header('Content-Type', 'application/json')
        body = json.dumps(body).encode('utf-8')
    with urllib.request.urlopen(req, body, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def _chat(text, source='xiaozhi'):
    d = _http('/api/chat', {'text': text, 'source': source}, method='POST')
    reply = d.get('reply', '')
    tid = d.get('id', '')
    return reply, tid


# ---------- 待播报队列: 任务完成 → 用户下次对小智开口时先播 ----------
# (小智WS是被动调用模式, 管家无法反向推语音; 队列=下次交互首播, ntfy/飞书即时兜底)
NOTIFY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           'state', 'xiaozhi_notify.jsonl')


def push_notify(text):
    """任务完结时调用(_on_summary跨模块), 追加一条待播报"""
    try:
        os.makedirs(os.path.dirname(NOTIFY_FILE), exist_ok=True)
        with open(NOTIFY_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps({'t': time.strftime('%H:%M'), 'text': text[:400]},
                               ensure_ascii=False) + chr(10))
    except Exception:
        pass


def _drain_notify():
    """取出并清空待播报(≤3条合并), 无则返回 ''"""
    try:
        lines = open(NOTIFY_FILE, encoding='utf-8').read().strip().splitlines()
    except OSError:
        return ''
    if not lines:
        return ''
    try:
        os.remove(NOTIFY_FILE)
    except Exception:
        pass
    texts = []
    for ln in lines[-3:]:
        try:
            texts.append('%s %s' % (json.loads(ln).get('t', ''),
                                    json.loads(ln).get('text', '')))
        except Exception:
            continue
    return '📢 之前派的任务有结果了: ' + ' ； '.join(texts)


# ---------- MCP 工具定义 ----------
TOOLS = [
    {'name': 'butler_chat',
     'description': '向AI管家快速提问(秒回): 知识问答/闲聊/查记忆/查状态。'
                    '耗时的真任务(写代码/深度搜索/处理文件/操作电脑)请改用 butler_run_task。',
     'inputSchema': {'type': 'object', 'properties': {
         'message': {'type': 'string', 'description': '你要对管家说的话/问题'}},
         'required': ['message']}},
    {'name': 'butler_run_task',
     'description': '把耗时的真任务派给AI管家的worker执行(写代码/联网深度搜索/文件处理/桌面操作), '
                    '立即返回任务号, 完成后结果自动推送到用户手机(ntfy)和飞书。简单问答请用 butler_chat。',
     'inputSchema': {'type': 'object', 'properties': {
         'task': {'type': 'string', 'description': '任务描述, 越具体越好'}},
         'required': ['task']}},
    {'name': 'butler_web_search',
     'description': '联网搜索最新信息(AI新闻/技术资料/实时事件等), 返回摘要',
     'inputSchema': {'type': 'object', 'properties': {
         'query': {'type': 'string', 'description': '搜索关键词'}},
         'required': ['query']}},
    {'name': 'butler_weather',
     'description': '查询指定城市的实时天气(默认惠州)',
     'inputSchema': {'type': 'object', 'properties': {
         'city': {'type': 'string', 'description': '城市名, 默认惠州'}},
         'required': []}},
    {'name': 'butler_task_status',
     'description': '查询任务进度或服务状态',
     'inputSchema': {'type': 'object', 'properties': {
         'task_id': {'type': 'string', 'description': '任务ID, 留空查服务概览'}},
         'required': []}},
    {'name': 'butler_task_find',
     'description': '用自然语言语义查找任务(不需要任务ID): 如"我刚才问的惠州天气结果怎么样"→'
                    '返回最匹配的任务+状态+完整结果摘要。用户想了解某件事的进度/结果时优先用这个',
     'inputSchema': {'type': 'object', 'properties': {
         'query': {'type': 'string', 'description': '任务内容描述, 如"惠州天气""翻译那段话"'}},
         'required': ['query']}},
    {'name': 'butler_search_memory',
     'description': '检索管家的全局记忆(历史任务+用户画像+知识库)',
     'inputSchema': {'type': 'object', 'properties': {
         'query': {'type': 'string', 'description': '搜索关键词'}},
         'required': ['query']}},
    {'name': 'butler_remind',
     'description': '设置一次性提醒, 到点管家会通知',
     'inputSchema': {'type': 'object', 'properties': {
         'when': {'type': 'string', 'description': '时间 YYYY-MM-DD HH:MM'},
         'text': {'type': 'string', 'description': '提醒内容'}},
         'required': ['when', 'text']}},
    {'name': 'butler_reminder_list',
     'description': '列出当前所有未触发的提醒',
     'inputSchema': {'type': 'object', 'properties': {}, 'required': []}},
    {'name': 'butler_reminder_delete',
     'description': '删除一条提醒',
     'inputSchema': {'type': 'object', 'properties': {
         'reminder_id': {'type': 'string', 'description': '提醒ID'}},
         'required': ['reminder_id']}},
    {'name': 'butler_note_add',
     'description': '添加一条笔记/备忘',
     'inputSchema': {'type': 'object', 'properties': {
         'content': {'type': 'string', 'description': '笔记内容'}},
         'required': ['content']}},
    {'name': 'butler_note_list',
     'description': '列出笔记(可按关键词筛选)',
     'inputSchema': {'type': 'object', 'properties': {
         'keyword': {'type': 'string', 'description': '筛选关键词, 留空全部'}},
         'required': []}},
    {'name': 'butler_note_edit',
     'description': '修改一条笔记的内容',
     'inputSchema': {'type': 'object', 'properties': {
         'note_id': {'type': 'string', 'description': '笔记ID'},
         'content': {'type': 'string', 'description': '新内容'}},
         'required': ['note_id', 'content']}},
    {'name': 'butler_note_delete',
     'description': '删除一条笔记',
     'inputSchema': {'type': 'object', 'properties': {
         'note_id': {'type': 'string', 'description': '笔记ID'}},
         'required': ['note_id']}},
    {'name': 'butler_daily_brief',
     'description': '获取今日简报(AI新闻+惠州天气+待办回顾)。定时预计算, 调用即秒回',
     'inputSchema': {'type': 'object', 'properties': {
         'regenerate': {'type': 'boolean', 'description': '强制重新生成'}},
         'required': []}},
]


# ---------- 工具执行 ----------
def _exec_tool(name, args):
    if name == 'butler_chat':
        # 待播报队列: 小智来源任务完成时入队, 用户下一次开口先播结果再答新话
        note = _drain_notify()
        reply = _chat_reply(args.get('message', ''))
        return (note + chr(10) + reply) if note else reply
    if name == 'butler_run_task':
        # 「任务：」前缀=路由零误判强制派发出口; 异步派发不阻塞小智语音
        d = _http('/api/chat',
                  {'text': '任务：' + args.get('task', ''), 'source': 'xiaozhi'},
                  method='POST')
        reply = d.get('reply', '') or '已派发'
        return '%s (异步执行中, 完成后结果会自动推送到手机和飞书; 也可对我说「进度」查询)' % reply
    if name == 'butler_task_find':
        # 语义任务查找: 走路由进度快速路径(内部按内容匹配, 不需要ID)
        d = _http('/api/chat',
                  {'text': args.get('query', '') + ' 进度', 'source': 'xiaozhi',
                   'wait': True, 'max_wait': 30},
                  method='POST')
        return (d.get('reply') or '没有找到相关任务')[:600]
    if name == 'butler_task_status':
        tid = args.get('task_id', '')
        if tid:
            t = _http('/api/task/%s?op=detail' % tid)
            return '[%s] %s\n总结: %s' % (tid, t.get('status', ''),
                                          (t.get('summary') or t.get('result') or '')[:200])
        s = _http('/api/status')
        w = s.get('worker', {})
        return 'worker %s(运行%s/排队%s) | 飞书%s' % (
            w.get('max_workers', 3), w.get('running', 0),
            w.get('queued', 0), '在线' if s.get('feishu', {}).get('connected') else '离线')
    if name == 'butler_search_memory':
        d = _http('/api/search?q=' + urllib.parse.quote(args.get('query', '')))
        out = []
        for k in (d.get('keyword', {}).get('tasks') or [])[:5]:
            out.append('[%s] %s' % (k.get('id', ''), (k.get('snippet') or '')[:100]))
        for r in (d.get('rag') or [])[:3]:
            out.append('RAG[%s]: %s' % (r.get('file_name', ''), (r.get('content') or '')[:80]))
        return '\n'.join(out) or '(无结果)'
    if name == 'butler_remind':
        when = args.get('when', '')
        text = args.get('text', '')
        d = _http('/api/chat',
                  {'text': '提醒我 %s %s' % (when, text), 'source': 'xiaozhi'},
                  method='POST')
        return d.get('reply', '已设置')
    if name == 'butler_daily_brief':
        try:
            import daily_brief
            cached = daily_brief.load()
            if cached:
                return cached['text']
            d = _http('/api/chat',
                      {'text': '【每日简报】搜索今天AI新闻Top5+惠州天气+待办回顾, 不超过15行',
                       'source': 'agent', 'wait': True, 'max_wait': 600}, method='POST', timeout=660)
            t = d.get('summary') or d.get('reply') or '简报生成失败'
            return t
        except Exception as e:
            return '简报获取失败: %s' % str(e)[:100]
    if name == 'butler_web_search':
        d = _http('/api/chat',
                  {'text': '请联网搜索: ' + args.get('query', ''),
                   'source': 'xiaozhi', 'wait': True, 'max_wait': 300}, method='POST', timeout=330)
        return d.get('summary') or d.get('reply', '(无结果)')
    if name == 'butler_weather':
        city = args.get('city') or '惠州'
        d = _http('/api/chat',
                  {'text': '查今天%s的天气, 只回复天气要点' % city,
                   'source': 'xiaozhi', 'wait': True, 'max_wait': 180}, method='POST', timeout=210)
        return d.get('summary') or d.get('reply', '(无结果)')
    if name == 'butler_reminder_list':
        rems = _http('/api/reminders').get('reminders', [])
        if not rems:
            return '(暂无提醒)'
        return chr(10).join('%s | %s | %s' % (r['id'], r['dt'], r['text'])
                            for r in rems)
    if name == 'butler_reminder_delete':
        _http('/api/reminders/del', {'id': args.get('reminder_id', '')},
              method='POST')
        return '已删除'
    if name == 'butler_note_add':
        _http('/api/note', {'content': args.get('content', '')}, method='POST')
        return '笔记已添加'
    if name == 'butler_note_list':
        kw = args.get('keyword', '')
        d = _http('/api/search?q=' + urllib.parse.quote(kw))
        notes = d.get('keyword', {}).get('notes', [])
        if not notes:
            return '(无匹配笔记)'
        return chr(10).join('%s | %s' % (n.get('note_id', '')[:20],
                                         (n.get('snippet') or '')[:80])
                            for n in notes[:10])
    if name == 'butler_note_delete':
        _http('/api/note/delete', {'note_id': args.get('note_id', '')},
              method='POST')
        return '笔记已删除'
    if name == 'butler_note_edit':
        _http('/api/note/edit', {'note_id': args.get('note_id', ''),
                                 'content': args.get('content', '')},
              method='POST')
        return '笔记已更新'
    return '未知工具: %s' % name



# ---------- MCP over WebSocket ----------
async def _ws_loop(ws_url):
    """连 xiaozhi.me MCP WS 端点, 处理 JSON-RPC(工具调用)"""
    import websockets

    async with websockets.connect(ws_url, max_size=4 * 1024 * 1024) as ws:
        _status['connected'] = True
        _status['error'] = ''
        rpc_id = [0]

        async def send(obj):
            await ws.send(json.dumps(obj, ensure_ascii=False))

        async def recv_loop():
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                method = msg.get('method', '')
                mid = msg.get('id')
                if method == 'initialize':
                    await send({'jsonrpc': '2.0', 'id': mid, 'result': {
                        'protocolVersion': '2024-11-05',
                        'capabilities': {'tools': {}},
                        'serverInfo': {'name': 'butler-os', 'version': '6.4'}}})
                elif method == 'notifications/initialized':
                    pass
                elif method == 'tools/list':
                    await send({'jsonrpc': '2.0', 'id': mid, 'result': {
                        'tools': TOOLS}})
                    _status['tools_served'] = len(TOOLS)
                elif method == 'tools/call':
                    params = msg.get('params', {})
                    name = params.get('name', '')
                    args = params.get('arguments', {})
                    try:
                        text = _exec_tool(name, args)
                    except Exception as e:
                        text = '工具执行异常: %s' % str(e)[:150]
                    await send({'jsonrpc': '2.0', 'id': mid, 'result': {
                        'content': [{'type': 'text', 'text': str(text)}]}})
                elif method == 'ping':
                    await send({'jsonrpc': '2.0', 'id': mid, 'result': {}})
                else:
                    if mid:
                        await send({'jsonrpc': '2.0', 'id': mid,
                              'error': {'code': -32601,
                                        'message': 'unknown method'}})

        await recv_loop()


def _chat_reply(text):
    import urllib.request as _ur
    req = _ur.Request('http://127.0.0.1:9991/api/chat', method='POST')
    req.add_header('Content-Type', 'application/json')
    with _ur.urlopen(req, data=json.dumps(
            {'text': text, 'source': 'xiaozhi'}).encode('utf-8'), timeout=120) as r:
        d = json.loads(r.read().decode('utf-8'))
    return d.get('reply', d.get('id', ''))


def _start_ws(url):
    """启动 WS 循环(独立 asyncio 事件循环线程)"""
    global _ws_url
    _ws_url = url

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        backoff = 5
        while True:
            try:
                loop.run_until_complete(_ws_loop(url))
                backoff = 5
            except Exception as e:
                _status['connected'] = False
                _status['error'] = str(e)[:200]
                time.sleep(backoff)
                backoff = min(60, backoff * 2)

    threading.Thread(target=_run, daemon=True).start()


_ws_url = ''


if __name__ == '__main__':
    cfg = load_cfg().get('xiaozhi', {})
    url = sys.argv[1] if len(sys.argv) > 1 else cfg.get('ws_url', '')
    if not url:
        print('用法: python xiaozhi_bridge.py <ws_url>')
        print('或在 config.json 配 xiaozhi.ws_url')
        sys.exit(1)
    save_cfg({'xiaozhi': {'ws_url': url}})
    _start_ws(url)
    print('小智桥接已启动, Ctrl+C 退出')
    try:
        while True:
            time.sleep(2)
            st = status()
            if st['error']:
                print('  状态:', st['error'], file=sys.stderr)
    except KeyboardInterrupt:
        print('退出')
