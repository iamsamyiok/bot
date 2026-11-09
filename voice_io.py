# -*- coding: utf-8 -*-
"""语音输入(ASR)与播报(TTS) — 复用管家验证过的链路
ASR: MediaRecorder webm → ffmpeg 转 wav → transcribe-hq(SenseVoice/硅基) → 文本
TTS: edge-tts 中文朗读(winmm MCI 播放, 设置开关)
"""
import base64
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
TRANSCRIBE = os.path.join(os.path.expanduser('~'), '.agents', 'skills',
                          'transcribe-hq', 'transcribe.py')


def asr_webm(b64_data):
    """webm base64 → 文本; 失败返回 (None, err)"""
    try:
        tmp = tempfile.mkdtemp(prefix='butler_asr_')
        webm = os.path.join(tmp, 'in.webm')
        wav = os.path.join(tmp, 'in.wav')
        with open(webm, 'wb') as f:
            f.write(base64.b64decode(b64_data))
        # webm → wav 16k(ffmpeg 统一格式)
        ff = shutil.which('ffmpeg') or 'ffmpeg'
        r = subprocess.run([ff, '-y', '-i', webm, '-ar', '16000', '-ac', '1',
                            wav], capture_output=True, timeout=60)
        if not os.path.exists(wav):
            return None, 'ffmpeg 转换失败: ' + (r.stderr or b'').decode('utf-8', 'replace')[-150:]
        py = shutil.which('python')
        outd = os.path.join(tmp, 'out')
        os.makedirs(outd, exist_ok=True)
        if not (py and os.path.exists(TRANSCRIBE)):
            return None, 'ASR 脚本不可用(transcribe-hq 未装)'
        subprocess.run([py, TRANSCRIBE, wav, '-o', outd],
                       capture_output=True, timeout=180)
        jf = os.path.join(outd, 'transcript.json')
        if os.path.exists(jf):
            text = (json.load(open(jf, encoding='utf-8')).get('text') or '').strip()
            if text:
                return text, None
        return None, '未识别出内容'
    except Exception as e:
        return None, str(e)[:200]


_speak_lock = threading.Lock()


def speak(text):
    """edge-tts 中文播报(阻塞至播完, 调用方放线程); 失败静默"""
    if not text:
        return
    with _speak_lock:
        try:
            import asyncio
            import ctypes
            import edge_tts
            mp3 = os.path.join(tempfile.gettempdir(), 'butler_tts.mp3')
            asyncio.run(edge_tts.Communicate(text[:300],
                        'zh-CN-XiaoxiaoNeural').save(mp3))
            mci = ctypes.windll.winmm.mciSendStringW
            mci('open "%s" type mpegvideo alias btts' % mp3, None, 0, 0)
            mci('play btts', None, 0, 0)
            time.sleep(min(30, 2 + len(text) * 0.15))
            mci('close btts', None, 0, 0)
        except Exception:
            pass


def speak_async(text):
    threading.Thread(target=speak, args=(text,), daemon=True).start()
