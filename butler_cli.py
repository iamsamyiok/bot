# -*- coding: utf-8 -*-
"""butler CLI — AI agent 与本地管家的命令行入口

人类: Web/飞书;  AI agent: 本 CLI 或 butler_mcp.py
所有命令机器可解析(--json), 退出码: 0=成功 2=任务失败 3=超时/服务不可达

示例:
  butler ask "什么是RAG"                 # Router直答(1-3s)
  butler run "查今天的AI新闻"            # 派worker+等待, 打印总结(默认≤10分钟)
  butler run "任务" --timeout 120 --json
  butler send "任务"                     # 只派发, 打印ID即返回
  butler status T1104xxxx | butler status
  butler stop T1104xxxx
  butler search "关键词"
  butler history --limit 20
  butler export > 对话.md
  butler remind "2026-11-05 09:00" "交报告"
  butler serve                           # 未运行则拉起服务
"""
import json
import os
import socket
import sys
import time
import urllib.parse
import urllib.request
import shutil

BASE = os.path.dirname(os.path.abspath(__file__))
SERVER_PY = os.path.join(BASE, 'router_server.py')
PORT_FILE = os.path.join(os.path.expanduser('~'), '.butler-os', 'server.json')
TIMEOUT = 120


def _base():
    for a in sys.argv:
        if a.startswith('--base='):
            return a.split('=', 1)[1].rstrip('/')
    env = os.environ.get('BUTLER_URL')
    if env:
        return env.rstrip('/')
    try:
        d = json.load(open(PORT_FILE, encoding='utf-8'))
        return d['url'].rstrip('/')
    except Exception:
        pass
    for p in range(9991, 10000):          # 兜底扫描
        url = 'http://127.0.0.1:%d' % p
        try:
            with urllib.request.urlopen(url + '/api/health', timeout=1) as r:
                if r.status == 200:
                    return url.rstrip('/')
        except Exception:
            continue
    return None


def ensure_server():
    """服务未运行 → 后台拉起并等待就绪(≤25s)"""
    if _base():
        return True
    try:
        import subprocess
        pyw = shutil.which('pythonw') or shutil.which('python')
        subprocess.Popen([pyw, SERVER_PY], cwd=BASE,
                         creationflags=0x08000000)   # CREATE_NO_WINDOW
    except Exception as e:
        print('无法启动服务: %s' % e, file=sys.stderr)
        return False
    for _ in range(25):
        time.sleep(1)
        if _base():
            return True
    return False


def _req(method, path, body=None, timeout=TIMEOUT):
    url = _base() + path
    req = urllib.request.Request(url, method=method)
    data = None
    if body is not None:
        req.add_header('Content-Type', 'application/json')
        data = json.dumps(body).encode('utf-8')
    with urllib.request.urlopen(req, data=data, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def cmd_ask(args):
    text = ' '.join(args)
    d = _req('POST', '/api/chat', {'text': text, 'source': 'agent'})
    if '已接收' in d.get('reply', ''):     # 被当任务了 → 提示改用 run
        print('该请求已被派为任务(%s)。若要同步拿结果请用: butler run "..."'
              % d.get('id', ''), file=sys.stderr)
        sys.exit(1)
    print(d['reply'])


def cmd_run(args):
    timeout = 600
    as_json = False
    text_parts = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == '--timeout':
            i += 1
            timeout = int(args[i])
        elif a == '--json':
            as_json = True
        else:
            text_parts.append(a)
        i += 1
    text = ' '.join(text_parts)
    if not text:
        print('用法: butler run "任务" [--timeout 秒] [--json]', file=sys.stderr)
        sys.exit(1)
    deadline = time.time() + timeout
    d = _req('POST', '/api/chat',
             {'text': text, 'source': 'agent', 'wait': True,
              'max_wait': min(900, timeout)}, timeout=timeout + 30)
    tid = d.get('id')
    status = d.get('status', '')
    while tid and status not in ('done', 'failed', 'stopped') \
            and time.time() < deadline:
        time.sleep(3)
        try:
            t = _req('GET', '/api/task/%s?op=detail' % tid, timeout=20)
            status = t.get('status', 'unknown')
        except Exception:
            pass
    if as_json:
        print(json.dumps(d, ensure_ascii=False, indent=1))
        sys.exit(0 if status == 'done' else (2 if status else 3))
    if status == 'done':
        sm = d.get('summary') or ''
        if sm.startswith('['):
            nl = sm.find('\n')
            if nl > 0:
                sm = sm[nl + 1:]
        print(sm or d.get('reply', ''))
        sys.exit(0)
    if status in ('failed', 'stopped'):
        err = d.get('error_code', '') or status
        print('任务失败(%s): %s' % (err, (d.get('raw_answer') or '')[:300]),
              file=sys.stderr)
        sys.exit(2)
    print('超时未完成(任务 %s 仍在后台运行, 可用 butler status %s 查询)'
          % (tid, tid), file=sys.stderr)
    sys.exit(3)


def cmd_send(args):
    text = ' '.join(args)
    d = _req('POST', '/api/chat', {'text': text, 'source': 'agent'})
    m = d.get('id') or ''
    print(m or d['reply'])


def cmd_status(args):
    if args:
        d = _req('GET', '/api/task/%s?op=detail' % args[0])
        print(json.dumps(d, ensure_ascii=False, indent=1))
        return
    d = _req('GET', '/api/status')
    w = d['worker']
    print('worker %d/%d 运行, %d 排队 | 飞书 %s | 端口 %s'
          % (w['running'], w['max_workers'], w['queued'],
             '在线' if d['feishu']['connected'] else '离线', d['port']))


def cmd_search(args):
    q = ' '.join(args)
    d = _req('GET', '/api/search?q=' + urllib.parse.quote(q))
    print(json.dumps(d, ensure_ascii=False, indent=1))


def cmd_history(args):
    limit = 20
    if '--limit' in args:
        limit = int(args[args.index('--limit') + 1])
    rows = _req('GET', '/api/tasks?limit=%d' % limit)['tasks']
    for r in rows:
        print('%s | %s | %s | %s' % (r['id'], r.get('status', ''),
                                     ' '.join((r.get('question') or '')
                                              .split())[:40],
                                     r.get('created', '')))


def cmd_export(args):
    d = _req('GET', '/api/export/md', timeout=60)
    sys.stdout.write(d['markdown'])


def cmd_remind(args):
    if len(args) >= 2:
        when, text = args[0], ' '.join(args[1:])
        d = _req('POST', '/api/chat',
                 {'text': '提醒我 %s %s' % (when, text), 'source': 'agent'})
        print(d['reply'])
        return
    rems = _req('GET', '/api/reminders')['reminders']
    for r in rems:
        print('%s | %s' % (r['dt'], r['text']))


def cmd_serve(args):
    if not ensure_server():
        sys.exit(3)
    d = json.load(open(PORT_FILE, encoding='utf-8'))
    print('服务已运行: %s (PID %d)' % (d['url'], d['pid']))


CMDS = {'ask': cmd_ask, 'run': cmd_run, 'send': cmd_send,
        'status': cmd_status, 'stop': None, 'search': cmd_search,
        'history': cmd_history, 'export': cmd_export, 'remind': cmd_remind,
        'serve': cmd_serve}

if __name__ == '__main__':
    argv = sys.argv[1:]
    if not argv or argv[0] in ('-h', '--help'):
        print(__doc__)
        sys.exit(0)
    cmd, rest = argv[0], argv[1:]
    if _base() is None and cmd != 'serve':
        ensure_server()
    fn = CMDS.get(cmd)
    if fn is None:
        if cmd == 'stop':
            d = _req('POST', '/api/task/%s?op=stop' % rest[0])
            print(d.get('message', '已停止'))
            sys.exit(0)
        print('未知命令: %s\n' % cmd + __doc__, file=sys.stderr)
        sys.exit(1)
    fn(rest)
