# -*- coding: utf-8 -*-
"""免费模型路由层 — 三家免费池组合, 速度/质量横评定稿 (2026-10-29 实测)

文本链 (意图/闲聊/汇报, 横评两轮全对):
  1. 硅基流动 Qwen2.5-7B-Instruct 免费   ~330-370ms  最快
  2. OpenRouter recursive/flint-v1.0:free ~1.5-3s    稳定
  3. 魔搭 Qwen3.8-Flash-Next (2000次/天)   ~2s
  注: openrouter/free 自动路由不可控(曾路由到2.6B小模型答错), 不做主力
视觉链 (用户指定 Agnes 2.5 flash):
  1. AGNES_API_KEY (RPM限流时隔10s重试一次)
  2. AGNES_KEY_FREE
  3. omni 视觉兜底 (engine层)
语音链: 百炼 qwen3.5-omni-plus — "必要"场景 (三家免费池均无语音模型)
坑: Agnes 突发调用会报"请实名认证以获取更高rpm"实为RPM限流, 间隔即可
"""
import json, base64, subprocess, os, time

import config
TMP = os.environ.get('TEMP') or os.environ.get('TMP') or '.'   # 便携: 跟随目标机临时目录

SF_EP = 'https://api.siliconflow.cn/v1/chat/completions'
SF_MODEL = 'Qwen/Qwen2.5-7B-Instruct'
OR_EP = 'https://openrouter.ai/api/v1/chat/completions'
OR_MODEL = 'recursive/flint-v1.0:free'
MS_EP = 'https://api-inference.modelscope.cn/v1/chat/completions'
MS_MODEL = 'Qwen/Qwen3.8-Flash-Next'
MS_STRONG = 'ZhipuAI/GLM-5.2'   # 免费档强模型(2026-11-07实测 0.96s, 比旧7B还快)

# Responsible API 设计(用户指令): 用户在 config/bot.json 的 providers.llm_chain 里
# 自行填写 base_url / model / api_key_env —— bot 不内置任何供应商偏好, 任何
# OpenAI 兼容端点(含本地 Ollama)都能接。api_key_env 指向 keys 段或环境变量。
# 下面 DEFAULT_* 仅是 bot.json 缺失时的开箱示例(全部免费档)。
FAST_CHAIN = [
    ('siliconflow', SF_EP, 'SILICONFLOW_API_KEY', SF_MODEL),
    ('openrouter', OR_EP, 'OPENROUTER_KEY_1', OR_MODEL),
    ('modelscope', MS_EP, 'MODELSCOPE_API_TOKEN', MS_MODEL),
]
STRONG_CHAIN = FAST_CHAIN


def _chain_from_config():
    """bot.json providers.llm_chain → [(name, ep, keyname, model)]"""
    import config as _cfg
    raw = _cfg._load()
    chain = (raw.get('providers') or {}).get('llm_chain') or []
    out = []
    for p in chain:
        out.append((p.get('name', p.get('base_url', '?')[:20]),
                    p.get('base_url', ''), p.get('api_key_env', ''),
                    p.get('model', '')))
    return out


def _chains():
    cfg_chain = _chain_from_config()
    if cfg_chain:
        # strong链=同一链(用户配置已是自己选的模型); fast链=同链(简化, 单链双tier)
        return cfg_chain, cfg_chain
    return FAST_CHAIN, STRONG_CHAIN


def _key(name):
    return config.KEY(name)


def _post(ep, tok, body, timeout=60):
    bp = os.path.join(TMP, 'router_body_%d.json' % os.getpid())
    open(bp, 'w', encoding='utf-8').write(json.dumps(body))
    r = subprocess.run(['curl', '-sS', '--ssl-no-revoke', '--max-time', str(timeout),
                        '-X', 'POST', ep,
                        '-H', 'Authorization: Bearer ' + tok,
                        '-H', 'Content-Type: application/json',
                        '--data-binary', '@' + bp],
                       capture_output=True, timeout=timeout + 15)
    return json.loads(r.stdout.decode('utf-8', 'replace'))


def _extract(d):
    """OpenAI 兼容响应 -> (content, err)"""
    try:
        if d.get('error') or d.get('code'):
            return None, str(d.get('error') or d.get('code'))[:120]
        c = d['choices'][0]['message'].get('content')
        return (c or '').strip(), None
    except Exception:
        return None, '响应结构异常'


def chat(prompt, mt=500, system=None, tier='fast'):
    """文本双档降级链(tier='fast'|'strong'), 返回 (text, provider)
    全链免费模型; strong 用于直答/总结, fast 用于分类/提炼"""
    msgs = []
    if system:
        msgs.append({'role': 'system', 'content': system})
    msgs.append({'role': 'user', 'content': prompt})
    fast_c, strong_c = _chains()
    chain = (strong_c if tier == 'strong' else fast_c)
    last_err = ''
    for name, ep, keyname, model in chain:
        tok = _key(keyname)
        if not tok:
            last_err = name + ':无key'
            continue
        try:
            payload = {'model': model, 'messages': msgs, 'max_tokens': mt}
            if ep == MS_EP:
                # 魔搭思考型模型(GLM-5.2等)默认<think>吃掉全部token, 正文只剩
                # 首字(finish=length)——实测必须显式关闭思考(2026-11-07)
                payload['enable_thinking'] = False
                payload['max_tokens'] = max(mt, 800)
            d = _post(ep, tok, payload)
            text, err = _extract(d)
            if text:
                return text, name
            last_err = '%s:%s' % (name, err)
        except Exception as e:
            last_err = '%s:%s' % (name, str(e)[:60])
    return None, last_err


def vision(img_path, question, retry_wait=10):
    """视觉: Agnes 2.5 flash (RPM限流重试+免费钥匙), 返回 (text, provider)"""
    b64 = base64.b64encode(open(img_path, 'rb').read()).decode()
    content = [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + b64}},
               {'type': 'text', 'text': question}]
    base = _key('AGNES_BASE_URL').rstrip('/')
    keys = [('agnes主', _key('AGNES_API_KEY'), False),
            ('agnes主重试', _key('AGNES_API_KEY'), True),
            ('agnes免费', _key('AGNES_KEY_FREE'), False)]
    last_err = ''
    for name, tok, do_wait in keys:
        if not tok:
            continue
        if do_wait:
            time.sleep(retry_wait)  # RPM限流退避
        try:
            d = _post(base + '/chat/completions', tok,
                      {'model': _key('AGNES_MODEL'), 'max_tokens': 300,
                       'messages': [{'role': 'user', 'content': content}]}, timeout=90)
            text, err = _extract(d)
            if text:
                return text, name
            last_err = '%s:%s' % (name, err)
        except Exception as e:
            last_err = '%s:%s' % (name, str(e)[:60])
    return None, last_err


if __name__ == '__main__':
    t0 = time.time()
    t, p = chat('回复OK两个字母')
    print('文本链: %s (%s) %.0fms' % (t, p, (time.time() - t0) * 1000))
    t, p = vision(r'C:/Tools/UFO/logs/omni_task/action_step1.png', '截图里打开了什么应用? 一句话')
    print('视觉链: %s (%s) %.0fms' % (t, p, (time.time() - t0) * 1000))
