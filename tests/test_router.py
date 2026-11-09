# -*- coding: utf-8 -*-
"""离线单元测试 — 不联网/不派真worker(全部打桩), 秒级完成
用法: python test_router.py   (CI/改动后回归)

覆盖: 混合记忆引擎(写入/检索/中文子串) · Router大脑路由矩阵(LLM打桩)
· 任务生命周期(建/查/停) · 时间线回放(含cutoff/elapsed过滤) · 设置与触发器存取
"""
import json
import os
import sys
import tempfile
import time
import types

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 仓库根
sys.path.insert(0, BASE)

os.environ['AGENTIC_TEST'] = '1'

# ---- LLM 打桩(不联网): 意图/总结/直答全部可控 ----
import model_router
model_router.chat = lambda prompt, mt=300, system=None: (
    '{"kind": "B", "reply": "", "subtasks": [], "note": "stub"}', 'stub')

# ---- RAG 打桩(离线): 模拟服务不可达, 验证 rag_error 透出 ----
sys.modules['rag_client'] = types.SimpleNamespace(
    search=lambda q, top_k=3: (_ for _ in ()).throw(RuntimeError('offline-stub')))

import router_brain as brain
import codex_worker as W
import memory_engine

# 记忆库/账本 重定向到临时目录
tmp = tempfile.mkdtemp()
memory_engine.DB_PATH = os.path.join(tmp, 'mem.db')
memory_engine._conn = None
W.STATE = os.path.join(tmp, 'state')
W.LEDGER = os.path.join(W.STATE, 'ledger.jsonl')
W.LOGS = os.path.join(W.STATE, 'worker_logs')
W._tasks.clear()

fails = []
total = [0]
def check(name, cond):
    total[0] += 1
    print(('PASS  ' if cond else 'FAIL  ') + name)
    if not cond:
        fails.append(name)

# ===== 1) 混合记忆引擎 =====
memory_engine.index_task('T1', '查惠州天气', '惠州晴25度 来源中国天气网', 'web')
memory_engine.index_task('T2', '写快排函数', 'def quicksort(arr): ...', 'web')
memory_engine.add_note('用户服务器IP是10.0.0.2')
r = memory_engine.search_keyword('惠州')
check('记忆-中文子串检索命中', any(k['id'] == 'T1' for k in r['tasks']))
r = memory_engine.search_keyword('快排')
check('记忆-多词AND', any(k['id'] == 'T2' for k in r['tasks']))
r = memory_engine.search_keyword('服务器IP')
check('记忆-手动条目命中', len(r['notes']) >= 1)
r = memory_engine.search_hybrid('不存在词xyzq')
check('记忆-空结果不抛异常', isinstance(r, dict))
check('记忆-rag缺席时错误透出', 'rag_error' in r)

# ===== 2) Router 大脑路由矩阵(LLM打桩=B) =====
d = brain.route('请只回复两个字: OK', '', [])
check('路由-祈使句快速路径=task', d['kind'] == 'task')
d = brain.route('你是谁', '', [])
check('路由-闲聊快速路径=reply', d['kind'] == 'reply')
d = brain.route('继续刚才的', '', [{'id': 'T9', 'session': True}])
check('路由-续聊快速路径=continue', d['kind'] == 'continue')
d = brain.route('继续刚才的', '', [])     # 无会话→降级
check('路由-无会话续聊降级', d['kind'] in ('reply', 'task'))
snap = [{'id': 'T1', 'status': 'done', 'question': '查惠州天气',
         'started': time.strftime('%Y-%m-%d %H:%M:%S'), 'subtasks': [], 'progress': []}]
d = brain.route('刚才那个任务做的怎么样了', '', snap)
check('路由-进度问答=reply带状态', d['kind'] == 'reply' and 'done' in d.get('reply', ''))

# ===== 3) 任务生命周期(不启动worker线程, 只测簿记) =====
tid = W.new_task('离线测试任务', source='web')
t = W.get_task(tid)
check('任务-创建含唯一ID与queued', t and t['status'] == 'queued'
      and t['id'].startswith('T'))
check('任务-子任务默认1个', len(t['subtasks']) == 1)
tid2 = W.new_task('两步任务', plan=[{'brief': 'a', 'instruction': 'a'},
                                    {'brief': 'b', 'instruction': 'b'}])
t2 = W.get_task(tid2)
check('任务-编排2子任务', len(t2['subtasks']) == 2)
ok, msg = W.stop_task(tid2)
check('任务-停止排队任务', ok and W.get_task(tid2)['status'] == 'stopped')
check('任务-retry重建', W.retry_task(tid) is not None)
j = W.journal(100)
kinds = [i.get('type', i.get('kind')) for i in j]
check('时间线-任务入账本', 'task' in kinds)

# ===== 4) cutoff 过滤 + elapsed 透传 =====
W._append({'type': 'user', 'text': '旧消息', 'ts': '2000-01-01 00:00:00'})
W._append({'type': 'router', 'text': '新回复', 'ts': '2099-01-01 00:00:00',
           'elapsed': 0.5})
j = W.journal(100, cutoff='2099-01-01 00:00:00')
check('时间线-cutoff过滤旧记录', all((i.get('ts') or i.get('created') or '')
                                     >= '2099-01-01' for i in j))
j = W.journal(100)
rl = [i for i in j if i.get('type') == 'router' and i.get('elapsed') is not None]
check('时间线-elapsed透传', len(rl) >= 1)

# ===== 5) tokens 解析 =====
os.makedirs(W.LOGS, exist_ok=True)
with open(os.path.join(W.LOGS, 'T1.log'), 'w', encoding='utf-8') as f:
    f.write('tokens used\n26,374\n')
check('tokens-日志解析', W._tokens_from_log(os.path.join(W.LOGS, 'T1.log')) == 26374)
check('tokens-缺文件安全', W._tokens_from_log(os.path.join(W.LOGS, 'nope.log')) == 0)

# ===== 6) 触发器存取(经 router_server 函数) =====
import router_server as RS
RS.SETF = os.path.join(tmp, 'settings.json')
RS.TRGF = os.path.join(tmp, 'triggers.json')
RS.set_set('x', 1)
check('设置-读写', RS.get_set('x') == 1)
RS.save_triggers([{'id': 'G1', 'time': '08:30', 'text': '播报', 'last': ''}])
check('触发器-存取', RS.load_triggers()[0]['time'] == '08:30')


# ===== 7) 审查修复回归 =====
# 7a journal 增量缓存: 首读→追加→再读, 新行可见
W._JC.update(size=0, buf='', items=[], pos={})
j1 = W.journal(100)
n1 = len(j1)
W._append({'type': 'user', 'text': '增量消息', 'ts': '2099-06-01 12:00:00'})
j2 = W.journal(100)
check('审查-journal增量读到新行', len(j2) == n1 + 1 and j2[-1]['text'] == '增量消息')
# 7b 快照就地刷新不重复
W._append(dict(W.get_task(tid)))
j3 = W.journal(100)
check('审查-快照就地刷新', len([i for i in j3 if i.get('kind') == 'task'])
      == len([i for i in j1 if i.get('kind') == 'task']))
# 7c LIKE 通配符安全
memory_engine.index_task('T3', '100%完成_度', '百分百', 'web')
r = memory_engine.search_keyword('100%完成')
check('审查-通配符子串安全', any(k['id'] == 'T3' for k in r['tasks']))
# 7d stop 簿记
tid_s = W.new_task('待停止')
ok, msg = W.stop_task(tid_s)
check('审查-stop任务态', ok and W.get_task(tid_s)['status'] == 'stopped')
ok2, _ = W.stop_task(tid_s)
check('审查-stop重复被拒', ok2 is False)
# ===== 8) UFO2 worker 回归 =====
check('审查-UFO可用性检测', W.ufo_available() is True)
check('审查-JSON提取器嵌套安全',
      W._extract_last_json('前缀 {"reason": "内嵌{}测试", "ok": true} 尾')['ok'] is True)
t_ufo = W.new_task('打开计算器', wtype='ufo')
tu = W.get_task(t_ufo)
check('审查-ufo任务簿记', tu['wtype'] == 'ufo' and tu['status'] == 'queued')
# ===== 9) 飞书适配层回归(桩对象, 不联网) =====
import feishu_adapter as _F
import io as _io
class _FakeResp:
    def __init__(self, ok, code=0, msg='', data=None, file=None):
        self._ok = ok; self.code = code; self.msg = msg; self.data = data; self.file = file
    def success(self): return self._ok
check('审查-飞书resp检查成功路径', _F._check_resp(_FakeResp(True, 0, ''), '回推') is True)
check('审查-飞书resp检查失败路径', _F._check_resp(_FakeResp(False, 99991672, 'denied'), '回推') is False)
check('审查-飞书下载空流报错',
      _F._read_resp_file(_FakeResp(True, file=_io.BytesIO(b''))) == b'' and '为空' in _F._status['error'])
check('审查-飞书下载有流读取',
      _F._read_resp_file(_FakeResp(True, file=_io.BytesIO(b'abc'))) == b'abc')
_F._status['last_chat_id'] = ''
check('审查-飞书无chat_id拒发', _F.send_files_to_last([]) == [])
import xiaozhi_bridge as _X
check('审查-小智run_task工具存在', any(t['name'] == 'butler_run_task' for t in _X.TOOLS))
print('\n===== 离线测试: %d 项, 失败 %d =====' % (total[0], len(fails)))
sys.exit(1 if fails else 0)
