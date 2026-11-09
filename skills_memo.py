# -*- coding: utf-8 -*-
"""③技能化沉淀 — 同类任务第3次成功 → 自动提炼技能卡 → "再来一次X"带参重放
- maybe_distill(question): 任务done时调用, 判定同类计数, 够3次→LLM提炼技能卡存ReMe procedure
- find_skill(question): 语义命中技能卡 → 返回{path, task_template}供重放
"""
import io
import json
import os
import re
import sys
import threading
import time
import urllib.request

REME = 'http://127.0.0.1:2333'
STATE = None  # 由调用方(setup)注入
_seen_keys = {}


def setup(state_dir):
    global STATE
    STATE = state_dir
    os.makedirs(STATE, exist_ok=True)


def _norm_q(question):
    """任务归一化键: 去数字/日期/城市名等参数后剩主题骨架"""
    q = re.sub(r'T\d{10,}', '', question or '')
    q = re.sub(r'\d{4}-\d{2}-\d{2}|\d{1,2}[点:：时]\d{0,2}分?|\d+°C|\d+(\.\d+)?(km/h|mm|%)', '', q)
    q = re.sub(r'[惠广北 上深]\w{0,2}(天气|气温)', '某地天气', q)  # 城市天气归一
    return ' '.join(q.split())[:40]


def _post(tool, payload, timeout=30):
    req = urllib.request.Request(REME + '/' + tool,
        data=json.dumps(payload, ensure_ascii=False).encode('utf-8'), method='POST')
    req.add_header('Content-Type', 'application/json')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def _count_path():
    return os.path.join(STATE or '.', 'skill_counts.json')


def maybe_distill(question, wtype='codex'):
    if STATE is None:
        setup(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'state'))
    """done时调用(后台线程): 同骨架成功≥3次 → 提炼技能卡。返回提示或''"""
    if wtype not in ('codex', 'opencode'):
        return ''
    key = _norm_q(question)
    if not key or len(key) < 6:
        return ''
    cnt = {}
    try:
        cnt = json.load(open(_count_path(), encoding='utf-8'))
    except Exception:
        pass
    n = cnt.get(key, 0) + 1
    cnt[key] = n
    # 只保留50个键防膨胀
    if len(cnt) > 50:
        for k in sorted(cnt, key=cnt.get)[:-50]:
            del cnt[k]
    try:
        json.dump(cnt, open(_count_path(), 'w', encoding='utf-8'), ensure_ascii=False)
    except Exception:
        pass
    _seen_keys[key] = n
    if n < 3:
        return ''
    if _seen_keys.get(key + '_distilled'):
        return ''
    _seen_keys[key + '_distilled'] = True

    # 后台提炼(LLM), 不阻塞
    def _job():
        try:
            t = _llm(
                '以下任务被用户重复执行了%d次, 每次都成功。把它提炼成一张"技能卡", '
                '以后用户一句话就能重放。只输出严格JSON: '
                '{"name": "技能名(≤12字)", "task_template": "重放指令模板(把可变参数写成<参数名>), ≤80字", '
                '"trigger": "用户会怎么说(≤20字)"}。任务: %s' % (n, key), mt=200)
            import router_brain as _rb
            t = t if t else _llm_local(key, n)
            t = re.sub(r'```[a-z]*', '', t or '')   # 剥LLM围栏
            m = re.search(r'\{[^{}]*\}', t, re.S)
            if not m:
                return
            d = json.loads(m.group(0))
            day = time.strftime('%Y-%m-%d')
            _post('write', {
                'path': 'procedure/skill-%s-%s' % (day, int(time.time()) % 10000),
                'name': '技能: %s' % d.get('name', key[:16]),
                'description': '自动提炼(已成功%d次)。触发语: %s' % (
                    n, d.get('trigger', ''))[:90],
                'content': '# 技能卡: %s' % d.get('name', '') + chr(10) + chr(10) +
                           '重放模板: ' + d.get('task_template', '') + chr(10) +
                           '触发语: ' + d.get('trigger', '') + chr(10) +
                           '已成功次数: %d' % n,
                'metadata': {'valid_from': day, 'source': 'skill-distill'}})
        except Exception:
            pass
    threading.Thread(target=_job, daemon=True).start()
    return '同类任务已成功%d次, 正在自动提炼成技能卡(以后一句话重放)' % n


def _llm_local(key, n):
    try:
        import model_router
        t = model_router.chat(
            '任务"%s"被重复成功执行%d次。只输出JSON: {"name": "技能名≤12字", '
            '"task_template": "重放指令模板≤80字, 可变参数写<参数名>", "trigger": "触发语≤20字"}' % (
                key, n), mt=200, tier='fast')
        return (t[0] if isinstance(t, tuple) else t) or ''
    except Exception:
        return ''


def _llm(prompt, mt=200):
    try:
        import model_router
        t = model_router.chat(prompt, mt=mt, tier='fast')
        return (t[0] if isinstance(t, tuple) else t) or ''
    except Exception:
        return ''


def find_skill(question):
    """重放: 语义找技能卡(以'技能:'开头), 命中返回重放指令; 未命中None"""
    try:
        ans = _post('search', {'query': '技能: ' + question[:30], 'limit': 4},
                    timeout=20).get('answer', '')
        paths = re.findall(r'(procedure/skill-\S+?\.md)', ans)
        if not paths:
            return None
        rd = _post('read', {'path': paths[0].replace('.md', '')})
        txt = rd.get('answer', '')
        m = re.search(r'重放模板[::]\s*([^\n]+)', txt)
        if m and m.group(1).strip():
            return {'path': paths[0], 'template': m.group(1).strip()[:120]}
    except Exception:
        pass
    return None
