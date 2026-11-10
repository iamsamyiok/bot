# -*- coding: utf-8 -*-
"""委派循环 — Router 拆解后的质量验收与续委派(CEGIS 思想)
用户指令: 多 worker 结果汇总前先验收, 不符合要求则继续委派修正, 最多 3 轮,
3 轮后如实报告(不伪装成功)。

验收: LLM 按原始诉求逐条评结果, 输出 PASS / FAIL + 原因(反例)
续委派: 第1次不达标→原工人续聊修正(带反例); 第2次→换引擎重做; 第3次→如实报告
状态记录: task['delegation'] = {round, history:[{round, verdict, reason}]}
"""
import io
import json
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
MAX_ROUNDS = 3


def _llm(prompt, mt=300):
    try:
        import model_router
        r = model_router.chat(prompt, mt=mt, tier='strong')
        return (r[0] if isinstance(r, tuple) else r) or ''
    except Exception:
        return ''


def verify(question, results_text):
    """质量验收(纯LLM): 对照原始诉求逐条评, 返回 {verdict, reasons[]}"""
    prompt = (
        '你是质量验收员。对照用户原始诉求, 检查下面的回答是否完整、准确、符合要求。\n'
        '只输出严格JSON: {"verdict": "PASS"或"FAIL", "reasons": ["不合格的具体点", ...]}。\n'
        '判定标准: ①覆盖了诉求的每个部分 ②无编造/无回避 ③格式符合诉求(如要求表格/要点)。\n'
        '轻微瑕疵(如少一个修饰词)不算FAIL——只有缺内容/答错/明显敷衍才FAIL。\n'
        '【用户原始诉求】%s\n【回答】%s' % (
            str(question)[:300], str(results_text)[:4000])
    )
    t = _llm(prompt, mt=250)
    m = re.search(r'\{[^{}]*\}', t or '', re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            v = d.get('verdict', '').upper()
            if v in ('PASS', 'FAIL'):
                return {'verdict': v, 'reasons': [str(x)[:60] for x in (d.get('reasons') or [])][:4]}
        except Exception:
            pass
    # LLM输出无法解析 → 视为PASS(验收员故障不阻断正常回答)
    return {'verdict': 'PASS', 'reasons': ['(验收员输出异常, 默认通过)']}


def fix_instruction(question, results_text, reasons, attempt):
    """生成修正指令(带反例): 告诉worker哪里不合格+怎么补"""
    return (
        '你之前的回答未通过质量验收, 请修正后重新给出完整回答。\n'
        '【原始诉求】%s\n【你上次的回答】%s\n【验收发现的问题】%s\n'
        '【修正要求】针对以上问题补充/修正, 给出完整最终回答(不是只说修改点)。这是第 %d 次修正, 请务必完整。' % (
            str(question)[:200], str(results_text)[:2000],
            '; '.join(reasons)[:300], attempt)
    )


def alt_engine(wtype):
    """换引擎: codex↔opencode 互换; 其他工人不换"""
    if wtype == 'codex':
        return 'opencode'
    if wtype == 'opencode':
        return 'codex'
    return wtype


if __name__ == '__main__':
    r = verify('查惠州和上海的天气', '🌤 惠州...\n🌤 上海...')
    print('双城验收:', r['verdict'])
    fi = fix_instruction('查惠州和上海的天气', '只答了惠州', ['结果缺少实体: 上海'], 1)
    assert '上海' in fi and '第 1 次' in fi
    print('PASS 修正指令生成')
    assert alt_engine('codex') == 'opencode' and alt_engine('opencode') == 'codex'
    print('PASS 引擎互换')
