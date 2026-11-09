# -*- coding: utf-8 -*-
"""每日简报预计算 + 缓存
定时任务(触发器06:00)自动执行简报调研 → 结果写入 state/daily_brief.json
用户通过小智/网页/CLI问"简报" → 秒回缓存, 不现算
"""
import json
import os
import time

BASE = os.path.dirname(os.path.abspath(__file__))
BRIEF_FILE = os.path.join(BASE, 'state', 'daily_brief.json')


def load():
    try:
        d = json.load(open(BRIEF_FILE, encoding='utf-8'))
        return d if d.get('date') == time.strftime('%Y-%m-%d') else None
    except Exception:
        return None


def save(text, source='schedule'):
    os.makedirs(os.path.dirname(BRIEF_FILE), exist_ok=True)
    json.dump({'date': time.strftime('%Y-%m-%d'), 'text': text,
               'generated': time.strftime('%Y-%m-%d %H:%M:%S'),
               'source': source},
              open(BRIEF_FILE, 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)


def get_or_hint():
    """有当日缓存返回缓存文本; 没有返回提示语(用户可说"生成简报")"""
    d = load()
    if d:
        return d['text']
    return '今日简报尚未生成。可对我说"生成简报"立即生成, 或等到每日定时自动生成。'


def is_fresh():
    return load() is not None
