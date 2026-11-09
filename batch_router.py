# -*- coding: utf-8 -*-
"""②批处理路由(Superagent Trigger批处理思想) — 飞书/小智连发消息10秒窗口聚合
单用户连发3条=原来烧3次LLM路由+可能拆3个任务; 聚合后一次路由, 语义完整。
用法: on_chat入口先 enqueue_batch(text, source) → 返回 None(等待更多) 或 合并文本(窗口结束)
"""
import threading
import time

_LOCK = threading.Lock()
_BUF = {}          # source_key -> {'texts': [..], 'first_ts': float}
WINDOW = 8.0       # 秒: 首条到达后等8秒收尾
LAST_OUT = {}      # source_key -> 最近一次产出(供测试读)


def _key(source):
    return source.split(':')[0] if ':' in source else source


def enqueue_batch(text, source, window=None):
    """入队; 若本条是窗口首条→睡满窗口→返回合并文本; 否则→返回 None(已并入, 由首条线程处理)"""
    w = window if window is not None else WINDOW
    k = _key(source)
    now = time.time()
    with _LOCK:
        b = _BUF.get(k)
        if b is None:
            _BUF[k] = {'texts': [text], 'first_ts': now}
        else:
            b['texts'].append(text)
            return None
    # 首条线程: 等窗口收尾
    time.sleep(max(0, w - (time.time() - _BUF[k]['first_ts'])))
    with _LOCK:
        b = _BUF.pop(k, None)
    if not b:
        return text
    merged = '\n'.join('[%d] %s' % (i + 1, t) for i, t in enumerate(b['texts'])) \
        if len(b['texts']) > 1 else b['texts'][0]
    LAST_OUT[k] = {'merged': merged, 'n': len(b['texts']), 'ts': time.time()}
    return merged


def reset():
    with _LOCK:
        _BUF.clear()
        LAST_OUT.clear()
