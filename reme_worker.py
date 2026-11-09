# -*- coding: utf-8 -*-
"""reme_worker — 管家记忆工人: 用户增删改查数字分身记忆(ReMe)
共享 ReMe 服务(127.0.0.1:2333)与用户 workspace(见 ReMe 部署配置)
与 ZCode 的 reme MCP 桥是同一数据源 —— 数字分身双入口。

被 codex_worker._run_one 按 wtype='reme' 分流调用; 返回给用户的答案字符串。
意图解析: 确定性正则(零LLM), 与天气/笔记工人同风格。
"""
import json
import re
import urllib.parse
import urllib.request

REME = 'http://127.0.0.1:2333'


def _post(path, payload, timeout=60):
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(REME + path, data=body, method='POST')
    req.add_header('Content-Type', 'application/json')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def _get(path, timeout=60):
    """ReMe API 统一 POST /<tool> (GET 一律 405); path 含查询参数时转为 POST body"""
    if '?' in path:
        tool, qs = path.split('?', 1)
        payload = {k: v for k, v in urllib.parse.parse_qs(qs).items()
                   for v in v[:1]} if False else dict(urllib.parse.parse_qsl(qs))
        # 数值型还原
        for k, v in list(payload.items()):
            if v.isdigit():
                payload[k] = int(v)
        return _post(tool, payload, timeout=timeout)
    req = urllib.request.Request(REME + path, data=b'{}', method='POST')
    req.add_header('Content-Type', 'application/json')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def _fmt_search(ans):
    return '🔍 ' + (ans or '(无结果)')


def _llm_decide(new_fact, similar_memories):
    """LLM 判定新事实与相似记忆的关系: ADD / UPDATE(target_path+merged_content) / NOOP
    LLM 失败返回 None → 调用方降级为直接追加"""
    try:
        import model_router
        prompt = (
            '你是记忆管理器。用户新事实与已有记忆条目可能重复/矛盾/互补。' + chr(10) +
            '已有记忆:' + chr(10) + chr(10).join(
                '- [%s] %s' % (m['path'], m['text'][:120]) for m in similar_memories[:5]) +
            chr(10) + chr(10) + '新事实: ' + new_fact[:300] +
            chr(10) + chr(10) + '只输出严格JSON: {"action": "ADD|UPDATE|NOOP", "target_path": "UPDATE时的目标路径", '
            '"merged_content": "UPDATE时合并后的完整新内容(保留旧条目仍有效信息)"}' +
            chr(10) + chr(10) + '判定: 完全重复=NOOP; 同主题有新信息=UPDATE(合并旧+新); 新主题=ADD;'
            ' 禁止把系统状态/快照/清单类卡片当作 UPDATE 目标; 拿不准就 ADD')
        result = model_router.chat(prompt)
        raw = result[0] if isinstance(result, tuple) else result
        d = None
        if raw:
            m = re.search(r'\{.*\}', raw, re.S)
            if m:
                try:
                    d = json.loads(m.group(0))
                except Exception:
                    d = None
        if d and d.get('action') in ('ADD', 'UPDATE', 'NOOP'):
            return d
        return None
    except Exception:
        return None


def _conflict_resolve(content, date):
    """冲突消解: 记住前先 search 相似 → LLM 判定。
    返回 (action, message, extra); action=None 表示无相似记忆走直接追加"""
    import re as _re
    kw = ' '.join(_re.sub(r'[，。,.]', ' ', content).split())[:30]
    if not kw:
        return None, None, None
    try:
        hits = _get('/search?' + urllib.parse.urlencode({'query': kw, 'limit': 5}))
        ans = hits.get('answer', '')
        paths = _re.findall(r'(\S+?\.md)', ans)
        if not paths:
            return None, None, None
        similar = []
        for pth in paths[:3]:
            rd = _get('/read?' + urllib.parse.urlencode({'path': pth}))
            txt = rd.get('answer', '')
            body = txt.split('---', 2)[-1].strip()
            similar.append({'path': pth.replace(chr(92), '/'), 'text': body})
        decision = _llm_decide(content, similar)
        if decision is None:
            return None, None, None
        action = decision.get('action')
        if action == 'NOOP':
            return ('NOOP', '⏭ 该内容与已有记忆重复, 未重复记录。', None)
        if action == 'UPDATE':
            target = decision.get('target_path', '').replace(chr(92), '/')
            # 清理 LLM 可能包裹的 [] 与空白
            target = target.strip().strip('[]').strip()
            merged = decision.get('merged_content', '')
            if target and merged:
                _post('/write', {'path': target, 'name': target.split('/')[-1],
                                 'description': '记忆工人更新(冲突消解)',
                                 'content': merged})
                return ('UPDATED', '🔄 已更新记忆 %s: %s' % (target, merged[:80]), None)
    except Exception:
        pass
    return None, None, None


def run_reme(instruction):
    """入口: 解析用户意图 → ReMe API。返回给用户看的文本。"""
    msg = instruction.strip()

    # --- 记住(增) ---
    import re
    m = (re.match(r'^(?:请)?(?:帮我)?(?:记(?:住|一下|个)|记住)(?:到记忆|到数字分身)?[:：,，\s]*(.+)$', msg, re.S)
         or re.match(r'^(?:请)?(?:帮我)?(?:新增|添加|加)(?:一条)?(?:记忆|笔记)[:：,，\s]*(.+)$', msg, re.S))
    if m:
        content = ' '.join(m.group(1).split())
        # 冲突消解(TOP1): 先查相似 → LLM 判定 ADD/UPDATE/NOOP
        try:
            action, message, extra = _conflict_resolve(content, None)
            if action == 'NOOP':
                return message
            if action == 'UPDATED':
                return message
        except Exception:
            pass  # 消解失败 → 降级直接追加
        date = content[:10] if content[:2].isdigit() else __import__('time').strftime('%Y-%m-%d')
        # TOP2: 流程/教程/怎么做类 → procedure/(程序记忆层)
        # 判定收紧: 需命中强特征(步骤/教程/配置/部署/安装), '流程/方法' 单独出现是叙述不算
        is_procedure = bool(re.search(r'步骤|教程|配置|部署|安装|设置方法|操作指南', content[:60], re.I))
        # F统一入口: 三层自动选层(用户无感) - 教程->procedure, 偏好/身份/环境->画像卡, 其余->daily
        is_pref = bool(re.search(r'我喜欢|我偏好|我爱|我讨厌|我是|我叫|我的名字|我的密码|我的账号|'
                                 '我的服务器|我的邮箱|我的习惯|我常用|我用的是', content[:40]))
        if is_procedure:
            slug = re.sub(r'[\s/\\:*?"<>|]', '-', content[:30])
            path = 'procedure/%s-%s' % (date, slug)
        elif is_pref:
            path = 'digest/personal/pref-%s-%s' % (date, int(__import__('time').time()) % 10000)
        else:
            path = 'daily/%s/reme-worker-%s' % (date, int(__import__('time').time()) % 100000)
        r = _post('/write', {
            'path': path,
            'name': content[:40],
            'description': '记忆工人记录',
            'content': '# %s\n\n%s' % (content[:40], content),
            'metadata': {'valid_from': __import__('time').strftime('%Y-%m-%d'), 'source': 'butler-reme-worker'}})
        layer = '📋 程序记忆' if is_procedure else '🧠 已记入数字分身'
        return '%s: %s' % (layer, content[:80])

    # --- 删 ---
    m = re.match(r'^(?:请)?(?:帮我)?(?:删除|删掉|删)(?:一条)?(?:记忆|笔记)[:：,，\s]*(\S+)', msg)
    if m:
        path = m.group(1)
        if not path.startswith(('daily/', 'digest/', 'resource/')):
            # 按关键词搜索后删第一个
            hits = _get('/search?' + urllib.parse.urlencode({'query': path, 'limit': 5}))
            ans = hits.get('answer', '')
            import re as _re
            paths = _re.findall(r'(\S+?\.md)', ans)
            if not paths:
                return '🔍 没找到含「%s」的记忆' % path
            path = paths[0].replace('\\', '/')
        r = _post('/delete', {'path': path})
        return '🗑 已删除记忆: %s' % path

    # --- 改 ---
    m = re.match(r'^(?:请)?(?:帮我)?(?:修改|改|更新)(?:一条)?(?:记忆|笔记)[:：,，\s]*(\S+)\s*(?:为|成|改为|改成|:|：)\s*(.+)$', msg, re.S)
    if m:
        path, content = m.group(1), ' '.join(m.group(2).split())
        if not path.startswith(('daily/', 'digest/', 'resource/')):
            hits = _get('/search?' + urllib.parse.urlencode({'query': path, 'limit': 5}))
            import re as _re
            paths = _re.findall(r'(\S+?\.md)', hits.get('answer', ''))
            if not paths:
                return '🔍 没找到含「%s」的记忆' % path
            path = paths[0].replace('\\', '/')
        old = _get('/read?' + urllib.parse.urlencode({'path': path}))
        old_text = old.get('answer', '')
        # 全文重写: 保留 frontmatter, 替换正文
        r = _post('/write', {'path': path, 'name': path.split('/')[-1],
                             'description': '记忆工人更新',
                             'content': '# 更新\n\n%s' % content})
        return '✏️ 已更新记忆 %s: %s' % (path, content[:80])

    # --- 查(显式) ---
    m = re.match(r'^(?:请)?(帮我)?(?:查(?:一下)?|搜索|找|看看)(?:一下)?(?:记忆|数字分身|分身|笔记)[:：,，\s]*(.*)$', msg)
    if m:
        kw = (m.group(2) or '').strip()
        if kw:
            r = _get('/search?' + urllib.parse.urlencode({'query': kw, 'limit': 5}))
            ans = r.get('answer', '')
            # G1 双源: ReMe 无结果时 fallback 查管家 notes_fts(旧记忆源)
            if '========== ' not in ans and '(无结果)' not in ans:
                try:
                    import memory_engine as _me
                    fr = _me.search_keyword(kw, 5).get('notes') or []
                    if fr:
                        ans = chr(10).join(
                            '- [%s] %s (%s)' % (n['note_id'], (n.get('snippet') or '')[:80], n.get('ts', ''))
                            for n in fr)
                except Exception:
                    pass
            return _fmt_search(ans or '(无)')
        r = _get('/list?' + urllib.parse.urlencode({'path': 'digest', 'recursive': True}))
        return '📁 ' + (r.get('answer', '') or '(空)')[:800]

    # --- 宽松兜底: 含"记忆/分身"一律给结果(列出/搜索), 不再 raise —
    # 实测败因: "那你帮我看一下 有啥记忆给我呈现出来" 旧触发词(查|找|哪些|什么|看看|吗)全不中
    if ('记忆' in msg or '分身' in msg) and len(msg) <= 60:
        kw = re.sub(
            r'(那你|帮我|给我|有哪些|有什么|有啥|都有|看一下|看看|查一下|搜一下|搜索|查|找|'
            r'展示|呈现|列出|翻一下|翻|记忆|分身|数字|一下|出来|我|吗|呢|吧|嘛|[?？！!。，,、\s])',
            '', msg).strip()
        if kw and len(kw) >= 2:
            r = _get('/search?' + urllib.parse.urlencode({'query': kw[:30], 'limit': 5}))
            return _fmt_search(r.get('answer', '(无)'))
        r = _get('/list?' + urllib.parse.urlencode({'path': 'digest', 'recursive': True}))
        return '📁 ' + (r.get('answer', '') or '(空)')[:800]

    raise ValueError('记忆意图未识别(支持: 记住X / 查记忆[词] / 删除记忆词或路径 / 改记忆词为X)')


if __name__ == '__main__':
    print(run_reme(sys.argv[1] if len(sys.argv) > 1 else '查记忆'))
