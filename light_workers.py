# -*- coding: utf-8 -*-
"""轻工人模块 — search / weather / note 三类快速工人(零 codex, 秒级完成)

设计:
  - 与 codex worker 同一套账本/总结/推送链路(codex_worker._run_one 按 wtype 分流到此)
  - 本机 HTTP 一律 curl 子进程(urllib OpenSSL 握手坑), 密钥只从环境/vault 读
  - 纯函数(parse_weather/extract_query/note_*)与网络/DB 分离, 便于离线测试
  - 任何失败 raise, 由 _run_one 统一兜成任务失败(错误码 LIGHT_FAIL), 不静默
"""
import json
import os
import re
import subprocess
import urllib.parse
import urllib.request

SEARCH_CLI = os.environ.get('BUTLER_SEARCH_CLI', '')  # 可选: unified-search 脚本路径
DEFAULT_CITY = '惠州'


def run(wtype, instruction):
    """入口: wtype → 对应工人, 返回给用户看的文本"""
    if wtype == 'search':
        return run_search(instruction)
    if wtype == 'weather':
        return run_weather(instruction)
    if wtype == 'weekly':
        return run_weekly(instruction)
    if wtype == 'review':
        return run_review(instruction)
    if wtype == 'mail':
        return run_mail(instruction)
    if wtype == 'files':
        return run_files(instruction)
    if wtype == 'note':
        from reme_worker import run_reme
        # 记忆入口统一(G1): 笔记四则也走 ReMe(同一真相源), 旧 notes_fts 保留只读
        # 兼容旧说法: "添加笔记X"→reme_write, "查笔记X"→reme_search
        import re as _re
        m = _re.match(r'^(?:请)?(?:帮我)?(?:新增|添加|加)(?:一条)?笔记[:：,，\s]*(.+)$', instruction.strip(), _re.S)
        if m:
            content = ' '.join(m.group(1).split())
            from reme_worker import run_reme as _rr
            return _rr('记住: ' + content)
        m2 = _re.match(r'^(?:请)?(?:帮我)?(?:查(?:一下)?|搜索|找|看看)(?:一下)?笔记[:：,，\s]*(.*)$', instruction.strip())
        if m2:
            from reme_worker import run_reme as _rr
            kw = m2.group(1).strip()
            return _rr(('查记忆 ' + kw) if kw else '查记忆')
        return run_note(instruction)
    if wtype == 'reme':
        from reme_worker import run_reme
        return run_reme(instruction)
    if wtype == 'lab':
        return run_lab(instruction)
    raise ValueError('未知轻工人类型: %s' % wtype)


def run_lab(instruction):
    """lab 遥控: 转发目标到 Nightly AI Lab(9721), 走其网页 goal 同款入口"""
    import json
    body = json.dumps({'text': instruction}).encode('utf-8')
    req = urllib.request.Request('http://127.0.0.1:9721/api/goal', data=body, method='POST')
    req.add_header('Content-Type', 'application/json')
    with urllib.request.urlopen(req, timeout=180) as r:
        d = json.loads(r.read().decode('utf-8'))
    if d.get('ok'):
        lines = ['🧪 已转交 Nightly AI Lab(项目 %s):' % d.get('project_id', '')[-8:]]
        for t in d.get('tasks', [])[:4]:
            lines.append('· %s' % t.get('objective', '')[:60])
        lines.append('夜间窗口自动执行, 完成后可查 Lab Dashboard(9721)')
        return chr(10).join(lines)
    return '🧪 Lab 转发失败: %s' % d.get('error', '')[:100]


# ==================== review 复盘工人 ====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(BASE_DIR, 'state', 'ledger.jsonl')
import config as _cfg
KB_EXP_DIR = _cfg.experience_dir()
SYNC_KB = os.path.join(os.path.dirname(KB_EXP_DIR), 'sync_kb.py') if KB_EXP_DIR else ''


def parse_review_day(msg):
    """复盘日期解析: 今天(默认)/昨天/前天/YYYY-MM-DD(纯函数, 可测)"""
    import datetime
    m = re.search(r'(\d{4}-\d{1,2}-\d{1,2})', msg or '')
    if m:
        return m.group(1)
    off = -2 if '前天' in (msg or '') else (-1 if '昨天' in (msg or '') else 0)
    return (datetime.date.today() + datetime.timedelta(days=off)).strftime('%Y-%m-%d')


def scan_day_tasks(day, ledger_path=None):
    """扫 ledger 当日完结任务(同id取最新状态); 返回 [(status, question)]"""
    latest = {}
    try:
        fh = open(ledger_path or LEDGER, encoding='utf-8', errors='replace')
    except OSError:
        return []
    with fh:
        for line in fh:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get('type') is not None or 'status' not in r:
                continue
            ended = str(r.get('ended') or '')
            if ended.startswith(day) and r.get('status') in ('done', 'failed', 'stopped'):
                latest[r.get('id')] = (r['status'], str(r.get('question') or '')[:60])
    return list(latest.values())


def run_review(instruction):
    """复盘工人: 当日任务 → LLM 提炼 S/K → 追加经验库 → 后台触发 RAG 同步"""
    day = parse_review_day(instruction)
    tasks = scan_day_tasks(day)
    if not tasks:
        return '📭 %s 管家没有完结任务, 无可复盘。' % day
    listing = chr(10).join('- [%s] %s' % (st, q) for st, q in tasks[:20])
    s_lessons, k_lessons = [], []
    try:
        import model_router as _mr
        prompt = (
            '你是复盘教练。下面是AI管家 %s 完结的任务清单(状态+诉求)。' % day +
            chr(10) + listing +
            chr(10) + chr(10) + '提炼做对的可复制经验(S类)与踩坑教训(K类), 各1-4条, 每条≤40字, '
            '只输出严格JSON: {"s": ["..."], "k": ["..."]}')
        raw = _mr.chat(prompt, mt=120)
        raw = raw[0] if isinstance(raw, tuple) else raw
        m = re.search(r'\{.*\}', raw or '', re.S)
        if m:
            d = json.loads(m.group(0))
            s_lessons = [str(x)[:60] for x in (d.get('s') or [])][:4]
            k_lessons = [str(x)[:60] for x in (d.get('k') or [])][:4]
    except Exception:
        pass  # LLM失败 → 降级只归档任务清单
    body = ['# 管家复盘 %s' % day, '',
            '## 任务清单(%d项)' % len(tasks), '', listing, '']
    if s_lessons:
        body += ['## S类 可复制经验', ''] + ['- %s' % x for x in s_lessons] + ['']
    if k_lessons:
        body += ['## K类 踩坑教训', ''] + ['- %s' % x for x in k_lessons] + ['']
    try:
        os.makedirs(KB_EXP_DIR, exist_ok=True)
        with open(os.path.join(KB_EXP_DIR, '管家复盘-%s.md' % day),
                  'a', encoding='utf-8') as f:
            f.write(chr(10).join(body) + chr(10))
    except OSError as e:
        return '⚠ 经验库写入失败: %s' % e
    try:  # RAG 同步后台触发(不阻塞回复)
        if SYNC_KB and os.path.isfile(SYNC_KB):
            subprocess.Popen([sys.executable, SYNC_KB], cwd=os.path.dirname(SYNC_KB),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    return ('✅ %s 复盘完成: %d项任务, S类%d条/K类%d条 → 经验库《管家复盘-%s.md》, '
            'RAG同步已在后台跑' % (day, len(tasks), len(s_lessons), len(k_lessons), day))


# ==================== mail 邮件工人 ====================
def _fmt_mailtime(iso):
    """UTC ISO → 本地 MM-DD HH:MM(纯函数, 可测)"""
    try:
        import datetime
        dt = datetime.datetime.strptime(iso[:16], '%Y-%m-%dT%H:%M')
        dt += datetime.timedelta(hours=8)          # agent.qq.com 返回 UTC
        return dt.strftime('%m-%d %H:%M')
    except Exception:
        return (iso or '')[:16]


def extract_codes(msgs):
    """从邮件列表提取验证码: [(code, from, subject)] (纯函数, 可测)"""
    codes = []
    for m in msgs or []:
        text = '%s %s' % (m.get('subject', ''), m.get('snippet', ''))
        if not re.search(r'验证码|verification|confirm.?code| otp\b', text, re.I):
            continue
        mm = re.search(r'\b(\d{4,8})\b', text)
        if mm:
            codes.append((mm.group(1),
                          (m.get('from') or {}).get('email', ''),
                          (m.get('subject') or '')[:36]))
    return codes[:5]


def _agently_json(args, timeout=40):
    """agently-cli 子进程(npm .cmd shim, shell=True) → dict; 失败 raise"""
    r = subprocess.run(['agently-cli'] + args, capture_output=True, timeout=timeout,
                       text=True, encoding='utf-8', errors='replace', shell=True)
    out = (r.stdout or '').strip()
    d = json.loads(out[out.index('{'):])
    if not d.get('ok'):
        raise ValueError(str(d)[:120])
    return d


def run_mail(instruction):
    """邮件工人: agent.qq.com 信箱(agently-cli); 163 未配 IMAP 授权码, 明确告知"""
    if '163' in instruction:
        return ('📪 163邮箱的 IMAP 授权码还没配置, 暂只能查 agent.qq.com 信箱。'
                '配置方法: 163网页版→设置→POP3/IMAP→开启并拿授权码。')
    d = _agently_json(['message', '+list'])
    msgs = (d.get('data') or {}).get('data') or []
    if not msgs:
        return '📬 agent.qq.com 信箱是空的。'
    unread_n = sum(1 for m in msgs if not m.get('is_read'))
    if re.search(r'验证码|code|otp', instruction, re.I):
        codes = extract_codes(msgs)
        if not codes:
            return '🔢 最近 %d 封邮件里没有验证码。' % len(msgs)
        return '🔢 验证码:' + chr(10).join(
            '- %s ← %s (%s)' % (c, frm, sub) for c, frm, sub in codes)
    lines = ['📬 agent.qq.com: %d封未读 / 共%d封' % (unread_n, len(msgs))]
    for m in msgs[:5]:
        flag = '🔴' if not m.get('is_read') else '  '
        lines.append('%s %s (%s, %s)' % (
            flag, (m.get('subject') or '(无主题)')[:40],
            (m.get('from') or {}).get('email', '')[:30],
            _fmt_mailtime(m.get('created_at', ''))))
    return chr(10).join(lines)


# ==================== files 文件工人 ====================
ES_EXE = r'C:\Program Files\Everything\es.exe'
AD_PY = os.environ.get('BUTLER_ALIYUN_AD', '')  # 可选: aliyun-drive 技能 ad.py 路径
CLOUD_DEFAULT = '/zcode共享文件/'
LOCAL_ROOTS = (os.path.expanduser('~') + r'\Desktop',
               os.path.expanduser('~') + r'\Downloads')


def files_intent(msg):
    """文件意图解析(纯函数, 可测) → (action, arg, target)
    action: upload/ls/find/download/None"""
    msg = (msg or '').strip()
    # 列表(严格: 需有"有什么/哪些/列表/看看"字样; 排除上传下载语句)
    m = re.search(r'(?:云盘|阿里云盘|网盘)(?:里|内|中|上|里面)?(?:都?有(?:什么|哪些|啥)|列表|看看|ls)', msg)
    if m and not re.search(r'下载|上传|传', msg):
        return ('ls', None, None)
    # 下载: 支持「下载云盘的X到桌面」/「从云盘下载X」
    m = re.search(r'下载\s*(?:云盘|阿里云盘|网盘)?(?:的|里(?:的|面)?)?\s*(.+?)\s*(?:到|至)\s*'
                  r'(桌面|下载|D盘|当前)?', msg)
    if m and re.search(r'云盘|网盘', msg):
        return ('download', m.group(1).strip(' ""\''),
                {'桌面': LOCAL_ROOTS[0], '下载': LOCAL_ROOTS[1]}.get(m.group(2)))
    m = re.search(r'(?:把)?\s*(.+?)\s*(?:上传|传)(?:到|进|上|去)?(?:云盘|阿里云盘|网盘)'
                  r'(?:的)?\s*(\S+)\s*$', msg)
    if m:
        return ('upload', m.group(1).strip(' ""\''), m.group(2).strip('/'))
    m = re.search(r'(?:上传|传)(?:一下)?\s*(.+?)\s*(?:到|进)?(?:云盘|阿里云盘|网盘)', msg)
    if m:
        return ('upload', m.group(1).strip(' ""\''), None)
    # 兜底: 提到云盘但非写码/文档类 → 列表
    if re.search(r'云盘|阿里云盘|网盘', msg) and not re.search(r'写|代码|脚本|报告|调研|同步', msg):
        return ('ls', None, None)
    m = re.search(r'找(一下|下|找)?\s*(?:本地)?\s*(?:文件|那个|这个)?\s*(.+)$', msg)
    if m and len(msg) <= 60:
        return ('find', m.group(2).strip(' ""\''), None)
    return (None, None, None)


def _es_search(kw, n=8):
    """Everything 检索; 输出GBK逐级解码; 整句无果时按中点拆两词 AND 重试
    (Everything 整句=连续子串; 拆词必须传多个argv, 单argv带空格会被当精确短语)"""
    hits = _es_raw(kw, n)
    if not hits and kw and len(kw) >= 4:
        mid = len(kw) // 2
        hits = _es_raw([kw[:mid], kw[mid:]], n)
    return hits


def _es_raw(kw, n):
    try:
        terms = list(kw) if isinstance(kw, (list, tuple)) else [kw]
        r = subprocess.run([ES_EXE, '-n', str(n)] + terms, capture_output=True, timeout=15)
    except Exception:
        return []
    raw = r.stdout or b''
    out = ''
    for enc in ('utf-8', 'gbk'):
        try:
            out = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if not out:
        out = raw.decode('gbk', errors='replace')
    return [l.strip() for l in out.splitlines() if l.strip()]


def _resolve_local(name):
    """用户口中的文件名 → 本地路径: 绝对/常见根直查, 否则 Everything 挑最像的"""
    p = (name or '').strip()
    if not p:
        return ''
    if os.path.isabs(p) and os.path.exists(p):
        return p
    for base in LOCAL_ROOTS:
        cand = os.path.join(base, p)
        if os.path.exists(cand):
            return cand
    hits = _es_search(os.path.basename(p) or p)
    if not hits:
        return ''
    p_l = p.lower().replace(' ', '')
    for h in hits:  # 文件名包含/后缀命中优先
        bn = os.path.basename(h).lower().replace(' ', '')
        if p_l in bn or bn in p_l:
            return h
    return hits[0]


def _ad(args, timeout=900):
    """ad.py 子进程; 返回 (ok, 输出尾部)"""
    r = subprocess.run(['python', AD_PY] + args, capture_output=True, timeout=timeout,
                       text=True, encoding='utf-8', errors='replace')
    out = ((r.stdout or '') + (r.stderr or ''))[-800:]
    return (r.returncode == 0 and 'RESULT: OK' in out), out


def run_files(instruction):
    """文件工人: 云盘上传/列表/下载 + 本地 Everything 查找"""
    action, arg, target = files_intent(instruction)
    if action == 'ls':
        ok, out = _ad(['ls', ('/' + target) if target else CLOUD_DEFAULT.rstrip('/')], timeout=60)
        return ('☁️ 云盘 %s:' % (target or CLOUD_DEFAULT)) + chr(10) + (out.strip()[:600] or '(空)')
    if action == 'upload':
        local = _resolve_local(arg)
        if not local:
            return '🔍 本地没找到「%s」(已试桌面/下载/Everything全盘)' % arg
        dest = ('/' + target) if target else CLOUD_DEFAULT.rstrip('/')
        ok, out = _ad(['upload', local, dest])
        name = os.path.basename(local)
        return (('☁️ 已上传 %s → %s' % (name, dest)) if ok
                else '⚠ 上传存疑, 请核对:' + chr(10) + out[-300:])
    if action == 'download':
        # ad.py 无 download 封装 → 直调 CLI(默认落其 download/ 目录, 完了搬运到目标)
        dl_dir = r'C:\Tools\aliyunpan\download'
        dest_dir = target or LOCAL_ROOTS[0]
        before = set(os.listdir(dl_dir)) if os.path.isdir(dl_dir) else set()
        try:
            r = subprocess.run([r'C:\Tools\aliyunpan\aliyunpan.exe', 'download', arg],
                               capture_output=True, timeout=1200, text=True,
                               encoding='utf-8', errors='replace', stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return '⚠ 下载超时: %s' % arg
        out = ((r.stdout or '') + (r.stderr or ''))[-400:]
        os.makedirs(dest_dir, exist_ok=True)
        moved = []
        for f in (os.listdir(dl_dir) if os.path.isdir(dl_dir) else []):
            if f in before:
                continue
            src = os.path.join(dl_dir, f)
            if os.path.isfile(src):
                os.replace(src, os.path.join(dest_dir, f))
                moved.append(f)
        if moved:
            return '☁️ 已下载 %s → %s(%s)' % (arg, dest_dir, ', '.join(moved[:3]))
        return '⚠ 下载未见产物, 请核对:' + chr(10) + out[-300:]
    if action == 'find':
        hits = _es_search(arg, 10)
        if not hits:
            return '🔍 Everything 没找到「%s」' % arg
        return '🔍 找到 %d 项:' % len(hits) + chr(10).join(
            '- %s' % h for h in hits[:8])
    raise ValueError('文件意图未识别(支持: 把X传云盘[到目录] / 云盘里有什么 / '
                     '下载云盘的X到桌面 / 找一下X文件)')




def run_weekly(instruction):
    """E周报工人: 聚合近7天任务+成果 → 周报(总量/分布/关键成果/教训)"""
    import datetime
    today = datetime.date.today()
    days = [(today - datetime.timedelta(days=i)).strftime('%Y-%m-%d')
            for i in range(7)]
    latest = {}
    try:
        for line in open(LEDGER, encoding='utf-8', errors='replace'):
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get('type') is None and 'status' in r:
                latest[r.get('id')] = r
    except OSError:
        return '📭 没有任务历史。'
    week = [t for t in latest.values()
            if str(t.get('created', '')).startswith(tuple(days))]
    if not week:
        return '📭 近7天(周报窗口)没有任务记录。'
    from collections import Counter
    st = Counter(t['status'] for t in week)
    wt = Counter(t.get('wtype', 'codex') for t in week)
    # 关键成果(带raw的前5)
    results = [t for t in week if t['status'] == 'done' and (t.get('raw_answer') or '').strip()]
    listing = chr(10).join('- [%s] %s' % (
        t['status'], ' '.join((t.get('question') or '').split())[:44]) for t in week[:25])
    rep = ['📊 近7天周报(任务%d: 完成%d/失败%d/其他%d)' % (
        len(week), st.get('done', 0), st.get('failed', 0),
        len(week) - st.get('done', 0) - st.get('failed', 0)),
        '· 工人分布: ' + ', '.join('%s×%d' % kv for kv in wt.most_common(5))]
    if results:
        rep.append('· 关键成果TOP:')
        for t in results[:5]:
            first = ' '.join((t.get('raw_answer') or '').split())[:60]
            rep.append('  - %s → %s' % (' '.join((t.get('question') or '').split())[:30], first))
    fails = [t for t in week if t['status'] == 'failed']
    if fails:
        rep.append('· 失败%d项(需关注):' % len(fails))
        for t in fails[:3]:
            rep.append('  - %s' % ' '.join((t.get('question') or '').split())[:40])
    rep.append('· 任务清单(%d项, 节选):' % len(week))
    rep.append(listing)
    return chr(10).join(rep)[:2200]


def _curl(url, timeout=20):
    """GET 文本(curl 子进程, --ssl-no-revoke 治 schannel 卡死)"""
    r = subprocess.run(['curl', '-sL', '--ssl-no-revoke', '--max-time', str(timeout), url],
                       capture_output=True, timeout=timeout + 5)
    return r.stdout.decode('utf-8', errors='replace')


# ================= weather worker =================
def extract_city(instruction, default=DEFAULT_CITY):
    """从话里抽城市名; 抽不到用默认。匹配『XX的天气』『在XX』『去XX玩』及常见直呼"""
    # 预清理: 剥指令前缀动词 + 去时间词(否则『搜索下惠州今日的天气』整句被当城市)
    s = re.sub(r'^(请|帮我|麻烦|帮忙)?(联网|上网)?(搜索|搜一下|搜一搜|搜搜|查询|查一下|查查|查下|查|看看|看下|告诉我|下)+',
               '', instruction.strip()).strip()
    s = re.sub(r'(今日|今天|现在|当前|目前|实时)', '', s)
    instruction = s
    m = re.search(r'(?:在|去|到)([\u4e00-\u9fa5]{2,4}?)(?:市|的|玩|出差|旅行|天气|气温|温度|下雨|,|，|。|？|\?|$)',
                  instruction)
    if m and m.group(1) not in ('我', '这里', '外面', '网上', '家里'):
        return m.group(1)
    m = re.search(r'([\u4e00-\u9fa5]{2,7}?)(?:市)?(?:的|地区)?(?:天气|气温|温度|降雨|下雨|湿度)', instruction)
    if m:
        city = m.group(1)
        # 去掉常见前缀动词残留
        city = re.sub(r'^(查|看看|看下|帮我|请问|一下|今天|明天|后天|现在|当前)+', '', city)
        city = re.split(r'[和与及]', city)[-1]   # '汇总GitHub趋势Top5和惠州'→'惠州'
        if len(city) >= 2:
            return city
    # 英文城市: 'Tokyo weather' / 'weather in Paris' / 'xyzzz 的天气'。
    # 异常名也要透传给 wttr 报错——用户点名了城市却得到默认城市天气是严重误导
    m = (re.search(r'([A-Za-z][A-Za-z .\'-]{1,29})\s*(?:的)?(?:天气|weather)', instruction, re.I)
         or re.search(r'weather\s+(?:in|at|for)\s+([A-Za-z][A-Za-z .\'-]{1,29})', instruction, re.I))
    if m:
        city = re.sub(r'\s+(now|today|tomorrow)$', '',
                      m.group(1).strip().strip(',. '), flags=re.I)
        if (2 <= len(city) <= 30
                and not re.match(r'^(how|what|the|is|today|tomorrow|now|in|at|for|like)$', city, re.I)):
            return city
    return default


def _curl_retry(url, timeout=15, attempts=3):
    """带退避重试的 curl(空响应视为失败——wttr 限速时常见)"""
    import time as _t
    last = ''
    for i in range(attempts):
        last = _curl(url, timeout=timeout)
        if last.strip():
            return last
        _t.sleep(1 + i * 2)
    return last


WMO_ZH = {0: '晴', 1: '基本晴', 2: '局部多云', 3: '阴', 45: '雾', 48: '雾凇',
          51: '毛毛雨', 53: '毛毛雨', 55: '浓毛毛雨', 61: '小雨', 63: '中雨', 65: '大雨',
          66: '冻雨', 67: '冻雨', 71: '小雪', 73: '中雪', 75: '大雪', 77: '雪粒',
          80: '阵雨', 81: '阵雨', 82: '强阵雨', 85: '阵雪', 86: '阵雪', 95: '雷暴',
          96: '雷暴伴冰雹', 99: '雷暴伴冰雹'}

# wttr.in 的 lang=zh 已失效(2026-11-08实测 lang_zh 字段回英文), 英文短语本地译表
EN_DESC_ZH = {
    'sunny': '晴', 'clear': '晴', 'partly cloudy': '局部多云', 'cloudy': '多云',
    'overcast': '阴', 'mist': '薄雾', 'fog': '雾', 'haze': '霾',
    'patchy rain nearby': '附近有零星降雨', 'patchy rain possible': '可能有零星降雨',
    'light rain': '小雨', 'moderate rain': '中雨', 'heavy rain': '大雨',
    'light drizzle': '小毛毛雨', 'patchy light drizzle': '零星毛毛雨',
    'thundery outbreaks possible': '可能有雷暴', 'thundery outbreaks nearby': '附近雷暴',
    'light sleet': '小雨夹雪', 'light snow': '小雪', 'moderate snow': '中雪',
    'heavy snow': '大雪', 'blizzard': '暴风雪', 'light snow showers': '小阵雪',
    'light showers of ice pellets': '小冰雹', 'moderate or heavy rain shower': '强阵雨',
    'light rain shower': '小阵雨', 'freezing drizzle': '冻毛毛雨',
    'patchy light rain': '零星小雨', 'moderate rain at times': '时大时小中雨',
    'heavy rain at times': '时大时小大雨', 'smoky haze': '烟霾',
    'partly cloudy day': '局部多云', 'partly cloudy night': '夜间局部多云',
}


def _desc_zh(desc):
    """天气短语→中文: WMO码表之上, 英文短语小写匹配译表, 兜底原文"""
    if not desc:
        return desc
    return EN_DESC_ZH.get(desc.strip().lower(), desc)


def fetch_weather_om(city):
    """备用源: Open-Meteo(免费无key, 地理编码+预报); 返回统一 j1 风格 dict"""
    geo_raw = _curl_retry('https://geocoding-api.open-meteo.com/v1/search?name=%s&count=1&language=zh'
                          % urllib.parse.quote(city), timeout=15, attempts=2)
    g = json.loads(geo_raw)
    hits = g.get('results') or []
    if not hits:
        raise ValueError('未知城市: %s' % city)
    lat, lon = hits[0]['latitude'], hits[0]['longitude']
    cname = hits[0].get('name') or city
    fc_raw = _curl_retry('https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s'
                         '&current=temperature_2m,apparent_temperature,relative_humidity_2m,'
                         'weather_code,wind_speed_10m,wind_direction_10m,precipitation'
                         '&daily=temperature_2m_max,temperature_2m_min,weather_code,'
                         'precipitation_probability_max&forecast_days=3&timezone=auto'
                         % (lat, lon), timeout=15, attempts=2)
    f = json.loads(fc_raw)
    cur, daily = f['current'], f['daily']
    code = int(cur.get('weather_code', 0) or 0)
    j1 = {'current_condition': [{
        'temp_C': str(round(cur['temperature_2m'])),
        'FeelsLikeC': str(round(cur['apparent_temperature'])),
        'humidity': str(round(cur['relative_humidity_2m'])),
        'winddir16Point': ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'][int(
            ((cur.get('wind_direction_10m', 0) or 0) % 360) // 45)],
        'windspeedKmph': str(round(cur.get('wind_speed_10m', 0) * 3.6)),
        'precipMM': str(cur.get('precipitation', 0)),
        'visibility': '-', 'lang_zh': [{'value': WMO_ZH.get(code, '未知')}],
        'weatherDesc': [{'value': WMO_ZH.get(code, '未知')}]}],
        'weather': []}
    for i in range(min(3, len(daily.get('time', [])))):
        dc = int(daily['weather_code'][i] or 0)
        j1['weather'].append({
            'mintempC': str(round(daily['temperature_2m_min'][i])),
            'maxtempC': str(round(daily['temperature_2m_max'][i])),
            'hourly': [{'chanceofrain': str(daily['precipitation_probability_max'][i] or 0),
                        'lang_zh': [{'value': WMO_ZH.get(dc, '未知')}]}]})
    j1['_city_resolved'] = cname
    return j1


def fetch_weather(city):
    """wttr.in 主源(3次退避重试) → Open-Meteo 备源; 都失败 raise"""
    raw = _curl_retry('https://wttr.in/%s?format=j1&lang=zh' % urllib.parse.quote(city), timeout=20)
    try:
        d = json.loads(raw)
        if 'current_condition' in d:
            return d
    except Exception:
        pass
    return fetch_weather_om(city)


def parse_weather(j1, city):
    """纯函数: j1 JSON → 中文播报文本"""
    cur = (j1.get('current_condition') or [{}])[0]
    desc_zh = ''
    for lang in (cur.get('lang_zh') or []):
        desc_zh = lang.get('value', '')
        break
    desc = _desc_zh(desc_zh or ((cur.get('weatherDesc') or [{}])[0].get('value', '')))
    lines = ['🌤 %s当前天气' % city]
    lines.append('· %s, 气温 %s°C(体感 %s°C), 湿度 %s%%' % (
        desc, cur.get('temp_C', '?'), cur.get('FeelsLikeC', '?'),
        cur.get('humidity', '?')))
    wind_dir = cur.get('winddir16Point') or cur.get('windDir16Point') or '?'
    lines.append('· 风 %s %skm/h, 降水 %smm, 能见度 %skm' % (
        wind_dir, cur.get('windspeedKmph', '?'),
        cur.get('precipMM', '?'), cur.get('visibility', '?')))
    days = j1.get('weather') or []
    names = ['今天', '明天', '后天']
    for i, day in enumerate(days[:3]):
        mm = day.get('hourly') or []
        pop = max([int(h.get('chanceofrain', 0) or 0) for h in mm] or [0])
        lines.append('· %s: %s ~ %s°C, %s, 降雨概率 %s%%' % (
            names[i] if i < len(names) else day.get('date', ''),
            day.get('mintempC', '?'), day.get('maxtempC', '?'),
            _desc_zh(((day.get('hourly') or [{}])[len(mm) // 2].get('lang_zh') or
                      (day.get('hourly') or [{}])[len(mm) // 2].get('weatherDesc') or [{}])[0].get('value', '')
                     if mm else ''),
            pop))
    return '\n'.join(lines)


def run_weather(instruction):
    city = extract_city(instruction)
    return parse_weather(fetch_weather(city), city)


# ================= search worker =================
def extract_query(instruction):
    """剥掉指令性前缀, 留搜索关键词"""
    q = re.sub(r'^(请|帮我|麻烦|帮忙)?(联网|上网)?(搜索|搜一下|搜一搜|搜搜|查一下|查查|查询|查下|查)'
               r'(一下|下|看看|看)?[:：,，\s]?', '', instruction.strip()).strip()
    q = re.sub(r'^(今天的|今日的|最新的|最新|最近)', '', q).strip() or instruction.strip()
    return q[:120]


def run_search(instruction, summarize=True):
    """unified-search(anysearch→exa→tavily 降级链) + LLM 摘要"""
    query = extract_query(instruction)
    if not query:
        raise ValueError('空搜索词')
    r = subprocess.run(['python', SEARCH_CLI, query], capture_output=True,
                       timeout=60, encoding='utf-8', errors='replace')
    out = (r.stdout or '') + (r.stderr or '')
    # 解析条目: "N. 标题 / URL / 摘要" 或多行块
    entries = []
    for m in re.finditer(r'^\s*\d+\.\s*(.+)$', out, re.M):
        entries.append(m.group(1).strip())
    if not entries:
        # 兜底: 非空就当纯文本结果
        body = out.strip()
        if not body or '== 由' not in out and len(body) < 20:
            raise ValueError('搜索无结果')
        return body[:1500]
    ctx = '\n'.join(entries[:8])[:2400]
    if summarize:
        try:
            import model_router
            ans = model_router.chat(
                '用3-6行中文口语总结以下搜索结果来回答问题「%s」。'
                '结论在前, 引用来源标题, 不确定就说不确定。'
                '%s【搜索结果】%s' % (query, chr(10), ctx), mt=450)
            if ans and ans.strip():
                return ans.strip()[:1200] + '\n\n(来源: 前%d条搜索结果)' % min(len(entries), 8)
        except Exception:
            pass
    return '\n'.join(entries[:5])[:1200]


# ================= note worker =================
def _fmt_notes(rows, header='📝'):
    if not rows:
        return header + ' 没有找到笔记。'
    lines = [header + ' 找到 %d 条:' % len(rows)]
    for n in rows:
        lines.append('· [%s] %s (%s)' % (n.get('note_id', '?'),
                                         ' '.join(str(n.get('content', '')).split())[:80],
                                         n.get('ts', '')))
    return '\n'.join(lines)


def run_note(instruction):
    """笔记增删改查(确定性正则路由, 零 LLM)"""
    import memory_engine as ME
    msg = instruction.strip()

    # --- 增 ---
    m = (re.match(r'^(?:请)?(?:帮我)?(?:新增|添加|加)(?:一条)?笔记[:：,，\s]*(.+)$', msg, re.S)
         or re.match(r'^(?:请)?(?:帮我)?记(?:住|一下|一下这个|个)[:：,，\s]*(.+)$', msg, re.S)
         or re.match(r'^新笔记[:：,，\s]*(.+)$', msg, re.S))
    if m:
        content = ' '.join(m.group(1).split())
        if not content:
            raise ValueError('笔记内容为空')
        nid = ME.add_note(content)
        return '📝 已记下 [%s]: %s' % (nid, content[:80])

    # --- 删 ---
    m = re.match(r'^(?:请)?(?:帮我)?(?:删除|删掉|删)(?:一条)?笔记[:：,，\s]*(\S+)', msg)
    if m:
        arg = m.group(1)
        if arg.startswith('N'):
            ok = ME.note_delete(arg)
            return ('🗑 已删除笔记 [%s]。' % arg) if ok else ('没找到笔记 [%s]。' % arg)
        rows = ME.search_keyword(arg).get('notes') or []
        if not rows:
            return '没找到含「%s」的笔记。' % arg
        hit = rows[0]
        ME.note_delete(hit['note_id'])
        return '🗑 已删除笔记 [%s]: %s(共匹配%d条, 只删了第一条; 用ID可精确删)' % (
            hit['note_id'], ' '.join(hit['snippet'].split())[:60], len(rows))

    # --- 改 ---
    m = re.match(r'^(?:请)?(?:修改|改|更新)笔记[:：,，\s]*(\S+)\s*(?:内容?为|改成|改为|为|:|：)\s*(.+)$', msg, re.S)
    if m:
        arg, content = m.group(1), ' '.join(m.group(2).split())
        if arg.startswith('N'):
            ok = ME.note_edit(arg, content)
            return ('✏️ 已更新 [%s]: %s' % (arg, content[:80])) if ok else ('没找到笔记 [%s]。' % arg)
        rows = ME.search_keyword(arg).get('notes') or []
        if not rows:
            return '没找到含「%s」的笔记。' % arg
        hit = rows[0]
        ME.note_edit(hit['note_id'], content)
        return '✏️ 已更新 [%s] 为: %s' % (hit['note_id'], content[:80])

    # --- 查 ---
    m = re.match(r'^(?:请)?(?:帮我)?(?:查(?:一下)?|查询|找|搜索|看看)(?:一下)?笔记[:：,，\s]*(.*)$', msg)
    if m:
        kw = m.group(1).strip()
        if kw:
            rows = ME.search_keyword(kw).get('notes') or []
            return _fmt_notes([{'note_id': r['note_id'], 'content': r['snippet'], 'ts': r.get('ts', '')}
                               for r in rows[:10]], '🔍')
        return _fmt_notes(ME.list_notes(10))
    if re.match(r'^(我的|全部)?笔记(列表)?$', msg):
        return _fmt_notes(ME.list_notes(10))

    # 宽松兜底: 消息含「笔记」且明确是查询语气 → 关键词查询(否则视为未识别)
    if '笔记' in msg and len(msg) <= 40 and re.search(r'查|找|哪些|什么|看看|吗|？|\?', msg):
        kw = re.sub(r'^(?:我的|有哪些|有什么|查|看看)|笔记|吗|\?|？', '', msg).strip()
        if kw:
            rows = ME.search_keyword(kw).get('notes') or []
            return _fmt_notes([{'note_id': r['note_id'], 'content': r['snippet'], 'ts': ''}
                               for r in rows[:10]], '🔍')
        return _fmt_notes(ME.list_notes(10))

    raise ValueError('笔记意图未识别(支持: 记住X / 添加笔记X / 查笔记[词] / 删除笔记ID或词 / 改笔记ID为X)')
