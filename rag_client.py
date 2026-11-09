# -*- coding: utf-8 -*-
"""RAG 知识库客户端 — 常驻 MCP stdio 会话调 search_knowledge

懒启动: 首次查询才拉起 RAG 服务(冷启动十几秒, 之后毫秒级); 任何失败返回 []
永不抛异常, 永不阻塞主流程(等待有超时)。
"""
import json
import os
import queue
import subprocess
import threading
import time

RAG_DIR = os.environ.get('BUTLER_RAG_DIR', '')  # 可选: multimodal-rag-mcp 目录, 留空=RAG关闭
ARGS = [os.path.join(RAG_DIR, '.venv', 'Scripts', 'python.exe'),
        '-m', 'app', 'serve', '--config', os.path.join(RAG_DIR, 'config.toml')]

_proc = None
_msgs = queue.Queue()
_lock = threading.Lock()
_nid = [100]


def _reader():
    for line in _proc.stdout:
        try:
            _msgs.put(json.loads(line.decode('utf-8', 'replace')))
        except Exception:
            pass          # 日志/非JSON行跳过


def _send(obj):
    _proc.stdin.write((json.dumps(obj) + '\n').encode('utf-8'))
    _proc.stdin.flush()


def _wait(rid, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            m = _msgs.get(timeout=0.5)
        except queue.Empty:
            continue
        if m.get('id') == rid:
            return m
    return None


def _rpc(method, params, rid, wait=10):
    _send({'jsonrpc': '2.0', 'id': rid, 'method': method, 'params': params})
    return _wait(rid, wait)


def _start():
    global _proc
    _proc = subprocess.Popen(ARGS, cwd=RAG_DIR, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    threading.Thread(target=_reader, daemon=True).start()
    _rpc('initialize', {'protocolVersion': '2024-11-05', 'capabilities': {},
                        'clientInfo': {'name': 'secretary', 'version': '1.0'}},
         rid=1, wait=25)
    _send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})


def search(query, top_k=3, max_chars=500):
    """→ [{'file_name','content'}...]; 失败/未配置返回[]"""
    if not os.path.exists(ARGS[0]):
        return []          # 便携机未装 RAG 知识库 → 静默停用检索
    with _lock:
        try:
            if _proc is None or _proc.poll() is not None:
                _start()
            _nid[0] += 1
            m = _rpc('tools/call',
                     {'name': 'search_knowledge',
                      'arguments': {'query': str(query)[:300], 'top_k': top_k,
                                    'max_content_chars': max_chars}},
                     rid=_nid[0], wait=10)
            if not m or m.get('error'):
                return []
            txt = (m.get('result', {}).get('content') or [{}])[0].get('text', '')
            d = json.loads(txt)
            out = []
            for r in (d.get('results') or [])[:top_k]:
                c = str(r.get('content', '')).strip()
                if c:
                    out.append({'file_name': str(r.get('file_name', '')),
                                'content': c[:max_chars]})
            return out
        except Exception:
            return []


if __name__ == '__main__':
    t0 = time.time()
    print(search('水箱 FEA 仿真经验', top_k=2), '%.1fs' % (time.time() - t0))
    t0 = time.time()
    print(search('管家程序', top_k=1), '%.1fs' % (time.time() - t0))
