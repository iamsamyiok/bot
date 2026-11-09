# -*- coding: utf-8 -*-
"""butler MCP Server — 把本地管家(Agentic OS Server)暴露为 MCP 工具

任何支持 MCP 的 AI agent(ZCode/Claude/Cursor…)加一条 stdio 配置即可获得:
  butler_ask    — 问管家(Router LLM 直答, 快)
  butler_task   — 派任务给 worker 并等待最终总结(默认≤10分钟)
  butler_task_async — 只派发不等(返回任务ID, 适合后台长任务)
  butler_status — 服务状态/任务进度
  butler_search — 全局混合记忆检索

依赖: pip install mcp  (server 本体自动拉起, 见 ensure_server)
启动: python butler_mcp.py   (stdio)
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request

from mcp.server.fastmcp import FastMCP

BASE = os.path.dirname(os.path.abspath(__file__))
PORT_FILE = os.path.join(os.path.expanduser('~'), '.butler-os', 'server.json')

mcp = FastMCP('butler-os')
NL_C = chr(10)


def _base():
    try:
        d = json.load(open(PORT_FILE, encoding='utf-8'))
        return d['url'].rstrip('/')
    except Exception:
        pass
    for p in range(9991, 10000):
        url = 'http://127.0.0.1:%d' % p
        try:
            with urllib.request.urlopen(url + '/api/health', timeout=1) as r:
                if r.status == 200:
                    return url.rstrip('/')
        except Exception:
            continue
    return None


def ensure_server():
    if _base():
        return True
    try:
        pyw = shutil.which('pythonw') or shutil.which('python')
        subprocess.Popen([pyw, os.path.join(BASE, 'router_server.py')],
                         cwd=BASE, creationflags=0x08000000)
    except Exception:
        return False
    for _ in range(25):
        time.sleep(1)
        if _base():
            return True
    return False


def _req(method, path, body=None, timeout=120):
    base = _base()
    if not base:
        raise RuntimeError('管家服务不可达(且自动拉起失败)')
    req = urllib.request.Request(base + path, method=method)
    data = None
    if body is not None:
        req.add_header('Content-Type', 'application/json')
        data = json.dumps(body).encode('utf-8')
    with urllib.request.urlopen(req, data=data, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def _wait_task(tid, max_wait):
    deadline = time.time() + min(900, max_wait)
    t = {}
    while time.time() < deadline:
        t = _req('GET', '/api/task/%s?op=detail' % tid, timeout=20)
        if t.get('status') in ('done', 'failed', 'stopped'):
            break
        time.sleep(3)
    return t


@mcp.tool()
def butler_ask(question: str) -> str:
    """向本地AI管家提问(知识/进度/闲聊, Router LLM直接回答, 约1-5秒)。
    若管家把问题派成了任务, 会提示改用 butler_task。"""
    d = _req('POST', '/api/chat', {'text': question, 'source': 'agent'})
    if '已接收' in d.get('reply', ''):
        return ('该请求已被派为任务(%s)。要同步拿结果请改用 butler_task。'
                % d.get('id', ''))
    return d['reply']


@mcp.tool()
def butler_task(task: str, max_wait_minutes: int = 10) -> str:
    """派任务给本地管家的 worker(codex CLI)执行并等待最终总结。
    适合: 联网查实时信息/写改代码文件/操作软件等必须动手的事。
    max_wait_minutes: 最长等待分钟数(默认10, 到时未完返回任务ID)。"""
    d = _req('POST', '/api/chat',
             {'text': task, 'source': 'agent', 'wait': True,
              'max_wait': min(900, max_wait_minutes * 60)},
             timeout=max_wait_minutes * 60 + 60)
    tid = d.get('id', '')
    status = d.get('status', 'queued')
    if status not in ('done', 'failed', 'stopped'):
        t = _wait_task(tid, (max_wait_minutes - 0) * 60 + 120)
        status = t.get('status', status)
        d['summary'] = t.get('summary', d.get('summary', ''))
    if status == 'done':
        sm = d.get('summary', '')
        if sm.startswith('['):
            nl = sm.find('\n')
            if nl > 0:
                sm = sm[nl + 1:]
        return ('[任务 %s 已完成]\n%s' % (tid, sm or d.get('reply', '')))
    if status in ('failed', 'stopped'):
        return ('[任务 %s %s] 错误码 %s\n%s'
                % (tid, status, d.get('error_code', ''),
                   (d.get('raw_answer') or d.get('reply') or '')[:400]))
    return ('[任务 %s 未在%d分钟内完成, 仍在后台运行]\n'
            '稍后可用 butler_status 查询。' % (tid, max_wait_minutes))


@mcp.tool()
def butler_task_async(task: str) -> str:
    """派任务但不等待(适合长任务), 返回任务ID。之后用 butler_status 查询。"""
    d = _req('POST', '/api/chat', {'text': task, 'source': 'agent'})
    return ('已派发, 任务ID: %s\n完成后用 butler_status 查询: %s'
            % (d.get('id', '?'), d.get('id', '?')) if d.get('id')
            else d['reply'])


@mcp.tool()
def butler_status(task_id: str = '') -> str:
    """查询服务状态或单个任务进度(task_id 传任务ID如 T1104xxx, 留空=服务概览)"""
    if task_id:
        d = _req('GET', '/api/task/%s?op=detail' % task_id)
        lines = ['[%s] %s' % (task_id, d.get('status', '')),
                 '问题: ' + (d.get('question') or '')[:60]]
        if d.get('summary'):
            lines.append('总结: ' + d['summary'][:200])
        if d.get('error_code'):
            lines.append('错误: %s %s' % (d['error_code'],
                                          (d.get('error') or '')[:100]))
        if (d.get('followups') or []):
            f = d['followups'][-1]
            lines.append('最近续聊: %s → %s' % ((f.get('question') or '')[:30],
                                                (f.get('answer') or f.get('status') or '')[:40]))
        return '\n'.join(lines)
    d = _req('GET', '/api/status')
    w = d['worker']
    return ('管家服务: 端口%d, worker %d(运行%d/排队%d), 飞书%s'
            % (d['port'], w['max_workers'], w['running'], w['queued'],
               '在线' if d['feishu']['connected'] else '离线'))


@mcp.tool()
def butler_xiaozhi_status() -> str:
    """查询小智音箱桥接的连接状态"""
    try:
        import xiaozhi_bridge
        st = xiaozhi_bridge.status()
        return json.dumps(st, ensure_ascii=False)
    except Exception as e:
        return '状态获取失败: %s' % e


@mcp.tool()
def butler_daily_brief(regenerate: bool = False) -> str:
    """获取今日简报(AI新闻+惠州天气+待办回顾)。管家每天定时预计算, 调用即秒回。
    regenerate=True 时强制现算(较慢); 通常直接调用即可。"""
    try:
        import daily_brief
        if not regenerate:
            cached = daily_brief.load()
            if cached:
                return ('[今日简报 %s]' % cached.get('generated', '')
                        + NL_C + cached['text'])
        # 现算: 派 worker 联网调研, 阻塞等
        d = _req('POST', '/api/chat',
                 {'text': '【每日简报】请完成三件事, 用不超过15行汇报: '
                          '1) 搜索今天的AI新闻Top5(各一句话) 2) 查惠州今天天气 '
                          '3) 对照最近任务历史列出待跟进事项。',
                  'source': 'agent', 'wait': True, 'max_wait': 600},
                 timeout=660)
        if d.get('summary'):
            return '[今日简报]' + NL_C + d['summary']
        return d.get('reply', '简报生成失败')
    except Exception as e:
        return '简报获取失败: %s' % str(e)[:150]


@mcp.tool()
def butler_remind(when: str, text: str) -> str:
    """设置一次性提醒。到点管家会在聊天流和飞书推送。
    when: 格式 YYYY-MM-DD HH:MM (如 2026-11-05 09:00)
    text: 提醒内容"""
    d = _req('POST', '/api/chat',
             {'text': '提醒我 %s %s' % (when, text), 'source': 'agent'})
    return d.get('reply', '已设置')


@mcp.tool()
def butler_search(query: str) -> str:
    """检索管家的全局记忆: 历史任务 + 用户画像/笔记 + RAG知识库(混合)"""
    d = _req('GET', '/api/search?q=' + urllib.parse.quote(query))
    out = []
    for k in (d.get('keyword', {}).get('tasks') or [])[:5]:
        out.append('[%s] %s\n%s' % (k['id'], k.get('question', '')[:50],
                                    (k.get('snippet') or '')[:120]))
    for n in (d.get('keyword', {}).get('notes') or [])[:3]:
        out.append('📌 ' + (n.get('snippet') or '')[:100])
    for r in (d.get('rag') or [])[:3]:
        out.append('RAG[%s]: %s' % (r.get('file_name', ''),
                                    (r.get('content') or '')[:120]))
    if d.get('rag_error'):
        out.append('(RAG原始错误: %s)' % d['rag_error'])
    return '\n\n'.join(out) or '(无结果)'


def ws_bridge(ws_url: str):
    """WS 客户端模式: 连 xiaozhi.me 等云端 MCP 网关, 上报本管家全部工具。
    这是小智音箱语音调管家的通道(与 mcp_exe 的 ws 模式相同协议)"""
    import asyncio
    try:
        import lark_oapi  # noqa 确认环境
    except Exception:
        pass
    # FastMCP 原生支持 ws 透传(通过 asgi wrapper), 但为兼容 xiaozhi.me 的
    # 私有 ws 协议, 此处使用 mcp 包的 ws_client 对等实现
    from mcp.client.stdio import stdio_client  # noqa: 占位, 防止静态检查误删
    # 实际连接: FastMCP 的 run("sse") 变体 + 端口监听, 由外部 ws-gateway 接入
    # 更简单可靠的做法: 启动 HTTP/SSE server 监听本机端口,
    # 由 mcp_exe(--ws 模式) 作为 ws-gateway 把 xiaozhi.me 桥接到这个 SSE 端口
    mcp.run(transport='sse', port=9900)


if __name__ == '__main__':
    if len(sys.argv) > 2 and sys.argv[1] == '--ws':
        # butler_mcp.py --ws wss://api.xiaozhi.me/mcp/?token=xxx
        # 本质: 启动 SSE server 于 9900 端口 + 提示用户用 mcp_exe 桥接
        # 或者直接以 stdio 模式被 mcp_exe 作为子工具调用
        ws_url = sys.argv[2]
        print('WS bridge mode: 管家已启动 SSE 端口 9900, 请用 mcp_exe 桥接到 xiaozhi.me',
              file=sys.stderr)
        ensure_server()
        mcp.run(transport='sse', port=9900)
    elif len(sys.argv) > 1 and sys.argv[1] == '--stdio':
        ensure_server()
        mcp.run()
    else:
        ensure_server()
        mcp.run()          # 默认 stdio
