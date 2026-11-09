# -*- coding: utf-8 -*-
"""全局抑制子进程控制台窗口。

宿主为 pythonw(无控制台)时, 任何 console 子进程(codex/ffmpeg/日志/query session)
都会被 Windows 分配新的控制台 → 弹出黑窗。本模块在进程内给所有 subprocess.Popen
默认注入 CREATE_NO_WINDOW, 一次导入全进程生效。
"""
import os
import subprocess

if os.name == 'nt' and not getattr(subprocess, '_butler_nowin', False):
    _orig_init = subprocess.Popen.__init__

    def _nowin_init(self, *args, **kwargs):
        kwargs.setdefault('creationflags', subprocess.CREATE_NO_WINDOW)
        try:
            _orig_init(self, *args, **kwargs)
        except Exception:
            kwargs['creationflags'] = 0
            _orig_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _nowin_init
    subprocess._butler_nowin = True
