# -*- coding: utf-8 -*-
"""
daemon.py — OneBot v11 事件接收端 + 长轮询消息队列

三个端口，各干一件事：

    5701  收 OneBot v11 的 HTTP POST 事件（post_type == 'message' 等）
    5702  给 Agent 侧脚本长轮询用的 GET /wait_msg，以及 GET /desktop_grant
    5703  注入口 POST /inject —— 给**另一个进程或另一个 AI** 往队列里塞消息

## 为什么需要中间这一层

Agent（大模型）不能常驻等消息：它每次「醒来」都要一整个请求的 token 成本。
所以让它挂一个纯 Python 的长轮询，空闲时不产生任何 token，
收到真消息才唤醒。这个进程就是那个「缓冲」和「闸门」。

## 这个进程是唯一的真相来源

- 消息队列落盘（重启不丢）
- 权限票只存在本进程内存里（不落盘 —— 落盘就等于让被授权方能自己写票）
- 「主人身份」必须向 NapCat 核验 message_id 才成立（挡伪造）

配置全部在 bridge/config.json，源码里没有任何真实号码。
"""

import sys
import json
import time
import queue
import logging
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import urllib.request
import urllib.error
import os
import hashlib
import hmac
import shutil
import threading
import collections

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as _config  # noqa: E402

CFG = _config.load()

# Set up logging
# 🔴 加文件日志，不只是 stdout。
#   原来只有 logging.basicConfig（打到 stdout），而启动时 stdout 被重定向到
#   daemon_stdout.log/stderr.log —— 那个文件**会被下一次启动覆盖成 0 字节**，
#   于是「丢了消息」连证据都没了（本机实测丢过 3 次，全部无日志可查）。
#   现在额外挂一个 FileHandler，按天不切割、只追加，重启不覆盖。
_LOG_DIR = _config.workspace_of(CFG, __file__)
try:
    os.makedirs(_LOG_DIR, exist_ok=True)
    _fh = logging.FileHandler(os.path.join(_LOG_DIR, "daemon.log"), encoding="utf-8")
    _fh.setFormatter(logging.Formatter(
        '[%(asctime)s] [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
    _fh.setLevel(logging.INFO)
except Exception:
    _fh = None

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger("qq_daemon")
if _fh is not None:
    logger.addHandler(_fh)

# Message queue
# Bounded so a stalled agent cannot grow it without limit; oldest entries are
# dropped (and logged) instead of silently piling up until a restart loses them.
QUEUE_MAXSIZE = 200
msg_queue = queue.Queue(maxsize=QUEUE_MAXSIZE)

# 🔴 2026-10-21 修复 B3：队列落盘（重启不丢）。
#   原来 msg_queue 是纯内存，进程一停队列就没了 —— 今天实测因此丢过 3 条
#   群友 @ 的消息，对方以为 bot 看到了。
#   现在每条入队/取出都同步写 PENDING_FILE；启动时若有残留就先装回队列。
#   为什么是整份 JSON 而不是 append-only log：队列上限 200，整份重写只有
#   几十 KB，简单可靠；日志式反而要实现回放与截断。
PENDING_FILE = os.path.join(_LOG_DIR, "pending_messages.json")
_queue_lock = threading.Lock()


def _persist_queue():
    """把当前队列快照写盘（原子写 + 重试）。调用方必须持 _queue_lock。

    🔴 2026-10-22 修复：实测发生过一次**队列文件损坏**。
       现场：pending_messages.json 内容是 `[]"post_type": "message", ...`
       —— 也就是 `[]` 后面又拼了半条消息，json.load 直接抛 Extra data，
       整个队列在重启时被跳过（restore_queue 里 unreadable）＝ 丢消息。

       ## 根因
       1. 写入用固定文件名 `<file>.tmp`，而**多个写入方并发**时（webhook 线程
          + restore_queue 启动路径 + 消费循环）会互相踩同一个 tmp：A 写完还没
          os.replace，B 已经把它截断重写；A 的 replace 就把 B 的半截内容搬成了
          正式文件。
       2. `os.replace` 单独失败时（WinError 32 文件被占用），tmp 残留下来，
          下一次写入继续复用这个残骸，越滚越脏。
       3. 完全没有重试：一次占用就永久留下坏文件。

       ## 现在
       - tmp 名带 pid+线程号，写入方之间不会抢同一个文件
       - 失败重试 3 次（短暂退避），Windows 的文件占用通常几十毫秒就释放
       - 失败时清理自己的 tmp，绝不给下一次留残骸
       - 加 fsync：断电/崩溃时不会留下"文件在但内容空"的半截状态
    """
    for attempt in range(3):
        tmp = "%s.%d.%d.tmp" % (PENDING_FILE, os.getpid(),
                                threading.get_ident())
        try:
            with msg_queue.mutex:
                snapshot = list(msg_queue.queue)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, PENDING_FILE)
            return
        except Exception as e:
            # 落盘失败绝不能让消息本身丢掉，只告警
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            if attempt == 2:
                logger.warning(f"[QUEUE_PERSIST] failed after 3 tries: {e}")
            else:
                time.sleep(0.05 * (attempt + 1))


def _salvage_queue_file(raw):
    """从损坏的队列文件里尽量抢救消息（2026-10-22 新增）。

    ## 为什么需要
    实测坏成过 `[]"post_type": "message", ...}]` —— 即一个合法但空的数组
    后面拼了半条消息。直接 json.load 会抛 Extra data，旧代码 `return 0`
    就把这条消息永久丢了。而这条很可能是群友刚发的重要消息。

    ## 做法
    按 `{"post_type"` 切块，逐块尝试补全成合法 JSON 对象。
    宁可救回部分，也绝不整份丢弃。
    """
    parts = []
    idx = 0
    while True:
        start = raw.find('{"post_type"', idx)
        if start < 0:
            break
        nxt = raw.find('{"post_type"', start + 1)
        chunk = raw[start:] if nxt < 0 else raw[start:nxt]
        chunk = chunk.strip()
        # 去掉可能多出来的尾部 ']' 或 '}]'
        while chunk and chunk[-1] in '],}':
            if chunk.endswith('}]'):
                chunk = chunk[:-2]
            else:
                chunk = chunk[:-1]
            chunk = chunk.strip()
            # 只剥到能解析为止，避免把对象本身的 } 也削掉
            try:
                json.loads(chunk)
                break
            except Exception:
                continue
        if not chunk.endswith('}'):
            chunk += '}'
        try:
            obj = json.loads(chunk)
            if isinstance(obj, dict) and obj.get("post_type"):
                parts.append(obj)
        except Exception:
            continue
        if nxt < 0:
            break
        idx = start + 1
    return parts


def restore_queue():
    """启动时把上次没被取走的消息装回队列。返回恢复条数。"""
    if not os.path.exists(PENDING_FILE):
        return 0
    items = None
    try:
        with open(PENDING_FILE, "r", encoding="utf-8") as f:
            items = json.load(f)
    except Exception as e:
        logger.warning(f"[QUEUE_RESTORE] unreadable ({e}), trying salvage...")
        try:
            with open(PENDING_FILE, "r", encoding="utf-8",
                      errors="replace") as f:
                raw = f.read()
            salvaged = _salvage_queue_file(raw)
            if salvaged:
                logger.warning(f"[QUEUE_RESTORE] salvaged {len(salvaged)} "
                               f"message(s) from corrupted file")
                # 留一份坏文件作证据，便于以后排查
                try:
                    bad = PENDING_FILE + ".corrupt"
                    if not os.path.exists(bad):
                        shutil.copyfile(PENDING_FILE, bad)
                except Exception:
                    pass
                items = salvaged
            else:
                logger.warning("[QUEUE_RESTORE] salvage found nothing")
                return 0
        except Exception as e2:
            logger.warning(f"[QUEUE_RESTORE] salvage failed: {e2}")
            return 0
    if not isinstance(items, list) or not items:
        return 0
    restored = 0
    for it in items[:QUEUE_MAXSIZE]:
        try:
            msg_queue.put_nowait(it)
            restored += 1
        except queue.Full:
            break
    logger.warning(f"[QUEUE_RESTORE] 恢复了 {restored} 条上次未处理的消息")
    _persist_queue()
    return restored



def _enqueue(item):
    """Put an item on the queue, dropping the oldest entry when full.

    入队成功后立刻落盘，保证重启不丢（B3）。
    """
    with _queue_lock:
        try:
            msg_queue.put_nowait(item)
        except queue.Full:
            try:
                dropped = msg_queue.get_nowait()
                logger.warning(
                    f"[QUEUE_FULL] Dropped oldest message from "
                    f"{dropped.get('nickname')} ({dropped.get('user_id')}) to make room"
                )
            except queue.Empty:
                pass
            try:
                msg_queue.put_nowait(item)
            except queue.Full:
                logger.error("[QUEUE_FULL] Could not enqueue message even after dropping oldest")
                return False
        _persist_queue()
        return True

# Constants —— 全部来自 bridge/config.json，源码里不出现任何真实号码
BOT_QQ = CFG["bot_qq"]
OWNER_QQS = CFG["owner_qq"]            # 列表：可以有多个主人
MASTER_QQ = OWNER_QQS[0]               # 兼容旧代码里的单值引用
BOT_NAMES = [n for n in (CFG.get("bot_names") or []) if n]

# 群过滤的两个名单，从配置来。源码里不含任何真实群号。
BLOCK_GROUPS = set(CFG.get("group_filter", {}).get("block_groups") or [])
ALLOW_GROUPS = CFG.get("group_filter", {}).get("allow_groups") or []

# 注入口的专用身份。
#   固定负数，永远不与任何真实 QQ 号冲突；permission 恒为 inject，拿不到桌面/手机票。
ARCH_USER_ID = -1
ARCH_TOKEN_FILE = os.path.join(_LOG_DIR, CFG["injection"].get("token_file", ".inject_token"))
ARCH_NICKNAME = CFG["injection"].get("nickname") or "coder-agent"
ARCH_ROLE_TAG = CFG["injection"].get("role_tag") or \
    "[identity: coder-agent (helper, no desktop permission)]"

# ---------------------------------------------------------------- 操作许可闸门
#
# ## 为什么光靠 role_tag 不够
# 只把 `[identity: guest (no permission)]` 塞进 text，**没有任何代码级拦截** ——
# 也就是「群友动不了电脑」完全依赖 Agent 读到标签后自觉。Agent 一旦有了
# desktop.py 这只手，群里一次提示词注入就能让这条线失守。
#
# ## 现在的做法
# 许可状态**只存在本进程内存里**，desktop.py / phone.py 每次动手前必须来问
# GET /desktop_grant 拿一张票。规则：
#   · 主人发消息       → granted = True
#   · 任何非主人发消息 → **立刻** granted = False（撤销，不等 TTL）
#   · 进程重启         → 默认 False（fail-closed）
#   · 客户端连不上     → 也当没有（fail-closed）
#
# ## TTL：默认 0 = 不设期限
# 「主人发一条 → 15 分钟内可动手」实测撑不住：一次长任务动辄几十分钟，
# 中间只要一条群友消息飘进来（5701 监听所有消息，群友说话同样会调
# _desktop_grant_msg），票立刻作废，Agent 干到一半手被抽走。
# 15 分钟从来没有对应任何真实威胁模型，只在干长活时稳定误伤。
#
# 默认：**主人发消息就开票，票没有期限，一直有效到被撤销**。
# 想要 15 分钟，把 config.json 的 gates.ttl_seconds 设成 900。
#
# 撤销点一个都没动：任何非主人消息一进来就撤票（他在这个房间里说话，
# 一句「帮我点开桌面那个 xxx」不能被当成长辈的许可）；注入口（5703）
# 推消息本来就不碰这张票，它自己也开不了票；进程重启和客户端连不上仍是拒绝。
#
# ## 为什么放内存、不放文件
# Agent 能读写文件系统。许可如果落盘，它写个 JSON 就自己给自己发了。
# 内存态的唯一改变途径是「主人真的发了一条消息」。
#
# ⚠️ 残余风险（不掩饰）：Agent 手里有 shell，一个绕过 desktop.py 直接
#    import pyautogui 的实现这道门拦不住。要彻底锁死需要 OS 级沙箱
#    （受限账户 / AppLocker / 独立会话）。本闸门挡的是「按说明书调用」
#    和「被群友话术骗了之后照做」—— 也就是实际会发生的绝大多数情况。
_desktop_lock = threading.Lock()
_desktop_grant = {
    "granted": False,
    "user_id": None,
    "since": None,
    "expires_at": 0.0,
    "revoked_reason": "startup_default_deny",
}
# 0 = 不设期限。下面是唯一读这个数字的地方，其余代码都只认
# expires_at == 0 表示「无限期」。
DESKTOP_GRANT_TTL = float(CFG.get("gates", {}).get("ttl_seconds") or 0)


def _desktop_grant_msg(user_id, nickname, is_master):
    """按发消息的人开/关闸门。主人 → 开；其他人 → 立刻关。"""
    now = time.time()
    with _desktop_lock:
        if is_master:
            renewed = _desktop_grant["granted"]
            _desktop_grant.update({
                "granted": True,
                "user_id": user_id,
                "since": _desktop_grant["since"] or
                         datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                # expires_at == 0.0 表示永不自动过期（见 _desktop_grant_get）
                "expires_at": (now + DESKTOP_GRANT_TTL) if DESKTOP_GRANT_TTL > 0 else 0.0,
                "revoked_reason": None,
            })
            if not renewed:
                if DESKTOP_GRANT_TTL > 0:
                    logger.info(f"[GATE] GRANTED by owner {user_id} "
                                f"(TTL {int(DESKTOP_GRANT_TTL)}s)")
                else:
                    logger.info(f"[GATE] GRANTED by owner {user_id} "
                                f"(no expiry until revoked)")
        else:
            was = _desktop_grant["granted"]
            _desktop_grant.update({
                "granted": False,
                "user_id": user_id,
                "expires_at": 0.0,
                "revoked_reason": f"revoked_by_user_message:{user_id}",
            })
            if was:
                logger.warning(
                    f"[GATE] REVOKED — non-owner {nickname} ({user_id}) sent a "
                    f"message; desktop/phone permission revoked immediately")


def _desktop_grant_get():
    """给 desktop.py / phone.py 查票。读的时候顺手过期。

    expires_at == 0.0 表示**不设期限**，永远不在这里被判过期；
    只有非主人消息、进程重启这两种情况会让票失效。
    """
    with _desktop_lock:
        g = dict(_desktop_grant)
    # 只有真的设了期限（expires_at > 0）才判过期；0 代表无限期，绝不能
    # 和 time.time() 比较 —— 否则每次读票都会被当成"已过期"，票永远发不出去。
    if g["granted"] and g["expires_at"] > 0 and time.time() > g["expires_at"]:
        with _desktop_lock:
            _desktop_grant.update({"granted": False,
                                   "expires_at": 0.0,
                                   "revoked_reason": "ttl_expired"})
            g = dict(_desktop_grant)
    g["ttl_seconds"] = DESKTOP_GRANT_TTL
    expires_at = g.get("expires_at", 0) or 0
    if expires_at > 0:
        g["remaining_seconds"] = max(0, int(expires_at - time.time()))
        g["unlimited"] = False
    else:
        # 老字段保留、值给 -1：任何把 remaining_seconds 当剩余秒数显示的旧脚本
        # 都还能跑，不会显示 0 秒的假象。
        g["remaining_seconds"] = -1
        g["unlimited"] = bool(g.get("granted"))
    return g


# Pillar 4: Per-user anti-spam cooldown
# Messages inside the cooldown window are NOT discarded any more: they are
# tagged as throttled and still delivered, so a genuine second question that
# arrives right after a normal line is never lost.
user_last_seen = {}
USER_COOLDOWN_SECONDS = float(CFG.get("cooldowns", {}).get("user_seconds") or 15)

# 收到的图片落盘目录。只存原件 —— 不分类、不打标签、不解析内容。
MEDIA_INBOX_DIR = os.path.join(_LOG_DIR, CFG.get("media_inbox") or "media_inbox")
os.makedirs(MEDIA_INBOX_DIR, exist_ok=True)

# 单张图上限。超过就不收 —— 防止一条大图把 webhook 线程拖住
MEDIA_MAX_BYTES = int(CFG.get("media_max_bytes") or 8 * 1024 * 1024)

# 群「图+说明」放行冷却：同群 N 秒内最多叫醒一次，防连甩表情包刷屏烧 token
GROUP_IMAGE_COOLDOWN = float(CFG.get("cooldowns", {}).get("group_image_seconds") or 30)
group_image_last_seen = {}

# 🔴 上面两个冷却表原来**永不清理**，每个发过言的用户/每个群
#   各留一条，进程常驻跑几个月会缓慢变胖。加一个软上限，超了就把
#   过期条目扫掉（15s/60s 的表留 1 小时内活跃的足够）。
_COOLDOWN_SWEEP_AT = 500


def _sweep_cooldowns():
    """超过阈值时清掉过期条目。只在表变大时才做，正常聊天零开销。"""
    now = time.time()
    if len(user_last_seen) > _COOLDOWN_SWEEP_AT:
        for uid in [k for k, v in user_last_seen.items() if now - v > 3600]:
            user_last_seen.pop(uid, None)
    if len(group_image_last_seen) > 100:
        for gid in [k for k, v in group_image_last_seen.items() if now - v > 3600]:
            group_image_last_seen.pop(gid, None)


# ---------------------------------------------------------------- 引用回复判定
# 🔴 2026-10-22：`[CQ:reply,id=X]` 是否指向**我们自己**发的消息。
#
# ## 为什么要记这个表
# 群友点「回复」针对 bot 的某句话追问时，raw_message 里既没有 @、正文里
# 也不一定写名字 —— 旧过滤逻辑于是把它当闲聊丢掉。但群友之间互相回复
# 同样普遍，**不能"有 reply 就放行"**，那等于没过滤。
#
# ## 方案
# OneBot 的 message_sent 事件（我们发出去的消息）和 response 里的
# message_id 都能拿到。这里只记我们自己发出的 id，收到 reply 时查表。
# 表用 deque 限长，避免常驻进程无限增长；保留最近 500 条足够覆盖
# 群聊里"刚回你一句你就点回复追问"的真实节奏。
SELF_ID_FILE = os.path.join(_LOG_DIR, "self_message_ids.txt")
_SELF_MSG_IDS = collections.deque(maxlen=500)
_SELF_MSG_SET = set()


def _load_self_ids():
    """启动时把上次记下的 self message id 读回来。

    为什么必须落盘：引用回复常常发生在"她说一句 → 过几分钟有人点回复"，
    中间如果 daemon 重启过，纯内存的表就空了，那条追问会被当闲聊丢掉。
    """
    if not os.path.exists(SELF_ID_FILE):
        return 0
    try:
        with open(SELF_ID_FILE, "r", encoding="utf-8") as f:
            ids = [ln.strip() for ln in f if ln.strip()]
    except Exception as e:
        logger.warning(f"[SELF_ID] load failed: {e}")
        return 0
    for mid in ids[-500:]:
        if mid not in _SELF_MSG_SET:
            if len(_SELF_MSG_IDS) == _SELF_MSG_IDS.maxlen:
                _SELF_MSG_SET.discard(str(_SELF_MSG_IDS[0]))
            _SELF_MSG_IDS.append(mid)
            _SELF_MSG_SET.add(mid)
    return len(_SELF_MSG_SET)


def remember_self_message(message_id):
    """记下一条"我们自己发出去的消息 id"，供引用回复判定使用。"""
    if message_id is None:
        return
    mid = str(message_id)
    if mid in _SELF_MSG_SET:
        return
    if len(_SELF_MSG_IDS) == _SELF_MSG_IDS.maxlen:
        old = _SELF_MSG_IDS[0]
        _SELF_MSG_SET.discard(str(old))
    _SELF_MSG_IDS.append(mid)
    _SELF_MSG_SET.add(mid)


def append_self_id_file(message_id):
    """把 id 追加到 self_message_ids.txt。

    🔴 2026-10-22 补：这个文件原来是**只增不减** —— 每发一条消息就 append 一行，
    常驻跑几个月会变成几万行，而 load 时只取最后 500 条，多出来的纯属垃圾。
    现在追加后若超过 1000 行，就整体重写成最后 500 行（原子写）。
    1000 这个阈值：留一倍余量，避免每写一行都要重写整个文件。
    """
    mid = str(message_id)
    try:
        with open(SELF_ID_FILE, "a", encoding="utf-8") as f:
            f.write(mid + "\n")
    except Exception as e:
        logger.warning(f"[SELF_ID] append failed: {e}")
        return

    # 体积守卫：只在明显变大时才做一次压缩，正常路径零开销
    try:
        if os.path.getsize(SELF_ID_FILE) > 1000 * 20:   # 约 1000 行（每行 ~20B）
            with open(SELF_ID_FILE, "r", encoding="utf-8") as f:
                lines = [ln.strip() for ln in f if ln.strip()]
            keep = lines[-500:]
            tmp = SELF_ID_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("\n".join(keep) + "\n")
            os.replace(tmp, SELF_ID_FILE)
            logger.info(f"[SELF_ID] compacted {len(lines)} -> {len(keep)} lines")
    except Exception as e:
        logger.warning(f"[SELF_ID] compact failed: {e}")


def _reply_target_is_bot(reply_id):
    """这条 reply 指向的是不是我们自己发的消息。"""
    return str(reply_id) in _SELF_MSG_SET


def _record_sent_message(result):
    """从 OneBot 发送响应里抠出 message_id 并记住。

    不同实现的字段名不完全一样：`message_id` 最通用，
    个别版本放在 `data.message_id`（这里已经解包过 data 了）。
    """
    if not isinstance(result, dict):
        return
    mid = result.get("message_id")
    if mid is None:
        mid = result.get("messageId")
    remember_self_message(mid)

# ---------------------------------------------------------------- 收到的图片
#
# ## 这里做什么、不做什么
# **做**：把群里/私聊发给机器人的图片原样存到 media_inbox/，并把文件名
#       写进消息体的 `image_files` 字段，让 Agent 不用再发一次网络请求就能读。
#
# **不做**：不分类、不打标签、不判断内容、不建索引、不自动清理。
#       这些都是「看起来该有」的功能，但每一样都要回答「标签存哪、谁能改、
#       会不会被绕过」——没想清楚就不写进代码，也不写进文档。
#
# ## 为什么只存原件
# 一个 Agent 需要的是「这张图在哪」，它自己看得懂图。给它一套标签体系
# 反而会制造「我给它打过标签所以我安全」的错觉 —— 那是假的。原始文件
# 在硬盘上，谁读过、读到了什么，日志证明不了，也不需要假装能证明。
#
# ## 去重
# 同 sha256 的图只存一份，后面再收到就复用已有文件。群里同一张梗图
# 一天出现几十次是常态，不去重会让目录长得很快。

MEDIA_LOCK = threading.Lock()


def _sniff_ext(blob):
    """按文件头认格式，不信任 URL 里的扩展名。

    实测踩过：URL 是 .jpg，内容其实是 GIF，写出去用图片库打不开。
    """
    head = blob[:16]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return "gif"
    if head[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "webp"
    if head.startswith(b"BM"):
        return "bmp"
    return "bin"


def save_incoming_media(url, user_id, message_id=None):
    """下载一张图片到 media_inbox/。返回文件名，失败返回 None。

    这个函数**不抛异常**：图片存不下来不能让整条消息丢掉。
    群友说了一句话，那句话比图重要。
    """
    if not url or not str(url).startswith("http"):
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            # 先读进内存（上限内），避免半截文件落盘
            blob = r.read(MEDIA_MAX_BYTES + 1)
        if not blob:
            return None
        if len(blob) > MEDIA_MAX_BYTES:
            logger.info(f"[MEDIA] Skipped oversize image {len(blob)}B from {user_id}")
            return None

        h = hashlib.sha256(blob).hexdigest()
        name = f"{h[:16]}.{_sniff_ext(blob)}"
        target = os.path.join(MEDIA_INBOX_DIR, name)

        with MEDIA_LOCK:
            if os.path.isfile(target):
                logger.info(f"[MEDIA] Duplicate image skipped (already as {name})")
                return name
            tmp = target + ".part"
            try:
                with open(tmp, "wb") as f:
                    f.write(blob)
                os.replace(tmp, target)
            except Exception:
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except Exception:
                        pass
                raise

        logger.info(f"[MEDIA] Saved incoming image: {name} "
                    f"({len(blob)}B, from {user_id}, msg={message_id})")
        return name
    except Exception as e:
        logger.warning(f"[MEDIA] save failed: {e}")
        return None
# ---------------------------------------------------------------- 链接卡片解析
#
# 🔴 2026-10-22 新增（实测发现：群里发 B 站视频链接，bot 只收到一张封面图）。
#
# ## 实测到的原始结构
# 群友转发 B 站视频时，NapCat 推过来的是 `[CQ:json,data={...}]`，真实内容：
#   {
#     "app": "com.tencent.miniapp_01",
#     "prompt": "[QQ小程序]【黑鸦】头皮发麻！Gemini 4 Carbon被曝测试...",
#     "meta": {"detail_1": {
#         "title": "哔哩哔哩",
#         "desc":  "【黑鸦】头皮发麻！Gemini 4 Carbon被曝测试...",
#         "preview": "https://qq.ugcimg.cn/...",      ← 封面图（会被误当成图片下载！）
#         "qqdocurl": "https://b23.tv/TdoaZiM?..."    ← 真正的视频链接
#     }}
#   }
#
# ## 为什么要做成「通用」而不是「认 B 站」
# 用户明确要求：不能只认 B 站。明天可能是腾讯视频、爱奇艺、抖音、YouTube……
# 各平台的卡片字段名**不完全一样**（qqdocurl / jumpUrl / url / link …），
# 所以这里用**按优先级扫描候选字段**的策略，而不是 if platform == 'bilibili'。
#
# ## 关键点
# 1. `preview` / `icon` 是图片，**绝不能**当成链接或表情包
# 2. 判「是不是视频」靠**域名白名单 + 标题关键词**，不靠 appid

# 常见视频站域名 → 中文名。只用于「让 bot 知道这是啥」，不认识的也不丢。
VIDEO_HOSTS = {
    "b23.tv": "B站", "bilibili.com": "B站", "bilibili.tv": "B站",
    "youtube.com": "YouTube", "youtu.be": "YouTube",
    "douyin.com": "抖音", "iesdouyin.com": "抖音",
    "kuaishou.com": "快手",
    "v.qq.com": "腾讯视频", "qq.com": "腾讯",
    "iqiyi.com": "爱奇艺",
    "youku.com": "优酷",
    "mgtv.com": "芒果TV",
    "acfun.cn": "A站",
    "xiaohongshu.com": "小红书", "xhslink.com": "小红书",
    "weibo.com": "微博", "weibo.cn": "微博",
    "zhihu.com": "知乎",
    "tiktok.com": "TikTok",
    "twitch.tv": "Twitch",
    "nicovideo.jp": "niconico",
}

# 卡片里可能承载真实链接的字段，**按可靠性从高到低**扫描。
# qqdocurl 实测最准（QQ 自己生成的跳转）；url 有时是广告位；link 是通用兜底。
_LINK_FIELDS = ("qqdocurl", "jumpUrl", "jump_url", "jumpurl", "url",
                "link", "shareUrl", "share_url", "targetUrl",
                "sourceUrl", "source_url", "playUrl", "play_url")

# 这些字段一定是图片，扫描链接时必须跳过（否则会把封面当成链接）
_IMAGE_FIELDS = ("preview", "icon", "imageUrl", "image_url", "thumb",
                 "thumbnail", "cover", "picUrl", "pic_url", "img")


def _looks_like_url(v):
    return isinstance(v, str) and v.startswith(("http://", "https://"))


def _host_of(url):
    try:
        from urllib.parse import urlparse
        h = urlparse(url).hostname or ""
        return h.lower().lstrip("www.")
    except Exception:
        return ""


def _platform_name(url):
    """根据域名给个中文平台名；不认识的返回空串（不丢链接，只是不标平台）。"""
    h = _host_of(url)
    if not h:
        return ""
    for dom, name in VIDEO_HOSTS.items():
        if h == dom or h.endswith("." + dom):
            return name
    return ""


def _walk_strings(obj, out, depth=0):
    """递归收集 JSON 里所有字符串字段（含键名->值），供扫描用。"""
    if depth > 6:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str):
                out.append((k, v))
            else:
                _walk_strings(v, out, depth + 1)
    elif isinstance(obj, list):
        for it in obj:
            _walk_strings(it, out, depth + 1)


def parse_card_link(cq_data):
    """从 `[CQ:json,data=...]` 的 data 里抽出**真实链接 + 标题**。

    做成通用的，不认平台：
    1. 先在已知字段名里找 URL（qqdocurl 等），跳到图片字段
    2. 找不到就在全 JSON 里扫，跳过图片字段和图片域名
    3. 标题取自 desc / title / prompt，哪个有内容用哪个

    @returns {dict|None} {"url","title","platform","kind"}；无链接返回 None
    """
    try:
        raw = cq_data
        if isinstance(raw, str):
            # QQ 会把 , : [ ] 转义成 &#44; &#58; &#91; &#93;
            raw = raw.replace("&#44;", ",").replace("&#58;", ":") \
                     .replace("&#91;", "[").replace("&#93;", "]") \
                     .replace("&amp;", "&")
            d = json.loads(raw)
        else:
            d = raw
    except Exception:
        return None
    if not isinstance(d, dict):
        return None

    # 1) 已知字段优先
    url = ""
    pairs = []
    _walk_strings(d, pairs)
    by_key = {}
    for k, v in pairs:
        by_key.setdefault(k, []).append(v)

    for f in _LINK_FIELDS:
        for v in by_key.get(f, []):
            if _looks_like_url(v):
                url = v
                break
        if url:
            break

    # 2) 兜底：全 JSON 扫，但排除图片字段
    if not url:
        for k, v in pairs:
            if k in _IMAGE_FIELDS:
                continue
            if not _looks_like_url(v):
                continue
            # 图片 CDN 域名排除掉
            h = _host_of(v)
            if any(x in h for x in ("qpic.cn", "ugcimg.cn", "gtimg.cn",
                                    "qlogo.cn", "gchat.qpic")):
                continue
            url = v
            break

    if not url:
        return None

    # 3) 标题：desc > title > prompt，取第一个非空的
    title = ""
    for f in ("desc", "title", "prompt"):
        for v in by_key.get(f, []):
            v = (v or "").strip()
            # 过滤掉纯平台名（如 title="哔哩哔哩"），它不算内容
            if v and v not in VIDEO_HOSTS.values() and len(v) >= 4:
                title = v
                break
        if title:
            break
    if not title:
        for v in by_key.get("prompt", []):
            v = (v or "").strip()
            if v:
                title = v.replace("[QQ小程序]", "").strip()
                break

    platform = _platform_name(url)
    # 判是不是视频：平台名命中视频站，或标题里带视频词
    kind = "链接"
    if platform or any(w in (title or "") for w in
                       ("视频", "直播", "video", "解说", "剪辑", "番剧", "电影")):
        kind = "视频链接"

    # URL 里的 &amp; 要还原
    url = url.replace("&amp;", "&")
    return {"url": url, "title": title[:120],
            "platform": platform, "kind": kind}


def format_card_link(info):
    """把解析结果格式化成给 bot 看的文本。"""
    if not info:
        return ""
    plat = info.get("platform") or ""
    kind = info.get("kind") or "链接"
    head = f"[{kind}]"
    if plat:
        head = f"[{kind}·{plat}]"
    title = info.get("title") or ""
    if title:
        return f"{head}{title}  {info['url']}"
    return f"{head}{info['url']}"



def _save_and_record(url, user_id, message_id, sink):
    """异步存图，并把文件名补进 sink 列表。

    ⚠️ 已知的诚实边界：这是**异步**的，消息体在 _enqueue 那一刻可能还没
    填好 image_files。所以消息里**同时**给了原始 image_urls，Agent 两条路
    都能走：先看 image_files（已存好），没有就用 image_urls 自己去取。
    不在入队前 join 线程 —— 一张慢图不该让整条消息晚到几秒。
    """
    name = save_incoming_media(url, user_id, message_id)
    if name:
        sink.append(name)



LAST_TARGET_FILE = os.path.join(_LOG_DIR, "last_target.json")
last_target = {
    "message_type": "private",
    "user_id": MASTER_QQ,
    "group_id": None,
    "nickname": "owner",
    "updated_at": None
}

def save_last_target(info):
    global last_target
    last_target.update(info)
    last_target["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(LAST_TARGET_FILE, "w", encoding="utf-8") as f:
            json.dump(last_target, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Failed to save last_target: {e}")

def load_last_target():
    global last_target
    if os.path.exists(LAST_TARGET_FILE):
        try:
            # 🔴 2026-10-23：改成 utf-8-sig。今晚实测过一次 —— 手写/覆盖这个
            #   文件时如果带了 BOM，`json.load` 直接抛 "Unexpected UTF-8 BOM"，
            #   整份目标被丢弃，last_target 退回默认值。utf-8-sig 有无 BOM 都能读。
            with open(LAST_TARGET_FILE, "r", encoding="utf-8-sig") as f:
                last_target = json.load(f)
                logger.info(f"Loaded existing last target: {last_target}")
        except Exception as e:
            logger.warning(f"Could not load last_target: {e}")

def call_onebot_api(endpoint, payload, timeout=5):
    """调 OneBot 的 HTTP API。

    🔴 这里曾经写死 `http://127.0.0.1:3000`，于是配置里的 onebot_api 形同虚设 ——
    自测时伪造的核验请求全打到本机真实的 NapCat 上去了，日志里看起来像
    「防伪层工作正常」，实际上测的根本不是被测代码。写死地址的代价
    不是"不方便"，是**测试会撒谎**。
    """
    base = (CFG.get("onebot_api") or "http://127.0.0.1:3000").rstrip("/")
    url = f"{base}/{endpoint.lstrip('/')}"
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        logger.error(f"Failed to call OneBot API {endpoint}: {e}")
        return None


# ---------------------------------------------------------------- 消息真伪核验
#
# 🔴 这是**写这套桥的时候自己造出来的一个漏洞**，记下来是因为它太容易再犯：
#   许可闸门的判定依据是 `user_id == 主人号`。
#   但事件口是**裸 HTTP + 监听所有网卡 + 零鉴权**：
#   本机任意进程、乃至同网段任何一台机器，只要 POST 一条
#
#       {"post_type":"message","user_type":"private",
#        "user_id":<主人号>,"raw_message":"...任意内容..."}
#
#   就能冒充主人、直接开票。实测确实开成了（两次），
#   闸门当场作废 —— 这道门挡得住群友的话术，挡不住一条 curl。
#
# ## 为什么这个核验是可靠的
#   NapCat 的 `get_msg` 只认它自己记录过的 message_id：
#       真实 id  → status=ok,  retcode=0,   data 非空
#       伪造 id  → status=failed, retcode=200, data 空
#   实测单次约 6ms，群/私聊都有效。伪造者猜不出一个真实存在的 id。
#
# ## 三种结果，三种处理
#   ok        → 真消息，正常放行
#   failed    → 假 id，**直接丢弃**（不进队列、不撤权、不授权）
#   核验不了  → NapCat 没响应。**不等于假**，普通群友照常放行；
#               但凡是「主人身份」一律按未核验处理 → 不授权。
#               理由：拒绝一条真消息只让主人错失一次回复，而错认一个
#               假冒主人 = 把电脑交出去。这两件事的代价不对等。
def _verify_message_id(message_id):
    """返回 True(真) / False(假) / None(核验不了)。

    🔴 超时从默认 5s 收到 1.2s。
       webhook 线程是同步处理的，NapCat 卡住时每条消息要等
       2 次 × 5s = 10s 才放行 —— 半夜它连话都收不及时。
       本机回环调用的正常延迟是 6ms，1.2s 已经是 200 倍余量。
    """
    if message_id is None:
        return None
    for attempt in range(2):
        try:
            res = call_onebot_api("get_msg", {"message_id": message_id},
                                  timeout=1.2)
        except Exception:
            res = None
        if res is not None:
            status = str(res.get("status", "")).lower()
            if status == "ok" and res.get("data"):
                return True
            if status == "failed":
                return False
            # 其它形态（比如 ok 但 data 空）当作核验不了
            return None
        if attempt == 0:
            time.sleep(0.1)      # 刚到的消息可能还没入库，给一次机会
    return None


# ==============================================================================
# Handler for Port 5701 (OneBot v11 Webhook Event Receiver)
# ==============================================================================
class OneBotWebhookHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _read_body(self):
        transfer_encoding = self.headers.get("Transfer-Encoding", "").lower()
        if "chunked" in transfer_encoding:
            chunks = []
            while True:
                line = self.rfile.readline()
                if not line:
                    break
                chunk_size_str = line.strip().split(b";")[0]
                if not chunk_size_str:
                    continue
                chunk_size = int(chunk_size_str, 16)
                if chunk_size == 0:
                    while True:
                        trailer = self.rfile.readline().strip()
                        if not trailer:
                            break
                    break
                chunk_data = self.rfile.read(chunk_size)
                chunks.append(chunk_data)
                self.rfile.readline()
            return b"".join(chunks).decode("utf-8", errors="replace")

        cl_header = self.headers.get("Content-Length")
        if cl_header is not None:
            try:
                cl = int(cl_header)
                if cl > 0:
                    return self.rfile.read(cl).decode("utf-8", errors="replace")
            except ValueError:
                pass

        return self.rfile.read().decode("utf-8", errors="replace")

    def _send_json_response(self, data_dict, code=200):
        body = json.dumps(data_dict, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            body = self._read_body()
            if not body.strip():
                self._send_json_response({}, 200)
                return
            data = json.loads(body)
        except Exception as e:
            logger.error(f"Error parsing POST JSON from OneBot: {e}")
            self._send_json_response({"status": "error", "message": "invalid json"}, 400)
            return

        post_type = data.get("post_type")

        # Meta events
        if post_type == "meta_event":
            self._send_json_response({}, 200)
            return

        # 🔴 2026-10-22：记下我们自己发出去的消息 id，供「引用回复判定」使用。
        #   NapCat 会把 bot 自己发的消息以 post_type=message_sent 推过来，
        #   data 结构与 message 事件基本相同，这里只需要 message_id。
        #   为什么不用 qq_step 发送时的响应：那是**另一个进程**，它的返回值
        #   到不了这个守护进程的内存。webhook 是唯一跨进程的可靠通道。
        if post_type == "message_sent":
            mid = data.get("message_id")
            if mid is None:
                mid = data.get("messageId")
            if mid is not None:
                remember_self_message(mid)
                append_self_id_file(mid)
                logger.info(f"[SELF_ID] recorded self message {mid}")
            self._send_json_response({}, 200)
            return

        # Auto-approve friend requests
        if post_type == "request":
            req_type = data.get("request_type")
            flag = data.get("flag")
            user_id = data.get("user_id")
            if req_type == "friend" and flag:
                logger.info(f"Auto-accepting friend request from QQ: {user_id}")
                call_onebot_api("set_friend_add_request", {"flag": flag, "approve": True, "remark": "owner"})
            self._send_json_response({}, 200)
            return

        # Handle message events
        if post_type == "message":
            msg_type = data.get("message_type")  # "private" or "group"
            user_id = data.get("user_id")
            group_id = data.get("group_id")
            raw_message = data.get("raw_message", "")
            sender = data.get("sender", {})
            nickname = sender.get("card") or sender.get("nickname") or str(user_id)
            message_id = data.get("message_id")

            # 群过滤：block_groups 里的群**一条都不处理**（不偷图、不入队、零唤醒）。
            # 放在最顶层：在解析图片之前直接丢弃，不存一张图、不占一丝带宽。
            # 这既是省 token，也是一种礼貌 —— 有些群里明确不想让 AI 说话，
            # 那就连读都别读。
            if msg_type == "group" and group_id in BLOCK_GROUPS:
                self._send_json_response({}, 200)
                return

            # 🔴 防伪层：拿 message_id 找 NapCat 核对。
            #   ## 为什么只对「声称是主人」的消息核
            #   5701 如果对外监听，任何人都能 POST 一条 user_id=所有者号的假消息
            #   来开许可 —— 这是必须堵的。但**没必要给每条群友消息都加
            #   一次 6ms 的核验往返**：
            #     · 冒充普通群友没有收益（本来就没有电脑权限）
            #     · 更要命的是 NapCat 一旦抖动，全量核验会把**所有**消息
            #       判成 failed 然后全丢掉 —— 为了防一个不存在的攻击面，
            #       把正常聊天全弄挂，这个交换不划算。
            #   所以：只有主人号才核验，其余照旧放行。
            verified = None
            if user_id in OWNER_QQS:
                verified = _verify_message_id(message_id)
                if verified is False:
                    logger.warning(
                        f"[FORGED] dropped: NapCat has no message_id="
                        f"{message_id}, yet it claims to be from the owner "
                        f"{user_id}: {(raw_message or '')[:60]}")
                    self._send_json_response({}, 200)
                    return

            # 身份判定。**主人身份只有在 NapCat 核验通过时才成立。**
            #   核验不了（NapCat 挂了/超时）也不算数 —— 见 _verify_message_id 的取舍说明。
            is_owner = (user_id in OWNER_QQS) and (verified is True)
            if user_id in OWNER_QQS and verified is not True:
                logger.error(
                    f"[UNVERIFIED] claims to be owner ({user_id}) but message_id "
                    f"{message_id} could not be verified -> no permission granted, "
                    f"treated as an ordinary user")

            # Parse message content first
            text = raw_message
            message_field = data.get("message")
            has_image = False
            has_link = False
            at_me = False
            image_urls = []
            image_files = []          # 存盘成功后的本地文件名
            link_infos = []
            if isinstance(message_field, list):
                text_parts = []
                for elem in message_field:
                    if elem.get("type") == "text":
                        text_parts.append(elem.get("data", {}).get("text", ""))
                    elif elem.get("type") == "image":
                        img_url = elem.get('data', {}).get('url', '')
                        has_image = True
                        if img_url:
                            image_urls.append(img_url)
                        text_parts.append(f"[image:{img_url}]")
                        # 存原件（异步，绝不让下载拖住 webhook 线程）。
                        # 存下来的文件名稍后填进 image_files 字段。
                        threading.Thread(target=_save_and_record,
                                         args=(img_url, user_id, message_id,
                                               image_files),
                                         daemon=True).start()
                    elif elem.get("type") == "record":
                        text_parts.append(f"[voice:{elem.get('data', {}).get('url', '')}]")
                    elif elem.get("type") == "at":
                        # 🔴 修复过的一个真 bug：`at` 段原来**没有任何分支**，
                        #   被直接跳过。后果是递给 Agent 的 text 里看不到「有人@我」：
                        #
                        #     原始：'直接给ai写[CQ:at,qq=<bot>] 用c#写一个...'
                        #     Agent 收到：'直接给ai写 用c#写一个...'
                        #
                        #   于是它判定"这只是句普通闲聊，不是在叫我"，**没回**。
                        #   实测：群友明确 @ 它让它写代码，它一声没吭；
                        #   下一轮另一条它回了 —— 差别就在 at 段有没有被表达出来。
                        #
                        #   现在把 at 段转成人可读文本：
                        #     @机器人  → "[@you]"（一眼看出在叫自己）
                        #     @别人    → "[@QQ号]"
                        #   注意保留在 text 里而不是只塞标志位：要看到上下文才知道
                        #   "是叫我" 还是 "在叫别人顺便提了一嘴"。
                        at_qq = str(elem.get("data", {}).get("qq", ""))
                        if at_qq == str(BOT_QQ):
                            text_parts.append("[@you]")
                            at_me = True
                        elif at_qq and at_qq != "all":
                            text_parts.append(f"[@{at_qq}]")
                        elif at_qq == "all":
                            text_parts.append("[@all]")
                    elif elem.get("type") == "reply":
                        # 引用回复：保留被引用消息的 id，方便 Agent 理解上下文
                        rid = elem.get("data", {}).get("id")
                        if rid:
                            text_parts.append(f"[reply:{rid}]")
                    elif elem.get("type") == "face":
                        # QQ 小黄脸表情，例如 [CQ:face,id=178]
                        fid = elem.get("data", {}).get("id")
                        if fid:
                            text_parts.append(f"[face:{fid}]")
                    elif elem.get("type") in ("json", "xml"):
                        # 🔴 新增：链接/视频卡片。
                        #   实测 B 站分享过来是 [CQ:json,data={...}]，里面有
                        #   qqdocurl 和 preview；旧代码整段丢弃，Agent 完全
                        #   不知道有人发了视频链接。
                        #   这里解析成 "[video link·Bilibili]标题 url" 送出去。
                        raw_card = elem.get("data", {}).get("data", "")
                        if not raw_card:
                            # 有的实现把内容放在 data 本身
                            raw_card = elem.get("data") or {}
                        info = parse_card_link(raw_card)
                        if info:
                            text_parts.append(format_card_link(info))
                            has_link = True
                            link_infos.append(info)
                            logger.info(
                                f"[LINK_CARD] {info['kind']} {info['platform'] or '?'} "
                                f"-> {info['url'][:80]}")
                        else:
                            text_parts.append("[卡片]")
                if text_parts:
                    text = "".join(text_parts).strip()

            # 每个用户的冷却：窗口内的消息**不丢弃**，只打 throttled 标记照常投递。
            # 曾经是直接丢弃，结果「刚说完一句日常，紧跟着问的正事」被吞了。
            throttled = False
            throttle_gap = None
            if not is_owner:
                now_ts = time.time()
                last_seen_ts = user_last_seen.get(user_id, 0.0)
                if now_ts - last_seen_ts < USER_COOLDOWN_SECONDS:
                    throttled = True
                    throttle_gap = round(now_ts - last_seen_ts, 1)
                    logger.info(
                        f"[COOLDOWN] Deferred (not dropped) user {nickname} ({user_id}) - "
                        f"{throttle_gap}s < {USER_COOLDOWN_SECONDS}s"
                    )
                else:
                    user_last_seen[user_id] = now_ts
                    _sweep_cooldowns()      # 表变大时顺手清过期条目（有阈值，零开销）

            # Pillar 1: Absolute permission tagging
            #   主人身份必须**通过 NapCat 核验**才算数，不再只看 user_id 自报。
            #   见 _verify_message_id 上方说明。
            if is_owner:
                permission = "owner"
                role_tag = "[identity: owner (full permission)]"
            else:
                permission = "guest"
                role_tag = "[identity: guest (no permission to operate computer or phone)]"

            # 🔴 操作许可闸门。
            #   **必须放在入队之前** —— 这样群友的消息在进队列的那一刻
            #   就已经把票作废了，Agent 拿到这批消息时票已经是 false 的。
            #   如果放到入队之后，同一批里"主人刚授权 + 群友紧跟着说话"
            #   就有先后竞态。
            _desktop_grant_msg(user_id, nickname, is_owner)

            # 群过滤器（三段式，全部可在 config.json 里配）
            #
            #   block_groups  一条都不处理，顶层就丢了（见 handler 开头）
            #   allow_groups  ["*"] = 所有群按下面的规则；[] = 所有群都不理
            #   mention_only  = 只回被点名的（默认，最省 token）
            #
            # 为什么默认 mention_only：原来的规则带「问句关键词」，实测
            # **任何一句日常闲聊**都会把它叫醒，每次唤醒 = 一次完整 LLM 请求。
            # 日志铁证：同一个人 5 分钟内问了三遍「醒了吗」，三次全都触发了。
            # 「吃了吗 / 睡了吗 / 好看吗 / 你傻吗 / 去吗」全都会命中；「你笑什么」
            # 命中「什么」；「人上身了？」命中「？」，也就是等于没有过滤。
            # 真正的解法就是「只认点名」。想放宽就改 mode。
            if msg_type == "group":
                gf = CFG.get("group_filter", {})
                mode = (gf.get("mode") or "mention_only").lower()
                allow = gf.get("allow_groups") or []
                wildcard = "*" in allow or any(str(x) == "*" for x in allow)

                if not wildcard and group_id not in allow:
                    self._send_json_response({}, 200)
                    return

                if mode != "all":
                    # 先洗掉 URL 和媒体标签，避免 '?appid=...' 里的问号误触发
                    import re
                    clean_text = re.sub(
                        r'\[(?:image|voice|video|卡片|CQ)[^\]]*\]', '', text)
                    clean_text = re.sub(r'https?://\S+', '', clean_text).strip()

                    # 引用回复也算明确点名。
                    #   不能"有 reply 就放行"——群友互相回复也极常见，那样等于没过滤。
                    #   判定方式：抽出现有 reply 的 id，查是不是我们自己发的。
                    replied_to_me = False
                    reply_m = re.search(r'\[CQ:reply,id=(-?\d+)\]', raw_message or '')
                    if reply_m:
                        replied_to_me = _reply_target_is_bot(reply_m.group(1))

                    mentioned_bot = any(
                        alias and alias.lower() in text.lower()
                        for alias in BOT_NAMES)

                    is_targeted = (
                        str(BOT_QQ) in raw_message or
                        at_me or
                        mentioned_bot or
                        is_owner or
                        replied_to_me
                    )

                    is_question = False
                    if mode == "mention_or_question":
                        is_question = bool(clean_text) and any(
                            kw in clean_text for kw in
                            (gf.get("question_keywords") or []))

                    # 「带图 + 带话」也算有效意图。
                    #   曾经纯图+一句「看我发的表情」被静默丢弃 —— 图存下来了，
                    #   但 Agent 一句都没回，因为它既不是问句也没艾特人。
                    #   现在放宽成：**图 + 至少 2 个字的说明**。纯图仍丢弃
                    #   （群里随手一张图就把它叫起来，token 会烧穿）。
                    has_caption = has_image and len(clean_text) >= 2

                    # 群图防刷：同群 N 秒内最多放行 1 条带图消息，否则一个人
                    #   连甩 10 张表情包 = 叫醒 10 次。
                    #
                    # 🔴 修复过：原来冷却只在 `if has_caption` 里判，而放行判定
                    #   是短路的 —— 艾特/问句一旦成立，冷却压根没被检查。
                    #   实测艾特+图+说明在冷却中照样放行，连发 10 张每张都叫醒。
                    #   现在把冷却**提到放行判定之前**：只要带图就受冷却约束，
                    #   唯一的例外是主人。
                    image_ok = False
                    if has_image:
                        now_ts = time.time()
                        last_img = group_image_last_seen.get(group_id, 0.0)
                        if is_owner:
                            image_ok = True          # 主人豁免（与冷却同口径）
                        elif now_ts - last_img >= GROUP_IMAGE_COOLDOWN:
                            group_image_last_seen[group_id] = now_ts
                            image_ok = True
                        else:
                            logger.info(
                                f"[GROUP_IMG_COOLDOWN] Deferred image from "
                                f"{nickname} ({user_id}) - {int(now_ts - last_img)}s "
                                f"< {GROUP_IMAGE_COOLDOWN}s")

                    # 冷却拦图时这条**不进队列**（避免 Agent 再去看一遍图），
                    # 但"艾特 + 无图"照旧放行，冷却只管图。
                    if has_image and not image_ok:
                        self._send_json_response({}, 200)
                        return

                    # 链接卡片也算有效意图。
                    #   否则群友甩一条「看这个视频」的卡片进来，因为既没 @ 也不是
                    #   问句，会被这条过滤直接丢掉 —— Agent 压根不知道有这回事。
                    #   注意：卡片**不受图片冷却约束**（它不带图，不涉及刷屏看图）。
                    if not (is_targeted or is_question or
                            (has_caption and image_ok) or has_link):
                        self._send_json_response({}, 200)
                        return

            target_info = {
                "message_type": msg_type,
                "user_id": user_id,
                "group_id": group_id if msg_type == "group" else None,
                "nickname": nickname
            }
            save_last_target(target_info)

            normalized_msg = {
                "post_type": "message",
                "message_type": msg_type,
                "user_id": user_id,
                "group_id": group_id if msg_type == "group" else None,
                "nickname": nickname,
                "permission": permission,
                "role_tag": role_tag,
                # 这条消息之后，桌面/手机这只手还能不能用。
                #   只有主人发来的消息是 true；任何群友一来就翻成 false。
                #   Agent 应该在动手前看这个字段，而不是只看 role_tag。
                "desktop_allowed": permission == "owner",
                "text": text,
                "raw_message": raw_message,
                "message_id": message_id,
                # 图片：image_urls 是原始地址（一定有），image_files 是本地
                # 已存好的文件名（异步，可能晚一步到位，也可能为空）。
                "has_image": has_image,
                "image_count": len(image_urls),
                "image_urls": image_urls,
                "image_files": list(image_files),
                # 链接卡片的结构化信息。
                #   has_link 让 Agent 先判"这条要不要去读网页"，
                #   links 里每项含 {url,title,platform,kind}。
                "has_link": has_link,
                "links": link_infos,
                # 本轮是不是明确 @ 了机器人。
                #   text 里也已插入 "[@you]"，这里再给个布尔值，
                #   让它不必解析字符串就能判断优先级。
                "at_me": at_me,
                "time": data.get("time", int(time.time())),
                "formatted_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "throttled": throttled,
                "throttle_gap": throttle_gap
            }

            _enqueue(normalized_msg)
            if msg_type == "group":
                logger.info(f"--> [QQ GROUP {group_id}] {role_tag} from {nickname} ({user_id}): {text}")
            else:
                logger.info(f"--> [QQ PRIVATE] {role_tag} from {nickname} ({user_id}): {text}")

            self._send_json_response({}, 200)
            return

        self._send_json_response({}, 200)


# ==============================================================================
# Handler for the polling port (default 5702)
# ==============================================================================
class LongPollingHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    # ---- 注入口要用到的两个小工具 ----
    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return ""
        return self.rfile.read(length).decode("utf-8", "replace")

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        # desktop.py / phone.py 每次动手前必须来这里取票。
        #   只读、只回状态，不接受任何"我要授权"的参数 ——
        #   授权只能由「主人本人发一条 QQ 消息」触发，HTTP 请求改不了。
        if path == "/desktop_grant":
            body = json.dumps(_desktop_grant_get(),
                              ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/wait_msg":
            timeout = 60
            if "timeout" in query:
                try:
                    timeout = int(query["timeout"][0])
                except ValueError:
                    timeout = 60

            try:
                first_msg = msg_queue.get(timeout=timeout)
                batch = [first_msg]
                # Pillar 3: Batch Pop - drain all accumulated messages in queue
                while not msg_queue.empty() and len(batch) < 10:
                    try:
                        batch.append(msg_queue.get_nowait())
                    except queue.Empty:
                        break
                # B3：取出后立刻同步落盘，这样"已取走"的状态也是持久的。
                # 否则崩溃重启会把已经交给 Agent 的消息再喂一遍（重复回复）。
                _persist_queue()

                # Legacy listener scripts only read batch[0] and then return,
                # which silently discarded every other message in the batch.
                # Attaching the whole batch onto batch[0] means those scripts
                # still see everyone's message even without being changed.
                if len(batch) > 1:
                    first_msg["_batch"] = {
                        "count": len(batch),
                        "messages": [
                            {
                                "nickname": m.get("nickname"),
                                "user_id": m.get("user_id"),
                                "message_id": m.get("message_id"),
                                "role_tag": m.get("role_tag"),
                                "text": m.get("text"),
                                "has_image": m.get("has_image", False),
                                "image_count": m.get("image_count", 0),
                                "throttled": m.get("throttled", False),
                                "formatted_time": m.get("formatted_time"),
                            }
                            for m in batch
                        ],
                    }
                    first_msg["text"] = (
                        first_msg.get("text", "")
                        + "\n\n[同批还有 "
                        + str(len(batch) - 1)
                        + " 条消息，见 _batch 字段："
                        + " / ".join(
                            f"{m.get('nickname')}: {(m.get('text') or '')[:40]}"
                            for m in batch[1:]
                        )
                        + "]"
                    )

                response = {
                    "status": "ok",
                    "has_message": True,
                    "data": first_msg,  # backward compatibility
                    "messages": batch,   # full list of popped messages
                    "batch": batch,      # dual compatibility
                    "count": len(batch),
                    # 🔴 2026-10-23：把「这一批交出去之后，桌面这只手还能不能动」
                    #   一并告诉她。批次里只要混进一条群友消息，这里就是 False。
                    "desktop_grant": _desktop_grant_get(),
                }
            except queue.Empty:
                response = {
                    "status": "heartbeat",
                    "has_message": False,
                    "message": "No new messages received within timeout"
                }

            body = json.dumps(response, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        elif path == "/last_target":
            body = json.dumps(last_target, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        elif path in ("/health", "/status", "/"):
            status_data = {
                "status": "running",
                "queue_size": msg_queue.qsize(),
                "last_target": last_target,
                "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            body = json.dumps(status_data, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        """注入口：给**另一个进程或另一个 AI** 往队列里塞一条消息。

        ## 为什么需要它
        一个编码助手（或任何自动化脚本）想跟这个 QQ Agent 说句话，原来的路
        只有两条，都不好：
          1. 伪造一条 QQ 消息塞进事件口 —— 要自报 user_id，等于在**冒充主人**。
             事件口有防伪层，冒充主人的会被 [FORGED] 丢掉；冒充普通群友虽然
             能进，但那是骗它"有个真人说了话"，不诚实。
          2. 让主人自己转述 —— 每次都要真人当中转，不现实。
        所以开一个**独立的、说清楚自己不是主人**的入口。

        ## 安全边界（这是关键，不是可选项）
        · 只绑定 127.0.0.1（见 run_server），外网/同网段碰不到
        · 必须带 X-Inject-Token，值取自 workspace/.inject_token（首次自动生成）
        · **permission 强制为 "inject"，desktop_allowed 强制为 False**
          它永远开不了桌面/手机的门 —— 那扇门只认「主人本人真发了一条 QQ 消息」
        · user_id 用固定负数 -1，与任何真实 QQ 号都不冲突
        """
        parsed = urlparse(self.path)
        if parsed.path not in ("/inject", "/arch_msg"):    # /arch_msg 是旧名，保留兼容
            self.send_response(404)
            self.end_headers()
            return

        # 1) 只接受本机
        try:
            peer = self.client_address[0]
        except Exception:
            peer = ""
        if peer not in ("127.0.0.1", "::1"):
            logger.warning(f"[INJECT] rejected non-local peer {peer}")
            self._json({"status": "error", "message": "local only"}, 403)
            return

        # 2) token
        token = self.headers.get("X-Inject-Token") or self.headers.get("X-Arch-Token", "")
        if not _inject_token_ok(token):
            logger.warning("[INJECT] rejected bad token")
            self._json({"status": "error", "message": "bad token"}, 403)
            return

        # 3) body
        try:
            body = self._read_body()
            data = json.loads(body or "{}")
        except Exception as e:
            logger.error(f"[INJECT] bad json: {e}")
            self._json({"status": "error", "message": "invalid json"}, 400)
            return

        text = (data.get("text") or "").strip()
        if not text:
            self._json({"status": "error", "message": "empty text"}, 400)
            return
        if len(text) > 8000:
            text = text[:8000] + "...[truncated]"

        now = datetime.now()
        msg = {
            "post_type": "message",
            "message_type": "inject",         # 第三种类型：不是 private，也不是 group
            "user_id": ARCH_USER_ID,          # -1，固定，不对应任何真人
            "group_id": None,
            "nickname": ARCH_NICKNAME,
            # 🔴 这个身份**永远不是** owner。它没有桌面/手机许可。
            "permission": "inject",
            "role_tag": ARCH_ROLE_TAG,
            "desktop_allowed": False,
            "text": text,
            "raw_message": text,
            "message_id": data.get("message_id") or f"inject-{int(time.time()*1000)}",
            "has_image": False,
            "image_count": 0,
            "image_urls": [],
            "image_files": [],
            "has_link": False,
            "links": [],
            "at_me": True,                    # 是直接对它说的
            "time": int(time.time()),
            "formatted_time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "throttled": False,
            "throttle_gap": None,
            "injected": True,                 # 让它能明确分辨这条从哪来
        }

        # 注意：**不调用 _desktop_grant_msg** —— 注入方说话不改变桌面/手机票。
        # 这是刻意的：如果它说话也能续票，那"只有主人发消息才开门"这条就破了。
        # 注入方本身也是 AI，它可以被提示词骗 —— 一个能被骗的 AI 不能给别人发许可。
        _enqueue(msg)
        logger.info(f"[INJECT] --> {ARCH_NICKNAME}: {text[:80]}")
        self._json({"status": "ok", "queued": True,
                    "queue_size": msg_queue.qsize()})


def _inject_token_ok(given):
    """比对 workspace/.inject_token。文件不存在就生成一个再比。"""
    try:
        if not os.path.exists(ARCH_TOKEN_FILE):
            os.makedirs(os.path.dirname(ARCH_TOKEN_FILE) or ".", exist_ok=True)
            import secrets
            with open(ARCH_TOKEN_FILE, "w", encoding="utf-8") as f:
                f.write(secrets.token_hex(24))
            try:
                os.chmod(ARCH_TOKEN_FILE, 0o600)
            except Exception:
                pass
            logger.info(f"[INJECT] generated new token at {ARCH_TOKEN_FILE}")
        with open(ARCH_TOKEN_FILE, "r", encoding="utf-8") as f:
            want = f.read().strip()
        return bool(want) and hmac.compare_digest(given or "", want)
    except Exception as e:
        logger.error(f"[INJECT] token check failed: {e}")
        return False


class _ExclusiveServer(ThreadingHTTPServer):
    """独占端口的 HTTP server。

    ## 为什么必须关掉 allow_reuse_address

    这是本项目踩过的最隐蔽的一个坑，值得完整记下来。

    `http.server.HTTPServer` 的 `allow_reuse_address` 默认是 **1**，
    它在 `server_bind()` 里会设 `SO_REUSEADDR`。

    在 Linux 上，这个选项的语义是「TIME_WAIT 状态的端口可以复用」——
    两个**正在监听**的进程仍然不能抢同一个端口，所以无害。

    **但 Windows 的语义不一样：SO_REUSEADDR 允许两个进程绑同一个端口。**
    实测确认：

        s1 = socket(...); s1.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)
        s1.bind(('127.0.0.1', 15999)); s1.listen(1)   # OK
        s2 = socket(...); s2.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)
        s2.bind(('127.0.0.1', 15999))                  # ← 也 OK！

    后果：两个 daemon 实例可以同时"启动成功"，都打印
    `QQ bridge daemon started`，然后**连接被随机分给其中一个**。

    这解释了一个真实事故：验证部署流程时，测试实例和生产实例同时
    占着 5701/5702，测试请求有时打到生产 daemon 上，日志和状态
    互相矛盾，排查了很久才定位。用户遇到这个现象会完全摸不着头脑
    —— 改了配置没生效、日志和实际行为对不上、消息偶尔"丢"。

    ## 代价

    进程重启时如果上个进程还在 TIME_WAIT，可能要等几十秒才能重新绑。
    但那个代价是**可见的**（明确报错 + 明确原因），
    远好于「两个实例静默并存、行为随机」。
    """
    allow_reuse_address = False
    daemon_threads = True


def _bind_or_die(host, port, handler, label):
    """绑端口；被占用时给出**人能看懂**的错误并退出。

    用 _ExclusiveServer 而不是 ThreadingHTTPServer —— 理由见那个类的注释，
    简单说：Windows 上 allow_reuse_address=1 会让两个实例同时绑成功。
    """
    try:
        return _ExclusiveServer((host, int(port)), handler)
    except OSError as e:
        # 🔴 只走 logger，不另外 print。
        #   logger 已经挂了 FileHandler + basicConfig 的 stderr handler，
        #   再 print 一遍会让同一条错误在控制台上出现两次（实测很干扰）。
        #
        #   也不在消息里放 emoji：Windows 控制台默认编码可能是 GBK，
        #   一个 ⚠ 字符就能让**报错本身**抛 UnicodeEncodeError，
        #   用户看到的是一条 traceback 盖住了真正的原因。
        logger.error(
            "\n" + "=" * 62 + "\n"
            "启动失败：%s 端口 %s:%s 绑不上。\n"
            "\n"
            "系统报错：%s\n"
            "\n"
            "最常见的原因：已经有一个实例在跑了。\n"
            "\n"
            "查是谁占着（Windows）：\n"
            "  netstat -ano | findstr :%s\n"
            "  然后 tasklist /FI \"PID eq <上面那个PID>\"\n"
            "\n"
            "也可能：\n"
            "  - 端口写错了（config.json 的 listen 段）\n"
            "  - 上一次的进程没退干净（任务管理器里结束它）\n"
            "  - 别的程序恰好用了这个端口（改个端口号）\n"
            "\n"
            "不要忽略这个错误就继续 —— 你之后看到的所有现象\n"
            "都会来自那个旧的进程，而不是你刚启动的这个。\n"
            + "=" * 62,
            label, host, port, e, port)
        sys.exit(2)
        sys.exit(2)


def run_server():
    load_last_target()
    # 先把上次没被取走的消息装回队列，再开始收新的。
    # 必须在 serve_forever 之前，否则新消息会插到旧消息前面。
    restore_queue()
    # 把上次记下的"我们自己发的消息 id"读回来，否则重启后
    # 群友点「回复」追问会因查不到指向而被当闲聊丢掉。
    _load_self_ids()

    L = CFG["listen"]

    # 🔴 事件口只绑 127.0.0.1。
    #   NapCat 是 httpClient 模式，主动 POST 到 http://127.0.0.1:5701，
    #   **不需要**对外监听。绑 0.0.0.0 等于同网段任何一台设备都能直接
    #   伪造消息（实测过：从局域网另一台机器 POST 到 5701 返回 200）。
    #   轮询口默认也绑 127.0.0.1；Agent 跑在另一台机器上时才需要改。
    server_events = _bind_or_die(
        L["events_host"], L["events_port"], OneBotWebhookHandler, "事件口")
    server_poll = _bind_or_die(
        L["poll_host"], L["poll_port"], LongPollingHandler, "轮询口")

    # 🔴 注入口单独一个端口，**只绑本机**。
    #   为什么不复用轮询口：轮询口可能是 0.0.0.0，同网段任何机器都能连；
    #   把「能往队列里塞消息」的能力挂在那儿，等于把写权限开出局域网。
    #   独立端口 + 只绑本机 + 独立 token，三把锁。
    #
    #   注入口失败**不致命**（消息收发仍可用，只是另一个 AI 说不了话），
    #   所以这里只告警不退出 —— 但要把原因说清楚。
    if CFG.get("injection", {}).get("enabled", True):
        try:
            server_inject = _ExclusiveServer(
                (L["inject_host"], int(L["inject_port"])), LongPollingHandler)
            threading.Thread(target=server_inject.serve_forever,
                             daemon=True).start()
            logger.info("[INJECT] endpoint open: http://%s:%s/inject (local only)"
                        % (L["inject_host"], L["inject_port"]))
        except OSError as e:
            logger.warning(f"[INJECT] could not bind inject port "
                           f"(main service unaffected): {e}")

    threading.Thread(target=server_events.serve_forever, daemon=True).start()
    threading.Thread(target=server_poll.serve_forever, daemon=True).start()

    # 启动自检 —— 把关键脚本的磁盘 mtime 打出来。
    #
    # ## 为什么需要
    # 真实踩过一次：改完一个模块没重启，进程里还是旧代码，跑了一小时，
    # 排查时只能靠"行为对不对"反推，很被动。
    # 现在启动时直接打印各文件的修改时间和本次启动时间，
    # 一眼就能看出"我改的代码到底进没进这个进程"。
    # 以后凡是怀疑热更没生效，先看这几行。
    logger.info("-" * 60)
    logger.info("[SELFCHECK] loaded modules (restart this process after editing)")
    _base = os.path.dirname(os.path.abspath(__file__))
    _root = os.path.dirname(_base)
    for _label, _p in [
            ("bridge/daemon.py", os.path.join(_base, "daemon.py")),
            ("bridge/config.py", os.path.join(_base, "config.py")),
            ("bridge/step.py", os.path.join(_base, "step.py")),
            ("agents/desktop.py", os.path.join(_root, "agents", "desktop.py")),
            ("agents/phone.py", os.path.join(_root, "agents", "phone.py")),
    ]:
        try:
            _t = datetime.fromtimestamp(os.path.getmtime(_p))
            logger.info("[SELFCHECK]   %-22s mtime=%s" % (_label, _t))
        except Exception:
            logger.info("[SELFCHECK]   %-22s (not found)" % _label)
    logger.info("[SELFCHECK] process started = %s"
                % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    _n_media = 0
    try:
        _n_media = len(os.listdir(MEDIA_INBOX_DIR))
    except Exception:
        pass
    logger.info("[SELFCHECK] config=%s | bot=%s | owners=%s | "
                "media_inbox=%d files | gate ttl=%s"
                % (CFG.get("_source"), BOT_QQ, OWNER_QQS, _n_media,
                   ("unlimited" if DESKTOP_GRANT_TTL <= 0
                    else int(DESKTOP_GRANT_TTL))))
    logger.info("-" * 60)

    logger.info("=" * 60)
    logger.info("QQ bridge daemon started")
    logger.info("  events  : %s:%s" % (L["events_host"], L["events_port"]))
    logger.info("  polling : %s:%s" % (L["poll_host"], L["poll_port"]))
    logger.info("  inject  : %s:%s (local only)"
                % (L["inject_host"], L["inject_port"]))
    logger.info("  gate    : %s"
                % ("owner message grants, no expiry"
                   if DESKTOP_GRANT_TTL <= 0
                   else "owner message grants for %ds" % int(DESKTOP_GRANT_TTL)))
    logger.info("=" * 60)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server_events.shutdown()
        server_poll.shutdown()

if __name__ == "__main__":
    if sys.stdout.encoding != 'utf-8':
        try:
            sys.stdout.reconfigure(encoding='utf-8')
        except Exception:
            pass
    run_server()
