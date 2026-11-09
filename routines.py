# -*- coding: utf-8 -*-
"""①通用定时例程 — "每天早8点看GitHub趋势发飞书"
routines.json 由 router 的 routine 快速路径写入; 本模块:
  - parse_routine(msg): 自然语言→{schedule, task} 纯函数
  - check_and_fire(router): 每分钟被 triggers_loop 调, 到点走 /api/chat 派单(结果走全通道推送)
"""
import io
import json
import os
import re
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
ROUTINES = os.path.join(BASE, 'state', 'routines.json')

WD = {'一': 0, '二': 1, '三': 2, '四': 3, '五': 4, '六': 5, '日': 6, '天': 6}


def load():
    try:
        return json.load(open(ROUTINES, encoding='utf-8'))
    except Exception:
        return []


def save(rs):
    os.makedirs(os.path.dirname(ROUTINES), exist_ok=True)
    json.dump(rs, open(ROUTINES, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)


def parse_routine(msg):
    """自然语言例程 → dict 或 None(纯函数, 可测)
    支持: 每天HH:MM / 每周X HH:MM / 每N小时 / 每N分钟 + 任务描述"""
    msg = msg.strip()
    r = {'id': 'R' + time.strftime('%m%d%H%M%S'), 'created': time.strftime('%Y-%m-%d %H:%M'),
         'last_fired': '', 'enabled': True}
    m = re.match(r'^(?:每天|每日)[\s]*([\d]{1,2})[点:：时]([\d]{0,2})分?[,\s]+(.+)', msg)
    if m:
        r.update(kind='daily', hh=int(m.group(1)), mm=int(m.group(2) or 0), task=m.group(3))
        return r
    m = re.match(r'^每周([一二三四五六日天])[\s]*([\d]{1,2})[点:：时]([\d]{0,2})分?[,\s]+(.+)', msg)
    if m:
        r.update(kind='weekly', wd=WD[m.group(1)], hh=int(m.group(2)),
                 mm=int(m.group(3) or 0), task=m.group(4))
        return r
    m = re.match(r'^每([\d]{1,2})小时[\s]*[,，]?(.+)', msg)
    if m:
        r.update(kind='interval_h', n=int(m.group(1)), task=m.group(2), last_ts=0)
        return r
    m = re.match(r'^每([\d]{1,3})分钟[\s]*[,，]?(.+)', msg)
    if m:
        n = int(m.group(1))
        if n < 15:   # 防轰炸: 分钟级例程最小15分钟(验收临时用法除外)
            return None
        r.update(kind='interval_m', n=n, task=m.group(2), last_ts=0)
        return r
    return None


def due(r, now=None):
    """该例程现在是否该触发(纯函数)"""
    now = now or time.localtime()
    k = r.get('kind')
    if k == 'daily':
        cur = now.tm_hour * 60 + now.tm_min
        tgt = r['hh'] * 60 + r['mm']
        today_str = time.strftime('%Y-%m-%d')
        if cur >= tgt and r.get('last_fired') != today_str:
            return True
    elif k == 'weekly':
        if now.tm_wday == r.get('wd'):
            cur = now.tm_hour * 60 + now.tm_min
            tgt = r['hh'] * 60 + r['mm']
            wk = time.strftime('%G-W%V')
            if cur >= tgt and r.get('last_fired') != wk:
                return True
    elif k == 'interval_h':
        if time.time() - r.get('last_ts', 0) >= r['n'] * 3600:
            return True
    elif k == 'interval_m':
        if time.time() - r.get('last_ts', 0) >= r['n'] * 60:
            return True
    return False


def mark_fired(r):
    k = r.get('kind')
    if k == 'daily':
        r['last_fired'] = time.strftime('%Y-%m-%d')
    elif k == 'weekly':
        r['last_fired'] = time.strftime('%G-W%V')
    else:
        r['last_ts'] = time.time()


def describe(rs):
    if not rs:
        return '⏰ 还没有定时例程。对我说"每天8点,做XXX"即可创建。'
    lines = ['⏰ 定时例程 %d 项:' % len(rs)]
    for r in rs:
        k = r.get('kind')
        if k == 'daily':
            when = '每天 %02d:%02d' % (r['hh'], r['mm'])
        elif k == 'weekly':
            when = '每周' + list('一二三四五六日')[r.get('wd', 0)] + ' %02d:%02d' % (r['hh'], r['mm'])
        else:
            when = '每%s' % ('%d小时' % r['n'] if k == 'interval_h' else '%d分钟' % r['n'])
        lines.append('· [%s] %s — %s%s' % (r['id'][-6:], when,
                     ' '.join(r['task'].split())[:40],
                     '' if r.get('enabled', True) else ' (已停用)'))
    return chr(10).join(lines)


def check_and_fire(chat_fn, active_questions=None):
    """触发器入口(triggers_loop每分钟调): 到点→派单→标记→保存
    active_questions: 当前running/queued任务的问题列表 → 同题在跑则本轮跳过(防堆叠)"""
    rs = load()
    fired = 0
    changed = False
    for r in rs:
        if not r.get('enabled', True):
            continue
        try:
            if due(r):
                if active_questions:
                    tnorm = ' '.join(r['task'].split())[:30]
                    if any(tnorm in q or q in r['task'] for q in active_questions):
                        mark_fired(r)   # 视为已处理, 防每分钟重派
                        changed = True
                        continue
                chat_fn('任务：' + r['task'], 'routine')
                mark_fired(r)
                changed = True
                fired += 1
        except Exception:
            pass
    if changed:
        save(rs)
    return fired
