# -*- coding: utf-8 -*-
"""
send.py — OneBot v11 发送层 + 小黄脸名字解析

## 它保证什么

任何往 OneBot 发消息的脚本，自动获得这些行为：

1. `[偷笑]` `[得意]` `[怒]` 这类方括号里的表情名 → 真正的 `[CQ:face,id=N]`
2. 常用别名 → 官方 id（`[旺柴]`→277、`[傲娇]`→231、`[怒]`→11）
3. `[CQ:reply,id=N]` 前缀正确拼接
4. 连续发送之间有人类式的停顿（避开腾讯 `result:120` 风控）
5. 当模块 import 或当脚本跑，两种用法都行

## 返回约定
所有 `send_*` 返回 `SendResult`（dict 的子类），**两种写法都对**：
    r = send_private_msg(...); r["status"]          # 当 dict
    ok, res = send_private_msg(...)                 # 当元组
    r.ok                                            # 语义化的成功判据

## 🔴 铁律：发送失败一律不重试
`ok is False` 只代表"没拿到明确回执"，**不代表没发出去**。
命令退出码非 0 同理（异常常常发生在发送之后的调用方代码里）。

重试的后果是重复投递。真实事故：同一张 GIF 因为调用方按元组解包一个
返回 dict 的函数、抛了 ValueError 被误判成"没发出去"，结果发了两次。
是否真送达只能让真人确认。
"""

import sys
import os
import re
import json
import time
import random
import urllib.request
import urllib.error

# Ensure UTF-8 output on Windows terminal
if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

ONEBOT_BASE_URL = "http://127.0.0.1:3000"

# Alias table: common shorthand/nicknames -> official QQ face ID.
# NOTE: qface_map.json (official names) is checked FIRST, so an alias here can
# never override an official mapping. That is why doge/生气/赞/大哭 now resolve
# to their real official ids (179/326/76/9) instead of a nearby guess.
FACE_ALIASES = {
    # 狗头 / 旺柴 系（官方 "doge" 是 179，这些是没有官方名的昵称）
    "旺柴": 277, "汪汪": 277, "狗头": 277,
    # 情绪衍生（官方表里没有同名项，才放这里）
    "傲娇": 231, "哼": 231,
    "怒": 11, "发怒": 11, "愤怒": 11,
    "哭": 5, "流泪": 5,
    "问号": 32, "疑惑": 32, "疑问": 32,
    "滑稽": 178, "斜眼笑": 178,
    "哈哈": 193, "大笑": 193,
    "吐血": 265,
}

# Auto-build full name -> face ID dictionary from qface_map.json
# 位置：bridge/ 或 workspace/ 下都找。找不到只是不发小黄脸，不影响发消息。
_HERE = os.path.dirname(os.path.abspath(__file__))
QFACE_MAP_CANDIDATES = [
    os.path.join(_HERE, "qface_map.json"),
    os.path.join(os.path.dirname(_HERE), "workspace", "qface_map.json"),
    os.path.join(_HERE, "..", "workspace", "qface_map.json"),
]

FACE_NAME_TO_ID = {}

def _load_face_map():
    global FACE_NAME_TO_ID
    for path in QFACE_MAP_CANDIDATES:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                FACE_NAME_TO_ID = {
                    k: v for k, v in raw.items()
                    if isinstance(v, int) and not k.isdigit()
                }
                return
            except Exception as e:
                pass

_load_face_map()

# Tokens in square brackets that are media or structural, NEVER to be stripped
NON_FACE_TOKENS = {"图片", "语音", "视频", "文件", "链接", "表情", "动画表情"}

# Tokens that are known to be fake face placeholders (not real QQ faces) and
# should be removed rather than sent as literal brackets. Kept explicit so that
# ordinary bracketed words are never silently eaten.
KNOWN_FAKE_FACES = {
    "打call", "傲娇", "旺柴", "狗头", "doge", "汪汪", "滑稽", "斜眼笑",
    "吐血", "问号", "疑惑", "疑问", "哈哈", "大笑", "怒", "发怒", "愤怒",
    "哭", "流泪", "哼",
}

_BRACKET_RE = re.compile(r"\[([^\[\]\r\n]{1,15})\]")

def format_qq_faces(text):
    """
    Format string containing [表情] into genuine OneBot [CQ:face,id=N].
    Unknown bracketed words are left untouched; only known fake face
    placeholders are dropped.
    """
    if not isinstance(text, str):
        return text

    def _sub(m):
        token = m.group(1)
        # Preserve CQ codes, media tags, pure digits
        if token.startswith("CQ:") or token in NON_FACE_TOKENS or token.isdigit():
            return m.group(0)

        # 1. Official qface_map FIRST, so aliases can never override it.
        if token in FACE_NAME_TO_ID:
            return f"[CQ:face,id={FACE_NAME_TO_ID[token]}]"

        # 2. Then the alias table for nicknames the official map lacks.
        if token in FACE_ALIASES:
            return f"[CQ:face,id={FACE_ALIASES[token]}]"

        # 3. Only strip tokens that are KNOWN fake face placeholders.
        #    Never strip ordinary words like [颜色]/[褪色] just because they
        #    are Chinese inside brackets.
        if token in KNOWN_FAKE_FACES:
            return ""

        return m.group(0)

    return _BRACKET_RE.sub(_sub, text)


class SendResult(dict):
    """OneBot 返回值。**同时兼容两种取值写法**，这样调用方怎么写都不会炸。

    ## 为什么要有这个类（2026-10-23 事故）
    见下方 `_call_api` 的说明。简单说：
    历史上这个模块返回单个 dict，而 `qq_step.send_onebot_request` 返回
    `(bool, res)` 元组。有人按元组写法写成 `ok, res = qq_send.send_private_msg(...)`
    → 消息**已经发出去了**，却在解包那一行抛 ValueError → 命令退出码 1
    → 被误判成"没发出去" → 重发一次 → 同一条消息发出两遍。

    所以这里让返回值两种解包都对：

        r = send_private_msg(...)          # ① 当 dict 用
        r["status"], r.get("data")         #    ✅

        ok, res = send_private_msg(...)    # ② 当元组解包
        # ok  = status == "ok" 且 retcode == 0
        # res = 这个 dict 本身          ✅

    实现方式是重写 `__iter__`：只在**解包/迭代**时吐出那两个值，
    `[]` / `.get()` / `json.dumps()` / `isinstance(x, dict)` 全部照旧。
    """

    @property
    def ok(self):
        """是否真的被 OneBot 受理（status ok 且 retcode 0）。"""
        return self.get("status") == "ok" and self.get("retcode") == 0

    def __iter__(self):
        # 注意：这会让 `for k in r` 只迭代两个元素（ok 与自身），
        # 而不是 key。本模块内没有任何地方那样迭代 dict，安全。
        return iter((self.ok, self))


def _call_api(endpoint, payload, onebot_url=ONEBOT_BASE_URL):
    """POST 到 OneBot。返回 `SendResult`（dict 子类，可解包成 (ok, res)）。

    ## ⚠️ 调用方必读（2026-10-23 复盘）
    这个函数**只在 HTTP 请求完全失败时**才返回 error。也就是说：

    - 返回值是 error ≠ 没发出去（可能是超时，但服务端已经收了）
    - **抛异常/退出码非 0 ≠ 没发出去**（这里根本没有"发一半"的异常，
      真正的异常通常发生在调用方自己后续的代码里，图已经上路了）

    **所以：任何失败都不要自动重试。** 重试 = 大概率重复投递。
    要确认到底发没发出去，只能让真人看（见 skill `onebot-send-result`）。
    """
    url = f"{onebot_url.rstrip('/')}/{endpoint.lstrip('/')}"
    try:
        data_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data_bytes,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
            return SendResult(raw if isinstance(raw, dict) else {"raw": raw})
    except Exception as e:
        return SendResult({"status": "error", "message": str(e),
                           "retcode": -1, "data": None})


def send_group_msg(group_id, message, reply_id=None, raw=False, onebot_url=ONEBOT_BASE_URL):
    """
    Send group message with automatic face formatting and optional reply quoting.

    返回 `SendResult`：既能当 dict 读，也能 `ok, res = ...` 解包。
    **失败不要重试** —— 返回值不可靠，重试等于重复投递（见 `_call_api` 注释）。
    """
    msg_str = message if raw else format_qq_faces(message)
    if reply_id:
        msg_str = f"[CQ:reply,id={int(reply_id)}]{msg_str}"

    payload = {
        "group_id": int(group_id),
        "message": msg_str
    }
    return _call_api("send_group_msg", payload, onebot_url)


def send_private_msg(user_id, message, reply_id=None, raw=False, onebot_url=ONEBOT_BASE_URL):
    """
    Send private message with automatic face formatting and optional reply quoting.

    返回 `SendResult`：既能当 dict 读，也能 `ok, res = ...` 解包。
    **失败不要重试** —— 返回值不可靠，重试等于重复投递（见 `_call_api` 注释）。
    """
    msg_str = message if raw else format_qq_faces(message)
    if reply_id:
        msg_str = f"[CQ:reply,id={int(reply_id)}]{msg_str}"

    payload = {
        "user_id": int(user_id),
        "message": msg_str
    }
    return _call_api("send_private_msg", payload, onebot_url)


def send_image(target_id, image_path, is_group=True, reply_id=None, prefix_text="", onebot_url=ONEBOT_BASE_URL):
    """
    Send a local image file via file:/// URI with optional text and reply quoting.
    """
    clean_path = os.path.abspath(image_path).replace("\\", "/")
    img_cq = f"[CQ:image,file=file:///{clean_path}]"
    text = (format_qq_faces(prefix_text) + "\n" + img_cq) if prefix_text else img_cq
    if is_group:
        return send_group_msg(target_id, text, reply_id=reply_id, raw=True, onebot_url=onebot_url)
    else:
        return send_private_msg(target_id, text, reply_id=reply_id, raw=True, onebot_url=onebot_url)


def set_msg_emoji_like(message_id, emoji_id="124", onebot_url=ONEBOT_BASE_URL):
    """
    React to a message with an emoji sticker (Pillar 2 & 3).
    Common emoji IDs: '124' (OK 👌), '178' (Salute 🫡), '76' (Thumbs up 👍).
    """
    payload = {"message_id": int(message_id), "emoji_id": str(emoji_id)}
    return _call_api("set_msg_emoji_like", payload, onebot_url)


def send_batch_replies(replies, delay_min=2.0, delay_max=3.5, onebot_url=ONEBOT_BASE_URL):
    """
    Pillar 3: Send multiple replies sequentially with humanized typing delays.
    replies: list of dicts:
      [
        {'text': '...', 'reply_id': 12345, 'group_id': 111222333, 'user_id': None},
        ...
      ]
    """
    results = []
    for i, item in enumerate(replies):
        if i > 0:
            delay = random.uniform(delay_min, delay_max)
            time.sleep(delay)

        group_id = item.get("group_id")
        user_id = item.get("user_id")
        text = item.get("text", "")
        reply_id = item.get("reply_id")
        raw = item.get("raw", False)

        if group_id:
            res = send_group_msg(group_id, text, reply_id=reply_id, raw=raw, onebot_url=onebot_url)
        elif user_id:
            res = send_private_msg(user_id, text, reply_id=reply_id, raw=raw, onebot_url=onebot_url)
        else:
            res = {"status": "error", "message": "No group_id or user_id provided"}

        results.append(res)
    return results


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Send formatted message to QQ via OneBot")
    parser.add_argument("--group", type=int, help="Target group ID")
    parser.add_argument("--user", type=int, help="Target user ID")
    parser.add_argument("--reply", type=int, help="Target message ID to reply to")
    parser.add_argument("--msg", type=str, required=True, help="Message text")
    args = parser.parse_args()

    if args.group:
        res = send_group_msg(args.group, args.msg, reply_id=args.reply)
        print("Group Result:", res)
    elif args.user:
        res = send_private_msg(args.user, args.msg, reply_id=args.reply)
        print("Private Result:", res)
    else:
        print("Error: Specify either --group or --user")
