# -*- coding: utf-8 -*-
"""③Watchdog 自愈(Superagent五模块之一) — 管家进程死亡自动拉起
独立进程(watchdog.py --daemon, 由计划任务每分钟唤起):
  1. 检查 9991 端口; 无监听 → start_router.bat 拉起
  2. 检查 9721(Lab)/2333(ReMe) → 各自脚本拉起
  3. 日志落 state/watchdog.log(自愈事件可审计)
单用户场景: 轻量探活即可, 无需 supervisor 级复杂度。
"""
import io
import subprocess
import sys
import time
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
LAB = os.environ.get('BUTLER_LAB_DIR', '')  # 可选: Nightly AI Lab 目录
LOG = BASE + r'\state\watchdog.log'


def log(msg):
    try:
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write('%s %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), msg))
    except Exception:
        pass


def alive(url, timeout=4):
    try:
        req = urllib.request.Request(url, data=b'{}', method='POST') \
            if '2333' in url else urllib.request.Request(url)
        req.add_header('Content-Type', 'application/json')
        urllib.request.urlopen(req, timeout=timeout)
        return True
    except urllib.error.HTTPError:
        return True    # 有HTTP响应(404也算活)
    except Exception:
        return False


def start_bat(bat_path, cwd):
    subprocess.Popen(['cmd', '/c', bat_path], cwd=cwd,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=0x08)  # DETACHED_PROCESS


def check(url, bat, cwd, name):
    if alive(url):
        return False
    start_bat(bat, cwd)
    log('%s 掉线 → 已拉起(%s)' % (name, bat))
    return True


def main():
    acted = []
    if check('http://127.0.0.1:9991/api/health',
             BASE + r'\start_router.bat', BASE, '管家9991'):
        acted.append('butler')
    if check('http://127.0.0.1:9721/docs',
             LAB + r'\scripts\dashboard.bat', LAB, 'Lab9721'):
        acted.append('lab')
    if check('http://127.0.0.1:2333/version',
             os.path.join(reme_home, 'scripts', 'autostart_reme.bat'),
             reme_home, 'ReMe2333'):
        acted.append('reme')
    if acted:
        print('自愈: ' + ', '.join(acted))
    else:
        print('三平台健康')


if __name__ == '__main__':
    main()
