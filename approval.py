# -*- coding: utf-8 -*-
"""审批闸 v2 — 借鉴 agent-autoloop 拍板卡片模型(Superagent哲学: 占用最少注意力)
流程: worker执行遇高危动作 → 拦截挂起 → 飞书交互卡片(带[确认/取消]按钮)
→ 你点按钮(ws回调, 无需公网) → 管家续跑原会话; 等待期进程已退出零占用
卡片状态机: open(等拍板) → done(已执行) / closed(已取消)
"""
import io
import json
import os
import re
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
APPROVALS = os.path.join(BASE, 'state', 'approvals.json')
_lock = threading.Lock()

# 高危动作关键词(命中即拦截; 大小写不敏感; 用户可加)
RISK_KW = ('发送邮件', '发邮件', '转账', '支付', '购买', '下单', '删除文件', '删除目录',
           'rm -rf', '格式化', '重启服务', '关机', '对外发布', '发到群里', '群发',
           '提交代码', 'git push', '发布上线', '退订', '注销')


def risk_check(text):
    """高危动作判定(纯函数): 返回命中的关键词或 None"""
    low = (text or '').lower()
    for kw in RISK_KW:
        if kw.lower() in low:
            return kw
    return None


def _load():
    try:
        return json.load(open(APPROVALS, encoding='utf-8'))
    except Exception:
        return {}


def _save(d):
    os.makedirs(os.path.dirname(APPROVALS), exist_ok=True)
    json.dump(d, open(APPROVALS, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)


def create(task_id, question, action_desc, risk_kw):
    """创建待审批记录; 返回 approval_id"""
    with _lock:
        d = _load()
        aid = 'A' + time.strftime('%m%d%H%M%S')
        d[aid] = {'task_id': task_id, 'question': question[:100],
                  'action': action_desc[:200], 'risk': risk_kw,
                  'state': 'open', 'created': time.strftime('%Y-%m-%d %H:%M:%S'),
                  'decided': ''}
        # 只留30条防膨胀
        if len(d) > 30:
            for k in sorted(d, key=lambda x: d[x]['created'])[:-30]:
                del d[k]
        _save(d)
        return aid


def decide(aid, approved, note=''):
    """拍板结果落账; 返回该审批或None"""
    with _lock:
        d = _load()
        a = d.get(aid)
        if not a or a['state'] != 'open':
            return None
        a['state'] = 'done' if approved else 'closed'
        a['decided'] = time.strftime('%Y-%m-%d %H:%M:%S')
        a['note'] = note[:100]
        _save(d)
        return a


def get(aid):
    return _load().get(aid)


def pending_list():
    return [a for a in _load().values() if a['state'] == 'open']


def wait_decision(aid, timeout=3600):
    """阻塞等拍板(在worker线程里, 不占LLM token); 超时视为拒绝"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        a = get(aid)
        if a and a['state'] != 'open':
            return a
        time.sleep(5)
    decide(aid, approved=False, note='超时未拍板, 自动取消')
    return get(aid)
