# -*- coding: utf-8 -*-
"""Agentic OS Server — 本地多任务 Agent 服务端 v2 (管家形态)

用户唯一交互面 = 聊天流(网页 + 飞书, 语义/格式完全一致):
  每句话 → Router 大脑消化 → 直接回答(问答/进度) 或 编排派发(1..5 worker)
  → 即时回执 → 完成后总结性回复 → 全程进聊天流与全局记忆
Web 组织方式参考 agentic-os (MIT); 飞书长连接模式参考 ControlMesh (MIT);
Worker = openai/codex (Apache-2.0)。许可证见 LICENSES/。
"""
import json
import re
import os
import socket
import subprocess
import config as _cfgp
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import nowin                     # 全局抑制子进程黑窗(必须最先)
BASE = os.path.dirname(os.path.abspath(__file__))
VERSION = '6.7'
_T0 = time.time()
sys.path.insert(0, BASE)

WEB = os.path.join(BASE, 'web', 'index.html')
STATE = os.path.join(BASE, 'state')
LOGS_DIR = os.path.join(STATE, 'worker_logs')
SETF = os.path.join(STATE, 'settings.json')


def get_set(name, default=None):
    try:
        return json.load(open(SETF, encoding='utf-8')).get(name, default)
    except Exception:
        return default


def set_set(name, val):
    try:
        d = json.load(open(SETF, encoding='utf-8'))
    except Exception:
        d = {}
    d[name] = val
    json.dump(d, open(SETF, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)


TRGF = os.path.join(STATE, 'triggers.json')


def load_triggers():
    try:
        return json.load(open(TRGF, encoding='utf-8'))
    except Exception:
        return []


def save_triggers(trs):
    json.dump(trs, open(TRGF, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)


def _triggers_loop(router):
    """事件驱动: 定时触发器到点即以用户名义发指令(DeepSeek superagent 主动性)"""
    while True:
        try:
            now = time.strftime('%H:%M')
            today = time.strftime('%Y-%m-%d')
            changed = False
            for g in load_triggers():
                if g.get('time') == now and g.get('last') != today:
                    trs = load_triggers()
                    for x in trs:
                        if x.get('id') == g.get('id'):
                            x['last'] = today
                    save_triggers(trs)
                    threading.Thread(target=router.on_chat,
                                     args=(g.get('text', ''), 'schedule'),
                                     daemon=True).start()
                    changed = True
            # ①通用定时例程: 每分钟检查, 到点派单(结果走全通道推送)
            import routines as _rt
            _active = [t.get('question', '') for t in router.W._tasks.values()
                       if t.get('status') in ('running', 'queued')]
            _rt.check_and_fire(lambda text, src: router.on_chat(text, src),
                               active_questions=_active)
        except Exception:
            pass
        time.sleep(20)


def free_port():
    for p in range(9991, 10000):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(('127.0.0.1', p))
            s.close()
            return p
        except OSError:
            s.close()
    raise RuntimeError('端口 9991-9999 全部被占用')


def _ntfy_push(text, title='管家'):
    """任务完结推手机(复用 task-notify 的 ntfy 通道); 失败静默(不阻断主流程)。
    用 JSON 发布格式: 中文标题放 HTTP header 会被 latin-1 编码卡死"""
    try:
        import urllib.request
        topic = ''
        for line in open('C:/Tools/task-notify/config.txt',
                         encoding='utf-8', errors='replace'):
            if line.strip().startswith('NTFY_TOPIC='):
                topic = line.split('=', 1)[1].strip()
                break
        if not topic:
            return False
        body = json.dumps({'topic': topic, 'message': text,
                           'title': title, 'tags': ['robot']}).encode('utf-8')
        req = urllib.request.Request('https://ntfy.sh/', data=body,
                                     method='POST')
        req.add_header('Content-Type', 'application/json')
        urllib.request.urlopen(req, timeout=10).read()
        return True
    except Exception:
        return False


def _fmt_tasks(rows):
    if not rows:
        return '[任务列表] (空)'
    lines = ['[任务列表] 最近%d条' % len(rows)]
    for r in rows:
        lines.append('%s | %s | %s | %s' % (
            r.get('id', ''), r.get('status', ''),
            ' '.join((r.get('question') or '').split())[:30],
            r.get('created', '')))
    return '\n'.join(lines)


DESKTOP1 = _cfgp.desktop_dir()
KB_PROJECT_DIR = os.path.join(_cfgp.knowledge_dir(), 'projects')
TASK_LOG = os.path.join(DESKTOP1, 'task_log.md')


def _syslog(text):
    """模块级系统日志: 直接追加 ledger.jsonl(append-only, 不经 W 广播)"""
    try:
        with open(os.path.join(STATE, 'ledger.jsonl'), 'a', encoding='utf-8') as f:
            f.write(json.dumps({'type': 'sys', 'text': text, 'ts': time.strftime('%Y-%m-%d %H:%M:%S')}, ensure_ascii=False) + chr(10))
    except Exception:
        pass


def _archive_research(t):
    """调研任务完结归档(标准三件套): 报告副本入RAG项目库 → RAG同步 → 交办事项记录
    报告定位: 优先 raw_answer 里「产物文件: <路径>」; 退化扫 Desktop1 最新 *调研报告.md"""
    import re as _re
    import shutil
    raw = t.get('raw_answer') or ''
    report = ''
    m = _re.search(r'产物文件[:：]\s*(\S+?\.md)', raw)
    if m and os.path.isfile(m.group(1)):
        report = m.group(1)
    if not report:  # 退化: Desktop1 最近10分钟内的 *调研报告.md
        try:
            cutoff = time.time() - 600
            cands = [os.path.join(DESKTOP1, f) for f in os.listdir(DESKTOP1)
                     if f.endswith('调研报告.md')
                     and os.path.getmtime(os.path.join(DESKTOP1, f)) > cutoff]
            if cands:
                report = max(cands, key=os.path.getmtime)
        except OSError:
            pass
    if not report:
        _syslog('[research归档] 未定位到报告文件: %s' % t.get('id'))
        return
    # ① 副本入项目库 + ② RAG 后台同步
    try:
        dest = os.path.join(KB_PROJECT_DIR, os.path.basename(report))
        if not os.path.exists(dest):
            shutil.copyfile(report, dest)
        if os.path.isdir(os.path.dirname(KB_PROJECT_DIR)):
            subprocess.Popen([sys.executable,
                          os.path.join(os.path.dirname(KB_PROJECT_DIR), 'sync_kb.py')],
                         cwd=os.path.dirname(KB_PROJECT_DIR),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        _syslog('[research归档] 项目库/RAG失败: %s' % e)
    # ③ 交办事项记录(常设规则4: ≤200字/条, 只追加)
    try:
        body = ('## %s 调研任务交付(%s)\n调研「%s」。报告: %s; 副本已入RAG项目库并触发同步。'
                % (time.strftime('%Y-%m-%d %H:%M'), t.get('id', '')[-8:],
                   ' '.join((t.get('question') or '').split())[:60], report))
        if not body.endswith(chr(10)):
            body += chr(10)
        with open(TASK_LOG, 'a', encoding='utf-8') as f:
            f.write(chr(10) + body)
    except OSError:
        pass
    _syslog('[research归档] %s → 项目库+RAG同步+交办记录 ✓' % os.path.basename(report))


def _fmt_search(res):
    lines = ['[全局搜索] 关键词命中 %d 条, RAG 命中 %d 条' % (
        len((res.get('keyword') or {}).get('tasks', [])), len(res.get('rag', [])))]
    for k in (res.get('keyword') or {}).get('tasks', [])[:5]:
        lines.append('%s | %s' % (k.get('id', ''), (k.get('snippet') or '')[:60]))
    for h in res.get('rag', []):
        lines.append('RAG:%s | %s' % (h.get('file_name', ''), (h.get('content') or '')[:60]))
    if res.get('rag_error'):
        lines.append('(RAG原始错误: %s)' % res['rag_error'])
    return '\n'.join(lines)


DIGEST_DIR = os.path.join(STATE, 'digest')
_digest_state = {'n': 0, 'day': ''}


def _digest_path():
    """滚动摘要按天分文件, 旧的自然过期"""
    day = time.strftime('%Y-%m-%d')
    return os.path.join(DIGEST_DIR, 'chat_digest-%s.md' % day)


def _maybe_digest(dlg_lines):
    """P3滚动摘要: 每累计6条消息, 后台把最近对话压缩合并进当日摘要(不阻塞回复)"""
    try:
        os.makedirs(DIGEST_DIR, exist_ok=True)
    except OSError:
        return
    _digest_state['n'] += 1
    if _digest_state['n'] % 6 or len(dlg_lines) < 4:
        return
    lines = list(dlg_lines)

    def _job():
        try:
            import router_brain as rb
            old = ''
            try:
                old = open(_digest_path(), encoding='utf-8').read()[:800]
            except OSError:
                pass
            t = rb._llm('把AI管家与用户的最近对话压缩成要点摘要(≤8行; 保留人名/文件名/决定/'
                        '未完事项, 已完成事项一句话带过), 与既有摘要合并去重, 只输出新摘要全文。'
                        '【既有摘要】' + (old or '(无)') + chr(10) + '【最近对话】' + chr(10)
                        + chr(10).join(lines[-10:]), mt=400)
            t = ' '.join((t or '').split())
            if t and not t.startswith(('抱歉', '无法')):
                with open(_digest_path(), 'w', encoding='utf-8') as f:
                    f.write(t[:1000])
        except Exception:
            pass
    threading.Thread(target=_job, daemon=True).start()


def _with_digest(dialog_ctx):
    """直答上下文注入滚动摘要 — 超出10轮窗口的旧对话靠它不失忆"""
    try:
        dg = open(_digest_path(), encoding='utf-8').read().strip()
        if dg:
            return '【更早对话摘要】' + dg + chr(10) + dialog_ctx
    except OSError:
        pass
    return dialog_ctx


class Router:
    def __init__(self):
        import codex_worker as W
        import feishu_adapter as F
        self.W = W
        self.F = F
        W.start(push_done_cb=None,
                push_summary_cb=self._on_summary, max_workers=3)
        F.start(self.on_chat)

    # ---- 聊天统一入口(Web与飞书同一路径) ----
    def on_chat(self, text, source, image=None):
        import router_brain
        W, F = self.W, self.F
        t0 = time.time()
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        W._append({'type': 'user', 'text': text, 'source': source, 'ts': now})
        # ③技能重放: 派单前查技能卡, 命中且用户语气是"再来/重跑"→按模板重派
        try:
            import skills_memo
            skills_memo.setup(STATE)
            if re.search(r'再来一次|重跑|再跑一次|上次那个.{0,20}(再来|重跑)', text):
                sk = skills_memo.find_skill(text)
                if sk:
                    text = sk['template']
                    W._append({'type': 'router', 'text': '[技能重放] %s' % sk['path'],
                               'ts': now})
        except Exception:
            pass
        mem = ''
        try:
            import memory_engine
            mem = memory_engine.context_for(text)[:1000]
        except Exception:
            pass
        try:  # P2画像注入: 路由与直答都看得见"你是谁"(原先只有记忆查询快路径才查)
            prof = [p.get('text', '') for p in memory_engine.profile_list()[-20:]
                    if p.get('text')]
            if prof:
                mem = (mem + chr(10) + '【用户画像】' + ' ; '.join(prof)).strip()
        except Exception:
            pass
        dlg_lines = []
        try:
            recents = W.recent_dialog(10)
            n = len(recents)
            for idx, it in enumerate(recents):
                pre = '用户' if it['type'] == 'user' else '管家'
                # P2分层: 最近3轮全文(≤400字) + 更早的80字 — 引用/指代不再失真
                cap = 400 if idx >= n - 3 else 80
                dlg_lines.append(pre + ': ' + ' '.join(it['text'].split())[:cap])
        except Exception:
            pass
        dialog_ctx = chr(10).join(dlg_lines)
        _maybe_digest(dlg_lines)          # P3滚动摘要: 后台压缩旧对话, 不阻塞
        dialog_ctx = _with_digest(dialog_ctx)
        # 确定性快路径(与Web/飞书完全同一实现)
        t = text.strip()
        d = {'kind': 'reply'}
        # 记忆查询快速路径
        m_mq = re.search(r'(?:我有没有|查一下|帮我查|告诉我).{0,8}(?:关于|有关)?(.{1,20})(?:的)?记忆', text)
        if m_mq:
            kw = m_mq.group(1).strip()
            try:
                import memory_engine
                prof = [p['text'] for p in memory_engine.profile_list() if kw in p.get('text', '')]
                sr = memory_engine.search_keyword(kw, 5)
                notes = sr.get('notes', [])
                tasks = sr.get('tasks', [])
                if prof or notes or tasks:
                    parts = ['关于「%s」的记忆:' % kw]
                    for p_text in prof:
                        parts.append('· ' + p_text)
                    for n in notes:
                        parts.append('· ' + (n.get('snippet') or n.get('content', ''))[:80])
                    for tk in tasks:
                        parts.append('· [%s] %s' % (tk.get('id', ''), (tk.get('question') or tk.get('snippet') or '')[:60]))
                    reply = chr(10).join(parts)
                else:
                    reply = '没有找到关于「%s」的记忆。可对我说"记住: …"来添加。' % kw
            except Exception as e:
                reply = '记忆检索失败: %s' % str(e)[:80]
        elif t.startswith(('任务列表', '列表', '/tasks')):
            reply = _fmt_tasks(W.list_tasks(10))
        elif t in ('状态', '/status'):
            s = W.status()
            reply = '[服务状态] worker %d(上限%d, %d运行/%d排队) | codex: %s' % (
                s['max_workers'], 3, s['running'], s['queued'],
                os.path.basename(s['codex']))
        elif t.startswith(('搜索记忆 ', '搜记忆 ', '/搜索 ')):
            # 多工人版语义: 「搜索X」= web 搜索 worker(router_brain 路由); 本地记忆搜索=「搜索记忆X」
            try:
                import memory_engine
                reply = _fmt_search(memory_engine.search_hybrid(t.split(' ', 1)[1], 5))
            except Exception as e:
                reply = '[搜索错误] %s' % e
        else:
            try:
                d = router_brain.route(text, mem, W.snapshot_for_brain(10),
                                       dialog_ctx=dialog_ctx)
            except Exception as e:
                err = 'route异常: ' + traceback.format_exc()[-300:]
                W._append({'type': 'router', 'text': '[internal] ' + err,
                           'ts': time.strftime('%Y-%m-%d %H:%M:%S')})
                d = {'kind': 'task', 'reply': None, 'note': '',
                     'subtasks': [{'brief': t[:10], 'instruction': t}]}
            if image and d['kind'] != 'remind':
                d = {'kind': 'task', 'reply': None, 'subtasks': None,
                     'note': '收到图片, 已派 worker 看图处理'}
            if d['kind'] == 'remind':
                rem = {'id': 'R' + time.strftime('%m%d%H%M%S'),
                       'dt': d['when'], 'text': d['text'], 'source': source,
                       'fired': False}
                rems = get_set('reminders') or []
                rems.append(rem)
                set_set('reminders', rems)
                reply = d['reply']
            elif d['kind'] == 'continue' and d.get('target'):
                ok, msg = W.continue_task(d['target'], t, source)
                reply = ('▶ 已在任务 #%s 的会话上继续你的话, 完成后这里给结果。'
                         % d['target']) if ok else ('续聊失败: %s' % msg)
            elif d['kind'] == 'memory_write':
                fact = d['fact']
                import memory_engine
                memory_engine.profile_add(fact)
                # G1统一: notes_fts 只写本地; ReMe 侧全权交 reme_worker
                # (冲突消解+procedure分层), 防止 add_note 双写造成重复记忆
                memory_engine.add_note(fact, reme=False)
                try:
                    import reme_worker as _rw
                    _r = _rw.run_reme('记住: ' + fact)
                    reply = (d['reply'] + chr(10) + _r) if d['reply'] else _r
                except Exception:
                    reply = d['reply'] + chr(10) + '🧠 已同步到数字分身记忆'
            elif d['kind'] == 'routine_create':
                import routines as _rt
                r = _rt.parse_routine(t)
                if r:
                    rs = _rt.load()
                    rs.append(r)
                    _rt.save(rs)
                    reply = '⏰ 例程已创建[%s]: %s — %s' % (
                        r['id'][-6:], ('每天 %02d:%02d' % (r['hh'], r['mm'])) if r['kind'] == 'daily'
                        else ('每周%d %02d:%02d' % (r['wd']+1, r['hh'], r['mm'])) if r['kind'] == 'weekly'
                        else ('每%d小时' % r['n'] if r['kind'] == 'interval_h' else '每%d分钟' % r['n']),
                        r['task'][:50])
                else:
                    reply = '例程格式没识别。示例: "每天8点, 看GitHub趋势发我"'
            elif d['kind'] == 'routine_list':
                import routines as _rt
                reply = _rt.describe(_rt.load())
            elif d['kind'] == 'reply':
                reply = d['reply']
            else:
                tid = W.new_task(t, source=source, plan=d['subtasks'],
                                 plan_note=d.get('note', ''), image=image,
                                 wtype=d.get('worker', 'codex'))
                # 任务气泡本身就是回执(时间线立即可见), 不再追加重复的 ack 气泡;
                # reply 仅作为飞书侧的文字回执返回
                reply = '[已接收 %s] %s。完成后这里会给总结; 可发「进度 %s」随时查。' % (
                    tid, d.get('note') or '已派 worker 执行', tid)
        if d.get('kind') == 'reply':
            W._append({'type': 'router', 'text': reply,
                       'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
                       'elapsed': round(time.time() - t0, 2)})
            def _extract():
                try:
                    import router_brain
                    fact = router_brain.extract_profile(text, reply)
                    if fact:
                        import memory_engine
                        idx = router_brain.profile_conflict(
                            fact, memory_engine.profile_list())
                        memory_engine.profile_add(fact, replace_idx=idx)
                except Exception:
                    pass
            threading.Thread(target=_extract, daemon=True).start()
            # 直答Q&A入全局记忆(FTS), 之后按关键词可检索/可注入
            try:
                import memory_engine
                cid = 'C' + time.strftime('%m%d%H%M%S') + '%03d' % (int(time.time() * 1000) % 1000)
                memory_engine.index_task(cid, text[:500], reply[:4000], source)
            except Exception:
                pass
        return reply

    def _on_summary(self, t):
        """整任务完结: 总结已在任务气泡内更新; 飞书来源回推+产物回传; ntfy推手机; TTS可选播报; 简报存缓存"""
        # 只处理整任务完结(子任务逐个完结时 t.status 仍是 running, 不推)
        if t.get('status') not in ('done', 'failed', 'stopped'):
            return
        if t.get('wtype') == 'research' and t.get('status') == 'done':
            try:
                _archive_research(t)
            except Exception as _e:
                _syslog('[research归档失败] %s' % _e)
        # 看图任务完结 → 完整分析入ReMe记忆(用户要求: 图片内容可查可追溯)
        if t.get('status') == 'done' and (t.get('image') or '图片' in (t.get('question') or '')):
            try:
                import threading as _th

                def _archive_image(task=t):
                    import json as _json
                    import urllib.request as _uq
                    body = _json.dumps({
                        'path': 'daily/%s/image-analysis-%s' % (
                            time.strftime('%Y-%m-%d'), task.get('id', '')[-8:]),
                        'name': '看图分析 ' + task.get('id', '')[-8:],
                        'description': (task.get('summary') or '图片分析')[:80],
                        'content': (task.get('raw_answer') or task.get('summary') or '')[:12000],
                        'metadata': {'valid_from': time.strftime('%Y-%m-%d'),
                                     'source': 'butler-image-task'}},
                        ensure_ascii=False).encode('utf-8')
                    req = _uq.Request('http://127.0.0.1:2333/write', data=body, method='POST')
                    req.add_header('Content-Type', 'application/json')
                    _uq.urlopen(req, timeout=15).read()
                    _syslog('[看图归档] %s → ReMe daily ✓' % task.get('id'))
                _th.Thread(target=_archive_image, daemon=True).start()
            except Exception as _e:
                _syslog('[看图归档失败] %s' % _e)
        # A任务成果沉淀: 重任务done → 精华入ReMe knowledge层(去重闸+bilinks)
        if t.get('status') == 'done' and t.get('wtype') in ('codex', 'research', 'ufo') \
                and len((t.get('raw_answer') or '').strip()) > 300 \
                and '图片' not in (t.get('question') or ''):
            try:
                import threading as _th

                def _archive_result(task=t):
                    import json as _json
                    import urllib.request as _uq
                    raw = (task.get('raw_answer') or '').strip()
                    q = ' '.join((task.get('question') or '').split())[:60]
                    slug = re.sub(r'[\s/\\:*?"<>|,，。]', '-', q)[:36]
                    day = time.strftime('%Y-%m-%d')
                    path = 'daily/%s/knowledge-%s' % (day, task.get('id', '')[-8:])
                    # 去重闸: ReMe搜相似, 高分同主题→UPDATE合并而非新增
                    action = 'ADD'
                    try:
                        sr = _uq.urlopen(_uq.Request(
                            'http://127.0.0.1:2333/search',
                            data=_json.dumps({'query': q[:40], 'limit': 3},
                                             ensure_ascii=False).encode('utf-8'),
                            method='POST'), timeout=15)
                        import re as _re2
                        ans = _json.loads(sr.read().decode('utf-8')).get('answer', '')
                        paths = _re2.findall(r'(daily/\S+?knowledge-\S+?\.md)', ans)
                        if paths:
                            path = paths[0].replace(chr(92), '/').replace('.md', '')
                            action = 'UPDATE(同主题已有, 合并更新)'
                    except Exception:
                        pass
                    # 产物清单入卡片(B: 产物索引)
                    files_txt = ''
                    outdir = os.path.join(STATE, 'outputs', task.get('id', ''))
                    if os.path.isdir(outdir):
                        fs = [f for f in os.listdir(outdir)
                              if os.path.isfile(os.path.join(outdir, f))]
                        if fs:
                            files_txt = (chr(10) + chr(10) + '## 产物文件' + chr(10) +
                                         chr(10).join('- %s (完整路径: %s)' % (
                                             f, os.path.join(outdir, f)) for f in fs[:10]))
                    body = _json.dumps({
                        'path': path,
                        'name': '任务成果 ' + q[:40],
                        'description': '%s (%s)' % (q[:70], action.split('(')[0]),
                        'content': '# 任务: %s' % q + chr(10) + chr(10) +
                                   '结果: ' + raw[:9000] + files_txt +
                                   chr(10) + chr(10) + '相关: [[daily/%s/index]]' % day,
                        'metadata': {'valid_from': day, 'source': 'butler-task-result'}},
                        ensure_ascii=False).encode('utf-8')
                    req = _uq.Request('http://127.0.0.1:2333/write', data=body, method='POST')
                    req.add_header('Content-Type', 'application/json')
                    _uq.urlopen(req, timeout=20).read()
                    _syslog('[成果沉淀] %s %s → %s' % (task.get('id', '')[-8:], action, path))
                _th.Thread(target=_archive_result, daemon=True).start()
            except Exception as _e:
                _syslog('[成果沉淀失败] %s' % _e)
        sm = t.get('summary') or ''
        first_line = sm.split(chr(10), 1)[0] if sm else '任务完成'
        q = t.get('question', '')
        if '简报' in q:
            try:
                import daily_brief
                txt = sm
                if txt.startswith('['):
                    txt = txt.split(chr(10), 1)[-1]
                daily_brief.save(txt)
            except Exception:
                pass
        if get_set('tts_on'):
            try:
                import voice_io
                txt = sm
                if txt.startswith('['):
                    txt = txt.split(chr(10), 1)[-1] if (NLDEF := chr(10)) in txt else txt
                voice_io.speak_async(txt)
            except Exception:
                pass
        # D: 后续建议附在完成通知尾(飞书/ntfy/队列三路自然带出)
        sug = t.get('suggest') or ''
        if sug and t.get('status') == 'done':
            sm = sm + chr(10) + '💡 可以接着说: ' + sug
        if t.get('source') == 'feishu':
            body = sm.split(chr(10), 1)[-1] if sm.startswith('[') else sm
            emoji = '✅' if t.get('status') == 'done' else ('⛔' if t.get('status') == 'stopped' else '❌')
            try:
                self.F.reply_to_last(emoji + ' ' + body)
            except Exception as e:
                _status_err = '回推失败: %s' % str(e)[:100]
            # 产物回传: 扫任务产物目录, 有文件则上传发到对话
            try:
                outdir = os.path.join(STATE, 'outputs', t.get('id', ''))
                if os.path.isdir(outdir):
                    files = sorted(
                        os.path.join(outdir, x) for x in os.listdir(outdir)
                        if os.path.isfile(os.path.join(outdir, x)))
                    sent = self.F.send_files_to_last(files)
                    if sent:
                        self.F.reply_to_last('📎 已回传产物(%d个): %s' % (
                            len(sent), ', '.join(sent)))
            except Exception:
                pass
        else:
            # 非飞书来源(网页/小智/CLI)补推飞书: 任务结果全通道可达(用户指令:
            # 无论哪个端口派的, 完成后各端都能主动告知); 小智另入待播报队列
            try:
                emoji = '✅' if t.get('status') == 'done' else (
                    '⛔' if t.get('status') == 'stopped' else '❌')
                src_tag = {'xiaozhi': '🔊小智', 'web': '💻网页'}.get(
                    t.get('source'), '📋' + (t.get('source') or '?'))
                self.F.reply_to_last('%s [%s派的任务%s完成] %s' % (
                    emoji, src_tag, t.get('id', '')[-8:],
                    (sm.split(chr(10), 1)[-1] if sm.startswith('[') else sm)[:600]))
                _syslog('[补推飞书] %s(%s来源) ✓' % (t.get('id', '')[-8:], t.get('source')))
            except Exception as _e:
                _syslog('[补推飞书失败] %s: %s' % (t.get('id', '')[-8:], str(_e)[:80]))
            if t.get('source') == 'xiaozhi':
                try:
                    import xiaozhi_bridge as _xz
                    _xz.push_notify('%s(任务%s)' % (
                        first_line[:200], t.get('id', '')[-8:]))
                except Exception:
                    pass
        # 全来源推 ntfy 到手机(状态盖章; feishu 用户手机上飞书/ntfy 双保险)
        try:
            _ntfy_push(first_line[:280],
                       title='管家·任务%s' % {'done': '完成', 'failed': '失败',
                                              'stopped': '已停止'}.get(
                                                  t.get('status'), t.get('status', '')))
        except Exception:
            pass

    # ---- API ----
    def api(self, method, path, qs, body):
        W, F = self.W, self.F
        if path == '/api/debug/route':
            text = qs.get('text', [''])[0]
            import router_brain
            try:
                import memory_engine
                mem = memory_engine.context_for(text)[:300]
            except Exception as e:
                mem = 'ERR ' + str(e)
            snap = W.snapshot_for_brain(10)
            d = router_brain.route(text, mem, snap)
            return 200, {'route': d, 'snap_sessions': [(x['id'], x.get('session')) for x in snap][:6],
                         'snap_count': len(snap)}
        if path == '/api/status':
            xz_st = {}
            try:
                import xiaozhi_bridge as _xz
                xz_st = _xz.status()
            except Exception:
                pass
            return 200, {'port': self.port, 'worker': W.status(),
                         'feishu': F.public_state(),
                         'xiaozhi': xz_st,
                         'cutoff': bool(get_set('view_cutoff'))}
        if path == '/api/task/T1104130032489?op=interrupt_test':
            return 200, {'ok': True}
        if path == '/api/session/new' and method == 'POST':
            set_set('view_cutoff', time.strftime('%Y-%m-%d %H:%M:%S'))
            return 200, {'ok': True}
        if path == '/api/session/restore' and method == 'POST':
            set_set('view_cutoff', '')
            return 200, {'ok': True}
        if path == '/api/llm/test' and method == 'POST':
            t0 = time.time()
            try:
                import model_router
                txt, prov = model_router.chat('回复OK两个字母', mt=10)
                return 200, {'ok': bool(txt), 'provider': prov,
                             'elapsed': int((time.time() - t0) * 1000),
                             'sample': (txt or '')[:40]}
            except Exception as e:
                return 200, {'ok': False, 'error': str(e)[:200]}
        if path == '/api/help':
            return 200, {
                'service': 'Agentic OS Server (butler)',
                'version': VERSION,
                'usage': 'POST /api/chat {text, source, [image], [wait], '
                         '[max_wait], [idempotency_key]} → {reply, [id], '
                         '[status], [summary]}',
                'endpoints': [
                    'POST /api/chat — 统一入口(直答或派任务)',
                    'GET  /api/journal?limit — 聊天时间线',
                    'GET  /api/tasks?limit — 任务列表',
                    'GET  /api/task/{id}?op=detail|tail|stop|continue|retry',
                    'GET  /api/search?q= — 混合记忆检索',
                    'POST /api/note {content} — 手动记忆',
                    'GET  /api/export/md — 导出Markdown',
                    'POST /api/session/new|restore — 新会话/恢复历史',
                    'GET/POST /api/triggers — 定时主动任务',
                    'GET/POST /api/reminders · /api/reminders/del',
                    'GET/POST /api/profile(/add/edit/del) — 用户画像',
                    'POST /api/upload {b64,name} → path; POST /api/asr {b64}',
                    'GET/POST /api/config · /api/feishu/restart · /api/llm/info|test',
                    'GET  /api/status · /api/health'],
                'hints': '任务唯一ID形如T1104xxxx; 完成后summary为Router要点'
                         '总结, raw_answer为worker全文; 引用类问题以【最近对话】为准'}
        if path == '/api/bye' and method == 'POST':
            globals()['BYE'] = True
            globals()['LAST_SEEN'] = time.time()
            return 200, {'ok': True}
        if path == '/api/debug/files':
            import glob
            files = []
            seen = set()
            for pat in (os.path.join(STATE, '*.log'),
                        os.path.join(LOGS_DIR, '*.log'),
                        os.path.join(LOGS_DIR, '*.txt')):
                for fp in glob.glob(pat):
                    b = os.path.basename(fp)
                    if b in seen:
                        continue
                    seen.add(b)
                    try:
                        stt = os.stat(fp)
                    except Exception:
                        continue
                    files.append({'name': b, 'size': stt.st_size,
                                  'mtime': time.strftime(
                                      '%m-%d %H:%M',
                                      time.localtime(stt.st_mtime))})
            files.sort(key=lambda x: x['mtime'], reverse=True)
            order = ('server_err.log', 'server_out.log')
            main_logs = [f for nm in order for f in files
                         if f['name'] == nm]
            rest = [f for f in files if f not in main_logs]
            return 200, {'files': (main_logs + rest)[:40]}
        if path == '/api/debug/tail':
            name = os.path.basename(qs.get('name', [''])[0])
            n = min(500, max(10, int(qs.get('n', ['200'])[0])))
            for d in (STATE, LOGS_DIR):
                fp = os.path.join(d, name)
                if os.path.isfile(fp):
                    data = open(fp, encoding='utf-8', errors='replace').read()
                    return 200, {'tail': chr(10).join(data.splitlines()[-n:])}
            return 404, {'error_code': 'NOT_FOUND', 'error': '日志不存在'}
        if path == '/api/debug/clear' and method == 'POST':
            name = os.path.basename(body.get('name', ''))
            ok_c, err = False, ''
            for d in (STATE, LOGS_DIR):
                fp = os.path.join(d, name)
                if os.path.isfile(fp):
                    try:
                        open(fp, 'w', encoding='utf-8').close()
                        ok_c = True
                    except Exception as e2:
                        err = str(e2)[:100]
            return 200, {'ok': ok_c, 'error': err}
        if path == '/api/openlogs' and method == 'POST':
            try:
                os.startfile(LOGS_DIR)
                os.startfile(STATE)
            except Exception:
                pass
            return 200, {'ok': True}
        if path == '/api/health':
            return 200, {'ok': True, 'version': VERSION,
                         'uptime_s': int(time.time() - _T0)}
        if path == '/api/export/md':
            items = W.journal(500)
            lines = ['# 管家对话导出 ' + time.strftime('%Y-%m-%d %H:%M'), '']
            for it in items:
                k = it.get('type') or it.get('kind')
                if k == 'user':
                    lines += ['## 用户 ' + it.get('ts', ''), '',
                              it.get('text', ''), '']
                elif k == 'router':
                    lines += ['**管家** ' + it.get('ts', ''), '',
                              it.get('text', ''), '']
                elif k == 'task':
                    lines += ['## 任务 ' + it.get('id', '') + ' (' +
                              it.get('status', '') + ')', '',
                              it.get('question', ''), '']
                    if it.get('summary'):
                        lines += [it.get('summary', ''), '']
                    if it.get('raw_answer'):
                        lines += ['<details><summary>worker原始回答</summary>', '',
                                  it.get('raw_answer', '')[:8000], '',
                                  '</details>', '']
            return 200, {'markdown': chr(10).join(lines)}
        if path == '/api/triggers' and method == 'GET':
            return 200, {'triggers': load_triggers()}
        if path == '/api/triggers' and method == 'POST':
            act = body.get('action')
            if act == 'add':
                tme = str(body.get('time', '')).strip()
                txt = str(body.get('text', '')).strip()
                if not re.match(r'^[0-2]?[0-9]:[0-5][0-9]$', tme):
                    return 400, {'error_code': 'BAD_TIME',
                                 'error': '时间格式须为 HH:MM'}
                if not txt:
                    return 400, {'error_code': 'EMPTY', 'error': '指令为空'}
                trs = load_triggers()
                trs.append({'id': 'G' + time.strftime('%H%M%S') +
                            str(len(trs)), 'time': tme, 'text': txt[:300],
                            'last': ''})
                save_triggers(trs)
                return 200, {'ok': True, 'triggers': trs}
            if act == 'del':
                trs = [x for x in load_triggers()
                       if x.get('id') != body.get('id')]
                save_triggers(trs)
                return 200, {'ok': True, 'triggers': trs}
            return 400, {'error_code': 'BAD_ACTION', 'error': '未知操作'}
        if path == '/api/upload' and method == 'POST':
            import base64 as _b64
            data = _b64.b64decode(body.get('b64') or '')
            if not data or len(data) > 8 * 1024 * 1024:
                return 400, {'error_code': 'BAD_IMAGE', 'error': '图片为空或超8MB'}
            ext = os.path.splitext(str(body.get('name') or 'img.png'))[1] or '.png'
            updir = os.path.join(STATE, 'uploads')
            os.makedirs(updir, exist_ok=True)
            fp = os.path.join(updir, 'img_' + time.strftime('%m%d%H%M%S') + ext)
            open(fp, 'wb').write(data)
            return 200, {'path': fp}
        if path == '/api/asr' and method == 'POST':
            try:
                import voice_io
                text, err = voice_io.asr_webm(body.get('b64') or '')
                return 200, {'text': text or '', 'error': err}
            except Exception as e:
                return 200, {'text': '', 'error': str(e)[:200]}
        if path == '/api/reminders':
            rems = [r for r in (get_set('reminders') or [])
                    if not r.get('fired')]
            return 200, {'reminders': rems}
        if path == '/api/reminders/del' and method == 'POST':
            rid = body.get('id')
            set_set('reminders', [r for r in (get_set('reminders') or [])
                                  if r.get('id') != rid])
            return 200, {'ok': True}
        if path == '/api/profile':
            import memory_engine
            return 200, {'profile': memory_engine.profile_list()}
        if path == '/api/profile/del' and method == 'POST':
            import memory_engine
            return 200, {'ok': memory_engine.profile_del(int(body.get('idx', -1)))}
        if path == '/api/profile/edit' and method == 'POST':
            import memory_engine
            return 200, {'ok': memory_engine.profile_edit(
                int(body.get('idx', -1)), body.get('text', ''))}
        if path == '/api/profile/add' and method == 'POST':
            import memory_engine
            rec = memory_engine.profile_add(body.get('text', ''))
            return 200, {'ok': True, 'rec': rec}
        if path == '/api/note/edit' and method == 'POST':
            nid = body.get('note_id', '')
            content = (body.get('content') or '').strip()
            try:
                import sqlite3
                c = sqlite3.connect(os.path.join(STATE, 'memory.db'))
                c.execute('UPDATE notes_fts SET content = ? WHERE note_id = ?',
                          (content[:1000], nid))
                c.commit()
                c.close()
                return 200, {'ok': True}
            except Exception as e:
                return 500, {'error_code': 'NOTE_EDIT_FAILED', 'error': str(e)[:150]}
        if path == '/api/note/delete' and method == 'POST':
            nid = body.get('note_id', '')
            try:
                import sqlite3
                c = sqlite3.connect(os.path.join(STATE, 'memory.db'))
                c.execute('DELETE FROM notes_fts WHERE note_id = ?', (nid,))
                c.commit()
                c.close()
                return 200, {'ok': True}
            except Exception as e:
                return 500, {'error_code': 'NOTE_DEL_FAILED', 'error': str(e)[:150]}
        if path == '/api/stats':
            import json as _j
            st = W.stats_overview()
            st['chat_replies'] = sum(
                1 for i in W.journal(500)
                if i.get('type') == 'router' and i.get('elapsed') is not None)
            return 200, st
        if path == '/api/llm/info':
            import model_router as mr
            import config as cf
            def mk(nm):
                v = cf.KEY(nm)
                if not v:
                    return '未配置'
                if len(v) > 12:
                    return v[:4] + '****' + v[-4:]
                return v[:2] + '****'
            return 200, {'providers': [
                [{'name': p[0], 'url': p[1], 'model': p[3],
                  'key': mk(p[2])} for p in mr._chains()[0]],
            ], 'worker': 'Codex CLI (独立登录计费) · 自动发现: ' + W.codex_exe()}
        if path == '/api/chat' and method == 'POST':
            text = (body.get('text') or '').strip()
            if not text:
                return 400, {'error_code': 'EMPTY', 'error': '内容为空'}
            ikey = body.get('idempotency_key')
            if ikey:
                hit = _idem.get(ikey)
                if hit and time.time() - hit[0] < 3600:
                    return 200, hit[1]
                while len(_idem) > 200:
                    oldest = min(_idem, key=lambda k: _idem[k][0])
                    _idem.pop(oldest, None)
            reply = self.on_chat(text, body.get('source', 'web'),
                                 image=body.get('image'))
            resp = {'reply': reply}
            m = re.search(r'(T\d{8,})', reply)
            tid = m.group(1) if m else None
            if tid:
                resp['id'] = tid
                resp['status'] = 'queued'
                if body.get('wait'):
                    deadline = time.time() + min(
                        900, max(10, int(body.get('max_wait', 600))))
                    tdata = {}
                    while time.time() < deadline:
                        tdata = W.get_task(tid) or {}
                        if tdata.get('status') in ('done', 'failed', 'stopped'):
                            break
                        time.sleep(2)
                    if tdata:
                        resp['status'] = tdata.get('status')
                        resp['summary'] = tdata.get('summary', '')
                        resp['raw_answer'] = (tdata.get('raw_answer') or '')[:4000]
                        resp['error_code'] = tdata.get('error_code', '')
                    else:
                        resp['status'] = 'unknown'
            if ikey:
                resp['idempotency_key'] = ikey
                _idem[ikey] = (time.time(), resp)
            return 200, resp
        if path == '/api/journal':
            cutoff = str(get_set('view_cutoff') or '')
            return 200, {'items': W.journal(int(qs.get('limit', ['300'])[0]),
                                            cutoff=cutoff)}
        if path == '/api/task' and method == 'POST':      # 兼容旧直派接口
            q = (body.get('question') or '').strip()
            if not q:
                return 400, {'error_code': 'EMPTY_QUESTION', 'error': '任务内容为空'}
            tid = W.new_task(q, source=body.get('source', 'web'))
            return 200, {'id': tid, 'question': q, 'status': 'queued'}
        if path == '/api/tasks':
            return 200, {'tasks': W.list_tasks(int(qs.get('limit', ['50'])[0]))}
        if path.startswith('/api/task/'):
            tid = path.split('/')[-1]
            sub = qs.get('op', [''])[0]
            if sub == 'stop':
                ok, msg = W.stop_task(tid)
                return (200 if ok else 400), {'ok': ok, 'message': msg}
            if sub == 'continue' and method == 'POST':
                ok, msg = W.continue_task(tid, body.get('message', ''),
                                          source=body.get('source', 'web'))
                return (200 if ok else 400), {'ok': ok, 'message': msg}
            if sub == 'retry':
                nt = W.retry_task(tid, source=(body.get('source') or 'web'))
                if not nt:
                    return 404, {'error_code': 'NOT_FOUND', 'error': '任务不存在'}
                return 200, {'id': nt, 'retried': tid}
            if sub == 'tail':
                import glob as _g
                logs = sorted(_g.glob(os.path.join(LOGS_DIR, tid + '*.log')),
                              key=os.path.getmtime)
                tail = ''
                if logs:
                    tail = open(logs[-1], encoding='utf-8',
                                errors='replace').read()[-1200:]
                return 200, {'tail': tail}
            if sub == 'detail':
                t = W.get_task(tid)
                if not t:
                    return 404, {'error_code': 'NOT_FOUND', 'error': '任务不存在'}
                # 附磁盘全文(worker -o 文件未截断)
                full = ''
                try:
                    for s2 in (t.get('subtasks') or []):
                        fp = os.path.join(LOGS_DIR, s2['sid'] + '.answer.txt')
                        if os.path.exists(fp):
                            full += (open(fp, encoding='utf-8',
                                          errors='replace').read().strip()
                                     + chr(10) * 2)
                except Exception:
                    pass
                t['full_answer'] = full[:60000]
                return 200, t
        if path == '/api/search':
            q = (qs.get('q', [''])[0] or '').strip()
            try:
                import memory_engine
                return 200, memory_engine.search_hybrid(q, 10)
            except Exception as e:
                return 500, {'error_code': 'SEARCH_FAILED', 'error': str(e)}
        if path == '/api/note' and method == 'POST':
            content = (body.get('content') or '').strip()
            if content:
                import memory_engine
                memory_engine.add_note(content[:1000])
                return 200, {'ok': True}
            return 400, {'error_code': 'EMPTY', 'error': '内容为空'}
        if path == '/api/config' and method == 'GET':
            cfg = F.load_config()
            f = cfg.get('feishu', {})
            return 200, {'tts_on': bool(get_set('tts_on')),
                         'app_id': f.get('app_id', ''),
                         'has_secret': bool(f.get('app_secret')),
                         'secret_masked': F.mask_secret(f.get('app_secret')),
                         'migrated': f.get('migrated_from', ''),
                         'max_workers': W.status()['max_workers'],
                         'feishu_state': F.public_state()}
        if path == '/api/config' and method == 'POST':
            f = body.get('feishu', {})
            cur = F.load_config().get('feishu', {})
            new = dict(cur)
            if f.get('app_id'):
                new['app_id'] = f['app_id'].strip()
            if f.get('app_secret'):
                new['app_secret'] = f['app_secret'].strip()
            F.save_config({'feishu': new})
            if body.get('max_workers') is not None:
                W.set_config(max_workers=int(body['max_workers']))
            if 'tts_on' in body:
                set_set('tts_on', bool(body['tts_on']))
            return 200, {'ok': True}
        if path == '/api/feishu/restart' and method == 'POST':
            threading.Thread(target=F.start, args=(self.on_chat,), daemon=True).start()
            return 200, {'ok': True, 'state': F.public_state()}
        if path == '/api/feishu/simulate' and method == 'POST':
            """模拟飞书消息 — 走与真实飞书完全相同的 on_chat 路径"""
            reply = self.on_chat((body.get('text') or '').strip(), 'feishu',
                                 image=body.get('image'))
            return 200, {'reply': reply}
        if path == '/api/controlmesh/migrate' and method == 'POST':
            moved = F.import_from_controlmesh()
            return 200, {'migrated': moved, 'state': F.public_state()}
        return 404, {'error_code': 'NOT_FOUND', 'error': '未知接口 %s' % path}


ROUTER = None
_idem = {}      # idempotency_key → (ts, response)
LAST_SEEN = None   # 最近一次前端/CLI活动; None=页面从未打开
BYE = False        # 页面关闭(sendBeacon) → 触发全进程退出


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == '/' or u.path == '/index.html':
            try:
                data = open(WEB, 'rb').read()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self._json(500, {'error': str(e)})
            return
        if u.path.startswith('/api/'):
            globals()['LAST_SEEN'] = time.time()
            qs = parse_qs(u.query)
            try:
                code, obj = ROUTER.api('GET', u.path, qs, {})
            except Exception:
                code, obj = 500, {'error_code': 'INTERNAL',
                                  'error': traceback.format_exc()[-500:]}
            self._json(code, obj)
            return
        self._json(404, {'error_code': 'NOT_FOUND', 'error': 'not found'})

    def do_POST(self):
        u = urlparse(self.path)
        if u.path.startswith('/api/'):
            globals()['LAST_SEEN'] = time.time()
        if not u.path.startswith('/api/'):
            self._json(404, {'error_code': 'NOT_FOUND', 'error': 'not found'})
            return
        try:
            n = int(self.headers.get('Content-Length', 0) or 0)
            if n > 20 * 1024 * 1024:
                self._json(413, {'error_code': 'TOO_LARGE',
                                 'error': '请求体超过 20MB 上限'})
                return
            body = json.loads(self.rfile.read(n).decode('utf-8')) if n else {}
        except Exception:
            body = {}
        qs = parse_qs(u.query)
        try:
            code, obj = ROUTER.api('POST', u.path, qs, body)
        except Exception:
            code, obj = 500, {'error_code': 'INTERNAL',
                              'error': traceback.format_exc()[-500:]}
        self._json(code, obj)


def _instance_lock():
    """单实例互斥: state/server.lock 记录存活PID, 双开直接退出(防账本双派发)"""
    import ctypes
    lock = os.path.join(STATE, 'server.lock')
    if os.path.exists(lock):
        try:
            old_pid = int(open(lock).read().strip() or 0)
            h = ctypes.windll.kernel32.OpenProcess(0x1000, 0, old_pid)  # SYNCHRONIZE
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                return False, old_pid      # 旧实例还活着
        except Exception:
            pass
    with open(lock, 'w') as f:
        f.write(str(os.getpid()))
    return True, 0


def _reminders_loop(router):
    """一次性提醒: 到点 → 聊天流气泡(⏰) + 飞书回推(若来源飞书)"""
    while True:
        try:
            now = time.strftime('%Y-%m-%d %H:%M')
            rems = get_set('reminders') or []
            due = [r for r in rems if not r.get('fired') and r.get('dt', '') <= now]
            for r in due:
                r['fired'] = True
                set_set('reminders', rems)
                txt = '⏰ 提醒: ' + r.get('text', '')
                router.W._append({'type': 'router', 'text': txt,
                                  'ts': time.strftime('%Y-%m-%d %H:%M:%S')})
                if r.get('source') == 'feishu':
                    try:
                        router.F.reply_to_last(txt)
                    except Exception:
                        pass
        except Exception:
            pass
        time.sleep(20)


def _write_port_file(port):
    """端口信息写 ~/,butler-os/server.json → CLI/MCP/agent 免扫描发现服务"""
    try:
        home = os.path.join(os.path.expanduser('~'), '.butler-os')
        os.makedirs(home, exist_ok=True)
        json.dump({'port': port, 'pid': os.getpid(),
                   'url': 'http://127.0.0.1:%d' % port,
                   'version': VERSION,
                   'started': time.strftime('%Y-%m-%d %H:%M:%S')},
                  open(os.path.join(home, 'server.json'), 'w',
                       encoding='utf-8'), ensure_ascii=False, indent=1)
    except Exception:
        pass


def _lifecycle_loop():
    """浏览器关闭 → 相关进程全部退出(用户要求); CLI/MCP活动同样续命"""
    import codex_worker as _W
    while True:
        time.sleep(5)
        try:
            # 仅在收到页面关闭信号(BYE)时退出; 无活动不自杀(CLI/MCP/挂机都合法)
            if not BYE:
                continue
            for p in list(_W._procs.values()):
                try:
                    if p.poll() is None:
                        subprocess.run(['taskkill', '/F', '/T', '/PID',
                                        str(p.pid)], capture_output=True,
                                       timeout=15)
                except Exception:
                    pass
            try:
                import rag_client as _rc
                if getattr(_rc, '_proc', None) and _rc._proc.poll() is None:
                    _rc._proc.kill()
            except Exception:
                pass
            try:
                pf = os.path.join(os.path.expanduser('~'), '.butler-os',
                                  'server.json')
                if os.path.exists(pf):
                    os.remove(pf)
            except Exception:
                pass
            try:
                lf = os.path.join(STATE, 'server.lock')
                if os.path.exists(lf):
                    os.remove(lf)
            except Exception:
                pass
            os._exit(0)
        except Exception:
            pass



def main():
    global ROUTER
    os.makedirs(STATE, exist_ok=True)
    ok, pid = _instance_lock()
    if not ok:
        print('已有实例在运行(PID %d), 本次启动退出。' % pid)
        return
    port = free_port()
    ROUTER = Router()
    ROUTER.port = port
    _write_port_file(port)
    srv = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    url = 'http://127.0.0.1:%d' % port
    print('=' * 56)
    print(' Agentic OS Server v2(管家形态) 已启动 (仅本机访问)')
    print(' 管理页: %s' % url)
    print(' 端口: 9991-9999 自动选第一个空闲; 当前=%d' % port)
    print(' 并行Worker≤3 · 编排子任务≤5 · 关窗即停')
    print('=' * 56)
    try:
        os.startfile(url)
    except Exception:
        pass
    threading.Thread(target=_reminders_loop, args=(ROUTER,),
                     daemon=True).start()
    threading.Thread(target=_lifecycle_loop, daemon=True).start()
    # 小智桥接(常驻, 自动重连)
    try:
        import xiaozhi_bridge as _XZ
        import feishu_adapter as _F
        xz_cfg = _F.load_config().get('xiaozhi', {})
        if xz_cfg.get('ws_url'):
            _XZ._start_ws(xz_cfg['ws_url'])
            print(' 小智桥接已启动(WS):', xz_cfg['ws_url'][:40] + '…')
        else:
            print(' 小智桥接未配置(config.json xiaozhi.ws_url)')
    except Exception as e:
        print(' 小智桥接启动失败:', e)
    threading.Thread(target=_triggers_loop, args=(ROUTER,),
                     daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
