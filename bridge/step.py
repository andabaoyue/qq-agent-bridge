# -*- coding: utf-8 -*-
"""
step.py — Agent 侧的「一次动作一条命令」CLI

## 它解决什么

Agent（大模型）不能自己常驻等消息：每次「醒来」都是一整个请求的 token。
所以这里做两件事：

    1. 发东西（文字 / 图 / 文件 / 语音）
    2. 静默长轮询等消息，空闲时**零 token**

一条命令只做一件事，参数越少越好，输出对人（和模型）都易读。

## 用法

    python step.py --reply "你好"                  # 回复上一个会话
    python step.py --reply "你好" --group_id 123   # 指定群
    python step.py --reply "你好" --user_id 456    # 指定私聊
    python step.py --image a.png --group_id 123    # 发图
    python step.py --upload-file a.zip --user_id 456
    python step.py --reply-id 789 --emoji-like 124 # 给某条消息贴表情回应

    python step.py --reply "好" --send-only        # 发完就退出，不进入等待
    python step.py                                  # 只等消息，不发任何东西

## 🔴 最重要的约定：发送失败一律不重试

`ok is False` 只代表"没拿到明确回执"，**不代表没发出去**。
命令退出码非 0 同理 —— 异常常常发生在发送**之后**的调用方代码里。

重试的后果是重复投递。真实事故：同一张 GIF 因为调用方按元组解包
抛了 ValueError、被误判成"没发出去"，于是重发，群里看到两遍。

判读细节见 send.py 的 `_call_api` 注释。

## 消息长什么样

`--reply` 发送后会进入等待。等到消息时打印结构化文本：

    ==================== [QQ_MESSAGE_START] ====================
    Role:    [identity: owner (full permission)] (Permission: owner)
    Type:    private
    Sender:  某人 (QQ: 123)
    Msg ID:  456
    Time:    2026-01-01 12:00:00
    Content: 说了什么
    ==================== [QQ_MESSAGE_END] ====================

后面的 RAW JSON 是完整字段（含 image_files / links / desktop_allowed），
需要看图、看链接、看许可时读它。
"""

import sys
import os
import re
import json
import time
import argparse
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Ensure UTF-8 output on Windows terminal
if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

import config as _config  # noqa: E402

CFG = _config.load()
WORKSPACE = _config.workspace_of(CFG, __file__)

LAST_TARGET_FILE = os.path.join(WORKSPACE, "last_target.json")
BOT_QQ = CFG["bot_qq"]

DEFAULT_ONEBOT = CFG.get("onebot_api") or "http://127.0.0.1:3000"
_L = CFG["listen"]
DEFAULT_DAEMON = "http://%s:%s" % (
    "127.0.0.1" if _L["poll_host"] in ("0.0.0.0", "") else _L["poll_host"],
    _L["poll_port"])


# ------------------------------------------------------------------ 目标解析
def get_last_target():
    if os.path.exists(LAST_TARGET_FILE):
        try:
            with open(LAST_TARGET_FILE, "r", encoding="utf-8-sig") as f:
                return json.load(f)
        except Exception:
            pass
    return None


def resolve_target(user_id=None, group_id=None):
    """没指定目标时，用「上一条收到的消息来自哪里」。

    为什么要这个：Agent 的典型循环是「收到消息 → 回复」。如果每条回复
    都要它自己从上下文里抄一遍群号，抄错一次就发到别人群里去了。
    """
    if not user_id and not group_id:
        last = get_last_target()
        if last:
            if last.get("message_type") == "group" and last.get("group_id"):
                group_id = last.get("group_id")
            elif last.get("user_id"):
                user_id = last.get("user_id")
    return user_id, group_id


def send_onebot_request(endpoint, payload, onebot_url=DEFAULT_ONEBOT):
    url = f"{onebot_url.rstrip('/')}/{endpoint.lstrip('/')}"
    data_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data_bytes,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            res = json.loads(resp.read().decode("utf-8"))
            return True, res
    except urllib.error.URLError as e:
        return False, f"URLError: {e.reason}"
    except Exception as e:
        return False, f"Exception: {e}"


# ------------------------------------------------------------------ 表情名
# 小黄脸的名字 -> id。qq_send.py 是唯一实现，这里只做转发，
# 避免两张表各自漂移（曾经漂移过，同一个词在两个脚本里发不同的脸）。
_QFACE_CANDIDATES = [
    os.path.join(WORKSPACE, "qface_map.json"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "qface_map.json"),
]
FACE_NAME_TO_ID = {}


def _load_face_map():
    for candidate in _QFACE_CANDIDATES:
        try:
            with open(candidate, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            continue
        return {k: v for k, v in raw.items()
                if isinstance(v, int) and not k.isdigit()}
    print("[FACE_MAP_WARN] qface_map.json not found; [表情名] will not resolve.")
    return {}


FACE_NAME_TO_ID = _load_face_map()

try:
    import send as _send_mod
except Exception:            # pragma: no cover
    _send_mod = None


def format_qq_faces(text):
    """把 [表情名] 换成正真的 [CQ:face,id=N]。实现只有一份，在 send.py。"""
    if not isinstance(text, str):
        return text
    if _send_mod is not None:
        return _send_mod.format_qq_faces(text)
    return text


# ------------------------------------------------------------------ 发送
def set_msg_emoji_like(message_id, emoji_id="124", onebot_url=DEFAULT_ONEBOT):
    """给某条消息贴表情回应。124=OK，178=Salute，76=赞。"""
    payload = {"message_id": int(message_id), "emoji_id": str(emoji_id)}
    ok, res = send_onebot_request("set_msg_emoji_like", payload, onebot_url)
    if ok and isinstance(res, dict) and res.get("status") == "ok":
        print(f"[EMOJI_OK] Reacted to message {message_id} with emoji {emoji_id}")
        return True
    print(f"[EMOJI_FAIL] Failed emoji reaction on {message_id}: {res}")
    return False


def send_text_message(text, user_id=None, group_id=None, reply_id=None,
                      onebot_url=DEFAULT_ONEBOT, raw=False):
    user_id, group_id = resolve_target(user_id, group_id)
    msg = text if raw else format_qq_faces(text)
    if reply_id:
        msg = f"[CQ:reply,id={int(reply_id)}]{msg}"

    if group_id:
        ok, res = send_onebot_request(
            "send_group_msg", {"group_id": int(group_id), "message": msg},
            onebot_url)
        target_desc = f"group {group_id}"
    elif user_id:
        ok, res = send_onebot_request(
            "send_private_msg", {"user_id": int(user_id), "message": msg},
            onebot_url)
        target_desc = f"private {user_id}"
    else:
        print("[SEND_ERR] Need --user_id or --group_id (or a previous message to reply to)")
        return False

    if ok and isinstance(res, dict) and res.get("status") == "ok":
        print(f"[SEND_OK] {target_desc}")
        return True

    # 🔴 这里**只报告，不重试**。见文件头的说明。
    print(f"[SEND_FAIL] {target_desc} -> {res}")
    print("[SEND_NOTE] 失败不代表没发出去，重试可能导致重复投递。"
          "要确认请让真人看一眼。")
    return False


def send_batch_replies(replies, delay_min=2.0, delay_max=3.5,
                       onebot_url=DEFAULT_ONEBOT):
    """连续发多条，中间加人类式的打字停顿。

    为什么要有停顿：机器人一次把三句话瞬间发完，在群里一眼就认出来。
    真人是一句一句打的。
    """
    import random
    results = []
    for i, item in enumerate(replies):
        if i > 0:
            time.sleep(random.uniform(delay_min, delay_max))
        group_id = item.get("group_id")
        user_id = item.get("user_id")
        if group_id:
            results.append(send_text_message(
                item.get("text", ""), group_id=group_id,
                reply_id=item.get("reply_id"), onebot_url=onebot_url,
                raw=item.get("raw", False)))
        elif user_id:
            results.append(send_text_message(
                item.get("text", ""), user_id=user_id,
                reply_id=item.get("reply_id"), onebot_url=onebot_url,
                raw=item.get("raw", False)))
        else:
            print("[SEND_ERR] item has neither group_id nor user_id")
            results.append(False)
    return results


def upload_file(file_path, user_id=None, group_id=None, onebot_url=DEFAULT_ONEBOT):
    user_id, group_id = resolve_target(user_id, group_id)
    if not os.path.exists(file_path):
        print(f"[UPLOAD_ERR] File not found: {file_path}")
        return False

    abs_path = os.path.abspath(file_path)
    if group_id:
        endpoint = "upload_group_file"
        payload = {"group_id": int(group_id), "file": abs_path,
                   "name": os.path.basename(file_path)}
        target_desc = f"group {group_id}"
    elif user_id:
        endpoint = "upload_private_file"
        payload = {"user_id": int(user_id), "file": abs_path,
                   "name": os.path.basename(file_path)}
        target_desc = f"private {user_id}"
    else:
        print("[UPLOAD_ERR] Need --user_id or --group_id")
        return False

    ok, res = send_onebot_request(endpoint, payload, onebot_url)
    if ok and isinstance(res, dict) and res.get("status") == "ok":
        print(f"[UPLOAD_OK] {os.path.basename(file_path)} -> {target_desc}")
        return True
    print(f"[UPLOAD_FAIL] {target_desc} -> {res}")
    return False


def send_image(image_path, user_id=None, group_id=None, onebot_url=DEFAULT_ONEBOT):
    if not os.path.exists(image_path):
        print(f"[IMAGE_ERR] Image file not found: {image_path}")
        return False
    abs_path = os.path.abspath(image_path).replace("\\", "/")
    cq_code = f"[CQ:image,file=file:///{abs_path}]"
    return send_text_message(cq_code, user_id=user_id, group_id=group_id,
                             onebot_url=onebot_url)


# ------------------------------------------------------------------ 语音
def _voice_mod():
    """惰性导入语音合成模块。

    为什么惰性：语音是可选增强（需要 edge-tts + 一个转 silk 的原生模块）。
    没有装的时候，其它所有功能都必须照常能用 —— 一个可选的增强
    不该让主路径起不来。
    """
    try:
        if WORKSPACE not in sys.path:
            sys.path.insert(0, WORKSPACE)
        import voice_tts
        return voice_tts
    except Exception as e:
        print(f"[VOICE_ERR] voice synthesis module unavailable: {e}")
        print("[VOICE_ERR] 发语音是可选项，其它功能不受影响。")
        return None


def send_voice_text(text, user_id=None, group_id=None,
                    onebot_url=DEFAULT_ONEBOT,
                    voice=None, rate="+0%", dry_run=False):
    """把一段文字**用语音发出去**。

    ## 为什么单独一个入口
    文字/图片/文件都是直接转 CQ 码，语音要经过「合成 → 转 silk」，链路长得多，
    而且失败点很多（网络、原生模块、格式）。所以它自己一条路径，失败就
    明确说失败，**不静默退回发文字** —— 用户以为收到语音、实际收到文字，
    比收不到更糟。

    ## dry_run
    只合成、不发送。调试一律先跑这个：语音发出去收不回来。
    """
    text = (text or "").strip()
    if not text:
        print("[VOICE_ERR] empty text")
        return False

    mod = _voice_mod()
    if mod is None:
        return False

    silk = mod.text_to_silk(text, voice=voice or mod.DEFAULT_VOICE, rate=rate)
    if not silk:
        print("[VOICE_FAIL] synthesis failed; nothing sent")
        return False

    if dry_run:
        print(f"[VOICE_DRY] synthesized {os.path.basename(silk)} "
              f"({os.path.getsize(silk)} bytes); not sending")
        return True

    abs_path = os.path.abspath(silk).replace("\\", "/")
    cq_code = f"[CQ:record,file=file:///{abs_path}]"
    ok = send_text_message(cq_code, user_id=user_id, group_id=group_id,
                           onebot_url=onebot_url)
    if ok:
        print(f"[VOICE_OK] voice message sent ({len(text)} chars)")
    else:
        print("[VOICE_FAIL] voice send failed")
    return ok


# ------------------------------------------------------------------ 截图
def capture_screenshot(output_path=None):
    """截当前屏幕并发出去（Agent 想看自己跑在哪台机器上时用）。

    默认存到 workspace/screen_capture.png —— 不再写死某个人的盘符。
    """
    if not output_path:
        output_path = os.path.join(WORKSPACE, "screen_capture.png")

    try:
        from PIL import ImageGrab
        img = ImageGrab.grab()
        img.save(output_path)
        print(f"[SCREENSHOT_OK] via PIL: {output_path}")
        return output_path
    except Exception:
        pass

    try:
        import mss
        with mss.mss() as sct:
            sct.shot(output=output_path)
        print(f"[SCREENSHOT_OK] via MSS: {output_path}")
        return output_path
    except Exception:
        pass

    print("[SCREENSHOT_WARN] Could not capture the screen in this session "
          "(headless / no permission / locked).")
    return None


def send_file_content(file_path, user_id=None, group_id=None,
                      onebot_url=DEFAULT_ONEBOT):
    """把文件内容当消息发；太长就拆成合并转发（群里的「聊天记录」样式）。"""
    user_id, group_id = resolve_target(user_id, group_id)
    if not os.path.exists(file_path):
        print(f"[REPLY_FILE_ERR] File not found: {file_path}")
        return False

    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()

    file_name = os.path.basename(file_path)

    if len(content) <= 3500:
        msg = f"{file_name}:\n\n```\n{content}\n```"
        return send_text_message(msg, user_id=user_id, group_id=group_id,
                                 onebot_url=onebot_url, raw=True)

    chunk_size = 2800
    chunks = [content[i:i + chunk_size]
              for i in range(0, len(content), chunk_size)]
    nodes = [{
        "type": "node",
        "data": {
            "name": "bot",
            "uin": str(BOT_QQ),
            "content": f"{file_name} "
                       f"({len(content)} chars, {len(chunks)} parts)",
        }
    }]
    for idx, c in enumerate(chunks):
        nodes.append({
            "type": "node",
            "data": {
                "name": "bot",
                "uin": str(BOT_QQ),
                "content": f"// --- Part {idx + 1} / {len(chunks)} ---\n{c}",
            }
        })

    if group_id:
        endpoint = "send_group_forward_msg"
        payload = {"group_id": int(group_id), "messages": nodes}
        target_desc = f"group {group_id}"
    else:
        endpoint = "send_private_forward_msg"
        payload = {"user_id": int(user_id), "messages": nodes}
        target_desc = f"private {user_id}"

    print(f"[FORWARD_SEND] sending {file_name} as merged forward to {target_desc}...")
    ok, res = send_onebot_request(endpoint, payload, onebot_url)
    if ok and isinstance(res, dict) and res.get("status") == "ok":
        print(f"[FORWARD_OK] {file_name} sent")
        return True

    # 合并转发失败 → 退回直接传文件。这不是"重试同一条消息"，
    # 而是换一种形态交付，不会造成重复投递。
    print(f"[FORWARD_FALLBACK] forward failed ({res}); falling back to file upload")
    return upload_file(file_path, user_id=user_id, group_id=group_id,
                       onebot_url=onebot_url)


# ------------------------------------------------------------------ 等待消息
def _print_one(m, index=None, total=None):
    role_tag = m.get("role_tag", "[identity: guest]")
    perm = m.get("permission", "guest")
    if total and total > 1:
        print(f"\n--- [Message {index}/{total}] ---")
    print(f"Role:    {role_tag} (Permission: {perm})")
    print(f"Type:    {m.get('message_type')}")
    print(f"Sender:  {m.get('nickname')} (QQ: {m.get('user_id')})")
    if m.get("group_id"):
        print(f"Group:   {m.get('group_id')}")
    print(f"Msg ID:  {m.get('message_id')}")
    print(f"Time:    {m.get('formatted_time')}")
    print(f"Content: {m.get('text')}")
    if m.get("image_files"):
        print(f"Images:  {', '.join(m['image_files'])}")
    if m.get("has_link"):
        for ln in (m.get("links") or []):
            print(f"Link:    {ln.get('title') or ''} {ln.get('url')}")
    if m.get("throttled"):
        print(f"Note:    throttled (gap={m.get('throttle_gap')}s) — "
              f"delivered anyway, not dropped")


def wait_for_message_infinite(daemon_url=None, poll_interval=30):
    """静默长轮询。空闲时不打印任何东西 = 不产生 token 成本。

    为什么是长轮询而不是让 Agent 反复查询：Agent 每一次「查一下有没有
    新消息」都是一次完整请求。挂在这里的只是一个纯 Python 循环，
    空闲时它的成本是 0。
    """
    daemon_url = daemon_url or DEFAULT_DAEMON
    print(f"[WAITING] silent polling on {daemon_url}/wait_msg "
          f"(zero token cost while idle)")
    sys.stdout.flush()

    while True:
        req_url = f"{daemon_url.rstrip('/')}/wait_msg?timeout={poll_interval}"
        try:
            req = urllib.request.Request(req_url, method="GET")
            with urllib.request.urlopen(req, timeout=poll_interval + 5) as resp:
                result = json.loads(resp.read().decode("utf-8"))

            if not result.get("has_message"):
                continue

            messages = result.get("messages") or [result.get("data")]
            primary = result.get("data") or messages[0]
            primary["batch_messages"] = messages
            count = len(messages)

            if count > 1:
                print(f"\n==================== "
                      f"[QQ_BATCH_START: {count} MESSAGES] "
                      f"====================")
                for idx, m in enumerate(messages):
                    _print_one(m, idx + 1, count)
                print("\n==================== [QQ_BATCH_END] ====================\n")
            else:
                print("\n==================== [QQ_MESSAGE_START] ====================")
                _print_one(primary)
                print("-------------------- RAW JSON --------------------")
                print(json.dumps(primary, ensure_ascii=False))
                print("==================== [QQ_MESSAGE_END] ====================\n")

            grant = result.get("desktop_grant") or {}
            if grant:
                print(f"[GATE] desktop/phone allowed = {bool(grant.get('granted'))}"
                      f" ({'no expiry' if grant.get('unlimited') else 'ttl'})")
            sys.stdout.flush()
            return primary

        except urllib.error.URLError:
            time.sleep(2)
        except KeyboardInterrupt:
            raise
        except Exception:
            time.sleep(2)


# ------------------------------------------------------------------ CLI
def main():
    parser = argparse.ArgumentParser(
        description="QQ bridge step tool - one action per command",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  python step.py --reply "hi" --group_id 123
  python step.py --reply "hi" --user_id 456
  python step.py --image a.png --group_id 123
  python step.py --reply "ok" --send-only      # send and exit, do not wait
  python step.py                                # wait for the next message only
""")
    parser.add_argument("--reply", type=str, default=None,
                        help="text to send")
    parser.add_argument("--reply-id", type=int, default=None,
                        help="message id to quote-reply to")
    parser.add_argument("--emoji-like", type=str, default=None,
                        help="emoji id to react with (124=OK, 178=salute, 76=like)")
    parser.add_argument("--reply-file", type=str, default=None,
                        help="send this file's content as a message")
    parser.add_argument("--upload-file", type=str, default=None,
                        help="upload this file to the group/private chat")
    parser.add_argument("--image", type=str, default=None,
                        help="send this image")
    parser.add_argument("--voice", type=str, default=None,
                        help="synthesize this text and send it as a voice message")
    parser.add_argument("--voice-name", type=str, default=None,
                        help="voice: xiaoxiao (default) / xiaoyi / yunxi / yunjian")
    parser.add_argument("--voice-rate", type=str, default="+0%",
                        help="speech rate, e.g. +10%% / -10%%")
    parser.add_argument("--voice-dry-run", action="store_true",
                        help="with --voice: synthesize only, do not send")
    parser.add_argument("--screenshot", action="store_true",
                        help="capture the screen and send it as an image")
    parser.add_argument("--user_id", type=int, default=None,
                        help="target private chat QQ number")
    parser.add_argument("--group_id", type=int, default=None,
                        help="target group id")
    parser.add_argument("--send-only", action="store_true",
                        help="send and exit; do not wait for the next message")
    parser.add_argument("--onebot-url", type=str, default=DEFAULT_ONEBOT,
                        help="OneBot HTTP API base url")
    parser.add_argument("--daemon-url", type=str, default=DEFAULT_DAEMON,
                        help="bridge daemon polling url")

    args = parser.parse_args()
    performed_action = False

    if args.emoji_like and args.reply_id:
        set_msg_emoji_like(args.reply_id, emoji_id=args.emoji_like,
                           onebot_url=args.onebot_url)
        performed_action = True

    if args.upload_file:
        upload_file(args.upload_file, user_id=args.user_id,
                    group_id=args.group_id, onebot_url=args.onebot_url)
        performed_action = True

    if args.screenshot:
        shot_path = capture_screenshot()
        if shot_path and os.path.exists(shot_path):
            send_image(shot_path, user_id=args.user_id, group_id=args.group_id,
                       onebot_url=args.onebot_url)
            performed_action = True

    if args.image:
        send_image(args.image, user_id=args.user_id, group_id=args.group_id,
                   onebot_url=args.onebot_url)
        performed_action = True

    if args.voice:
        _vn = args.voice_name
        if _vn:
            mod = _voice_mod()
            if mod is not None:
                _vn = getattr(mod, "VOICES", {}).get(_vn, _vn)
        send_voice_text(
            args.voice, user_id=args.user_id, group_id=args.group_id,
            onebot_url=args.onebot_url, voice=_vn, rate=args.voice_rate,
            dry_run=args.voice_dry_run)
        performed_action = True

    if args.reply_file:
        send_file_content(args.reply_file, user_id=args.user_id,
                          group_id=args.group_id, onebot_url=args.onebot_url)
        performed_action = True

    if args.reply:
        send_text_message(args.reply, user_id=args.user_id,
                          group_id=args.group_id, reply_id=args.reply_id,
                          onebot_url=args.onebot_url)
        performed_action = True

    if args.send_only:
        return

    # 长轮询等消息。空闲时零 token。
    wait_for_message_infinite(daemon_url=args.daemon_url)


if __name__ == "__main__":
    main()
