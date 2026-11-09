# -*- coding: utf-8 -*-
"""bot 配置层 — 全部路径与密钥集中于此, 用户只改 config/bot.json

优先级: ①程序目录 config/bot.json(推荐: 手改/后续Web设置页均可)
       ②环境变量 BUTLER_HOME(自定义数据目录)
安全: 真实密钥只存在于本机 bot.json, 永不入库(见 .gitignore)
"""
import json
import os
import sys

_DIR = os.path.dirname(os.path.abspath(__file__))


def home():
    """数据根目录: 环境变量 BUTLER_HOME → 用户主目录 ~/.butlerbot"""
    h = os.environ.get('BUTLER_HOME') or os.path.join(
        os.path.expanduser('~'), '.butlerbot')
    os.makedirs(h, exist_ok=True)
    return h


_CFG_PATH = os.path.join(_DIR, 'config', 'bot.json')
_cfg = None


def _load():
    global _cfg
    if _cfg is not None:
        return _cfg
    _cfg = {}
    for p in (_CFG_PATH, os.path.join(_DIR, 'config', 'keys.json')):
        if os.path.exists(p):
            try:
                _cfg = json.load(open(p, encoding='utf-8'))
                break
            except Exception:
                _cfg = {}
    return _cfg


def get(section, name, default=''):
    """读配置: config/bot.json → {section: {name: value}}"""
    return _load().get(section, {}).get(name, default)


def env(name, default=''):
    """读环境变量(密钥也可走环境)"""
    return os.environ.get(name, default)


def KEY(name, default=''):
    """密钥: bot.json keys 段 → 环境变量"""
    v = _load().get('keys', {}).get(name, '')
    return v or env(name, default)


def path(name, default=None):
    """路径配置: 默认全部落在 ~/.butlerbot 下(本机数据目录)"""
    d = {
        'state': os.path.join(home(), 'state'),
        'desktop_dir': os.path.join(home(), 'documents'),
        'knowledge_dir': os.path.join(home(), 'knowledge'),
        'experience_dir': os.path.join(home(), 'knowledge', 'experience'),
    }
    configured = _load().get('paths', {}).get(name)
    return configured or default or d.get(name, os.path.join(home(), name))


def state_dir():
    return path('state')


def desktop_dir():
    return path('desktop_dir')


def knowledge_dir():
    return path('knowledge_dir')


def experience_dir():
    return path('experience_dir')
