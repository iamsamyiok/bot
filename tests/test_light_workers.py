# -*- coding: utf-8 -*-
"""轻工人离线测试: 路由矩阵 + weather解析纯函数 + query抽取 + 笔记真实DB回环
运行: python test_light_workers.py  (无网络依赖, 笔记用真库测试后清理)"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
total = [0]
fails = []


def check(name, ok):
    total[0] += 1
    print(('PASS ' if ok else 'FAIL ') + name)
    if not ok:
        fails.append(name)


# ===== 1) 路由矩阵(全部走快速路径, 零 LLM) =====
import router_brain as RB

def wk(msg):
    d = RB.route(msg, '', [])
    return d.get('worker'), d.get('kind')

cases = [
    ('惠州天气怎么样', 'weather'), ('今天天气如何', 'weather'),
    ('北京气温多少度', 'weather'), ('明天会下雨吗', 'weather'),
    ('查一下上海天气', 'weather'), ('出门要带伞吗', 'weather'),
    ('搜索 最新AI新闻', 'search'), ('搜索一下 Python GIL', 'search'),
    ('查一下 今天的头条', 'search'), ('帮我查查 quantum computing', 'search'),
    ('记住: 明天下午3点开会', 'memory_write'), ('添加笔记: 项目密码在保险柜', 'note'),
    ('查笔记 编程计划', 'note'), ('我的笔记', 'note'), ('删除笔记 N123', 'note'),
    ('改笔记 N123 为 新内容', 'note'), ('新笔记 测试', 'note'),
    ('查笔记 天气', 'note'), ('记住: 天气预报说明天有雨', 'memory_write'),
    ('打开天气应用', 'ufo'), ('帮我写一个天气脚本', 'opencode'),
    ('任务：查惠州天气', 'weather'), ('任务：搜索AI新闻', 'search'),
    ('任务：记住我的 birthday 是 1月1日', 'note'), ('任务：写份周报', 'weekly'),
]
for msg, expect in cases:
    d = RB.route(msg, '', [])
    got = d.get('worker') or (d.get('kind') if d.get('kind') in ('memory_write', 'reply') else None)
    check('路由[%s]→%s' % (msg[:16], expect), got == expect and d.get('kind') in ('task', 'memory_write', 'reply'))

check('_infer_worker兜底: 天气', RB._infer_worker('惠州天气热不热') == 'weather')
check('_infer_worker兜底: codex', RB._infer_worker('分析我的银行账单数据') == 'codex')
check('_infer_worker兜底: opencode(公共)', RB._infer_worker('分析这份Excel数据') == 'opencode')
check('worker枚举完整', set(RB.WORKERS) == {'codex', 'opencode', 'ufo', 'search', 'weather', 'note', 'reme', 'review', 'mail', 'files', 'research'})

# ===== 2) weather 纯函数 =====
import light_workers as LW

FIX = {'current_condition': [{
    'temp_C': '28', 'FeelsLikeC': '31', 'humidity': '78',
    'windDir16Point': 'SE', 'windspeedKmph': '12', 'precipMM': '0.1',
    'visibility': '10', 'weatherDesc': [{'value': 'Partly cloudy'}],
    'lang_zh': [{'value': '局部多云'}]}],
    'weather': [
        {'mintempC': '24', 'maxtempC': '31', 'hourly': [
            {'chanceofrain': '60', 'lang_zh': [{'value': '多云'}]}]},
        {'mintempC': '23', 'maxtempC': '30', 'hourly': [{'chanceofrain': '10'}]},
        {'mintempC': '22', 'maxtempC': '29', 'hourly': [{'chanceofrain': '0'}]}]}
txt = LW.parse_weather(FIX, '惠州')
check('weather解析: 城市名', '惠州' in txt)
check('weather解析: 当前温度', '28' in txt and '体感 31' in txt)
check('weather解析: 三天预报', '今天' in txt and '明天' in txt and '后天' in txt)
check('weather解析: 降雨概率', '60%' in txt)

check('城市抽取: 惠州天气', LW.extract_city('惠州天气怎么样') == '惠州')
check('城市抽取: 查一下北京的天气', LW.extract_city('查一下北京的天气') == '北京')
check('城市抽取: 默认', LW.extract_city('今天天气如何') == '惠州')
check('城市抽取: 去东京玩', LW.extract_city('去东京玩天气如何') == '东京')

check('城市抽取: 用户原话(搜索下惠州今日的天气)', LW.extract_city('搜索下惠州今日的天气') == '惠州')
check('城市抽取: 帮我查下北京今天天气', LW.extract_city('帮我查下北京今天天气') == '北京')

check('城市抽取: 英文Tokyo', LW.extract_city('Tokyo weather today') == 'Tokyo')
check('城市抽取: 异常名透传', LW.extract_city('查一下 xyzzznotacity 的天气') == 'xyzzznotacity')
check('城市抽取: weather in Paris', LW.extract_city('weather in Paris now') == 'Paris')

# ===== 3) search query 抽取 =====
check('query: 搜索 X', LW.extract_query('搜索 最新AI新闻') == 'AI新闻')
check('query: 帮我查一下X', LW.extract_query('帮我查一下 Python GIL') == 'Python GIL')
check('query: 无前缀', LW.extract_query('quantum computing latest') == 'quantum computing latest')

# ===== 4) note 真实 DB 回环(带唯一标记, 结束清理) =====
MARK = '__lw_t%d__' % (int(__import__('time').time()) % 100000)
import memory_engine as ME

nid = ME.add_note(MARK + ' 原始内容A')
check('note增: 返回ID', bool(nid) and nid.startswith('N'))
found = ME.search_keyword(MARK).get('notes') or []
check('note查: 关键词命中', any(r['note_id'] == nid for r in found))

# G1统一后: note 查询走 ReMe(向量+BM25), notes_fts 旧条目需同步写入 ReMe 才能被查到
_t_sleep = __import__('time')
_t_sleep.sleep(7)  # ReMe watcher 索引延迟约5s(debounce), 写入后需等索引
r = LW.run('note', '查笔记 %s' % MARK)
check('note工人: 查询命中', nid in r and '原始内容A' in r)
r = LW.run('note', '我的笔记')
check('note工人: 列表含新笔记', nid in r)

ok = ME.note_edit(nid, MARK + ' 改后的内容B')
check('note改: 直接ID', ok and MARK in (ME.search_keyword(MARK).get('notes') or [{'snippet': ''}])[0].get('snippet', ''))

r = LW.run('note', '改笔记 %s 为 %s 最终内容C' % (nid, MARK))
check('note工人: 改', '最终内容C' in r)

r = LW.run('note', '删除笔记 %s' % nid)
check('note工人: 删ByID', '已删除' in r and ME.note_delete(nid) is False)  # 已删, 再删应False

nid2 = ME.add_note(MARK + ' 关键词删除测试D')
r = LW.run('note', '删除笔记 %s' % MARK)
check('note工人: 按关键词删', '已删除' in r)
ME.note_delete(nid2)  # 清理
left = ME.search_keyword(MARK).get('notes') or []
check('note清理: 库无残留', all(MARK not in (n.get('snippet') or '') for n in left))

# 工人边界
try:
    LW.run('note', '这句话和笔记毫无关系的普通任务描述')
    check('note边界: 未识别应raise', False)
except ValueError:
    check('note边界: 未识别应raise', True)

# ===== 5) review 复盘工人(纯函数 + 假ledger回环) =====
check('review日期: 默认今天', LW.parse_review_day('复盘一下') == __import__('datetime').date.today().strftime('%Y-%m-%d'))
check('review日期: 昨天', LW.parse_review_day('复盘昨天') ==
      (__import__('datetime').date.today() - __import__('datetime').timedelta(days=1)).strftime('%Y-%m-%d'))
check('review日期: 显式日期', LW.parse_review_day('复盘 2026-11-01') == '2026-11-01')

import tempfile
tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False, encoding='utf-8')
_day = __import__('datetime').date.today().strftime('%Y-%m-%d')
for r in [
    {'id': 'T1', 'status': 'done', 'question': '查天气', 'ended': _day + ' 10:00:00'},
    {'id': 'T1', 'status': 'running', 'question': '查天气', 'ended': ''},   # 旧状态应被最新覆盖
    {'id': 'T2', 'status': 'failed', 'question': '坏任务', 'ended': _day + ' 11:00:00'},
    {'id': 'T3', 'status': 'done', 'question': '昨天任务', 'ended': '2020-01-01 09:00:00'},
]:
    tmp.write(json.dumps(r, ensure_ascii=False) + chr(10))
tmp.close()
got = LW.scan_day_tasks(_day, ledger_path=tmp.name)
_q2s = dict((q, s) for s, q in got)
check('review扫描: 2项且状态新', len(got) == 2 and _q2s.get('查天气') == 'done'
      and _q2s.get('坏任务') == 'failed')
check('review扫描: 跨日隔离', LW.scan_day_tasks('2020-01-01', ledger_path=tmp.name)[0][1] == '昨天任务')
os.unlink(tmp.name)

# ===== 6) mail 邮件工人(纯函数) =====
check('mail时间: UTC+8', LW._fmt_mailtime('2026-11-07T02:30:11Z') == '11-07 10:30')
msgs = [
    {'subject': '您的验证码是482913', 'snippet': 'code: 482913 5分钟内有效',
     'from': {'email': 'n@example.com'}, 'is_read': False, 'created_at': '2026-11-07T02:00:00Z'},
    {'subject': '周报', 'snippet': '本周进展', 'from': {'email': 'b@x.com'},
     'is_read': True, 'created_at': '2026-11-06T09:00:00Z'},
]
codes = LW.extract_codes(msgs)
check('mail验证码: 提取', codes and codes[0][0] == '482913')
check('mail验证码: 无码不误报', not LW.extract_codes([msgs[1]]))

# ===== 7) files 文件工人(意图解析纯函数) =====
check('files意图: 上传到目录', LW.files_intent('把日程管理Pro.html传云盘tools') == ('upload', '日程管理Pro.html', 'tools'))
check('files意图: 上传默认目录', LW.files_intent('上传 报告.md 到云盘') == ('upload', '报告.md', None))
check('files意图: 列表', LW.files_intent('云盘里有什么')[0] == 'ls')
check('files意图: 下载到桌面', LW.files_intent('下载云盘的data.zip到桌面')[0] == 'download'
      and LW.files_intent('下载云盘的data.zip到桌面')[1] == 'data.zip')
check('files意图: 本地找', LW.files_intent('找一下 那个部署文档')[0] == 'find')
check('files意图: 无关不误判', LW.files_intent('帮我写个云盘同步脚本')[0] is None)

# ===== 8) 路由矩阵: 四新工人 =====
for msg, expect in [
    ('复盘一下今天', 'review'), ('查邮件', 'mail'), ('有没有新验证码', 'mail'),
    ('把日程管理Pro.html传云盘', 'files'), ('云盘里有什么', 'files'),
    ('任务：调研一下数字分身开源项目', 'research'), ('任务：研究agentscope写个demo', 'lab'),
    ('帮我发一封邮件给张三', 'codex'),
]:
    d = RB.route(msg, '', [])
    got = d.get('worker') or d.get('kind')
    check('路由[%s]→%s' % (msg[:16], expect), got == expect)

check('worker枚举完整', set(RB.WORKERS) == {'codex', 'opencode', 'ufo', 'search', 'weather', 'note', 'reme',
                                            'review', 'mail', 'files', 'research'})

print('\n===== 轻工人离线测试: %d 项, 失败 %d =====' % (total[0], len(fails)))
sys.exit(1 if fails else 0)
