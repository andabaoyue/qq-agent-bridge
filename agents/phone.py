#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
phone.py — 手机控制台（adb 的一层薄封装）

设计原则
  1. 一条命令干一件事，参数越少越好，输出对人（和模型）都易读。
  2. 任何动作都记日志到 workspace/phone_log.jsonl，出事后能回溯她到底按了什么。
  3. 没有设备时给出人话级的提示，不抛 stacktrace。

常用流程（看屏幕 → 看懂 → 点）
    python phone.py look            # 截图 + UI 树，一次调用拿到"屏幕上有什么"
    python phone.py tap 540 1620    # 点某个坐标
    python phone.py text "hello"    # 打字
    python phone.py key back        # 返回键

完整子命令见 python phone.py --help
"""

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import utf8_console  # noqa: F401  （导入即生效：控制台 UTF-8）

# 本脚本放在 agents/ 下，日志和截图统一落到仓库根的 workspace/
WORKSPACE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "workspace")
os.makedirs(WORKSPACE, exist_ok=True)
SHOT_DIR = os.path.join(WORKSPACE, "phone_shots")
LOG_PATH = os.path.join(WORKSPACE, "phone_log.jsonl")

# adb 的候选位置：PATH 之外，winget 包（本机装的就是这个）也要能找到
ADB_CANDIDATES = [
    "adb",
    os.path.expandvars(
        r"%LOCALAPPDATA%\Microsoft\WinGet\Packages"
        r"\Google.PlatformTools_Microsoft.Winget.Source_8wekyb3d8bbwe\platform-tools\adb.exe"
    ),
    os.path.expandvars(r"%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe"),
    os.path.expandvars(r"%ANDROID_HOME%\platform-tools\adb.exe"),
    os.path.expandvars(r"%ANDROID_SDK_ROOT%\platform-tools\adb.exe"),
    r"C:\platform-tools\adb.exe",
]

# keyevent 名字 → 键码。只暴露常用的，其余可用 `phone.py key 123` 直接给数字。
KEYS = {
    "back": 4, "home": 3, "recents": 187, "enter": 66, "del": 67,
    "tab": 61, "esc": 111, "space": 62, "menu": 82,
    "power": 26, "wakeup": 224, "sleep": 223,
    "volume_up": 24, "volume_down": 25, "mute": 164,
    "up": 19, "down": 20, "left": 21, "right": 22,
    "page_up": 92, "page_down": 93,
    "camera": 27, "search": 84, "notification": 83,
}

ADB_KEYBOARD_PKG = "com.android.adbkeyboard"
# 实测（2026-10-24，小米13/HyperOS3）：真正的输入法服务是 .AdbIME，
# 网上文档里流传的 .ADBKeyboard 是错的，用错会报 "Unknown input method"。
ADB_IME = f"{ADB_KEYBOARD_PKG}/.AdbIME"

# 和 desktop.py 共用同一张票：许可只存在于 qq_daemon.py 的内存里，
# 主人发消息才置 granted（不设期限，直到被撤销），任何群友发言立刻作废，daemon 没跑就一律拒绝。
# 手机比桌面更私密（聊天记录、相册、支付），所以只有 devices 这种不含内容的基础查询免票。
# 闸门地址。默认 5702，可用环境变量 QQ_BRIDGE_GATE 覆盖，
# 例如把 daemon 跑在另一台机器上时：
#   set QQ_BRIDGE_GATE=http://192.168.1.10:5702/desktop_grant
GRANT_URL = os.environ.get(
    "QQ_BRIDGE_GATE", "http://127.0.0.1:5702/desktop_grant")
FREE_ACTIONS = {"devices", "diagnose"}


class PhoneError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# adb 找路与调用
# --------------------------------------------------------------------------

_ADB = None


def find_adb():
    global _ADB
    if _ADB:
        return _ADB
    for cand in ADB_CANDIDATES:
        if os.path.sep in cand or cand.lower().endswith(".exe"):
            if os.path.isfile(cand):
                _ADB = cand
                return _ADB
        else:
            found = shutil.which(cand)
            if found:
                _ADB = found
                return _ADB
    raise PhoneError(
        "找不到 adb。先装 Android platform-tools：\n"
        "  winget install --id Google.PlatformTools -e"
    )


def adb(*args, serial=None, binary=False, timeout=30, check=True):
    """跑一条 adb 命令。binary=True 时返回 bytes，否则返回去尾空白的 str。"""
    cmd = [find_adb()]
    if serial:
        cmd += ["-s", serial]
    cmd += [str(a) for a in args]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        raise PhoneError(f"adb 超时（{timeout}s）：{' '.join(cmd[1:])}")

    if binary:
        if proc.returncode != 0:
            raise PhoneError(f"adb 失败：{proc.stderr.decode('utf-8', 'replace').strip()}")
        return proc.stdout

    out = proc.stdout.decode("utf-8", "replace").strip()
    err = proc.stderr.decode("utf-8", "replace").strip()
    # adb shell 的 "error: ..." 也可能是致命错误，但 returncode 仍为 0
    if check and proc.returncode != 0:
        raise PhoneError(err or out or f"adb 退出码 {proc.returncode}")
    return out if proc.returncode == 0 else (err or out)


def list_devices():
    """返回 [(serial, state, model)]，state 常见为 device / unauthorized / offline。"""
    raw = adb("devices", "-l", check=False)
    devs = []
    for line in raw.splitlines()[1:]:
        line = line.strip()
        if not line or line.startswith("*"):
            continue
        parts = line.split()
        serial, state = parts[0], parts[1] if len(parts) > 1 else "?"
        model = ""
        for p in parts[2:]:
            if p.startswith("model:"):
                model = p.split(":", 1)[1]
        devs.append((serial, state, model))
    return devs


def _pnp_android():
    """用 Windows 的设备列表侧写『手机插着但 adb 看不见』这种状态。

    返回 [{"status","class","name","instance"}]，只保留跟手机有关的条目。
    """
    ps = (
        "Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | "
        "Where-Object { $_.InstanceId -like '*VID_2717*' -or "
        "$_.InstanceId -like '*VID_18D1*' -or $_.InstanceId -like '*VID_22D9*' -or "
        "$_.InstanceId -like '*VID_12D1*' -or $_.Class -in @('WPD','AndroidUsbDeviceClass') -or "
        "$_.FriendlyName -match 'Xiaomi|Redmi|POCO|OPPO|vivo|HUAWEI|Honor|Samsung|OnePlus|realme|Pixel|ADB|Android' } | "
        "Select-Object Status,Class,FriendlyName,InstanceId | ConvertTo-Json -Compress"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.decode("utf-8", "replace").strip()
        if not out:
            return []
        data = json.loads(out)
        if isinstance(data, dict):
            data = [data]
        return [{"status": d.get("Status"), "class": d.get("Class"),
                 "name": d.get("FriendlyName"), "instance": d.get("InstanceId") or ""}
                for d in data]
    except Exception:
        return []


def diagnose_no_device():
    """没有设备时给出**基于实际证据**的诊断，而不是背一遍清单。"""
    lines = []
    devs = list_devices()
    n = len(devs)
    # adb 自己在不在
    try:
        ver = adb("version", check=False).splitlines()[0]
        lines.append(f"√ adb 可用：{ver}")
    except PhoneError as e:
        return str(e)

    if n == 0:
        lines.append("✗ adb 看不到任何设备（既没有 device，也没有 unauthorized）")
    else:
        for s, st, m in devs:
            lines.append(f"· {s} state={st} model={m}")

    phones = _pnp_android()
    if not phones:
        lines.append("✗ Windows 这边也没认出任何手机 —— 线没插、是纯充电线，或换根线/换个 USB 口")
        lines.append("")
        lines.append("结论：手机不在电脑上。插上数据线再来一次。")
        return "\n".join(lines)

    for p in phones:
        lines.append(f"· Windows 看到：{p['name']}  [{p['class']}/{p['status']}]")

    has_adb_iface = any("ADB" in (p["name"] or "").upper() for p in phones)
    has_wpd = any(p["class"] == "WPD" for p in phones)

    lines.append("")
    if n == 0 and has_wpd and not has_adb_iface:
        lines.append("结论：手机确实插着（Windows 认得出它是手机，能以文件设备访问），")
        lines.append("      但**没有任何 ADB 接口** —— 说明手机上的「USB 调试」开关没打开。")
        lines.append("")
        lines.append("在手机上做（小米/澎湃OS）：")
        lines.append("  1) 设置 → 我的设备 → 全部参数与信息 → 连点「OS 版本」7 次（你已经做过了）")
        lines.append("  2) 设置 → 更多设置 → 开发者选项 → 打开「USB 调试」")
        lines.append("  3) 顺手把「USB 调试（安全设置）」也打开 —— 小米不加这个，" )
        lines.append("     adb 的点击/滑动/打字会被系统拒掉（INJECT_EVENTS），截图和读界面倒是能用")
        lines.append("  4) 插拔一次数据线，手机弹「允许 USB 调试吗？」→ 勾「始终允许」→ 允许")
        return "\n".join(lines)

    if n == 0 and phones:
        lines.append("结论：Windows 认得手机，但没有可用的 ADB 通道。")
        lines.append("常见原因：USB 调试关了 / 授权弹窗没点 / 小米驱动没装 / 接口接触不良。")
        lines.append("先把「USB 调试」打开、拔插一次线看有没有授权弹窗。")
        return "\n".join(lines)

    return "\n".join(lines) or "状态未知"


def pick_device(serial=None, need=True):
    devs = list_devices()
    ready = [d for d in devs if d[1] == "device"]
    if serial:
        if any(d[0] == serial and d[1] == "device" for d in devs):
            return serial
        raise PhoneError(f"设备 {serial} 不在线。当前：{devs or '无'}")
    if len(ready) == 1:
        return ready[0][0]
    if len(ready) > 1:
        raise PhoneError(
            "接了不止一台设备，用 --device 指定序列号：" + ", ".join(d[0] for d in ready)
        )
    if need:
        if not devs:
            raise PhoneError(diagnose_no_device())
        states = ", ".join(f"{s}（{st}）" for s, st, _ in devs)
        if any(st == "unauthorized" for _, st, _ in devs):
            raise PhoneError(
                f"手机连上了但没授权：{states}\n"
                "请解锁手机，在弹出的「允许 USB 调试吗？」里勾上「始终允许」再点允许。"
                "如果没弹窗，拔插一次数据线，或到开发者选项里点「撤销 USB 调试授权」后重插。"
            )
        raise PhoneError(f"设备状态不对：{states}")
    return None


# --------------------------------------------------------------------------
# 闸门
# --------------------------------------------------------------------------

def check_grant():
    """问 daemon 要票。连不上 / 超时 / 格式不对 一律当**没有许可**（fail-closed）。"""
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(GRANT_URL, timeout=3) as r:
            data = json.loads(r.read().decode("utf-8"))
        return bool(data.get("granted")), data
    except Exception as e:
        return False, {"granted": False,
                       "reason": f"daemon_unreachable: {type(e).__name__}: {e}"}


def require_grant(action):
    granted, info = check_grant()
    if granted:
        return
    print(
        "[DENIED] 现在没有手机操作许可。\n"
        "手机比电脑还私密——相册、聊天记录、支付都在里面，所以这道门和桌面那边是同一张票：\n"
        "  只有主人本人发消息才会开，不设期限（直到被撤销）；任何群友发言立刻作废；\n"
        "  daemon 没在跑或连不上，同样一律拒绝。\n"
        f"daemon 回报：{json.dumps(info, ensure_ascii=False)}"
    )
    log(f"denied:{action}", info, ok=False)
    raise SystemExit(3)


# --------------------------------------------------------------------------
# 日志
# --------------------------------------------------------------------------

_CHAIN_CACHE = {"ts": 0.0, "chain": None}
_CHAIN_TTL = 60.0


def _parent_chain(depth=6):
    """沿父进程往上走，返回 [(pid, name)]。

    ## 2026-10-24 性能事故（必须记住）
    第一版是**循环里每次调一次 PowerShell**（depth=4 就是 4 个进程）。
    实测单次 `_parent_chain()` **2504 ms**，而 `log()` 每次都要调它 ——
    于是她每点一下手机日志就卡 2.5 秒，一次 look + 3 次 tap 白等 10 秒。
    **加诊断信息反而把主功能拖慢了，这是典型的帮倒忙。**

    修法：**一次 PowerShell 拿全表**，再在内存里沿链走；结果按 TTL 缓存
    （同一进程反复调 log() 时不用重复查）。

    为什么不用 psutil：本机没装，不想为一个诊断字段引入新依赖。
    """
    now = time.time()
    if _CHAIN_CACHE["chain"] is not None and now - _CHAIN_CACHE["ts"] < _CHAIN_TTL:
        return _CHAIN_CACHE["chain"]

    chain = []
    try:
        ps = (
            "Get-CimInstance Win32_Process "
            "| Select-Object ProcessId,ParentProcessId,Name,CommandLine "
            "| ConvertTo-Json -Compress"
        )
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.decode("utf-8", "replace").strip()
        if out:
            table = json.loads(out)
            if isinstance(table, dict):
                table = [table]
            by_pid = {}
            for d in table:
                try:
                    by_pid[int(d.get("ProcessId"))] = (
                        d.get("ParentProcessId") or 0,
                        d.get("Name") or "",
                        d.get("CommandLine") or "",
                    )
                except Exception:
                    continue
            pid = os.getpid()
            for _ in range(depth):
                hit = by_pid.get(pid)
                if not hit:
                    break
                ppid, name, cmdline = hit
                chain.append((pid, name, cmdline))
                if not ppid:
                    break
                pid = int(ppid)
    except Exception:
        chain = []

    _CHAIN_CACHE["ts"] = now
    _CHAIN_CACHE["chain"] = chain
    return chain


def _caller():
    """判断这一条日志是谁触发的。返回 (caller, source)。

    source 的意义（2026-10-24 补）：
      "env"    —— 调用方自己声明了 PHONE_CALLER。**这是自报，可以伪造**，
                  谁都能设成对方的名字。所以 who_called.py 会把它标成「(自报)」。
      "ppid"   —— 从父进程链推断，不依赖调用方诚实，可信度更高。
      "unknown"—— 推断不出来，如实说不知道。

    优先级：显式声明 > 父进程链。但可信度相反，所以两个都记下来。

    ## 为什么不用环境变量当唯一依据
    同一个 phone.py 被多个调用方共用时，事后分不清谁调的。
    如果只靠环境变量，被调方设一个 PHONE_CALLER=coder 就能把责任推给编码助手 ——
    那这个字段不但没用，还会制造假证据。父进程链是外部可观测的，改不了。
    """
    explicit = (os.environ.get("PHONE_CALLER") or "").strip()
    try:
        chain = _parent_chain()
    except Exception:
        chain = []
    # 🔴 2026-10-24 实测修正：**进程名里不会出现 standby_listener**。
    #   standby_listener.py 是 `python.exe standby_listener.py` 跑起来的，
    #   Name 永远是 "python.exe"，脚本名只出现在 CommandLine 里。
    #   第一版只匹配 Name，实测判成了 coder —— 差点把一个假判断当成修好了。
    #   所以必须把 Name 和 CommandLine 拼起来一起匹配。
    haystack = " ".join(
        ((n or "") + " " + (c or "")).lower() for _, n, c in chain
    )
    pids = [p for p, _, _ in chain if p]

    # 先算父进程链的结论（不依赖自报）
    inferred = None
    if any(k in haystack for k in ("standby_listener", "agent_runtime")):
        inferred = "agent"
    elif any(k in haystack for k in ("dsh", "harness", "electron", "node")):
        inferred = "coder"
    elif pids:
        inferred = "unknown(ppid=%s)" % pids[0]
    else:
        inferred = "unknown"

    if explicit:
        # 自报与推断不一致时，两个都留，别替人圆场
        if inferred and not inferred.startswith("unknown") and inferred != explicit:
            return explicit, "env(conflicts-with-ppid:%s)" % inferred
        return explicit, "env"
    return inferred, "ppid" if not inferred.startswith("unknown") else "unknown"


def log(action, detail=None, ok=True):
    try:
        os.makedirs(os.path.dirname(LOG_PATH) or ".", exist_ok=True)
        who, how = _caller()
        rec = {
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "action": action,
            "detail": detail,
            "ok": ok,
            # 🔴 2026-10-24 新增：谁调的。缺了这一格，事后只能靠时间戳猜。
            "caller": who,
            "caller_source": how,
            "pid": os.getpid(),
        }
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 日志失败不能挡住正事


# --------------------------------------------------------------------------
# 屏幕：截图
# --------------------------------------------------------------------------

def _png_ok(data):
    return data[:8] == b"\x89PNG\r\n\x1a\n"


def screenshot(serial=None, prefix="screen"):
    os.makedirs(SHOT_DIR, exist_ok=True)
    name = f"{prefix}_{datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
    path = os.path.join(SHOT_DIR, name)

    # 首选 exec-out（不经 Windows 换行转换）。个别机型会坏，坏了退回文件方式。
    data = b""
    try:
        data = adb("exec-out", "screencap", "-p", serial=serial, binary=True, timeout=30)
    except PhoneError:
        data = b""

    if not _png_ok(data):
        remote = "/sdcard/__phone_shot.png"
        adb("shell", "screencap", "-p", remote, serial=serial, timeout=30)
        data = adb("exec-out", "cat", remote, serial=serial, binary=True, timeout=30)

    if not _png_ok(data):
        raise PhoneError("截图失败：拿到的不是 PNG（手机可能锁屏或在权限对话框上）")

    with open(path, "wb") as f:
        f.write(data)

    # 可选：缩小，省 token。装了 Pillow 才做。
    try:
        from PIL import Image  # noqa
        small = os.path.join(SHOT_DIR, name.replace(".png", "_small.jpg"))
        with Image.open(path) as im:
            im = im.convert("RGB")
            w, h = im.size
            scale = min(1.0, 720.0 / max(w, h))
            if scale < 1.0:
                im = im.resize((int(w * scale), int(h * scale)))
            im.save(small, "JPEG", quality=72)
        return path, small
    except Exception:
        return path, None


def screen_size(serial=None):
    """(当前显示宽, 当前显示高)。注意横向时会和 wm size 的物理值相反。"""
    raw = adb("shell", "wm", "size", serial=serial, check=False)
    m = re.search(r"(\d+)x(\d+)", raw or "")
    w, h = (int(m.group(1)), int(m.group(2))) if m else (1080, 2400)
    # 横屏时实际坐标空间是反过来的，UI 树和截图都按当前方向给
    rot = adb("shell", "dumpsys", "window", "displays", serial=serial, check=False)
    if "mRotation=ROTATION_90" in (rot or "") or "mRotation=ROTATION_270" in (rot or ""):
        w, h = h, w
    return w, h


# --------------------------------------------------------------------------
# 屏幕：UI 树（比纯截图更好用——直接拿到可点的东西和它的文字）
# --------------------------------------------------------------------------

def dump_ui(serial=None):
    remote = "/sdcard/__ui_dump.xml"
    for attempt in range(3):
        adb("shell", "uiautomator", "dump", remote, serial=serial, timeout=40, check=False)
        xml_text = adb("exec-out", "cat", remote, serial=serial, timeout=30, check=False)
        if xml_text and "<hierarchy" in xml_text:
            return xml_text
        time.sleep(0.6)
    raise PhoneError("UI dump 失败（连续 3 次）。屏幕可能正好在动画/加载，稍等再试。")


def _bounds(s):
    m = re.findall(r"-?\d+", s or "")
    if len(m) >= 4:
        x1, y1, x2, y2 = map(int, m[:4])
        return x1, y1, x2, y2, (x1 + x2) // 2, (y1 + y2) // 2
    return None


def parse_nodes(xml_text):
    root = ET.fromstring(xml_text)
    out = []

    def walk(node):
        a = node.attrib
        text = (a.get("text") or "").strip()
        desc = (a.get("content-desc") or "").strip()
        rid = (a.get("resource-id") or "").strip()
        clickable = a.get("clickable") == "true"
        long_clickable = a.get("long-clickable") == "true"
        checkable = a.get("checkable") == "true"
        checked = a.get("checked") == "true"
        enabled = a.get("enabled") != "false"
        b = _bounds(a.get("bounds"))
        interesting = bool(text or desc or clickable or long_clickable or checkable)
        if interesting and b:
            out.append({
                "text": text, "desc": desc, "id": rid,
                "clickable": clickable, "long": long_clickable,
                "checkable": checkable, "checked": checked,
                "enabled": enabled,
                "bounds": a.get("bounds"), "center": (b[4], b[5]),
                "class": (a.get("class") or "").split(".")[-1],
                "pkg": a.get("package") or "",
            })
        for ch in node:
            walk(ch)

    walk(root)
    return out


def render_nodes(nodes, limit=120):
    if not nodes:
        return (
            "  （UI 树是空的 —— 这个画面多半是游戏 / 视频 / 投屏这类自绘内容，\n"
            "   系统拿不到任何控件。这种界面只能看截图，坐标得靠猜，\n"
            "   点之前务必先 screenshot 确认，别闭眼乱点。）"
        )
    lines = []
    for i, n in enumerate(nodes[:limit]):
        tags = []
        if n["clickable"]:
            tags.append("点")
        if n["long"]:
            tags.append("长按")
        if n["checkable"]:
            tags.append("勾选" + ("✓" if n["checked"] else ""))
        if not n["enabled"]:
            tags.append("禁用")
        label = n["text"] or n["desc"] or n["id"] or n["class"]
        label = label.replace("\n", " ")[:60]
        extra = f" id={n['id'].split('/')[-1]}" if n["id"] else ""
        cx, cy = n["center"]
        lines.append(
            f"  [{i:>2}] {label!r} <{'/'.join(tags) or '静态'}> "
            f"@{cx},{cy}{extra}"
        )
    if len(nodes) > limit:
        lines.append(f"  …还有 {len(nodes) - limit} 个节点被省略（用 --limit 放大）")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 输入
# --------------------------------------------------------------------------

def _has_adb_keyboard(serial=None):
    out = adb("shell", "pm", "list", "packages", serial=serial, check=False)
    return ADB_KEYBOARD_PKG in (out or "")


def type_text(text, serial=None):
    """ASCII 走 input text；中文/emoji 走 ADBKeyboard 广播。"""
    ascii_only = all(ord(c) < 128 for c in text)
    if ascii_only:
        esc = (text.replace("\\", "\\\\").replace(" ", "%s")
                   .replace("'", "\\'").replace('"', '\\"')
                   .replace("&", "\\&").replace("<", "\\<").replace(">", "\\>")
                   .replace("(", "\\(").replace(")", "\\)")
                   .replace("|", "\\|").replace(";", "\\;"))
        adb("shell", "input", "text", esc, serial=serial)
        return "input text"

    if _has_adb_keyboard(serial=serial):
        # ADBKeyboard 只在「它是当前输入法」时才收广播，所以临时切过去，打完再切回来。
        original = adb("shell", "settings", "get", "secure", "default_input_method",
                       serial=serial, check=False).strip()
        switched = False
        if original != ADB_IME:
            adb("shell", "ime", "set", ADB_IME, serial=serial, check=False)
            time.sleep(0.6)
            switched = True
        try:
            b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
            adb("shell", "am", "broadcast", "-a", "ADB_INPUT_B64",
                "--es", "msg", b64, serial=serial, check=False)
            time.sleep(0.4)
        finally:
            if switched and original and original != "null":
                adb("shell", "ime", "set", original, serial=serial, check=False)
        return "ADBKeyboard(b64)" + ("（已自动切回原输入法）" if switched else "")

    raise PhoneError(
        "这段文字含非 ASCII 字符（中文/emoji），而手机没装 ADBKeyboard 输入法。\n"
        "装它：python phone.py setup-input\n"
        "（若安装被拒，见 phone-control 技能里「USB安装管理」那一节）"
    )


# --------------------------------------------------------------------------
# 子命令
# --------------------------------------------------------------------------

def cmd_devices(a):
    devs = list_devices()
    if not devs:
        print("没有设备。")
        return 0
    for serial, state, model in devs:
        mark = "✅" if state == "device" else "⚠️"
        print(f"{mark} {serial}  state={state}  model={model or '未知'}")
    return 0


def cmd_diagnose(a):
    """不需要许可：只回答「为什么连不上」，不碰手机内容。"""
    print(diagnose_no_device() if not list_devices() else
          "adb 已经能看到设备，直接跑 python phone.py status 就行。")
    return 0


def cmd_status(a):
    serial = pick_device(a.device)
    model = adb("shell", "getprop", "ro.product.model", serial=serial, check=False)
    brand = adb("shell", "getprop", "ro.product.brand", serial=serial, check=False)
    ver = adb("shell", "getprop", "ro.build.version.release", serial=serial, check=False)
    sdk = adb("shell", "getprop", "ro.build.version.sdk", serial=serial, check=False)
    w, h = screen_size(serial)
    battery = adb("shell", "dumpsys", "battery", serial=serial, check=False)
    level = re.search(r"level: (\d+)", battery or "")
    charging = "charging" in (battery or "") and "USB powered: true" in (battery or "")
    cur = adb("shell", "dumpsys", "window", serial=serial, check=False)
    focus = "未知"
    if cur:
        # 优先 mFocusedApp 的 pkg/Activity 形式，它不会被 Window{...} 里的空格截断
        m = re.search(r"mFocusedApp=ActivityRecord\{\S+ \S+ (\S+)}", cur)
        if not m:
            m = re.search(r"mCurrentFocus=Window\{\S+ \S+ (\S+)}", cur)
        if m:
            focus = m.group(1)

    print(f"设备     {brand} {model}")
    print(f"Android  {ver} (API {sdk})")
    print(f"屏幕     {w}x{h}")
    print(f"电量     {level.group(1) + '%' if level else '未知'}{'（充电中）' if charging else ''}")
    print(f"当前界面 {focus}")
    print(f"已装 ADBKeyboard  {'是' if _has_adb_keyboard(serial) else '否（中文输入受限）'}")
    # 🔴 2026-10-24 自检发现：status 是唯一漏了日志的命令。
    #   它只读、不改变手机，但**读了屏幕内容和当前界面**，这本身就该留痕——
    #   否则「谁在什么时间看过我的手机」查不出来。
    log("status", {"focus": focus, "level": level.group(1) if level else None})
    return 0


def cmd_shot(a):
    serial = pick_device(a.device)
    path, small = screenshot(serial, prefix=a.prefix)
    log("screenshot", {"path": path})
    print(f"截图   {path}")
    if small:
        print(f"缩略图 {small}（发给人看/省 token 用这个）")
    print(f"尺寸   {os.path.getsize(path) // 1024} KB")
    return 0


def cmd_ui(a):
    serial = pick_device(a.device)
    nodes = parse_nodes(dump_ui(serial))
    log("ui", {"count": len(nodes)})
    print(f"可交互节点 {len(nodes)} 个（坐标为点击中心点）：")
    print(render_nodes(nodes, limit=a.limit))
    return 0


def cmd_look(a):
    serial = pick_device(a.device)
    w, h = screen_size(serial)
    path, small = screenshot(serial, prefix="look")
    nodes = parse_nodes(dump_ui(serial))
    log("look", {"path": path, "count": len(nodes)})
    print(f"屏幕 {w}x{h}")
    print(f"截图 {small or path}")
    print(f"可交互 {len(nodes)} 个：")
    print(render_nodes(nodes, limit=a.limit))
    if a.send_to and a.send_to.isdigit():
        print("（提示：要发图给她自己，用 qq_step.py --image <上面的路径> --user_id ... )")
    return 0


def cmd_tap(a):
    serial = pick_device(a.device)
    adb("shell", "input", "tap", a.x, a.y, serial=serial)
    log("tap", {"x": a.x, "y": a.y})
    print(f"已点击 ({a.x}, {a.y})")
    if a.settle:
        time.sleep(a.settle)
    return 0


def cmd_swipe(a):
    serial = pick_device(a.device)
    adb("shell", "input", "swipe", a.x1, a.y1, a.x2, a.y2, a.ms, serial=serial)
    log("swipe", {"from": [a.x1, a.y1], "to": [a.x2, a.y2], "ms": a.ms})
    print(f"已滑动 ({a.x1},{a.y1}) → ({a.x2},{a.y2}) 用时 {a.ms}ms")
    if a.settle:
        time.sleep(a.settle)
    return 0


def cmd_longpress(a):
    serial = pick_device(a.device)
    adb("shell", "input", "swipe", a.x, a.y, a.x, a.y, a.ms, serial=serial)
    log("longpress", {"x": a.x, "y": a.y, "ms": a.ms})
    print(f"已长按 ({a.x}, {a.y}) {a.ms}ms")
    if a.settle:
        time.sleep(a.settle)
    return 0


def cmd_text(a):
    serial = pick_device(a.device)
    how = type_text(a.text, serial=serial)
    log("text", {"text": a.text, "via": how})
    print(f"已输入（{how}）：{a.text}")
    if a.enter:
        adb("shell", "input", "keyevent", 66, serial=serial)
        print("已回车")
    return 0


def cmd_key(a):
    serial = pick_device(a.device)
    k = a.key.lower()
    code = KEYS.get(k, k if k.isdigit() else None)
    if code is None:
        raise PhoneError(f"不认识的键 {a.key!r}。可用：{', '.join(sorted(KEYS))}，或直接给键码数字。")
    adb("shell", "input", "keyevent", code, serial=serial)
    log("key", {"key": a.key, "code": code})
    print(f"已按键 {a.key}（{code}）")
    if a.settle:
        time.sleep(a.settle)
    return 0


def cmd_open(a):
    serial = pick_device(a.device)
    target = a.target
    if "/" in target:
        adb("shell", "am", "start", "-n", target, serial=serial)
    else:
        adb("shell", "monkey", "-p", target, "-c",
            "android.intent.category.LAUNCHER", "1", serial=serial, check=False)
    log("open", {"target": target})
    print(f"已尝试启动 {target}")
    time.sleep(a.settle)
    return 0


def cmd_apps(a):
    serial = pick_device(a.device)
    out = adb("shell", "pm", "list", "packages", "-3", serial=serial, check=False)
    pkgs = sorted(p.replace("package:", "").strip() for p in (out or "").splitlines() if p.strip())
    if a.filter:
        pkgs = [p for p in pkgs if a.filter.lower() in p.lower()]
    print(f"第三方应用 {len(pkgs)} 个：")
    for p in pkgs:
        print("  " + p)
    return 0


def cmd_shell(a):
    serial = pick_device(a.device)
    out = adb("shell", *a.shell_args, serial=serial, check=False, timeout=a.timeout)
    log("shell", {"cmd": a.shell_args})
    print(out or "(无输出)")
    return 0


def cmd_pull(a):
    serial = pick_device(a.device)
    dest = a.local or os.path.join(WORKSPACE, os.path.basename(a.remote.rstrip("/")))
    adb("pull", a.remote, dest, serial=serial, timeout=300)
    log("pull", {"remote": a.remote, "local": dest})
    print(f"已拉到 {dest}")
    return 0


def cmd_push(a):
    serial = pick_device(a.device)
    adb("push", a.local, a.remote, serial=serial, timeout=300)
    log("push", {"local": a.local, "remote": a.remote})
    print(f"已推到 {a.remote}")
    return 0


def cmd_clipboard(a):
    """把文字放进手机剪贴板。需要设备上有 cmd clipboard（Android 10+ 多数有）。"""
    serial = pick_device(a.device)
    out = adb("shell", "cmd", "clipboard", "set", a.text, serial=serial, check=False)
    if "Exception" in (out or "") or "Unknown" in (out or ""):
        raise PhoneError(
            "这台机器的 cmd clipboard 不可用。改用 ADBKeyboard 输入法，"
            "或用 python phone.py shell 走应用自带的分享/搜索入口。"
        )
    log("clipboard", {"len": len(a.text)})
    print("已写入剪贴板（长按输入框粘贴即可）")
    return 0


def cmd_setup_input(a):
    """装 ADBKeyboard 并启用。不抢默认输入法——type_text 会在需要时自动临时切换。"""
    serial = pick_device(a.device)
    apk = a.apk or os.path.join(
        os.path.dirname(WORKSPACE), "tools", "ADBKeyboard.apk")
    if not os.path.isfile(apk):
        raise PhoneError(
            f"找不到 {apk}。\n先下载：curl -sL -o tools\\ADBKeyboard.apk "
            "https://github.com/senzhk/ADBKeyBoard/raw/master/ADBKeyboard.apk"
        )
    if not _has_adb_keyboard(serial):
        out = adb("install", "-r", apk, serial=serial, timeout=180, check=False)
        print(out)
        if "Success" not in (out or "") and not _has_adb_keyboard(serial):
            raise PhoneError(
                "安装被拒。小米最常见的原因是它把包名拉进了「USB安装管理」的黑名单：\n"
                "  设置 → 开发者选项 → USB安装管理 → 取消勾选被禁的包\n"
                "另外确认开发者选项里的「USB安装」开关是开的。\n"
                "原始错误：" + (out or "")[:200]
            )
    adb("shell", "ime", "enable", ADB_IME, serial=serial, check=False)
    log("setup_input", {"apk": apk})
    cur = adb("shell", "settings", "get", "secure", "default_input_method",
              serial=serial, check=False).strip()
    print("ADBKeyboard 已安装并启用。")
    print(f"当前默认输入法保持不动：{cur}")
    print("中文输入时 type_text 会自动临时切到它、打完再切回来，平时不影响你打字。")
    return 0


def cmd_wireless(a):
    """切到无线 adb。Android 11+ 走「无线调试」配对，老办法走 tcpip 5555。"""
    if a.pair:
        # 配对模式：不需要先有设备，直接对 IP:配对端口 下配对码
        target, _, code = a.pair.partition(":")
        port, _, code = code.partition(":")
        if not port or not code:
            raise PhoneError(
                "配对格式：--pair IP:配对端口:配对码\n"
                "例：python phone.py wireless --pair 192.168.1.5:37123:123456\n"
                "配对端口和配对码都在手机「无线调试 → 使用配对码配对设备」弹窗里。"
            )
        out = adb("pair", f"{target}:{port}", code, check=False)
        log("wireless_pair", {"target": f"{target}:{port}"})
        print(out or "(配对无输出)")
        if "Successfully paired" not in (out or ""):
            raise PhoneError(
                "配对没成功。常见原因：\n"
                "  · 端口/配错码抄错（弹窗每次都不一样，重开一次弹窗）\n"
                "  · 手机和电脑不在同一个 Wi-Fi\n"
                "  · 弹窗超时了（关掉重开）"
            )
        print("配对成功，接着连：python phone.py wireless")
        return 0

    serial = pick_device(a.device)

    # 拿手机 IP：优先 Wi-Fi，退而求其次用当前默认路由的源地址
    wifi = adb("shell", "ip", "-f", "inet", "addr", "show", "wlan0", serial=serial, check=False)
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", wifi or "")
    if not m:
        route = adb("shell", "ip", "route", serial=serial, check=False)
        m = re.search(r"src (\d+\.\d+\.\d+\.\d+)", route or "")
    if not m:
        wi = adb("shell", "cmd", "wifi", "status", serial=serial, check=False)
        if "disabled" in (wi or "").lower():
            raise PhoneError(
                "手机的 Wi-Fi 是关着的，无线调试用不了。\n"
                "先打开手机 Wi-Fi 并连到和电脑同一个网络：python phone.py shell cmd wifi status 可看状态。"
            )
        raise PhoneError("拿不到手机 IP，确认手机连着和电脑同一个 Wi-Fi。")
    phone_ip = m.group(1)

    if a.port:
        adb("connect", f"{phone_ip}:{a.port}", serial=None, check=False)
        print(f"已尝试连接 {phone_ip}:{a.port}")
        log("wireless", {"ip": phone_ip, "port": a.port})
        return 0

    # Android 11+ 的「无线调试」开在随机端口上，先问系统
    port = None
    for probe in (
        adb("shell", "settings", "get", "global", "adb_wifi_port", serial=serial, check=False),
        adb("shell", "getprop", "service.adb.tcp.port", serial=serial, check=False),
    ):
        mm = re.search(r"(\d{4,5})", probe or "")
        if mm:
            port = int(mm.group(1))
            break

    if not port:
        # 老办法：让手机在 5555 起服务
        adb("tcpip", 5555, serial=serial, check=False)
        time.sleep(1.5)
        port = 5555

    adb("connect", f"{phone_ip}:{port}", serial=None, check=False)
    time.sleep(0.8)
    print(f"已尝试无线连接 {phone_ip}:{port}")
    print(f"拔线后可用：--device {phone_ip}:{port}")
    if port == 5555:
        print("（如果 5555 连不上，说明这版系统只认「无线调试」的随机端口：")
        print("  手机开发者选项 → 无线调试 → 使用配对码配对设备 → 用 --pair IP:端口:配对码）")
    log("wireless", {"ip": phone_ip, "port": port})
    return 0



# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        description="手机控制台（adb 封装）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""例子：
  python phone.py look                 # 截图+UI树，看屏幕上有什么
  python phone.py tap 540 1620         # 点击
  python phone.py text "hello world"   # 打字
  python phone.py key back             # 返回
  python phone.py open com.tencent.mm  # 开微信
  python phone.py status               # 设备/电量/当前界面
""",
    )
    p.add_argument("--device", "-s", default=None, help="设备序列号（多设备时必填）")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("devices", help="列出已连接设备")
    sp.set_defaults(func=cmd_devices)

    sp = sub.add_parser("diagnose", help="连不上时先跑这个：基于证据说清卡在哪")
    sp.set_defaults(func=cmd_diagnose)

    sp = sub.add_parser("status", help="设备信息：型号/电量/当前界面")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("shot", help="截一张图")
    sp.add_argument("--prefix", default="screen")
    sp.set_defaults(func=cmd_shot)

    sp = sub.add_parser("ui", help="只 dump UI 树（比截图省流量）")
    sp.add_argument("--limit", type=int, default=120)
    sp.set_defaults(func=cmd_ui)

    sp = sub.add_parser("look", help="截图 + UI 树（推荐的第一步）")
    sp.add_argument("--limit", type=int, default=80)
    sp.add_argument("--send-to", default=None, help="预留：把截图发给指定 QQ（仅提示，不发送）")
    sp.set_defaults(func=cmd_look)

    sp = sub.add_parser("tap", help="点击坐标")
    sp.add_argument("x", type=int)
    sp.add_argument("y", type=int)
    sp.add_argument("--settle", type=float, default=0, help="点击后等几秒再返回")
    sp.set_defaults(func=cmd_tap)

    sp = sub.add_parser("swipe", help="滑动（上滑翻页等）")
    sp.add_argument("x1", type=int)
    sp.add_argument("y1", type=int)
    sp.add_argument("x2", type=int)
    sp.add_argument("y2", type=int)
    sp.add_argument("--ms", type=int, default=300)
    sp.add_argument("--settle", type=float, default=0)
    sp.set_defaults(func=cmd_swipe)

    sp = sub.add_parser("longpress", help="长按坐标")
    sp.add_argument("x", type=int)
    sp.add_argument("y", type=int)
    sp.add_argument("--ms", type=int, default=800)
    # 长按常常会弹出菜单/进入编辑态，等一会儿再截图才看得见结果。
    # 实机验证时发现 tap/swipe/key 都有 --settle 而 longpress 没有 ——
    # 参数不一致会让调用方在「看起来该有的地方」撞一次 usage error。
    sp.add_argument("--settle", type=float, default=0)
    sp.set_defaults(func=cmd_longpress)

    sp = sub.add_parser("text", help="输入文字（ASCII 直接打；中文需 ADBKeyboard）")
    sp.add_argument("text")
    sp.add_argument("--enter", action="store_true", help="输入后回车")
    sp.set_defaults(func=cmd_text)

    sp = sub.add_parser("key", help="按键：back/home/recents/enter/power/volume_up…")
    sp.add_argument("key")
    sp.add_argument("--settle", type=float, default=0)
    sp.set_defaults(func=cmd_key)

    sp = sub.add_parser("open", help="启动应用（包名）或指定 Activity（pkg/.Act）")
    sp.add_argument("target")
    sp.add_argument("--settle", type=float, default=1.5)
    sp.set_defaults(func=cmd_open)

    sp = sub.add_parser("apps", help="列出已装第三方应用")
    sp.add_argument("--filter", default=None)
    sp.set_defaults(func=cmd_apps)

    sp = sub.add_parser("shell", help="直接跑 adb shell 命令（逃生口）")
    sp.add_argument("shell_args", nargs=argparse.REMAINDER)
    sp.add_argument("--timeout", type=int, default=60)
    sp.set_defaults(func=cmd_shell)

    sp = sub.add_parser("pull", help="从手机拉文件到电脑")
    sp.add_argument("remote")
    sp.add_argument("local", nargs="?", default=None)
    sp.set_defaults(func=cmd_pull)

    sp = sub.add_parser("push", help="从电脑推文件到手机")
    sp.add_argument("local")
    sp.add_argument("remote")
    sp.set_defaults(func=cmd_push)

    sp = sub.add_parser("clipboard", help="把文字写进手机剪贴板")
    sp.add_argument("text")
    sp.set_defaults(func=cmd_clipboard)

    sp = sub.add_parser("wireless", help="切到无线 adb（Android 11+ 需先配对）")
    sp.add_argument("--port", type=int, default=None, help="已知端口时直接连")
    sp.add_argument("--pair", default=None,
                    help="配对格式 IP:配对端口:配对码（手机「无线调试」弹窗里抄）")
    sp.set_defaults(func=cmd_wireless)

    sp = sub.add_parser("setup-input", help="装 ADBKeyboard，让中文能打进去")
    sp.add_argument("--apk", default=None, help="APK 路径（默认 workspace/tools/ADBKeyboard.apk）")
    sp.set_defaults(func=cmd_setup_input)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.cmd not in FREE_ACTIONS:
        require_grant(args.cmd)
    try:
        return args.func(args) or 0
    except PhoneError as e:
        log(args.cmd, {"error": str(e)}, ok=False)
        print(f"❌ {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
