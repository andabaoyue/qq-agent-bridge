#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""让 Windows 控制台按 UTF-8 输出。被其它脚本 import 时自动生效。"""
import sys


def _patch():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    # Windows 下把控制台代码页切成 65001，防止管道/子进程再乱码
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass


_patch()
