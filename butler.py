# -*- coding: utf-8 -*-
"""bot 一键启动器 — 依赖自检 → 配置引导 → 启动 Router 服务

用法: python butler.py [--port 9991] [--check-only]
启动后: 网页聊天流 http://127.0.0.1:<port> 自动可用
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser

DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, DIR)


def check_env():
    """依赖与配置自检; 返回 (ok, 报告行列表)"""
    lines = []
    ok = True
    # Python 版本
    v = sys.version_info
    lines.append('Python %d.%d.%d %s' % (v.major, v.minor, v.micro,
                                         '✓' if v >= (3, 11) else '✗ 需要 3.11+'))
    if v < (3, 11):
        ok = False
    # 可选依赖
    for mod, use in [('lark_oapi', '飞书接入'), ('requests', '部分工人'),
                     ('psutil', '系统监控')]:
        try:
            __import__(mod)
            lines.append('%s ✓ (%s)' % (mod, use))
        except ImportError:
            lines.append('%s ✗ (可选, %s 需要时: pip install %s)'
                         % (mod, use, mod.replace('lark_oapi', 'lark-oapi')))
    # 配置
    cfgp = os.path.join(DIR, 'config', 'bot.json')
    if os.path.isfile(cfgp):
        lines.append('config/bot.json ✓ 已存在')
    else:
        lines.append('config/bot.json ✗ 未创建(从 bot.example.json 复制并填你的 key)')
        lines.append('  → 也可以不填: 核心链路支持免费模型, 先跑起来再说')
    return ok, lines


def open_browser_later(port, delay=3):
    def _open():
        time.sleep(delay)
        try:
            webbrowser.open('http://127.0.0.1:%d' % port)
        except Exception:
            pass
    threading.Thread(target=_open, daemon=True).start()


def main():
    ap = argparse.ArgumentParser(description='bot — 你的个人 AI 管家')
    ap.add_argument('--port', type=int, default=9991, help='服务端口(默认9991, 被占自动+1)')
    ap.add_argument('--check-only', action='store_true', help='只做自检不启动')
    ap.add_argument('--no-browser', action='store_true', help='不自动打开网页')
    args = ap.parse_args()

    ok, lines = check_env()
    print('=== bot 自检 ===')
    for l in lines:
        print(' ', l)
    if args.check_only:
        sys.exit(0 if ok else 1)
    if not ok:
        print('环境不满足, 退出。')
        sys.exit(1)

    os.environ.setdefault('BUTLER_PORT', str(args.port))
    if not args.no_browser:
        open_browser_later(args.port)
    print('=== 启动 Router(端口 %d, 被占自动+1)... ===' % args.port)
    from router_server import main as server_main
    server_main()


if __name__ == '__main__':
    main()
