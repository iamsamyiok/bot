# -*- coding: utf-8 -*-
"""飞书适配层 v2 — 基于 ControlMesh (MIT, github.com/qiaoqiao2521/ControlMesh,
用户指定入口 muqiao215/ControlMesh 已 404) 的长连接实现模式:
lark_oapi.ws.Client 官方长连接(免公网回调), 凭证复用 ControlMesh 已注册应用,
配置在 Web 页完成。消息语义完全由 Router on_chat 定义 — 与本地 Web 逐字对齐:
收到文本 → Router 消化(直接回答/编排派发) → 回复; 任务完成后总结自动回推本对话。

依赖(仅启用飞书时需要): pip install lark-oapi
"""
import json
import os
import re
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(BASE, 'config.json')
UPLOAD_DIR = os.path.join(BASE, 'state', 'uploads')
LAST_SEEN = os.path.join(BASE, 'state', 'feishu_last.json')  # 断电续推: 重启后仍能回推对话
_lock = threading.Lock()
_status = {'connected': False, 'error': '', 'app_id': '', 'started': '',
           'last_msg_id': '', 'last_chat_id': ''}
_on_chat = None
CM_CONFIG = os.path.join(os.path.expanduser('~'), '.controlmesh', 'config', 'config.json')


def _load_last():
    """恢复上次会话的 msg/chat id(否则每次重启后回推静默失效, 直到用户再发消息)"""
    try:
        d = json.load(open(LAST_SEEN, encoding='utf-8'))
        _status['last_msg_id'] = d.get('last_msg_id', '')
        _status['last_chat_id'] = d.get('last_chat_id', '')
    except Exception:
        pass


def _save_last():
    try:
        with _lock:
            json.dump({'last_msg_id': _status.get('last_msg_id', ''),
                       'last_chat_id': _status.get('last_chat_id', ''),
                       'ts': time.strftime('%Y-%m-%d %H:%M:%S')},
                      open(LAST_SEEN, 'w', encoding='utf-8'))
    except Exception:
        pass


_load_last()


def load_config():
    try:
        return json.load(open(CONFIG, encoding='utf-8'))
    except Exception:
        return {}


def save_config(d):
    with _lock:
        cur = load_config()
        cur.update(d)
        json.dump(cur, open(CONFIG, 'w', encoding='utf-8'),
                  ensure_ascii=False, indent=1)


def mask_secret(s):
    return (s[:4] + '****' + s[-4:]) if s and len(s) > 8 else ('<已配置>' if s else '')


def import_from_controlmesh():
    """首次运行: 从 ControlMesh 配置迁移已注册的飞书应用凭证(用户免重配)"""
    cfg = load_config()
    if cfg.get('feishu', {}).get('app_id'):
        return False
    try:
        cm = json.load(open(CM_CONFIG, encoding='utf-8'))
        f = cm.get('feishu', {}) or {}
        if f.get('app_id') and f.get('app_secret'):
            cfg.setdefault('feishu', {})
            cfg['feishu']['app_id'] = f['app_id']
            cfg['feishu']['app_secret'] = f['app_secret']
            cfg['feishu']['migrated_from'] = 'controlmesh'
            json.dump(cfg, open(CONFIG, 'w', encoding='utf-8'),
                      ensure_ascii=False, indent=1)
            return True
    except Exception:
        pass
    return False


def public_state():
    cfg = load_config().get('feishu', {})
    return {'configured': bool(cfg.get('app_id') and cfg.get('app_secret')),
            'app_id': cfg.get('app_id', ''),
            'connected': _status['connected'],
            'error': _status['error'],
            'started': _status['started']}


def reply_to_last(text):
    """任务完成总结 → 回推用户最后那条飞书消息所在的对话(失败退避重试一次)。
    无 last_msg_id(重启后未收到新消息)→ 降级为按 chat_id 直发, 不断链"""
    mid = _status.get('last_msg_id')
    chat_id = _status.get('last_chat_id') or ''
    if not mid and not chat_id:
        return False
    try:
        import lark_oapi as lark
        client = _api_client()
        if mid:
            req = _build_reply_req(mid, text)
            ok = _check_resp(client.im.v1.message.reply(req), '回推')
            if ok:
                return True
            time.sleep(1.5)                      # 限流等 → 退避重试一次
            ok = _check_resp(client.im.v1.message.reply(req), '回推(重试)')
            if ok:
                return True
        if chat_id:                              # reply 不可用 → 直发对话
            return _create_to_chat(chat_id, 'text', {'text': text})
        return False
    except Exception as e:
        _status['error'] = '回推失败: %s' % e
        return False


def _api_client():
    """每次新建 API client(与现有 reply 同风格, 无状态安全)"""
    import lark_oapi as lark
    cfg = load_config().get('feishu', {})
    return lark.Client.builder().app_id(cfg.get('app_id')).app_secret(
        cfg.get('app_secret')).build()


def _read_resp_file(resp):
    """下载类响应(resp.file 为 IO 流)读出 bytes, 空则置错误返回 b''"""
    data = b''
    try:
        data = resp.file.read()
    except Exception as e:
        _status['error'] = '下载响应读取失败: %s' % e
        return b''
    if not data:
        _status['error'] = '下载响应为空(可能权限不足或资源过期)'
    return data


def _tenant_token():
    """直连 REST 用的 tenant_access_token; 失败返回 ''(错误在 _status.error)"""
    import urllib.request
    cfg = load_config().get('feishu', {})
    try:
        body = json.dumps({'app_id': cfg.get('app_id'),
                           'app_secret': cfg.get('app_secret')}).encode('utf-8')
        req = urllib.request.Request(
            'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
            data=body, method='POST')
        req.add_header('Content-Type', 'application/json')
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read().decode('utf-8'))
        if d.get('code') != 0:
            _status['error'] = '获取token失败: %s %s' % (d.get('code'), d.get('msg'))
            return ''
        return d.get('tenant_access_token', '')
    except Exception as e:
        _status['error'] = '获取token异常: %s' % e
        return ''


def _rest_download(url, prefix, ext='.bin'):
    """直连 REST 下载二进制(lark 1.6.x SDK 模型缺必填 query 参数, 如图片的
    image_type=message); 返回本地路径, 失败返回 ''(错误在 _status.error)"""
    import urllib.request
    try:
        token = _tenant_token()
        if not token:
            return ''
        req = urllib.request.Request(url, method='GET')
        req.add_header('Authorization', 'Bearer ' + token)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
                ctype = (r.headers.get('Content-Type') or '').lower()
        except urllib.error.HTTPError as e:
            # 飞书 4xx 的错误详情(JSON)在 body 里, 读出来透传
            try:
                body = e.read().decode('utf-8', errors='replace')
                d = json.loads(body)
                _status['error'] = '下载失败: code=%s msg=%s' % (
                    d.get('code'), d.get('msg'))
            except Exception:
                _status['error'] = '下载失败: HTTP %s' % e.code
            return ''
        if 'json' in ctype:
            d = json.loads(data.decode('utf-8', errors='replace'))
            _status['error'] = '下载失败: code=%s msg=%s' % (
                d.get('code'), d.get('msg'))
            return ''
        if not data:
            _status['error'] = '下载失败: 响应为空'
            return ''
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        if ext == '.bin':
            if 'jpeg' in ctype or 'jpg' in ctype:
                ext = '.jpg'
            elif 'gif' in ctype:
                ext = '.gif'
            elif 'webp' in ctype:
                ext = '.webp'
            else:
                ext = '.png'
        path = os.path.join(UPLOAD_DIR, '%s_%s%s' % (
            prefix, time.strftime('%m%d%H%M%S'), ext))
        open(path, 'wb').write(data)
        return path
    except Exception as e:
        _status['error'] = '下载异常: %s' % e
        return ''


def download_image(message_id, image_key, prefix='feishu_img'):
    """下载消息图片到 state/uploads(REST 直连, 走消息资源端点 type=image);
    返回本地路径; 失败返回 ''(错误在 _status.error)"""
    if not (message_id and image_key):
        return ''
    return _rest_download(
        'https://open.feishu.cn/open-apis/im/v1/messages/%s/resources/%s?type=image'
        % (message_id, image_key), prefix)


def download_file(message_id, file_key, file_name, prefix='feishu_file'):
    """下载消息内文件到 state/uploads(REST 直连); 返回本地路径; 失败返回 ''"""
    if not (message_id and file_key):
        return ''
    safe = re.sub(r'[\\/:*?"<>|]', '_', os.path.basename(file_name or 'file.bin'))
    path = _rest_download(
        'https://open.feishu.cn/open-apis/im/v1/messages/%s/resources/%s?type=file'
        % (message_id, file_key), prefix)
    if path and safe:
        # 保留原始文件名后缀(便于 worker 识别类型)
        ext = os.path.splitext(safe)[1]
        if ext and not path.endswith(ext):
            newp = path + ext
            try:
                os.rename(path, newp)
                path = newp
            except Exception:
                pass
    return path


def _upload_im_file(path):
    """上传本地文件到飞书(im.v1.files), 返回 file_key; 失败返回 ''"""
    try:
        from lark_oapi.api.im.v1 import CreateFileRequest, CreateFileRequestBody
        fh = open(path, 'rb')
        body = (CreateFileRequestBody.builder().file_type('stream')
                .file_name(os.path.basename(path)).file(fh).duration(0).build())
        resp = _api_client().im.v1.file.create(
            CreateFileRequest.builder().request_body(body).build())
        if not _check_resp(resp, '上传文件'):
            return ''
        key = resp.data.file_key if resp.data else None
        if not key:
            _status['error'] = '上传文件: 响应缺 file_key'
        return key or ''
    except Exception as e:
        _status['error'] = '上传文件异常: %s' % e
        return ''


def _create_to_chat(chat_id, msg_type, content_obj):
    """给指定 chat 发一条消息(create); 1.6.8: receive_id 在 Body 里, receive_id_type 是
    Request 的 query(实测: 放Request builder会AttributeError, 文件外发曾全程静默失败)"""
    from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
    body = (CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .content(json.dumps(content_obj, ensure_ascii=False))
            .msg_type(msg_type).build())
    b = CreateMessageRequest.builder().receive_id_type('chat_id')
    req = (b.request_body(body).build()
           if hasattr(b, 'request_body') else b.body(body).build())
    return _check_resp(_api_client().im.v1.message.create(req), '发送消息')


def send_approval_card(aid, task_id, question, action_desc, risk_kw):
    """审批闸v2: 发交互卡片(带[确认执行/取消]按钮)到用户对话; 返回是否成功"""
    chat_id = _status.get('last_chat_id') or ''
    if not chat_id:
        _status['error'] = '审批卡片: 尚无chat_id'
        return False
    card = {
        "schema": "2.0",
        "config": {"update_multi": True, "enable_forward": False},
        "header": {"title": {"tag": "plain_text", "content": "🔐 审批请求"},
                   "template": "blue"},
        "body": {"elements": [
            {"tag": "markdown",
             "content": "**任务**: %s\n**高危动作**: %s\n**触发词**: `%s`\n等待拍板期间任务已挂起(不占进程/不耗token)" % (
                 question[:60], action_desc[:120], risk_kw)},
            {"tag": "interactive_container", "width": "fill", "has_border": True,
             "border_color": "blue", "corner_radius": "8px", "padding": "8px 12px",
             "behaviors": [{"type": "callback",
                            "value": {"aid": aid, "choice": "approve"}}],
             "elements": [{"tag": "div",
                           "text": {"tag": "plain_text", "content": "✅ 确认执行"}}]},
            {"tag": "interactive_container", "width": "fill", "has_border": True,
             "border_color": "grey", "corner_radius": "8px", "padding": "8px 12px",
             "behaviors": [{"type": "callback",
                            "value": {"aid": aid, "choice": "cancel"}}],
             "elements": [{"tag": "div",
                           "text": {"tag": "plain_text", "content": "❌ 取消任务"}}]},
        ]}}
    try:
        return _create_to_chat(chat_id, 'interactive', card)
    except Exception as e:
        _status['error'] = '审批卡片失败: %s' % e
        return False


def update_approval_card(_aid, approved):
    """拍板后更新原卡片为结案态(v2简化: 发一条结案消息代替原卡更新, 免cardkit依赖)"""
    try:
        chat_id = _status.get('last_chat_id') or ''
        if chat_id:
            _create_to_chat(chat_id, 'text', {'text': (
                '🔐 审批%s: 任务继续执行。' if approved else '🚫 审批取消: 任务已终止。')})
    except Exception:
        pass


def send_files_to_last(paths, max_files=5, max_bytes=30 * 1024 * 1024):
    """把产物文件上传并发到用户最后一条飞书消息所在对话(≤5个, 单个≤30MB)。
    返回成功发送的文件名列表; 失败细节在 _status.error"""
    chat_id = _status.get('last_chat_id') or ''
    if not chat_id:
        _status['error'] = '发送文件: 尚无 chat_id(用户未发过消息)'
        return []
    sent = []
    for p in paths:
        if len(sent) >= max_files:
            break
        try:
            if not os.path.isfile(p) or os.path.getsize(p) > max_bytes:
                continue
            fk = _upload_im_file(p)
            if not fk:
                continue
            if _create_to_chat(chat_id, 'file', {'file_key': fk}):
                sent.append(os.path.basename(p))
        except Exception as e:
            _status['error'] = '发送文件异常: %s' % e
    return sent


_seen_msgs = set()

# lark-oapi 类名兼容: >=1.7 叫 ReplyMessageBody, 1.6.x 叫 ReplyMessageRequestBody
# (本机 hermes venv 装 1.6.8 — 旧写法在 import 处炸, 被下方 except 吞成"回复失败"静默丢回复)
try:
    from lark_oapi.api.im.v1 import ReplyMessageBody as _ReplyBody
except ImportError:
    from lark_oapi.api.im.v1 import ReplyMessageRequestBody as _ReplyBody


def _check_resp(resp, where):
    """lark API 业务错误不抛异常, 必须显式检查 success(), 否则静默失败"""
    if resp.success():
        return True
    _status['error'] = '%s失败: code=%s msg=%s' % (where, resp.code, resp.msg)
    return False


def _build_reply_req(message_id, text):
    """构建回复请求, 屏蔽版本差异: >=1.7 用 .body(), 1.6.x 用 .request_body()"""
    from lark_oapi.api.im.v1 import ReplyMessageRequest
    body = _ReplyBody.builder().content(json.dumps(
        {'text': text}, ensure_ascii=False)).msg_type('text').build()
    b = ReplyMessageRequest.builder().message_id(message_id)
    if hasattr(b, 'body'):
        return b.body(body).build()
    return b.request_body(body).build()


def start(on_chat_cb):
    """启动飞书长连接(独立线程); 未配置/SDK缺失时置错误状态, 不抛出"""
    global _on_chat
    _on_chat = on_chat_cb
    import_from_controlmesh()
    cfg = load_config().get('feishu', {})
    app_id = cfg.get('app_id')
    app_secret = cfg.get('app_secret')
    if not (app_id and app_secret):
        _status['error'] = '未配置飞书凭证(Web页"飞书配置"填写; 已尝试从ControlMesh迁移)'
        return False
    try:
        import lark_oapi as lark
    except Exception as e:
        _status['error'] = '缺依赖 lark-oapi (pip install lark-oapi): %s' % e
        return False

    _status['app_id'] = app_id

    def on_message(data):
        try:
            msg = data.event.message
            if msg.message_id in _seen_msgs:      # 去重: 飞书会重试投递同一事件
                return
            _seen_msgs.add(msg.message_id)
            if len(_seen_msgs) > 2000:
                for x in list(_seen_msgs)[:1000]:
                    _seen_msgs.discard(x)
            _status['last_msg_id'] = msg.message_id
            _status['last_chat_id'] = getattr(msg, 'chat_id', '') or ''
            _save_last()
            # 去重/登记留在 ws 线程(必须立即返回); 处理+回复进工作线程 —
            # on_chat 内含 LLM 路由与记忆检索(可达数十秒), 内联执行会饿死 ws
            # 心跳导致断连重连循环(飞书"经常有问题"的主因)
            threading.Thread(target=_process_msg, args=(msg,), daemon=True).start()
        except Exception as e:
            _status['error'] = '消息处理异常: %s' % e

    def _process_msg(msg):
        try:
            mt = (msg.message_type or '').lower()
            if mt == 'text':
                text = json.loads(msg.content).get('text', '')
                # ②批处理(Superagent思想): 连发消息8秒窗口聚合, 首条线程统一送路由
                import batch_router as _br
                merged = _br.enqueue_batch(text, 'feishu')
                if merged is None:
                    return                      # 已并入窗口, 首条线程统一处理
                reply = (_on_chat(merged, 'feishu') if _on_chat
                         else '服务尚未就绪')
                _reply(msg, reply)
            elif mt == 'image':
                _handle_image(msg)
            elif mt == 'file':
                _handle_file(msg)
            else:
                _reply(msg, '暂支持文本/图片/文件消息')
        except Exception as e:
            _status['error'] = '消息处理异常: %s' % e
            _reply(msg, '消息处理异常: %s' % str(e)[:120])

    def _handle_image(msg):
        """图片消息 → 下载(消息资源端点) → 强制派看图任务(on_chat 的 image 参数路径)"""
        try:
            image_key = json.loads(msg.content).get('image_key', '')
            # 收到的消息图片必须走 messages/:id/resources/:file_key?type=image
            # (/im/v1/images/:key 对收图会 234001 invalid request param, 实测)
            path = download_image(msg.message_id, image_key) if image_key else ''
            if not path:
                _reply(msg, '图片接收失败: %s' % (_status.get('error', '')[:150]
                                                 or '未知原因(可能应用缺 im:resource 权限)'))
                return
            reply = (_on_chat('请查看并分析这张图片, 说明图里的内容和问题',
                              'feishu', image=path) if _on_chat
                     else '服务尚未就绪')
            _reply(msg, reply)
        except Exception as e:
            _status['error'] = '图片处理异常: %s' % e
            _reply(msg, '图片处理异常: %s' % str(e)[:120])

    def _handle_file(msg):
        """文件消息 → 下载 → 强制派 codex 任务(「任务：」前缀零误判, 免被
        路由到 files 轻工人报"意图未识别"——实测教训)"""
        try:
            c = json.loads(msg.content)
            path = download_file(msg.message_id, c.get('file_key', ''),
                                 c.get('file_name', '') or '')
            if not path:
                _reply(msg, '文件接收失败: %s' % (_status.get('error', '')[:150]
                                                 or '未知原因(可能应用缺 im:message 只读权限)'))
                return
            reply = (_on_chat('任务：读取并分析文件「%s」(已保存本机: %s)。'
                              '总结该文件的要点回复用户; 若是代码/网页文件给出结构与用途' % (
                                  os.path.basename(path), path),
                              'feishu') if _on_chat else '服务尚未就绪')
            _reply(msg, reply)
        except Exception as e:
            _status['error'] = '文件处理异常: %s' % e
            _reply(msg, '文件处理异常: %s' % str(e)[:120])

    _reply_cli = []                       # 复用 client(原先每条回复重建, 慢且易被限流)

    def _reply(msg, text):
        try:
            import lark_oapi as lark
            if not _reply_cli:
                _reply_cli.append(lark.Client.builder().app_id(app_id).app_secret(
                    load_config()['feishu']['app_secret']).build())
            req = _build_reply_req(msg.message_id, text)
            try:
                import uxlog
                uxlog.log('reply', 'out', text, 'feishu')
            except Exception:
                pass
            ok = _check_resp(_reply_cli[0].im.v1.message.reply(req), '回复')
            if not ok:                    # 业务错(限流等)→ 退避重试一次, 丢回复=用户侧"没反应"
                time.sleep(1.5)
                _check_resp(_reply_cli[0].im.v1.message.reply(req), '回复(重试)')
        except Exception as e:
            _status['error'] = '回复失败: %s' % e

    def on_card_action(data):
        """审批卡片按钮点击 → 落账 → toast回执 → 唤醒等待中的worker"""
        try:
            resp = lark_oapi.event.callback.model.p2_card_action_trigger.P2CardActionTriggerResponse()
            act = getattr(data.event, 'action', None)
            val = getattr(act, 'value', None) or {}
            aid = str(val.get('aid', ''))
            choice = str(val.get('choice', ''))
            if aid:
                import approval as _ap
                approved = (choice == 'approve')
                a = _ap.decide(aid, approved)
                if a:
                    toast = getattr(resp, 'toast', None)
                    if toast is None:
                        from lark_oapi.event.callback.model.p2_card_action_trigger import CallBackToast
                        toast = CallBackToast()
                        toast.type = 'success' if approved else 'info'
                        toast.content = '已确认, 任务继续执行' if approved else '已取消任务'
                        resp.toast = toast
                    update_approval_card(aid, approved)
            return resp
        except Exception as e:
            _status['error'] = '卡片回调异常: %s' % e
            return lark_oapi.event.callback.model.p2_card_action_trigger.P2CardActionTriggerResponse()

    handler = (lark.EventDispatcherHandler.builder('', '')
               .register_p2_im_message_receive_v1(on_message)
               .register_p2_card_action_trigger(on_card_action).build())

    def run():
        backoff = 5
        while True:
            try:
                client = lark.ws.Client(app_id, app_secret,
                                        event_handler=handler,
                                        log_level=lark.LogLevel.WARNING)
                _status['connected'] = True
                _status['started'] = time.strftime('%Y-%m-%d %H:%M:%S')
                _status['error'] = ''
                client.start()            # 阻塞至断开
                _status['connected'] = False
                _status['error'] = '连接断开, 自动重连中'
            except Exception as e:
                _status['connected'] = False
                _status['error'] = str(e)[:200]
            time.sleep(backoff)
            backoff = min(60, backoff * 2)

    threading.Thread(target=run, daemon=True).start()
    return True


if __name__ == '__main__':
    print(json.dumps(public_state(), ensure_ascii=False, indent=1))
