# -*- coding: utf-8 -*-
"""混合记忆引擎 — SQLite FTS5 关键词检索 + RAG 知识库检索, 全局可搜索

FTS5 模式沿用 agentic-os (MIT, github.com/modimihir07/agentic-os) 的
brain/memory_search.py 表结构设计: fts5 虚拟表 + MATCH + rank + snippet。
RAG 走本机 multimodal-rag-mcp 的 search_knowledge (缺席时静默停用,
但把原始错误暴露在 search()/api 的 rag_error 字段, 不吞错)。
"""
import json
import os
import sqlite3
import threading
import time

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       'state', 'memory.db')
PROFILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       'state', 'profile.json')
_lock = threading.Lock()
_conn = None

_RAG = None
_rag_err = ''


def _db():
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.execute('CREATE VIRTUAL TABLE IF NOT EXISTS tasks_fts USING fts5('
                      'task_id UNINDEXED, question, answer, source, ts UNINDEXED)')
        _conn.execute('CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5('
                      'note_id UNINDEXED, content, ts UNINDEXED)')
        _conn.commit()
    return _conn


def index_task(task_id, question, answer, source='web', ts=None):
    """任务完结 → 进全局记忆索引(同 id 重跑先删旧行)"""
    with _lock:
        c = _db()
        c.execute('DELETE FROM tasks_fts WHERE task_id = ?', (task_id,))
        c.execute('INSERT INTO tasks_fts VALUES (?,?,?,?,?)',
                  (task_id, question[:2000], answer[:8000], source,
                   ts or time.strftime('%Y-%m-%d %H:%M:%S')))
        c.commit()


def add_note(content, reme=True):
    """手动记忆条目; 返回 note_id; G1统一: 同步写入 ReMe(数字分身)双写
    reme=False: 只进 notes_fts(ReMe 侧交 reme_worker 冲突消解+分层路由, 防双写)"""
    nid = 'N%d%d' % (int(time.time() * 1000), threading.get_ident() % 10000)
    with _lock:
        c = _db()
        c.execute('INSERT INTO notes_fts VALUES (?,?,?)',
                  (nid, content[:1000], time.strftime('%Y-%m-%d %H:%M:%S')))
        c.commit()
    if not reme:
        return nid
    try:  # G1 双写: 同步进 ReMe(失败不阻断 notes_fts 主路)
        import urllib.request
        body = json.dumps({'path': 'daily/butler-notes/%s' % nid,
                           'name': content[:40], 'description': '管家笔记',
                           'content': '# ' + content[:40] + chr(10) + chr(10) + content},
                          ensure_ascii=False).encode('utf-8')
        req = urllib.request.Request('http://127.0.0.1:2333/write', data=body, method='POST')
        req.add_header('Content-Type', 'application/json')
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
    except Exception:
        pass  # ReMe 离线不阻断主路
    return nid


def note_delete(note_id):
    """删笔记; 返回是否命中"""
    with _lock:
        c = _db()
        cur = c.execute('DELETE FROM notes_fts WHERE note_id = ?', (note_id,))
        c.commit()
        return cur.rowcount > 0


def note_edit(note_id, content):
    """改笔记内容; 返回是否命中"""
    with _lock:
        c = _db()
        cur = c.execute('UPDATE notes_fts SET content = ? WHERE note_id = ?',
                        (content[:1000], note_id))
        c.commit()
        return cur.rowcount > 0


def list_notes(limit=10):
    """最近笔记(新→旧)"""
    with _lock:
        c = _db()
        rows = c.execute('SELECT note_id, content, ts FROM notes_fts '
                         'ORDER BY rowid DESC LIMIT ?', (int(limit),)).fetchall()
        return [{'note_id': r[0], 'content': r[1], 'ts': r[2]} for r in rows]


def profile_list():
    try:
        return json.load(open(PROFILE, encoding='utf-8'))
    except Exception:
        return []


def _save_profile(items):
    json.dump(items, open(PROFILE, 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)


def profile_add(text, replace_idx=None):
    """画像事实新增/取代(同一事实新状态时 replace_idx 指向被取代条目)"""
    items = profile_list()
    rec = {'text': str(text).strip()[:200],
           'ts': time.strftime('%Y-%m-%d %H:%M')}
    if replace_idx is not None and 0 <= replace_idx < len(items):
        rec['supersedes'] = items[replace_idx]['text']
        items[replace_idx] = rec
    else:
        items.append(rec)
    items = items[-50:]
    _save_profile(items)
    return rec


def profile_del(idx):
    items = profile_list()
    if 0 <= idx < len(items):
        items.pop(idx)
        _save_profile(items)
        return True
    return False


def profile_edit(idx, text):
    items = profile_list()
    if 0 <= idx < len(items):
        items[idx]['text'] = str(text).strip()[:200]
        items[idx]['ts'] = time.strftime('%Y-%m-%d %H:%M')
        _save_profile(items)
        return True
    return False


def _fts_query(q):
    """用户查询 → FTS5 安全 MATCH 表达式(词间 AND, 特殊符剥离)"""
    words = [w for w in
             ''.join(ch if (ch.isalnum() or ch in '_') else ' '
                     for ch in q).split() if w][:8]
    if not words:
        return None
    return ' AND '.join('"%s"' % w.replace('"', '') for w in words)


def search_keyword(q, limit=10):
    """关键词组: 历史任务 + 手动记忆。
    实现注记: FTS5 默认 tokenizer 对连续中文是单 token, 子串搜不到——
    本服务内容以中文为主, 改用 LIKE 子串扫描(个人量级 <10ms), FTS5 表保留
    以兼容 agentic-os(MIT) 的引擎模式归属。"""
    c = _db()
    tasks, notes = [], []
    words = [w for w in
             ''.join(ch if (ch.isalnum() or '\u4e00' <= ch <= '\u9fff') else ' '
                     for ch in (q or '')).split() if w][:6]
    try:
        if words:
            where = ' AND '.join(['(question LIKE ? OR answer LIKE ?)'] * len(words))
            args = []
            for w in words:
                args += ['%' + w + '%', '%' + w + '%']
            rows = c.execute(
                'SELECT task_id, question, answer, ts, source FROM tasks_fts '
                'WHERE ' + where + ' ORDER BY ts DESC LIMIT ?', args + [limit]).fetchall()
        else:
            rows = c.execute(
                'SELECT task_id, question, answer, ts, source FROM tasks_fts '
                'ORDER BY ts DESC LIMIT ?', (limit,)).fetchall()
        for tid, question, answer, ts, source in rows:
            snip = answer[:120]
            if q.strip():
                i = answer.find(words[0] if words else '')
                if i > 0:
                    snip = '…' + answer[max(0, i - 20): i + 90] + '…'
            tasks.append({'id': tid, 'snippet': snip, 'question': question,
                          'ts': ts, 'source': source})
        if q.strip() and words:
            where = ' AND '.join(['content LIKE ?'] * len(words))
            args = ['%' + w + '%' for w in words]
            notes = [{'note_id': r[0], 'snippet': r[1][:120], 'ts': r[2]}
                     for r in c.execute(
                         'SELECT note_id, content, ts FROM notes_fts '
                         'WHERE ' + where + ' ORDER BY ts DESC LIMIT 5', args).fetchall()]
    except sqlite3.OperationalError as e:
        tasks.append({'id': '', 'snippet': '检索错误: %s' % e, 'question': '',
                      'ts': '', 'source': 'error'})
    return {'tasks': tasks, 'notes': notes}


def _rag():
    """懒加载 RAG 客户端(持久会话); 记录原始错误到 _rag_err"""
    global _RAG, _rag_err
    if _RAG is None:
        try:
            import rag_client as rc
            _RAG = rc
        except Exception as e:
            _rag_err = str(e)
            _RAG = False
    return _RAG or None


def search_rag(q, top_k=3):
    """RAG 三库检索; 失败返回 [] 且 rag_error 保留原始错误"""
    global _rag_err
    cli = _rag()
    if not cli:
        return []
    try:
        hits = cli.search(q, top_k=top_k)
        _rag_err = ''
        return hits
    except Exception as e:
        _rag_err = str(e)
        return []


def rag_error():
    return _rag_err


def search_hybrid(q, limit=10):
    """全局混合检索: 关键词(历史+笔记) + RAG(三库); RAG 原始错误透出"""
    out = {'keyword': search_keyword(q, limit), 'rag': search_rag(q, 3)}
    if _rag_err:
        out['rag_error'] = _rag_err
    return out


def _reme_snippets(q, top=2, timeout=1.5):
    """数字分身(ReMe 2333)向量+BM25检索 topN; 超时/缺席静默返回[](不拖累路由)"""
    try:
        import urllib.request
        body = json.dumps({'query': q, 'limit': top}, ensure_ascii=False).encode('utf-8')
        req = urllib.request.Request('http://127.0.0.1:2333/search', data=body, method='POST')
        req.add_header('Content-Type', 'application/json')
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ans = json.loads(r.read().decode('utf-8')).get('answer', '')
        segs = []
        for seg in ans.split('==========')[1::2][:top]:
            txt = ' '.join(seg.split())
            if txt:
                segs.append(txt[:180])
        return segs
    except Exception:
        return []


def context_for(question):
    """Worker 分配的必要上下文(仅检索命中, 不带会话史):
    top3 历史 + top2 RAG + top2 数字分身(ReMe; 缺席静默跳过)"""
    parts = []
    kw = search_keyword(question, 3).get('tasks', [])
    kw = [k for k in kw if k.get('id')]
    if kw:
        lines = ['- [历史#%s] %s → %s' % (k['id'], k['question'][:60],
                                          k['snippet'][:100]) for k in kw]
        parts.append('【相关历史任务】\n' + '\n'.join(lines))
    rag = search_rag(question, 2)
    if rag:
        lines = ['- [%s] %s' % (h.get('file_name', ''), h.get('content', '')[:180])
                 for h in rag]
        parts.append('【知识库相关经验】\n' + '\n'.join(lines))
    reme = _reme_snippets(question)
    if reme:
        parts.append('【数字分身相关记忆】\n' + '\n'.join('- ' + s for s in reme))
    return '\n\n'.join(parts)


if __name__ == '__main__':
    add_note('测试记忆条目: 管家端口 9991-9999')
    print(json.dumps(search_hybrid('管家'), ensure_ascii=False, indent=1)[:500])
