# -*- coding: utf-8 -*-
"""Router 大脑 — 管家形态的意图消化层

用户每句话先到这里:
  A 对话/问答(含进度问答) → Router 直接回答(带用户记忆+任务状态快照), 不派 worker
  B 任务(默认)           → 1 个 worker, 指令=原话+验收要求
  C 确有必要才拆分的复杂任务 → ≤5 个子任务, 每个 worker 一条定制指令
全部走免费模型链(硅基→flint→魔搭), 任何失败静默降级为"按单任务派发"。
进度问答有确定性快速路径(不花LLM), LLM 只处理指代模糊的情况。
"""
import json
import re
import time

MAX_SUBTASKS = 5
GUI_KW = ('打开', '关闭', '点击', '截屏', '窗口', '界面', '设置里', '浏览器')
NL_LIT = chr(10)


def _llm(prompt, mt=300, tier='fast'):
    try:
        import model_router
        return model_router.chat(prompt, mt=mt, tier=tier)[0]
    except Exception:
        return None


def _json_obj(text):
    if not text:
        return None
    m = re.search(r'\{.*\}', text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _tasks_brief(tasks_snapshot):
    """任务状态快照(给 LLM 的紧凑文本)"""
    lines = []
    for t in tasks_snapshot[:10]:
        lines.append('%s | %s | %s | %s' % (
            t.get('id', ''), t.get('status', ''),
            ' '.join((t.get('question') or '').split())[:40],
            (t.get('summary') or t.get('result') or '')[:40]))
    return '\n'.join(lines) or '(当前无任务)'


WORKERS = ('codex', 'opencode', 'ufo', 'search', 'weather', 'note', 'reme',
           'review', 'mail', 'files', 'research')
WORKER_NOTE = {
    'search': '已派 🔍 搜索 worker 执行(轻量快速)',
    'weather': '已派 🌤 天气 worker 执行(轻量快速)',
    'note': '已派 📝 笔记 worker 执行(轻量快速)',
    'ufo': '已派 GUI worker 执行',
    'codex': '已派 1 个 worker 执行(私有任务)',
    'opencode': '已派 🔓 opencode worker 执行(公共任务, 免费LLM)',
    'reme': '已派 🧠 记忆 worker 执行(数字分身)',
    'review': '已派 🔄 复盘 worker 执行(提炼经验入知识库)',
    'mail': '已派 📬 邮件 worker 执行(agent.qq.com信箱)',
    'files': '已派 📁 文件 worker 执行(云盘/本地文件)',
    'research': '已派 🔬 调研 worker 执行(标准三件套交付)',
}


LAB_KEYWORDS = re.compile(r'研究|立项|夜间|报告生成|trending|热门项目|深度研究')
RESEARCH_KEYWORDS = re.compile(r'调研|专题报告|对比分析|生态分析|深度报告')


RECALL_RE = re.compile(r'记得|还记得|问过|聊过|说过|提到过')   # 追问旧事≠新调研


def _is_lab_task(msg):
    """lab 遥控: 研发向目标(造东西/夜间工厂)转 Nightly AI Lab;
    调研/报告类转 research 工人(标准三件套, 即时交付)"""
    if RECALL_RE.search(msg):
        return False
    if re.search(r'lab|实验室', msg, re.I):
        return True
    if RESEARCH_KEYWORDS.search(msg):
        return False
    return bool(LAB_KEYWORDS.search(msg)) and len(msg) <= 200


def _is_research_task(msg):
    """调研工人: 出报告的深度调研(不是造东西; 追问旧事不算)"""
    if RECALL_RE.search(msg):
        return False
    return bool(RESEARCH_KEYWORDS.search(msg)) and len(msg) <= 200


def _infer_worker(msg):
    """确定性工人推断(快速路径与 LLM 兜底共用):
    GUI > 天气 > 复盘 > 邮件 > 文件 > 笔记 > 调研 > 搜索 > codex"""
    if any(k in msg for k in GUI_KW):
        return 'ufo'
    if (len(msg) <= 60 and re.search(r'天气|气温|温度|降雨|下雨|带伞|湿度|热不热|冷不热|穿什么', msg)
            and not re.search(r'写|代码|应用|软件|脚本|报告|分析|页面|查询系统|笔记', msg)
            and not re.search(r'进度|怎么样了|做得如何|做完了吗|完成了吗|状态如何', msg)):
        return 'weather'
    if len(msg) <= 30 and re.search(r'复盘|回顾一下|总结一下?(今天|今日|昨天|前天)', msg):
        return 'review'
    if len(msg) <= 30 and re.search(r'周报|这[一周]我做了什么|本周总结|上周总结', msg):
        return 'weekly'
    if (len(msg) <= 40 and re.search(r'邮件|验证码|收件箱|inbox', msg)
            and not re.search(r'写|发送|发一?封|发邮件|给.{0,10}(写|发)|脚本|代码', msg)):
        return 'mail'
    if (len(msg) <= 60 and re.search(r'云盘|阿里云盘|网盘|找一下.{0,24}文件|找一下.{0,20}\.(zip|pdf|docx|xlsx|html|md|exe)|找(一下)?本地', msg)
            and not re.search(r'写|代码|脚本|报告|调研', msg)):
        return 'files'
    if re.match(r'^(请)?(帮我)?(新增|添加|加)(一条)?(记忆|笔记)|^(请)?(帮我)?记(住|一下|个)[:：]?|^新(笔记|记忆)|'
                r'^查(询)?(一下)?(记忆|笔记)|^删除(记忆|笔记)|^删掉(记忆|笔记)|^改(记忆|笔记)|^修改(记忆|笔记)|'
                r'^更新(记忆|笔记)|^记忆(里|中|查询)?', msg):
        if '记忆' in msg or '分身' in msg:
            return 'reme'
        return 'note'
    if _is_research_task(msg):
        return 'research'
    if re.match(r'^(请)?(帮我)?(联网|上网)?((搜索|搜一下|搜一搜|搜搜|查一下|查查|查下)(一下)?[:：,，\s]?|查询[:：,，\s])', msg) \
            and len(msg) <= 80:
        return 'search'
    # 公共/私有分流(用户指令): 公共任务走opencode免费LLM, 涉密/个人信息走codex;
    # opencode失败在worker侧自动降级codex
    PRIVATE_KW = ('密码', '账号', '账密', '个人', '涉密', '身份证', '银行', '支付',
                  '私密', '家里', '老婆', '家人', '内部', 'api-key', 'token', 'secret')
    if any(k in msg.lower() for k in PRIVATE_KW):
        return 'codex'
    return 'opencode'


def _task_result(msg, worker, note):
    return {'kind': 'task', 'reply': None,
            'subtasks': [{'brief': ' '.join(msg.split())[:10] or '任务',
                          'instruction': msg}],
            'worker': worker,
            'note': note or WORKER_NOTE.get(worker, WORKER_NOTE['codex'])}


def route(message, memory_ctx, tasks_snapshot, dialog_ctx=''):
    """→ {'kind': 'reply'|'task', 'reply': str|None,
          'subtasks': [{'brief','instruction'}] | None, 'note': str}"""
    msg = (message or '').strip()

    # ---- 快速路径-2: 明确的记忆写入指令(零LLM, 确保真正保存) ----
    m_mem = re.match(r'^(?:记住|写入记忆|纳入记忆|记一下|帮我记住|帮我记)[:：]?\s*(.+)', msg)
    if m_mem and len(msg) <= 200:
        fact = m_mem.group(1).strip()
        return {'kind': 'memory_write', 'fact': fact,
                'reply': '✓ 已记住: ' + fact[:60], 'subtasks': None, 'note': ''}

    # ---- 快速路径-2: 明确的记忆写入(零LLM, 确保真正保存到profile+notes) ----
    m_mem = re.match(r'^(?:记住|写入记忆|纳入记忆|记一下|帮我记住|帮我记|记着)[:：]?\s*(.+)', msg)
    if m_mem and len(msg) <= 200:
        fact = m_mem.group(1).strip()
        return {'kind': 'memory_write', 'fact': fact,
                'reply': '✓ 已记住: ' + fact[:60], 'subtasks': None, 'note': ''}

    # ---- 快速路径-3: 明确的记忆查询(我有没有关于X的记忆等) ----
    if re.search(r'(?:有没有|查一下|帮我查|告诉我).{0,6}关于.{1,20}(?:的)?记忆', msg):
        return {'kind': 'memory_query', 'query': msg, 'reply': '', 'subtasks': None, 'note': ''}

    # ---- 快速路径-1: 「任务：」前缀 = 强制派发(用户显式意图, 零误判) ----
    m_force = re.match(r'^任务[:：]\s*([\s\S]+)', msg)
    if m_force:
        instruction = m_force.group(1).strip()
        worker = _infer_worker(instruction)
        if worker == 'codex' and any(k in instruction for k in GUI_KW):
            worker = 'ufo'
        # L2 任务互转: 调研出报告 → research 工人; 研发造东西 → lab 夜间工厂
        if _is_research_task(instruction):
            worker = 'research'
        elif _is_lab_task(instruction):
            worker = 'lab'
        return {'kind': 'task', 'reply': None,
                'subtasks': [{'brief': ' '.join(instruction.split())[:10] or '任务',
                              'instruction': instruction}],
                'worker': worker,
                'note': '%s(显式任务)' % WORKER_NOTE.get(worker, WORKER_NOTE['codex'])}


    if (re.match(r'^(继续|接着|再说|然后呢|继续刚才)', msg) and len(msg) <= 40
            and not re.search(r'怎么样|进度|状态|吗|么|\?', msg)):
        tid = continue_target(tasks_snapshot)
        if tid:
            return {'kind': 'continue', 'reply': None, 'subtasks': None, 'note': '', 'target': tid}

    # ---- 快速路径2: 明确闲聊/能力询问(零LLM) ----
    if re.match(r'^(你好|您好|hi|hello|在吗|你是谁|你叫什么|你能做什么|你能干什么|你会什么|怎么用|如何使用)', msg, re.I) and len(msg) <= 40:
        reply = ('你好! 我是你的AI管家(Router)。直接吩咐事情, 我消化后派 codex worker 执行; '
                 '简单问题直接答; 复杂任务拆给最多5个worker并行, 完成后给总结。')
        return {'kind': 'reply', 'reply': reply, 'subtasks': None, 'note': ''}

    # ---- 快速路径2b: 祈使指令句(请回复/请写/请执行…) → 必是任务, 不经LLM ----
    if (len(msg) <= 30 and any(k in msg for k in GUI_KW)
            and not re.search(r'什么|哪些|区别|为什么|为何|吗|\?', msg)):
        return _task_result(msg, 'ufo', '已派 GUI worker 执行')
    if re.match(r'^(请|帮我|麻烦|帮忙)?(只)?(回复|输出|执行|运行|列出|写|生成|创建|计算|算出|下载|打开|关闭|安装|删除|整理|转换|统计|点击|截屏|设置)',
                msg) and len(msg) <= 200:
        return _task_result(msg, _infer_worker(msg), '')

    # ---- 快速路径2b+: 调研(出报告) → research 工人; 研发向 → lab 夜间工厂 ----
    if _is_research_task(msg):
        return _task_result(msg, 'research', '已转调研工人(标准三件套交付)')
    if _is_lab_task(msg):
        return _task_result(msg, 'lab', '已转交 Nightly AI Lab 执行')

    # ---- 快速路径: 明确的进度/状态问答, 规则直接答(零LLM延迟) ----
    if re.search(r'进度|怎么样了|做得如何|做完了吗|完成了吗|状态如何', msg) and len(msg) <= 40:
        ans = progress_answer(msg, tasks_snapshot)
        if ans:
            return {'kind': 'reply', 'reply': ans, 'subtasks': None, 'note': ''}

    # ---- 快速路径2b-routine: 定时例程 创建/列表(零LLM) ----
    if re.match(r'^(每天|每日|每周[一二三四五六日天]|每\d{1,3}分钟|每\d{1,2}小时)[\s]*\d{0,2}', msg) and len(msg) <= 200:
        return {'kind': 'routine_create', 'reply': None, 'subtasks': None, 'note': ''}
    if re.match(r'^(定时|例程|定时任务)(列表)?$', msg) and len(msg) <= 12:
        return {'kind': 'routine_list', 'reply': None, 'subtasks': None, 'note': ''}

    # ---- 快速路径2c: 笔记/天气/搜索 确定性识别 → 直接派轻工人(零LLM, 秒级) ----
    # 顺序: 笔记在前(『查笔记 天气』是笔记查询不是天气); 天气排除笔记字样
    # 新三工人(复盘/邮件/文件)在最前, 特定词零碰撞
    if len(msg) <= 30 and re.search(r'复盘|回顾一下|总结一下?(今天|今日|昨天|前天)', msg):
        return _task_result(msg, 'review', '')
    if len(msg) <= 30 and re.search(r'周报|这[一周]我做了什么|本周总结|上周总结', msg):
        return _task_result(msg, 'weekly', '')
    if (len(msg) <= 40 and re.search(r'邮件|验证码|收件箱|inbox', msg)
            and not re.search(r'写|发送|发一?封|发邮件|给.{0,10}(写|发)|脚本|代码', msg)):
        return _task_result(msg, 'mail', '')
    if (len(msg) <= 60 and re.search(r'云盘|阿里云盘|网盘|找一下.{0,24}文件|找一下.{0,20}\.(zip|pdf|docx|xlsx|html|md|exe)|找(一下)?本地', msg)
            and not re.search(r'写|代码|脚本|报告|调研', msg)):
        return _task_result(msg, 'files', '')
    if (re.search(r'记忆|数字分身', msg) and len(msg) <= 60 and
            re.search(r'记住|添加|新增|查|删除|删掉|修改|更新|找|有什么|有哪些|里|看', msg)):
        return _task_result(msg, 'reme', '')
    if re.match(r'^(请)?(帮我)?记(住|一下|个)', msg):
        return _task_result(msg, 'reme', '')
    if re.match(r'^(请)?(帮我)?(新增|添加|加)(一条)?笔记|^(请)?(帮我)?记[:：]|^新笔记|'
                r'^查(询)?(一下)?笔记|^删除笔记|^删掉笔记|^改笔记|^修改笔记|^更新笔记|^(我的|全部)笔记(列表)?$', msg):
        return _task_result(msg, 'note', '')
    if len(msg) <= 60 and re.search(r'天气|气温|温度|降雨|下雨|带伞|湿度', msg) \
            and not re.search(r'写|代码|应用|软件|脚本|报告|分析|笔记', msg) \
            and not re.search(r'进度|怎么样了|做得如何|做完了吗|完成了吗|状态如何', msg):
        return _task_result(msg, 'weather', '')
    if re.match(r'^(请)?(帮我)?(联网|上网)?((搜索|搜一下|搜一搜|搜搜|查一下|查查|查下)(一下)?[:：,，\s]?|查询[:：,，\s])', msg) \
            and len(msg) <= 80 and '天气' not in msg and '笔记' not in msg:
        return _task_result(msg, 'search', '')

    # ---- 快速路径1a: 提醒请求 → LLM 抽取绝对时间 ----
    if re.search(r'提醒我|别忘|记得提醒|闹钟', msg) and len(msg) <= 80:
        now_str = time.strftime('%Y-%m-%d %H:%M')
        d = _json_obj(_llm(
            '从消息中提取一次性提醒。只输出严格JSON: '
            '{"datetime": "YYYY-MM-DD HH:MM", "text": "提醒内容≤50字"}。'
            '相对时间(明天/今晚/下午3点等)按当前时间换算。无法确定时间时输出 {}'
            + NL_LIT + '【当前时间】' + now_str + NL_LIT + '【消息】' + msg[:200],
            mt=200)) or {}
        when = str(d.get('datetime') or '')
        if re.match(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$', when):
            return {'kind': 'remind', 'when': when,
                    'text': str(d.get('text') or msg)[:100],
                    'reply': '⏰ 已设提醒: %s (%s)。到点我会叫你。'
                             % (str(d.get('text') or msg)[:60], when),
                    'subtasks': None, 'note': ''}

    # ---- 快速路径1b: 明显的问答句式(知识/比较/解释, 无动手动词) → Router LLM直答 ----
    if (re.search(r'什么|哪些|区别|为什么|为何|介绍|解释|是什么|适合|优缺点|对比', msg)
            and len(msg) <= 60
            and not re.search(r'帮我|请帮|写一个|写个|生成|创建|下载|运行|执行|打开|安装|'
                              r'搜索|查一下|查查|拆成|并行|worker|codex|派|发到|上传|删除', msg)):
        reply = chat_reply(msg, memory_ctx, tasks_snapshot, dialog_ctx)
        if reply:
            return {'kind': 'reply', 'reply': reply[:800], 'subtasks': None, 'note': ''}

    # ---- LLM 消化 ----
    prompt = ('你是用户的AI管家(Router)。判断用户消息: '
              'A=纯对话(闲聊/问进度/问记忆/概念解释/知识问答/比较分析等'
              '用你自身知识就能回答, 且不含让系统执行动作的指令) '
              'B=任何指令性请求(请回复X/请写/请生成/请计算/请查/帮我做X/'
              '整理X/翻译X/测试X等——凡要求对具体内容做处理或产出的都是B, '
              '即使答案你也能直接编, 也必须判B派worker执行。需要联网查实时信息的(如今天的新闻/天气/股价)也判B) '
              '例: "整理这段材料:..."=B, "特殊字符测试...请只回复OK"=B, "Python是什么"=A '
              'B=必须真实执行动作的(联网查实时信息/写改运行代码文件/操作软件/下载等), '
              '或用户点名要worker/codex执行 '
              'C=拆分并行的复杂任务(默认保守不拆; 但用户明确要求并行/拆分/多个worker时必须遵从, 子任务需相互独立, 最多%d个)\n'
              '只输出严格JSON:\n'
              '{"kind": "A|B|C", '
              '"worker": "codex|ufo|search|weather|note|review|mail|files|research(B/C时必填: ufo=必须真实操作Windows图形界面'
              '(打开关闭应用/点击界面/截图看屏幕/系统设置/浏览器页面操作); '
              'search=联网搜索新闻资料等轻量查询; weather=查天气; note=笔记增删改查; '
              'review=复盘今日任务提炼经验; mail=查收邮件/验证码(agent.qq.com信箱); '
              'files=云盘上传下载/列表/本地找文件; research=深度调研出正式报告; '
              'codex=写代码写文件/复杂分析/下载处理等重活)", '
              '"reply": "A类: 结合记忆与任务状态直接回答用户(口语化); B/C类留空", '
              '"subtasks": [C类才填: {"brief": "子任务≤10字", '
              '"instruction": "给该worker的完整定制指令(包含必要的用户背景与明确验收标准)"}], '
              '"note": "B/C类给用户的一句反馈, 如: 已派1个worker执行 / 已拆成3个子任务并行"}\n'
              '【最近对话(用户问"我问过什么/聊到哪"时以此为准, 引用要准确)】\n%s\n'
              '【用户记忆】\n%s\n【当前任务状态】\n%s\n【用户消息】%s' % (
                  MAX_SUBTASKS, dialog_ctx[:1000], memory_ctx[:600],
                  _tasks_brief(tasks_snapshot), msg[:400]))
    d = _json_obj(_llm(prompt, mt=400)) or {}
    kind = d.get('kind')
    if kind not in ('A', 'B', 'C'):
        kind = 'B'                    # LLM 输出非法种类 → 按单任务兜底
    subs = d.get('subtasks') or []
    subs = [s for s in subs if isinstance(s, dict) and s.get('instruction')][:MAX_SUBTASKS]

    if kind == 'A':
        reply = str(d.get('reply') or '').strip()
        if reply:
            return {'kind': 'reply', 'reply': reply[:800], 'subtasks': None, 'note': ''}
        # LLM 说是对话但没给出回答 → 兜底进度/记忆作答
        ans = progress_answer(msg, tasks_snapshot)
        return {'kind': 'reply',
                'reply': ans or ('我在的。当前记忆里有你的记录, 任务执行情况可发「任务列表」查看。'),
                'subtasks': None, 'note': ''}

    worker = d.get('worker')
    if worker not in WORKERS:
        worker = _infer_worker(msg)
    # 写向/处理意图防误派: mail/files/review 是只读工人, 写/发/建/分析操作归 codex
    # (实测: LLM把"帮我发一封邮件给张三"派给mail; 把"文件总结"派给files)
    if worker in ('mail', 'files', 'review') and re.search(
            r'写|生成|创建|发送|发一?封|发邮件|代码|脚本|总结|读取|分析|解读', msg):
        worker = 'codex'

    if kind == 'C' and len(subs) >= 2:
        return {'kind': 'task', 'reply': None, 'subtasks': subs,
                'worker': worker,
                'note': str(d.get('note') or ('已拆成 %d 个子任务并行' % len(subs)))}

    # B 或解析失败 → 单任务; 含指代词时带对话改写成自足指令(worker无对话史, 不展开=瞎跑)
    note = str(d.get('note') or '') if kind == 'B' else ''
    instruction = msg
    if (dialog_ctx and len(msg) <= 120
            and re.search(r'那个|这个|刚才|上面|上次|之前|它|继续弄|照旧|老样子', msg)):
        try:
            rw = _llm('结合最近对话, 把用户消息改写成一条自足的任务指令: 展开所有指代(那个/刚才/它), '
                      '补全具体对象与文件名; 只输出改写后的指令正文, 不要解释不要客套。'
                      '【最近对话】' + dialog_ctx[:800] + NL_LIT + '【用户消息】' + msg, mt=200)
            rw = ' '.join((rw or '').split())
            if rw and len(rw) > len(msg) and not rw.startswith(('好的', '明白')):
                instruction = rw[:300]
        except Exception:
            pass
    return {'kind': 'task', 'reply': None,
            'subtasks': [{'brief': ' '.join(instruction.split())[:10] or '任务',
                          'instruction': instruction}],
            'worker': worker,
            'note': note or WORKER_NOTE.get(worker, WORKER_NOTE['codex'])}


def continue_target(tasks_snapshot):
    for t in tasks_snapshot:
        if t.get('session'):
            return t['id']
    return None

STOPWORDS_TQ = ('我', '刚才', '刚刚', '那个', '哪个', '问题', '任务', '结果', '怎么样', '怎样',
                '如何', '现在', '一下', '查询', '查查', '查一下', '进度', '状态', '完成',
                '做完', '了吗', '么样', '的', '了', '吗', '呢', '？', '?', '请问', '帮我',
                '之前', '上次', '派给', '问问', '看看')


def _task_kw(msg):
    """从进度问句提取内容关键词(去停用词+2-gram滑窗), 供语义匹配任务"""
    words = [w for w in re.split(r'[\s,，。？?!！]+', msg) if w]
    kws = []
    for w in words:
        w2 = w
        for sw in STOPWORDS_TQ:
            w2 = w2.replace(sw, '')
        w2 = w2.strip()
        if len(w2) >= 2:
            kws.append(w2)
        # 2-gram滑窗: 长粘连词拆出细粒度词(问惠州天气→问惠/惠州/州天/天气), 提高子串命中
        if len(w2) >= 4:
            kws += [w2[i:i + 2] for i in range(len(w2) - 1)]
    return [k for k in dict.fromkeys(kws) if len(k) >= 2]


def _find_task_by_semantics(msg, tasks_snapshot):
    """语义任务查找: 关键词对任务question打分(命中数×词长加权+时间新近加成)。
    返回 (target, score) 或 (None, 0)。小智/飞书口语查询的核心。"""
    kws = _task_kw(msg)
    if not kws:
        return None, 0
    best, best_s = None, 0.0
    for idx, t in enumerate(tasks_snapshot):
        q = ' '.join((t.get('question') or '').split())
        sm = (t.get('summary') or '') + ' ' + (t.get('raw_answer') or '')[:300]
        s = 0.0
        for kw in kws:
            if kw in q:
                s += 2.0 + min(len(kw), 6) * 0.5          # 问题命中权重高
            elif kw in sm:
                s += 1.0                                   # 结果命中权重低
        if s > 0:
            s += max(0, (10 - idx)) * 0.15                 # 新近加成(快照按新→旧)
        if s > best_s:
            best, best_s = t, s
    return (best, best_s) if best_s >= 2.0 else (None, 0)


def progress_answer(msg, tasks_snapshot):
    """进度问答快速路径: 编号精确 → 语义匹配 → 最近兜底 → 人话汇报(含结果摘要)"""
    import time as _t
    m = re.search(r'T\d{10,}', msg)
    target = None
    if m:
        for t in tasks_snapshot:
            if t.get('id') == m.group(0):
                target = t
                break
        if target is None:
            return '没有找到任务 %s。发「任务列表」看全部。' % m.group(0)
    else:
        # 语义优先: "我刚才问的那个XX现在怎么样了" → 按内容匹配, 不再盲取最近
        target, _s = _find_task_by_semantics(msg, tasks_snapshot)
        if target is None:
            cand = [t for t in tasks_snapshot
                    if t.get('status') in ('running', 'queued', 'done', 'failed')]
            target = cand[0] if cand else None
            if target is None:
                return '当前没有任何任务在跑, 也没有历史记录。'
    st = target.get('status', '')
    el = ''
    if st == 'running' and target.get('started'):
        try:
            import time as _tm
            sec = int(_tm.time() - _tm.mktime(_tm.strptime(
                target['started'], '%Y-%m-%d %H:%M:%S')))
            el = ', 已跑 %d 分 %d 秒' % (sec // 60, sec % 60) if sec >= 60 \
                else ', 已跑 %d 秒' % sec
        except Exception:
            pass
    head = '任务 %s(%s)当前状态: %s%s。' % (
        target.get('id', ''), ' '.join((target.get('question') or '').split())[:30],
        st, el)
    subs = target.get('subtasks') or []
    if len(subs) > 1:                     # 单子任务不显示子任务行(避免复读)
        sl = '; '.join('%s=%s' % (s.get('brief', '?'), s.get('status', '?'))
                       for s in subs)
        head += ' 子任务: %s。' % sl
    if st == 'done':
        body = (target.get('summary') or target.get('result') or '')[:400]
        if not body:
            for s in (target.get('subtasks') or []):
                if s.get('result'):
                    body = s['result'][:400]
                    break
        return head + '\n结果摘要: ' + (body or '(无结果内容)')
    if st == 'failed':
        return head + '\n失败原因: %s (错误码 %s)' % (
            (target.get('error') or '')[:150], target.get('error_code', ''))
    if st == 'running':
        trail = (target.get('progress') or [])[-2:]
        extra = ' | '.join(p.get('note', '') for p in trail) if trail else '正在执行'
        return head + '\n最新动态: ' + extra
    return head


def summarize_task(question, subtasks, memory_ctx=''):
    """全部子任务完结 → 总结性回复(结论先行, ≤8行); 失败返回 None"""
    try:
        import model_router
        parts = []
        for i, s in enumerate(subtasks, 1):
            parts.append('子任务%d(%s, %s): %s' % (
                i, s.get('brief', ''), s.get('status', ''),
                (s.get('result') or s.get('error') or '')[:600]))
        prompt = ('你是AI管家, 刚派出的worker们完成了任务。给用户一段简洁、重点突出的总结回复: '
                  '①第一行直接回答最初的诉求(一句话结论) ②然后用要点列出关键信息(每点一行, 用·开头, '
                  '最多5点) ③关键数字/文件路径/链接保留 ④如某子任务失败, 明确指出并给一句建议 '
                  '⑤总共不超过6行, 口语化不客套, 不复述任务原文。⑥不要用Markdown格式(加粗/表格/标题), 用纯文本短句——内容可能被语音TTS读出。\n⑦全程用简体中文回答, 专有名词/代码/文件名/单位可保留原文, 描述性词语必须中文。'
                  '用户最初要求: %s\n%s\n%s' % (
                      str(question)[:200],
                      ('【用户记忆】' + memory_ctx[:300]) if memory_ctx else '',
                      '\n'.join(parts))[:2400])
        t = _llm(prompt, mt=500, tier='strong')
        t = (t or '').strip()
        # LLM残缺输出保护(实测GLM-5.2思考吃token时正文只剩1-2字): 过短用原文兜底
        if len(t) < 40 and parts:
            first = ' '.join(((subtasks[0].get('result') or '')).split())
            if len(first) > len(t):
                return first[:400] + ('…(完整内容见任务详情)' if len(first) > 400 else '')
        return t[:2000] or None
    except Exception:
        return None


def chat_reply(message, memory_ctx, tasks_snapshot, dialog_ctx=''):
    """纯对话回复(意图A/问答直答): 带最近对话上下文, 引用类问题以对话为准
    强档模型(GLM-5.2免费) — 直答质量是用户主诉, 分类才用快档"""
    dlg = ('\n【最近对话(回答"我问过什么/聊过什么"类问题以此为准, 逐条准确引用)】\n'
           + dialog_ctx[:2400]) if dialog_ctx else ''
    t = _llm('你是用户的AI管家, 用口语化中文回答(≤6行)。结合最近对话/用户画像/记忆/任务状态; '
             '提到之前聊过的内容时要如实引用, 不要否认也不要编造。\n'
             '【用户记忆与画像】%s\n%s\n【任务状态】%s\n【用户消息】%s' % (
                 memory_ctx[:1200], dlg, _tasks_brief(tasks_snapshot),
                 str(message)[:400]), mt=300, tier='strong')
    return (t or '').strip()[:800]


def extract_profile(message, reply):
    """从一问一答中提炼用户画像事实; 无则 None"""
    t = _llm('从这轮对话中提炼一条值得长期记住的用户画像事实'
             '(身份/偏好/环境/项目背景, 如"用户主要用飞书沟通")。'
             '只输出严格JSON {"fact": "一句话"} 无则 {}'
             + NL_LIT + '用户: ' + str(message)[:200]
             + NL_LIT + '管家: ' + str(reply)[:300], mt=120)
    d = _json_obj(t) or {}
    f = str(d.get('fact') or '').strip()
    if not f or f in ('无', '{}'):
        return None
    return f[:160]


def profile_conflict(new_fact, facts):
    """画像冲突: 新事实是否为某条旧事实的最新状态 → 下标; 无/失败 None"""
    if not facts:
        return None
    lines = NL_LIT.join('%d. %s' % (i, f.get('text', '')[:60])
                        for i, f in enumerate(facts))
    d = _json_obj(_llm('新事实是否为某条旧画像事实的最新状态? '
                       '是则输出{"i":下标} 否则{}'
                       + NL_LIT + '新: %s' + NL_LIT + '旧:%s'
                       % (new_fact[:120], lines), mt=100)) or {}
    i = d.get('i')
    return i if isinstance(i, int) and 0 <= i < len(facts) else None

def suggest_followups(question, result):
    """D后续建议: 生成1-2个可接着说方向(fast档, 失败静默)"""
    try:
        t = _llm('任务"%s"已完成。基于结果给用户1-2个自然的后续追问方向(如追问细节/基于结果延伸/导出文件), '
                 '每条≤15字, 口语化, 只输出条目用;分隔, 不要解释。结果摘要: %s'
                 % (str(question)[:60], str(result)[:300]), mt=80)
        t = ' '.join((t or '').split())
        if t and len(t) < 80:
            return t[:80]
    except Exception:
        pass
    return ''
