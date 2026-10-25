#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
who_called.py — 查 phone_log.jsonl 里某条操作是谁调的。

## 为什么有这个文件

同一个 phone.py 被多个调用方共用、写同一个日志文件时，记录里没有署名。
事后出现「那次 screenshot 到底是谁调的」这种问题，只能靠时间戳猜 ——
而猜是会错的，实测错过两次。

现在 phone.py 写的日志带 caller 字段了，但这个工具还要处理**那之前的历史记录**：
早期条目没有 caller，工具会明确标成 `unknown(no-field)`，
而不是像人一样猜一个答案。

一个看起来确定的错误答案，比一个明确的「不知道」更有害 ——
前者会让人停止怀疑。

用法：
    python who_called.py                    # 按 caller 汇总
    python who_called.py --after 09:00      # 只看今天 09:00 之后
    python who_called.py --action look      # 只看某个动作
    python who_called.py --detail           # 逐条列出
"""

import argparse
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import utf8_console  # noqa: F401
except Exception:
    pass

WORKSPACE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "workspace")
LOG = os.path.join(WORKSPACE, "phone_log.jsonl")

# caller 字段是什么时候开始有的。之前的记录一律 unknown(no-field)。
# 这个时间点对每个部署都不同：第一次启用带 caller 的版本那天。
# 留空表示「不要断言历史」，所有缺字段的都标 unknown(no-field)。
CALLER_FIELD_SINCE = os.environ.get("PHONE_CALLER_SINCE", "")


def load():
    rows = []
    if not os.path.exists(LOG):
        return rows
    with open(LOG, "r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                # 不要静默跳过 —— 吞掉一行可能吞掉整段时间的证据
                print(f"[warn] line {i} failed to parse, skipped", file=sys.stderr)
                continue
            rows.append(d)
    return rows


def label(r):
    c = r.get("caller")
    if c:
        src = r.get("caller_source") or ""
        # 环境变量 PHONE_CALLER 是**自报**的，谁都能设。
        #   必须让它显形，否则这个字段会给人「已经查清」的错觉。
        if src.startswith("env(conflicts"):
            # 自报与父进程链不一致 —— 这本身就是一条证据，别合并掉
            return f"{c}[declared·ppid-mismatch]"
        if src.startswith("env"):
            return c + "(declared)"
        return c
    if CALLER_FIELD_SINCE and r.get("ts", "") < CALLER_FIELD_SINCE:
        return "unknown(no-field)"
    return "unknown"


def summarize(r):
    a = r.get("action", "?")
    d = r.get("detail") or {}
    if a == "text":
        return f"\"{str(d.get('text', ''))[:40]}\""
    if a in ("tap",):
        return f"({d.get('x')},{d.get('y')})"
    if a == "swipe":
        return f"{d.get('from')} -> {d.get('to')}"
    if a == "look":
        return f"nodes={d.get('count')}"
    if a == "key":
        return str(d.get("key"))
    if a == "open":
        return str(d.get("target"))
    if a == "screenshot":
        return "shot"
    if a == "shell":
        return str(d.get("cmd"))[:50]
    return str(d)[:50]


def main():
    ap = argparse.ArgumentParser(description="inspect who triggered each phone action")
    ap.add_argument("--after", default=None,
                    help="only entries at or after this HH:MM")
    ap.add_argument("--action", default=None, help="only this action")
    ap.add_argument("--caller", default=None, help="only this caller label")
    ap.add_argument("--detail", action="store_true", help="list every entry")
    a = ap.parse_args()

    rows = load()
    if a.after:
        rows = [r for r in rows if r.get("ts", "")[11:16] >= a.after]
    if a.action:
        rows = [r for r in rows if r.get("action") == a.action]
    if a.caller:
        rows = [r for r in rows if label(r) == a.caller]

    if not rows:
        print("no matching entries.")
        return 0

    if a.detail:
        for r in rows:
            print(f"{r.get('ts')}  {label(r):18} {r.get('action', ''):11} {summarize(r)}")
        return 0

    c = Counter(label(r) for r in rows)
    print(f"{len(rows)} entries")
    for k, v in c.most_common():
        print(f"  {k:20} {v}")

    unknown = [r for r in rows if label(r) in ("unknown(no-field)", "unknown")]
    if unknown:
        print()
        print(f"WARNING: {len(unknown)} entries have no usable caller field.")
        print("   These records **cannot** prove who triggered them - do not guess.")
        print("   To narrow it down you need external evidence (message times,")
        print("   what the screenshots show), and even then it stays an inference.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
