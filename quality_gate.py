# -*- coding: utf-8 -*-
"""轻工人输出质量检查环节 — 用户指令: 回复质量不过关不得发出
三道检查:
  Q1 完整性: 请求里每个实体(城市/引号词)都在结果中出现
  Q2 实质性: 结果长度/结构(非空话)
  Q3 失败透传: 已是失败信息则原样放行(用户该知道失败)
不达标 → 自动重试(换源/重跑) → 重试仍差 → 降级 codex 重做 → 仍差 → 带警告发出
"""
import re


def _entities(instruction):
    """抽必须出现在结果中的实体(城市: 复用extract_cities/引号词)"""
    ents = []
    try:
        import light_workers as _lw
        ents += _lw.extract_cities(instruction, '')
    except Exception:
        pass
    for m in re.finditer(r'["“]([^"”]{2,20})["”]', instruction or ''):
        ents.append(m.group(1))
    return [e for e in dict.fromkeys(ents) if e and len(e) >= 2]


def check(instruction, answer):
    """质检(纯函数): 返回 {ok, reasons[], missing[]}"""
    reasons, missing = [], []
    ans = (answer or '').strip()
    if not ans:
        return {'ok': False, 'reasons': ['空结果'], 'missing': []}
    # Q3 失败透传: 失败信息原样放行(中英文)
    if re.search(r'失败|错误|无法|超时|failed|timeout|error', ans[:120], re.I):
        return {'ok': True, 'reasons': ['(失败信息, 透传)'], 'missing': []}
    # Q2 实质性
    if len(ans) < 20:
        reasons.append('结果过短(%d字符)' % len(ans))
    # Q1 完整性: 城市实体
    miss = []
    for e in _entities(instruction):
        if e not in ans:
            miss.append(e)
    if miss:
        reasons.append('结果缺少实体: ' + '/'.join(miss[:4]))
    ok = not reasons
    return {'ok': ok, 'reasons': reasons, 'missing': miss}


def should_escalate(reasons):
    """是否需要升级到 codex 重做(结构性缺陷而非瞬时失败)"""
    return any('缺少实体' in r for r in reasons)


if __name__ == '__main__':
    cases = [
        ('check two cities', 'weather HW\nweather SH', True),
        ('check HW city', 'weather HW only', False),
        ('search news', '', False),
        ('weather fail', 'get failed: timeout', True),
    ]
    for ins, ans, exp in cases:
        r = check(ins, ans)
        assert r['ok'] == exp, (ins[:14], r)
        print('PASS', ins[:14], '->', r['reasons'] or 'ok')
