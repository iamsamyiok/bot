# -*- coding: utf-8 -*-
"""UXL — 用户视角体验记录系统 (User eXperience Log)

目的(用户指令): 无时无刻记录"用户发送的内容"与"用户看到的所有文字内容",
作为后续改进 Router/Worker 系统的依据。

与 ledger 的区别:
  ledger = 系统运行账本(内部视角: 状态机/事件/进度)
  UXL    = 用户体验流水(用户视角: 我发了什么 / 我看到了什么 / 在哪个端 / 等了多久)

每条记录:
  {ts, source, channel_event, direction, text, task_id, latency_ms, meta}
  direction: 'in'(用户发送) | 'out'(用户看到)
  channel_event: 'send'|'reply'|'ack'|'progress'|'push_done'|'push_ntfy'|'card'|'voice_out'|'brief'

落盘: state/uxl.jsonl (append-only, 单行<8KB截断, 按月轮转)
查询: recent()/by_day()/stats() 供后续 ZCode 分析改进
"""
import io
import json
import os
import re
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
UXL_DIR = os.path.join(BASE, 'state', 'uxl')
_lock = threading.Lock()

# 脱敏: 记录时不落密钥类内容(用户消息里可能带key, 改进分析不需要精确值)
SENSITIVE = re.compile(
    r'(sk-[A-Za-z0-9_\-]{8,}|(?:password|passwd|密码|密钥|token|api[-_]?key|凭证)(?:是|为|[:：=])\s*\S+)',
    re.I)


def _sanitize(text):
    if not text:
        return ''
    t = SENSITIVE.sub('[REDACTED]', str(text))
    return t[:6000]


def _month_file():
    os.makedirs(UXL_DIR, exist_ok=True)
    return os.path.join(UXL_DIR, 'uxl-%s.jsonl' % time.strftime('%Y-%m'))


def log(channel_event, direction, text, source='', task_id='', latency_ms=None, meta=None):
    """记录一条用户体验事件(线程安全, 失败静默不阻断业务)"""
    try:
        rec = {
            'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
            'ts_epoch': time.time(),
            'source': source,
            'channel_event': channel_event,
            'direction': direction,
            'text': _sanitize(text),
            'task_id': task_id or '',
            'latency_ms': latency_ms,
            'meta': meta or {},
        }
        with _lock:
            with open(_month_file(), 'a', encoding='utf-8') as f:
                f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    except Exception:
        pass


def recent(n=50, source=None, direction=None):
    """最近 n 条(跨当月文件倒序)"""
    try:
        files = sorted(os.listdir(UXL_DIR), reverse=True)[:2]
    except OSError:
        return []
    out = []
    for fn in files:
        try:
            lines = open(os.path.join(UXL_DIR, fn), encoding='utf-8').read().splitlines()
        except OSError:
            continue
        for ln in reversed(lines):
            try:
                r = json.loads(ln)
            except Exception:
                continue
            if source and r.get('source') != source:
                continue
            if direction and r.get('direction') != direction:
                continue
            out.append(r)
            if len(out) >= n:
                return out
    return out


def by_day(day=None):
    month = (day or time.strftime('%Y-%m-%d'))[:7]
    fn = os.path.join(UXL_DIR, 'uxl-%s.jsonl' % month)
    prefix = day or time.strftime('%Y-%m-%d')
    out = []
    try:
        for ln in open(fn, encoding='utf-8'):
            try:
                r = json.loads(ln)
                if r.get('ts', '').startswith(prefix):
                    out.append(r)
            except Exception:
                continue
    except OSError:
        pass
    return out


def stats(day=None):
    """当日体验统计: 事件分布/渠道分布/平均确认延迟/推送延迟"""
    recs = by_day(day)
    from collections import Counter
    ev = Counter(r.get('channel_event') for r in recs)
    src = Counter(r.get('source') for r in recs)
    lat = [r['latency_ms'] for r in recs
           if r.get('channel_event') == 'reply' and r.get('latency_ms')]
    push_lat = [r['latency_ms'] for r in recs
                if r.get('channel_event') == 'push_done' and r.get('latency_ms')]
    return {
        'day': day, 'events': len(recs),
        'by_event': dict(ev), 'by_source': dict(src),
        'avg_reply_ms': int(sum(lat) / len(lat)) if lat else None,
        'avg_push_ms': int(sum(push_lat) / len(push_lat)) if push_lat else None,
        'max_reply_ms': max(lat) if lat else None,
    }


def summary_text(day=None):
    """给人看的当日体验摘要"""
    s = stats(day)
    if not s['events']:
        return 'UXL %s 无记录。' % s['day']
    lines = ['📊 UXL %s: %d 条事件' % (s['day'], s['events']),
             '· 渠道: ' + ', '.join('%s×%d' % kv for kv in
                                    sorted(s['by_source'].items(), key=lambda x: -x[1])),
             '· 事件: ' + ', '.join('%s×%d' % kv for kv in
                                    sorted(s['by_event'].items(), key=lambda x: -x[1]))]
    if s['avg_reply_ms'] is not None:
        lines.append('· 平均确认延迟: %dms (最慢 %dms)' % (s['avg_reply_ms'], s['max_reply_ms'] or 0))
    if s['avg_push_ms'] is not None:
        lines.append('· 平均完成推送延迟: %dms' % s['avg_push_ms'])
    return '\n'.join(lines)


# ---------- 各埋点便捷函数(on_chat/_on_summary/feishu/xiaozhi 调用) ----------

def user_sent(text, source, task_id='', latency_ms=None):
    log('send', 'in', text, source, task_id, latency_ms)


def user_reply(text, source, task_id='', latency_ms=None):
    """用户看到的即时回复(ack/直答/错误)"""
    log('reply', 'out', text, source, task_id, latency_ms)


def user_progress(text, source, task_id=''):
    log('progress', 'out', text, source, task_id)


def user_push_done(text, source, task_id='', latency_ms=None, via='feishu'):
    """任务完成推送(用户在XX端看到)"""
    log('push_done', 'out', text, source, task_id, latency_ms, {'via': via})


def user_push_ntfy(text, task_id='', latency_ms=None):
    log('push_ntfy', 'out', text, 'phone', task_id, latency_ms, {'via': 'ntfy'})


def user_card(text, source, task_id=''):
    log('card', 'out', text, source, task_id)


if __name__ == '__main__':
    day = sys.argv[1] if len(sys.argv) > 1 else None
    print(summary_text(day))
