# -*- coding: utf-8 -*-
"""Codex CLI Worker 池 v2 — 管家形态

· 用户消息经 Router 大脑消化; 任务可为单 worker 或 ≤5 个子任务(相互独立才拆)
· 每个 run = 一次 codex exec(或 exec resume 续聊); worker 间完全隔离
· FIFO 并行(默认3, 上限3), 无优先级插队; 无单任务时长硬限制, 仅可手动停止
· Worker 错误退出码原样透传(EXIT_n), 不自动重试掩盖
· 全部子任务完结 → 触发总结回调(Router 出总结性回复)
· 会话捕获: 任务完结记录 codex session UUID → 支持"续聊该任务"(exec resume)
· journal(): 账本回放成聊天时间线(user/router 气泡 + 任务块), Web/飞书共用
"""
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(BASE, 'state')
LEDGER = os.path.join(STATE, 'ledger.jsonl')
LOGS = os.path.join(STATE, 'worker_logs')
MAX_DELEGATION_ROUNDS = 3  # 委派循环上限(用户指令: 3次无法完成就如实报告)
SESSIONS_DIR = os.path.join(os.path.expanduser('~'), '.codex', 'sessions')

MAX_WORKERS_CAP = 3
_cfg = {'max_workers': 3}
_tasks = {}
_queue = queue.Queue()
_procs = {}
_lock = threading.Lock()
_push_done = None       # (task) → None  单个子任务完结回调(飞书即时回传等)
_push_summary = None    # (task) → None  整任务总结回调(Router 总结+推聊天流)


# ---------- 账本 ----------
def _append(rec):
    with _lock:
        os.makedirs(STATE, exist_ok=True)
        with open(LEDGER, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')


def _load_ledger():
    if not os.path.exists(LEDGER):
        return
    for ln in open(LEDGER, encoding='utf-8', errors='replace'):
        try:
            r = json.loads(ln)
        except Exception:
            continue
        if r.get('id'):
            _tasks.setdefault(r['id'], {}).update(r)


def save_task(tid):
    t = _tasks.get(tid)
    if t:
        _append(dict(t))


# ---------- codex 发现 ----------
_codex = None

def codex_exe():
    global _codex
    if _codex is None:
        import glob
        appdata = os.environ.get('APPDATA', '')
        cands = glob.glob(os.path.join(appdata, 'npm', 'node_modules', '@openai',
                                       '*', '**', 'bin', 'codex.exe'), recursive=True)
        cands += [shutil.which('codex'), shutil.which('codex.exe')]
        cands += [os.path.join(appdata, 'npm', 'codex.exe'),
                  os.path.join(os.environ.get('LOCALAPPDATA', ''),
                               'Microsoft', 'WinGet', 'Links', 'codex.exe')]
        for c in cands:
            if c and os.path.isfile(c):
                _codex = c
                break
        _codex = _codex or 'codex'
    return _codex


UFO_SCRIPT = os.path.join(os.path.expanduser('~'), '.agents', 'skills',
                          'ufo-windows', 'scripts', 'hardened_ufo.py')
SKILLS_DIR = os.path.join(os.path.expanduser('~'), '.agents', 'skills')
GUI_KW = ('打开', '关闭', '点击', '截屏', '窗口', '界面', '设置里', '浏览器')


def ufo_available():
    return os.path.exists(UFO_SCRIPT)


def _ufo_cmd(t, sid, instruction):
    """构造 UFO2 加固执行器命令行(独立进程, 可被手动停止)"""
    name = 'butler_' + time.strftime('%m%d%H%M%S')
    return ([sys.executable, UFO_SCRIPT, instruction,
             '--name', name, '--timeout', '1800'], name)


def _extract_last_json(text):
    """从文本提取最后一个平衡的顶层JSON对象(嵌套/reason内嵌{}都不会误抓)"""
    end = text.rfind('}')
    while end != -1:
        depth = 0
        for i in range(end, -1, -1):
            ch = text[i]
            if ch == '}':
                depth += 1
            elif ch == '{':
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[i:end + 1])
                    except Exception:
                        break
        end = text.rfind('}', 0, end)
    return {}


def skills_context(task):
    """扫描 ~/.agents/skills, 把与任务相关的技能提示注入 worker(零LLM开销)"""
    try:
        names = os.listdir(SKILLS_DIR)
    except Exception:
        return ''
    grams = list(dict.fromkeys(
        task[i:i + 2] for i in range(len(task) - 1)
        if task[i] not in ' \n.,?!:;|' and task[i + 1] not in ' \n.,?!:;|'))[:40]
    if not grams:
        return ''
    scored = []
    for name in names:
        p = os.path.join(SKILLS_DIR, name, 'SKILL.md')
        if not os.path.isfile(p):
            continue
        try:
            head = open(p, encoding='utf-8', errors='replace').read(800)
        except Exception:
            continue
        desc = ''
        for ln in head.splitlines():
            st = ln.strip()
            if st.startswith('description:'):
                desc = st.split(':', 1)[1].strip('| ').strip()
            elif desc and st and not st.startswith(('-', '#')):
                desc = (desc + ' ' + st).strip()
            if len(desc) >= 70:
                break
        desc = ' '.join(desc.split())[:70]
        hay = (name + ' ' + desc).lower()
        score = sum(1 for g in grams if g.lower() in hay)
        if score:
            scored.append((score, name, desc))
    if not scored:
        return ''
    scored.sort(reverse=True)
    return ('【本机可用技能(必要时可在任务中使用)】\n' +
            '\n'.join('- %s: %s' % (n, d) for _, n, d in scored[:5]))


def _tokens_from_log(path):
    """从 codex 日志解析 tokens used(千分位逗号), 失败返回0"""
    try:
        txt = open(path, encoding='utf-8', errors='replace').read()[-3000:]
        m = re.search(r'tokens used\s*:?\s*([0-9,]+)', txt)
        return int(m.group(1).replace(',', '')) if m else 0
    except Exception:
        return 0


# ---------- 会话捕获(续聊基础) ----------
def _session_snapshot():
    out = set()
    for root, _, fs in os.walk(SESSIONS_DIR):
        for f in fs:
            if f.endswith('.jsonl'):
                out.add(os.path.join(root, f))
    return out


def _new_session_uuid(before):
    newest = ''
    for root, _, fs in os.walk(SESSIONS_DIR):
        for f in fs:
            if f.endswith('.jsonl'):
                p = os.path.join(root, f)
                if p not in before and os.path.getsize(p) > 0:
                    m = re.search(r'([0-9a-f]{8}-[0-9a-f-]{27,36})\.jsonl', f)
                    if m:
                        newest = m.group(1)
    return newest


# ---------- 任务生命周期 ----------
def new_task(question, source='web', plan=None, plan_note='', image=None,
             wtype='codex'):
    """plan=None 或 [{'brief','instruction'}](1..5)。image=图片路径(附加给codex -i)。返回任务ID"""
    tid = 'T' + time.strftime('%m%d%H%M%S') + '%03d' % (int(time.time() * 1000) % 1000)
    tags = []
    q = question
    while q.startswith('#'):
        m = re.match(r'#(\S+)\s*', q)
        if not m:
            break
        tags.append(m.group(1))
        q = q[m.end():]
    question = q.strip() or question
    subs = []
    if plan and len(plan) > 1:
        for i, s in enumerate(plan[:5], 1):
            subs.append({'sid': '%s-%d' % (tid, i), 'brief': (s.get('brief') or '子任务')[:12],
                         'instruction': s['instruction'], 'status': 'queued',
                         'result': '', 'session': '', 'seq': i})
    if not subs:
        subs = [{'sid': tid, 'brief': ' '.join(question.split())[:10] or '任务',
                 'instruction': question, 'status': 'queued',
                 'result': '', 'session': '', 'seq': 1}]
    t = {'id': tid, 'question': question, 'source': source, 'kind': 'task',
         'status': 'queued', 'created': time.strftime('%Y-%m-%d %H:%M:%S'),
         'plan_note': plan_note, 'subtasks': subs, 'progress': [],
         'summary': '', 'followups': [], 'session': '', 'continue_of': '',
         'tags': tags, 'image': image or '', 'wtype': wtype}
    with _lock:
        _tasks[tid] = t
    save_task(tid)
    for s in subs:
        _queue.put((tid, s['sid']))
    return tid


def continue_task(tid, message, source='web'):
    """续聊: 在该任务的 codex 会话上继续; 返回 (ok, msg)"""
    t = _tasks.get(tid)
    if not t:
        return False, '任务不存在'
    if t.get('continue_of'):
        tid = t['continue_of']
        t = _tasks.get(tid)
    sid = t.get('session')
    if not sid:
        return False, '该任务没有可续聊的会话(可能未成功执行过)'
    seq = len(t.get('followups', [])) + 1
    fu = {'seq': seq, 'question': message, 'answer': '', 'status': 'queued',
          'ts': time.strftime('%Y-%m-%d %H:%M:%S')}
    t.setdefault('followups', []).append(fu)
    t['status'] = 'running'
    t['continued'] = True
    save_task(tid)
    _queue.put((tid, 'F%d' % seq))       # followup run 标记
    return True, '已在任务 %s 的会话上继续(%d)' % (tid, seq)


def retry_task(tid, source=None):
    """失败/停止任务一键重试: 同问题重新派发"""
    t = _tasks.get(tid)
    if not t:
        return None
    return new_task(t.get('question', ''),
                    source=source or t.get('source', 'web'),
                    plan_note='重试 ' + tid)


def get_task(tid):
    with _lock:
        t = _tasks.get(tid)
        return json.loads(json.dumps(t, ensure_ascii=False)) if t else None


def list_tasks(limit=50):
    with _lock:
        ts = sorted(_tasks.values(), key=lambda t: t.get('created', ''), reverse=True)[:limit]
        # ④"需要人面板"(Superagent思想): 健康状态不占位——failed/interrupted置顶,
        # 其次运行中, 已完成沉底(用户扫一眼就知道有什么要处理)
        def _rank(t):
            return {'failed': 0, 'interrupted': 1, 'running': 2, 'queued': 3,
                    'stopped': 4, 'done': 5}.get(t.get('status'), 5)
        ts = sorted(ts, key=lambda t: (_rank(t), t.get('created', '')), reverse=False)
        ts = sorted(ts, key=lambda t: (_rank(t), ), )
        # 组内保持时间倒序: 先按时间倒序, 再稳定按rank排
        ts = sorted(_tasks.values(), key=lambda t: t.get('created', ''), reverse=True)[:limit]
        ts = sorted(ts, key=lambda t: _rank(t))
        return [{k: t.get(k) for k in
                 ('id', 'question', 'status', 'source', 'created', 'summary',
                  'plan_note')} for t in ts[:limit]]


def snapshot_for_brain(limit=10):
    with _lock:
        ts = sorted(_tasks.values(), key=lambda t: t.get('created', ''), reverse=True)
        out = []
        for t in ts[:limit]:
            out.append({'id': t['id'], 'status': t['status'],
                        'question': t.get('question', ''),
                        'summary': t.get('summary', ''),
                        'result': (t.get('subtasks') or [{}])[0].get('result', ''),
                        'started': t.get('started', ''),
                        'session': bool(t.get('session')),
                        'subtasks': [{'brief': s.get('brief'), 'status': s.get('status')}
                                     for s in (t.get('subtasks') or [])],
                        'progress': t.get('progress', [])[-3:]})
        return out


def stop_task(tid):
    t = _tasks.get(tid)
    if not t:
        return False, '任务不存在'
    if t.get('status') not in ('running', 'queued'):
        return False, '任务已处于 %s' % t.get('status')
    killed = 0
    # 只杀本任务的进程(按 log_id 前缀=任务ID 匹配), 不误伤其他并行任务
    for lid in [k for k in list(_procs.keys()) if k.startswith(tid)]:
        p = _procs.get(lid)
        try:
            if p and p.poll() is None:
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(p.pid)],
                               capture_output=True, timeout=15)
                killed += 1
        except Exception:
            pass
        _procs.pop(lid, None)          # 完毕即清理, 防表泄漏与陈旧PID
    t['status'] = 'stopped'
    t['error_code'] = 'STOPPED_BY_USER'
    t['error'] = '用户手动停止'
    t['ended'] = time.strftime('%Y-%m-%d %H:%M:%S')
    save_task(tid)
    return True, '已停止(终止 %d 个进程)' % killed


def set_config(max_workers=None):
    if max_workers is not None:
        _cfg['max_workers'] = max(1, min(MAX_WORKERS_CAP, int(max_workers)))


def _note(t, text):
    t.setdefault('progress', []).append(
        {'ts': time.strftime('%H:%M:%S'), 'note': text[:200]})
    t['progress'] = t['progress'][-15:]


def stats_overview():
    """用量看板数据"""
    with _lock:
        ts = list(_tasks.values())
    import collections
    by_status = collections.Counter(t.get('status', '?') for t in ts)
    tokens = sum(t.get('tokens') or 0 for t in ts)
    durs = []
    for t in ts:
        if t.get('started') and t.get('ended'):
            try:
                durs.append(time.mktime(time.strptime(
                    t['ended'], '%Y-%m-%d %H:%M:%S'))
                    - time.mktime(time.strptime(t['started'],
                                                '%Y-%m-%d %H:%M:%S')))
            except Exception:
                pass
    week = time.strftime('%Y-%m-%d', time.localtime(time.time() - 7 * 86400))
    week_n = sum(1 for t in ts if (t.get('created') or '')[:10] >= week)
    return {'total': len(ts), 'by_status': dict(by_status),
            'week_tasks': week_n, 'tokens_total': tokens,
            'avg_duration_s': int(sum(durs) / len(durs)) if durs else 0,
            'durations': len(durs)}


def status():
    with _lock:
        running = sum(1 for t in _tasks.values() if t.get('status') == 'running')
        queued = sum(1 for t in _tasks.values() if t.get('status') == 'queued')
    return {'max_workers': _cfg['max_workers'], 'running': running,
            'queued': queued, 'codex': codex_exe()}


# ---------- 最近对话(Router直答的记忆上下文) ----------
def recent_dialog(n=10):
    """最近 n 条 user/router 对话记录(过滤内部调试行)"""
    items = []
    if not os.path.exists(LEDGER):
        return items
    for ln in open(LEDGER, encoding='utf-8', errors='replace'):
        try:
            r = json.loads(ln)
        except Exception:
            continue
        if r.get('type') in ('user', 'router'):
            txt = r.get('text', '')
            if txt.startswith('[internal]'):
                continue
            items.append({'type': r['type'], 'text': txt, 'ts': r.get('ts', '')})
    return items[-n:]


# ---------- 时间线(journal, 增量缓存) ----------
_JC = {'size': 0, 'buf': '', 'items': [], 'pos': {}}


def _jc_reset():
    _JC.update(size=0, buf='', items=[], pos={})


def journal(limit=300, cutoff=''):
    """账本回放 → 聊天时间线(增量读取: 只解析账本新增字节, 任务快照就地刷新)"""
    try:
        size = os.path.getsize(LEDGER) if os.path.exists(LEDGER) else 0
    except OSError:
        size = 0
    if size < _JC['size']:
        _jc_reset()                      # 账本被轮转/替换 → 全量重建
    if size > _JC['size']:
        with open(LEDGER, 'rb') as f:
            f.seek(_JC['size'])
            chunk = _JC['buf'] + f.read().decode('utf-8', 'replace')
        _JC['size'] = size
        if not chunk.endswith(chr(10)):  # 末行可能写了一半, 留到下轮
            cut = chunk.rfind(chr(10))
            if cut == -1:
                _JC['buf'] = chunk
                chunk = ''
            else:
                _JC['buf'] = chunk[cut + 1:]
                chunk = chunk[:cut + 1]
        else:
            _JC['buf'] = ''
        for ln in chunk.split(chr(10)):
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
            except Exception:
                continue
            typ = r.get('type')
            items = _JC['items']
            pos = _JC['pos']
            if typ == 'user':
                items.append({'type': 'user', 'text': r.get('text', ''),
                              'source': r.get('source', 'web'),
                              'ts': r.get('ts', '')})
            elif typ == 'router':
                txt = r.get('text', '')
                if '已接收' in txt or txt.startswith('✅') or txt.startswith('[internal]'):
                    continue
                items.append({'type': 'router', 'text': txt,
                              'ts': r.get('ts', ''),
                              'elapsed': r.get('elapsed')})
            elif r.get('id'):
                tid = r['id']
                t = _tasks.get(tid)
                if t is not None:
                    snap = json.loads(json.dumps(t, ensure_ascii=False))
                    snap['kind'] = 'task'
                    if tid in pos:
                        items[pos[tid]] = snap
                    else:
                        pos[tid] = len(items)
                        items.append(snap)
    items = _JC['items']
    if cutoff:
        items = [i for i in items
                 if (i.get('ts') or i.get('created') or '9999') >= cutoff]
    return items[-limit:]


# ---------- run 执行 ----------
def _run_one(tid, sid):
    """执行一个 run: 子任务(sid=T*-n)或续聊(sid=F*)"""
    t = _tasks.get(tid)
    if not t or t.get('status') in ('stopped',):
        return
    fu = None
    if sid.startswith('F'):
        fu = t.get('followups', [])[int(sid[1:]) - 1]
        instruction = fu['question']
        fu['status'] = 'running'
        log_id = '%s-F%d' % (tid, fu['seq'])
    else:
        sub = next((s for s in t.get('subtasks', []) if s['sid'] == sid), None)
        if sub is None:
            return
        instruction = sub['instruction']
        instruction = t.get('fix_instruction') or instruction
        sub['status'] = 'running'
        log_id = sid
    if t.get('status') == 'queued':
        t['status'] = 'running'
        t['started'] = time.strftime('%Y-%m-%d %H:%M:%S')
    _note(t, 'run %s 领取' % log_id)
    save_task(tid)

    # ---- 轻工人分流: search/weather/note/reme 零 codex 秒级完成(跳过 RAG/skills 注入) ----
    if t.get('wtype') in ('search', 'weather', 'note', 'reme', 'lab',
                          'review', 'mail', 'files'):
        import light_workers
        ans, err = '', ''
        for _attempt in (1, 2):          # 瞬时故障(wttr抖动/ReMe重启窗)重试一次
            try:
                ans = light_workers.run(t['wtype'], instruction)
                err = ''
                break
            except Exception as e:
                err = 'LIGHT_FAIL: %s' % str(e)[:200]
                if _attempt == 1:
                    _note(t, '轻工人首次失败, 2s后重试: %s' % str(e)[:100])
                    time.sleep(2)
        if not ans and not err:
            err = 'EMPTY_RESULT'
        # ---- 质量检查环节(用户指令: 回复质量不过关不得发出) ----
        # Q1完整性+Q2实质性: weather/search 逐实体校验; 不达标→重试→升级codex重做
        if not err and t.get('wtype') in ('search', 'weather'):
            try:
                import quality_gate as _qg
                qr = _qg.check(instruction, ans)
                if not qr['ok'] and qr['missing']:
                    # 完整性缺陷(如两地天气只答了一地): 直接用多城市循环重跑一次
                    _note(t, '质检未过(%s), 重试' % '; '.join(qr['reasons'])[:60])
                    save_task(tid)
                    ans2 = light_workers.run(t['wtype'], instruction)
                    qr2 = _qg.check(instruction, ans2)
                    if qr2['ok']:
                        ans = ans2
                    elif _qg.should_escalate(qr2['reasons'] or qr['reasons']):
                        # 结构性缺陷(缺实体) → 升级 codex 重做(改wtype后重跑本函数)
                        _note(t, '重试仍缺实体 → 升级codex重做')
                        save_task(tid)
                        t['wtype'] = 'codex'
                        t['status'] = 'queued'
                        sub_or_none(t, sid)['status'] = 'queued'
                        _run_one(tid, sid)
                        return
                    else:
                        ans = ans2
            except Exception as _qe:
                _note(t, '质检异常(放行): %s' % str(_qe)[:60])
        # 用户可见的失败原因透传(原先只给"轻工人执行失败", 真因被吞)
        reason = err.replace('LIGHT_FAIL: ', '').replace('EMPTY_RESULT', '工人无返回')[:120] if err else ''
        _finish_run(t, sid, sub_or_none(t, sid), ans[:8000], err,
                    ('轻工人失败: %s' % reason) if err else '', log_id=log_id)
        return

    # ---- 调研工人: codex 执行 + 标准交付模板(报告落盘/结构/产物清单) ----
    if t.get('wtype') == 'research':
        instruction = ('%s\n\n【调研交付规范(必须遵守)】\n'
                       '1. 用中文写正式调研报告 Markdown, 保存到用户文档目录, '
                       '文件名格式「<主题>调研报告.md」(如: 数字分身调研报告.md)\n'
                       '2. 结构: ①结论先行摘要(≤5行) ②全景对比表格 ③给主人的落地建议'
                       '(每条标注实施难度+预期效果) ④来源链接\n'
                       '3. 数据必须实测核验(GitHub API/官方文档/命令实测), 不凭印象\n'
                       '4. 报告完成后, 回复末尾单独一行列「产物文件: <报告完整路径>」' % instruction)

    # Worker 仅接收: Router 定制指令(已含用户上下文) + 检索补充 + ID要求
    context = ''
    rag_err = ''
    try:
        import memory_engine
        context = memory_engine.context_for(instruction)
        rag_err = memory_engine.rag_error()
    except Exception as e:
        rag_err = str(e)
    sk = skills_context(instruction)
    # 产物目录: 统一收口, 任务完结后 router 扫此目录回传飞书
    outdir = os.path.join(STATE, 'outputs', tid)
    try:
        os.makedirs(outdir, exist_ok=True)
    except Exception:
        pass
    prompt = ('任务ID: %s\n原始诉求: %s\n\n%s\n\n'
              '产物要求: 若任务会产生文件(报告/代码/图表/文档等), 一律保存到产物目录 '
              '%s , 并在回复末尾用「产物文件:」列出文件清单; 纯问答类忽略本条。\n\n'
              '要求: 回复第一行必须写「[%s]」, 然后直接给出结果(结论在前)。' % (
                  tid, t['question'][:150],
                  (('Router 分配的指令(必须严格执行):\n' + instruction +
                    '\n\n' + (sk + '\n\n') +
                    '参考上下文(无关可忽略):\n' + context) if context
                   else ('Router 分配的指令(必须严格执行):\n' + instruction +
                         ('\n\n' + sk if sk else ''))),
                  outdir, tid))

    # ---- UFO2 GUI 工人分支(独立进程, hardened_ufo 加固链) ----
    if t.get('wtype') == 'ufo':
        _note(t, 'UFO2 GUI worker 领取')
        if not ufo_available():
            _finish_run(t, sid, sub_or_none(t, sid), '', 'UFO2_NOT_FOUND',
                        '本机未装 UFO2, 无法执行桌面GUI任务; '
                        '文本/编码类任务请正常派发(自动走Codex)')
            return
        ufo_cmd, ufo_name = _ufo_cmd(t, sid, instruction)
        ufo_log = os.path.join(LOGS, log_id + '.ufo.log')
        try:
            errf = open(ufo_log, 'w', encoding='utf-8')
            proc = subprocess.Popen(ufo_cmd, cwd=BASE, stdout=errf,
                                    stderr=errf)
            errf.close()
        except Exception as e:
            _finish_run(t, sid, sub_or_none(t, sid), '', 'SPAWN_FAILED', str(e))
            return
        _procs[log_id] = proc
        _note(t, 'UFO2 pid=%d 桌面执行中' % proc.pid)
        save_task(tid)
        rc = proc.wait()
        _procs.pop(log_id, None)
        out = ''
        if os.path.exists(ufo_log):
            out = open(ufo_log, encoding='utf-8', errors='replace').read()
        res = _extract_last_json(out)
        ok = res.get('ok') is True or str(res.get('complete')) == 'yes'
        answer = res.get('result') or res.get('answer') or json.dumps(
            res, ensure_ascii=False)[:800]
        err_code = '' if (ok and rc == 0) else ('EXIT_%d' % rc if rc != 0 else '')
        _finish_run(t, sid, sub_or_none(t, sid),
                    ('[UFO2桌面执行] ' if ok else '') + answer,
                    err_code if (rc == 0 and ok) else (err_code or 'UFO2_FAILED'),
                    res.get('reason', out[-400:]), log_id=log_id)
        return

    os.makedirs(LOGS, exist_ok=True)
    log_path = os.path.join(LOGS, log_id + '.log')
    stdin_path = os.path.join(LOGS, log_id + '.prompt.txt')
    with open(stdin_path, 'w', encoding='utf-8') as f:
        f.write(prompt)

    resume_sid = t.get('session') if (fu is not None and t.get('session')) else ''

    # ---- 审批闸v2(autoloop拍板卡片模型): 高危动作 → 飞书卡片拍板 → 等待零占用 ----
    try:
        import approval as _ap
        import feishu_adapter as _fa
        risk = _ap.risk_check(instruction)
        if risk and not resume_sid:   # 续聊任务不复审(拍过一次即可)
            aid = _ap.create(tid, t.get('question', ''), instruction[:160], risk)
            ok_card = _fa.send_approval_card(aid, tid, t.get('question', ''),
                                             instruction[:160], risk)
            if ok_card:
                _note(t, '审批闸: 高危动作[%s]已挂起, 等待飞书拍板(%s)' % (risk, aid))
                save_task(tid)
                a = _ap.wait_decision(aid, timeout=3600)
                if a and a['state'] == 'closed':
                    _finish_run(t, sid, sub_or_none(t, sid),
                                '用户取消了该任务(审批闸: %s)' % risk[:40],
                                'APPROVAL_CANCELLED', '用户未批准高危动作')
                    return
                _note(t, '审批通过, 继续执行')
                save_task(tid)
            else:
                _note(t, '审批卡片发送失败(%s), 放行并提示用户' % (_fa._status.get('error','')[:40]))
    except Exception as _ae:
        _note(t, '审批闸异常(放行): %s' % str(_ae)[:80])

    # ---- opencode 分流(用户指令: 公共任务用免费LLM省codex额度, 涉密/个人信息走codex) ----
    # 公共判定: 非涉密关键词 + 非续聊(followup须同引擎续会话) → opencode; 失败自动降级codex
    PRIVATE_KW = ('密码', '账号', '账密', '个人', '涉密', '身份证', '银行', '支付',
                  '私密', '家里', '老婆', '家人', '内部', 'api-key', 'token', 'secret')
    is_private = any(k in instruction.lower() for k in PRIVATE_KW) or bool(resume_sid) \
        or bool(t.get('image'))
    use_oc = (t.get('wtype') == 'opencode') and not is_private

    def _run_opencode():
        """opencode 免费LLM执行; 成功返回answer, 失败返回None(调用方降级codex)"""
        oc_models = ['opencode/glm-5.3-flash-free',
                     'opencode/deepseek-v4.1-flash-free',
                     'opencode/flint-40b-free']
        for om in oc_models:
            ocmd = ['opencode', 'run', '-m', om]
            if t.get('image') and os.path.exists(t['image']):
                ocmd += ['-f', t['image']]
            ocmd += [instruction]
            _note(t, 'opencode执行(%s)' % om.split('/')[-1])
            save_task(tid)
            try:
                oproc = subprocess.Popen(ocmd, cwd=BASE, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL)
                _procs[log_id] = oproc
                oout, _ = oproc.communicate(timeout=900)
                _procs.pop(log_id, None)
                oans = oproc.returncode == 0 and (oout or b'').decode('utf-8', 'replace').strip()
                # 过滤CLI横幅噪音行(> build · model 等)
                if oans:
                    oans = '\n'.join(l for l in oans.splitlines()
                                     if not re.match(r'^[>\s]*build\s*·', l)
                                     and l.strip() not in ('[0m]', '')).strip()
                if oproc.returncode == 0 and oans and 'Error:' not in oans[:60] \
                        and len(oans) > 10:
                    return oans
                _note(t, 'opencode(%s)失败, 换下一模型' % om.split('/')[-1])
            except Exception as oe:
                _note(t, 'opencode异常: %s' % str(oe)[:80])
        return None

    if use_oc:
        _oc = _run_opencode()
        if _oc:
            _finish_run(t, sid, sub_or_none(t, sid), _oc[:20000], '',
                        '', log_id=log_id)
            return
        _note(t, 'opencode全链失败 → 自动降级codex')
        save_task(tid)

    before_snap = _session_snapshot()
    cmd = [codex_exe(), 'exec']
    if t.get('image') and os.path.exists(t['image']):
        cmd += ['-i', t['image']]
    if resume_sid:
        # resume 子命令不支持 --sandbox(属exec级, 须在其之前); argv 传消息(原生exe无cmd层)
        cmd += ['--sandbox', 'danger-full-access',
                'resume', resume_sid, '--skip-git-repo-check',
                '-o', os.path.join(LOGS, log_id + '.answer.txt'), instruction]
    else:
        cmd += ['--skip-git-repo-check', '--sandbox', 'danger-full-access',
                '-o', os.path.join(LOGS, log_id + '.answer.txt')]
    if not (shutil.which(cmd[0]) or os.path.isfile(cmd[0])):
        _finish_run(t, sid, fu, '', 'CODEX_NOT_FOUND',
                    '未找到 codex CLI。安装: npm install -g @openai/codex 并 codex login')
        return
    try:
        errf = open(log_path, 'a', encoding='utf-8')
        inf = open(stdin_path, 'r', encoding='utf-8')
        proc = subprocess.Popen(cmd, cwd=BASE, stdin=inf, stdout=errf, stderr=errf)
        inf.close()
        errf.close()
    except Exception as e:
        _finish_run(t, sid, fu, '', 'SPAWN_FAILED', str(e))
        return
    _procs[log_id] = proc
    _note(t, 'run %s pid=%d%s' % (log_id, proc.pid, ' 续聊会话' if resume_sid else ''))
    save_task(tid)
    rc = proc.wait()      # 无硬超时
    _procs.pop(log_id, None)   # 进程已退出, 即刻移除防表泄漏
    answer = ''
    out_file = os.path.join(LOGS, log_id + '.answer.txt')
    if os.path.exists(out_file):
        answer = open(out_file, encoding='utf-8', errors='replace').read().strip()[:20000]

    new_session = _new_session_uuid(before_snap) if not resume_sid else ''

    if t.get('status') == 'stopped':
        _finish_run(t, sid, fu, answer[:800], 'STOPPED_BY_USER', '用户手动停止')
        return
    if fu is not None:
        fu['status'] = 'done' if (rc == 0 and answer) else 'failed'
        fu['answer'] = answer[:3000]
        tk = _tokens_from_log(log_path)
        if tk:
            t['tokens'] = (t.get('tokens') or 0) + tk
        if rc != 0:
            tail = open(log_path, encoding='utf-8', errors='replace').read()[-400:] \
                if os.path.exists(log_path) else ''
            fu['error_code'] = 'EXIT_%d' % rc
            fu['error'] = tail
        t['status'] = 'done'
        t['ended'] = time.strftime('%Y-%m-%d %H:%M:%S')
        save_task(tid)
        cb = _push_done
        if cb:
            try:
                cb(dict(t))
            except Exception:
                pass
        return
    _finish_run(t, sid, sub_or_none(t, sid), answer,
                ('EXIT_%d' % rc) if rc != 0 else '',
                (open(log_path, encoding='utf-8', errors='replace').read()[-800:]
                 if rc != 0 and os.path.exists(log_path) else ''),
                session=new_session, log_id=log_id)


def sub_or_none(t, sid):
    return next((s for s in t.get('subtasks', []) if s['sid'] == sid), None)


def _finish_run(t, sid, sub, answer, error_code, error, session='', log_id=''):
    tid = t['id']
    if log_id:
        tk = _tokens_from_log(os.path.join(LOGS, log_id + '.log'))
        if tk:
            t['tokens'] = (t.get('tokens') or 0) + tk
    if sub is None:
        sub = sub_or_none(t, sid)
    if sub is not None:
        if not error_code:
            sub['status'] = 'done'
            sub['result'] = answer
            if session:
                sub['session'] = session
        elif error_code == 'STOPPED_BY_USER':
            sub['status'] = 'stopped'
            sub['result'] = answer[:500]
        else:
            sub['status'] = 'failed'
            sub['result'] = answer[:500]
            sub['error_code'] = error_code
            sub['error'] = error
            if 'rate limit' in error.lower() or '使用上限' in error:
                sub['error_code'] = 'CODEX_RATE_LIMITED'
                sub['error'] = error  # 原样保留含重置时间
    _note(t, 'run %s 完结: %s' % (sid, error_code or 'done'))
    if sub is not None and sub.get('session') and not t.get('session'):
        t['session'] = sub['session']
    subs = t.get('subtasks', [])
    if subs and all(s.get('status') in ('done', 'failed', 'stopped') for s in subs):
        ok_n = sum(1 for s in subs if s['status'] == 'done')
        stopped_n = sum(1 for s in subs if s['status'] == 'stopped')
        t['ended'] = time.strftime('%Y-%m-%d %H:%M:%S')
        # Router 总结层: 回复给用户的 summary = 简洁重点突出(LLM要点, ≤6行);
        # worker 原始回答存 raw_answer → 聊天流默认折叠, 用户可展开
        raw_answer = (chr(10) * 2).join(
            filter(None, (s.get('result', '') for s in subs)))[:15000]
        t['raw_answer'] = raw_answer
        # ③技能化沉淀: 同类任务成功计数, 够3次自动提炼技能卡
        try:
            import skills_memo
            hint = skills_memo.maybe_distill(t.get('question', ''), t.get('wtype', 'codex'))
            if hint:
                _note(t, hint)
        except Exception:
            pass
        if ok_n:
            # 看图/识图类任务: 完整准确优先(用户指令), 跳过LLM压缩, 全文直出+入记忆
            _q = ' '.join((t.get('question') or '').split())
            if '图片' in _q or '识图' in _q or (t.get('image') and not _q):
                first = ' '.join((subs[0].get('result') or '').split())
                sm = first[:3000]
            elif t.get('wtype') in ('search', 'weather', 'note', 'reme', 'review', 'mail', 'files'):
                # 轻工人答案已是人话(且已秒级完成), 不再过 LLM 总结(避免串味+省延迟)
                first = subs[0].get('result', '')
                sm = ' '.join(first.split())[:400] if len(first) > 400 else first
            else:
                try:
                    import router_brain
                    # 总结只基于任务本身(混入记忆会让要点串味)
                    sm = router_brain.summarize_task(t.get('question', ''), subs)
                except Exception:
                    sm = None
            if not sm:
                first = subs[0].get('result', '')
                sm = (first[:280] + '…(worker原始回答可展开查看)') \
                    if len(first) > 300 else first
        else:
            sm = '\n'.join('%s(%s): %s' % (s.get('brief', ''), s.get('status', ''),
                          (s.get('error') or s.get('result') or '')[:200])
                          for s in subs)
        # ===== delegation loop (user directive) =====
        deleg = t.get('delegation') or {'round': 0}
        _can_delegate = (len(subs) == 1 and subs[0].get('status') == 'done'
                         and not stopped_n
                         and t.get('wtype') in ('codex', 'opencode')
                         and deleg['round'] < MAX_DELEGATION_ROUNDS)
        if _can_delegate:
            import delegation as _dg
            verdict = _dg.verify(t.get('question', ''), raw_answer)
            hist = deleg.setdefault('history', [])
            hist.append({'round': deleg['round'] + 1,
                         'verdict': verdict['verdict'],
                         'reasons': verdict['reasons']})
            t['delegation'] = deleg
            if verdict['verdict'] == 'FAIL' and deleg['round'] < MAX_DELEGATION_ROUNDS - 1:
                deleg['round'] += 1
                t['delegation'] = deleg
                _note(t, 'delegation: FAIL(%s), round %d' % (
                    '; '.join(verdict['reasons'])[:50], deleg['round']))
                new_wt = t['wtype']
                if deleg['round'] >= 2:
                    new_wt = _dg.alt_engine(t['wtype'])
                fix_ins = _dg.fix_instruction(t.get('question', ''), raw_answer,
                                              verdict['reasons'], deleg['round'])
                t['status'] = 'queued'
                subs[0]['status'] = 'queued'
                subs[0]['result'] = ''
                t['fix_instruction'] = fix_ins
                t['delegation'] = deleg
                save_task(tid)
                if new_wt == 'opencode':
                    _run_one(tid, sid)
                else:
                    import threading as _th2
                    _th2.Thread(target=_run_one, args=(tid, sid), daemon=True).start()
                return
            elif verdict['verdict'] == 'FAIL':
                _note(t, 'delegation: rounds exhausted, report honestly')
                sm = (sm + chr(10) + chr(10) +
                      'Warning: after %d attempts still below standard (%s). Above is the last actual result.' % (
                          MAX_DELEGATION_ROUNDS, '; '.join(verdict['reasons'])[:80]))
        t['summary'] = '[%s] 「%s」\n%s' % (tid, t.get('question', '')[:50], sm)
        # status set last; stopped beats failed (user stop is not failure)
        if stopped_n:
            t['status'] = 'stopped'
        else:
            t['status'] = 'done' if ok_n else 'failed'
        t['ended'] = t.get('ended') or time.strftime('%Y-%m-%d %H:%M:%S')
        # set status last; stopped beats failed
        try:
            import memory_engine
            memory_engine.index_task(tid, t.get('question', ''),
                                     raw_answer[:8000],
                                     t.get('source', 'web'))
        except Exception:
            pass
    save_task(tid)
    cb = _push_summary
    if cb:
        try:
            cb(dict(t))
        except Exception:
            pass
    cb2 = _push_done
    if cb2 and sub is not None:
        try:
            cb2(dict(t))
        except Exception:
            pass


def _watchdog_loop():
    """自愈: 存活worker线程不足时自动补建(DeepSeek superagent Watchdog模块)"""
    while True:
        time.sleep(30)
        try:
            alive = sum(1 for th in threading.enumerate()
                        if th.name.startswith('worker-'))
            if alive < _cfg['max_workers']:
                for i in range(alive, _cfg['max_workers']):
                    threading.Thread(target=_worker_loop, daemon=True,
                                     name='worker-%d' % i).start()
        except Exception:
            pass


def _worker_loop():
    while True:
        tid, sid = _queue.get()
        try:
            _run_one(tid, sid)
        except Exception as e:
            t = _tasks.get(tid)
            if t:
                _note(t, 'run %s 内部错误: %s' % (sid, str(e)[:150]))
                save_task(tid)
        finally:
            time.sleep(0.2)


def start(push_done_cb=None, push_summary_cb=None, max_workers=3):
    global _push_done, _push_summary
    _push_done = push_done_cb
    _push_summary = push_summary_cb
    set_config(max_workers=max_workers)
    _load_ledger()
    for t in list(_tasks.values()):
        if t.get('status') == 'running':
            t['status'] = 'interrupted'
            t['error_code'] = 'SERVICE_RESTARTED'
            t['error'] = '服务重启, 执行中断; 可续聊或重试'
            t['ended'] = time.strftime('%Y-%m-%d %H:%M:%S')
            save_task(t['id'])
        elif t.get('status') == 'queued':
            for s2 in t.get('subtasks', []):        # 自愈: 排队任务原样续跑
                if s2.get('status') == 'queued':
                    _queue.put((t['id'], s2['sid']))
    # 中断任务通知: 如果有interrupted任务, 通知用户(飞书/ntfy), 不让中断悄悄发生
    _interrupted_notify = [t for t in _tasks.values()
                           if t.get('status') == 'interrupted'
                           and t.get('error_code') == 'SERVICE_RESTARTED']
    for t in _interrupted_notify:
        try:
            import feishu_adapter as _fa
            _fa.reply_to_last('⚠ 服务重启导致以下任务被中断, 可对我说「重试 %s」继续:\n· %s' % (
                t['id'][-8:], ' '.join((t.get('question') or '').split())[:60]))
        except Exception:
            pass
    for i in range(_cfg['max_workers']):
        threading.Thread(target=_worker_loop, daemon=True,
                         name='worker-%d' % i).start()
    threading.Thread(target=_watchdog_loop, daemon=True,
                     name='watchdog').start()


if __name__ == '__main__':
    start()
    tid = new_task('请只回复两个字: OK')
    for _ in range(40):
        time.sleep(5)
        t = get_task(tid)
        if t and t['status'] in ('done', 'failed'):
            print(json.dumps(t, ensure_ascii=False, indent=1)[:600])
            break
