# -*- coding: utf-8 -*-
"""
desktop.py — 桌面「手」：屏幕截图 + 鼠标键盘注入。

## 这个文件做什么

Agent 跑在这台机器上，和主人是**同一个 Windows 会话、同一个桌面**，
所以「截图 → 看懂 → 点击/打字」这条路是通的。实测：
  · 屏幕 1920x1080，截图 0.12s（局部 0.03s）
  · PyAutoGUI 0.9.54 / mss / PyGetWindow / pyperclip 全部就绪
  · SendInput 注入正常，DPI 一致（截图尺寸 == pyautogui.size()，无错位）
  · 未锁屏（OpenInputDesktop 返回 Default 桌面）

## 安全闸门 —— 这个文件不是谁都调得动

硬要求：**普通群友绝不能碰**。只靠 role_tag 提示词挡不住注入，
所以这里加了一道**进程外、内存态**的许可检查：

    GET http://127.0.0.1:5702/desktop_grant   (地址可用 QQ_BRIDGE_GATE 覆盖)

许可状态**只存在 qq_daemon.py 的内存里，不落盘**：
  · 主人发消息     → 置 granted=true，默认**不设期限**（可用 gates.ttl_seconds 改）
  · 任何群友发消息 → 立刻 granted=false
  · daemon 重启     → 默认 false（fail-closed）
  · daemon 连不上   → 也拒绝（fail-closed）

为什么放内存不放文件：她能读写文件系统，写个 JSON 就能自己发许可；
内存态只能靠「让主人真的发一条消息」改变。

⚠️ 残余风险的诚实说明：她手里有 shell。一个足够坚决、绕过本脚本直接
   `import pyautogui` 的 agent，这道门拦不住 —— 那需要 OS 级沙箱
   （受限账户 / AppLocker / 独立会话）。本脚本拦的是「按说明书调用」
   与「被群友话术骗了之后照做」，也就是实际会发生的绝大多数情况。

## 用法

    python desktop.py --shot                        # 截图，打印路径
    python desktop.py --size                        # 屏幕尺寸（免许可）
    python desktop.py --pos                         # 光标位置（免许可）
    python desktop.py --windows                     # 列出可见窗口
    python desktop.py --focus "记事本"              # 激活窗口
    python desktop.py --click 800 450               # 左键点一下
    python desktop.py --double 800 450              # 双击
    python desktop.py --right 800 450               # 右键
    python desktop.py --move 800 450                # 移动光标
    python desktop.py --drag 100 100 600 400        # 拖拽
    python desktop.py --scroll 3                    # 滚轮（正=上，负=下）
    python desktop.py --type "你好"                 # 输入文本（含中文走剪贴板）
    python desktop.py --press enter                 # 单键
    python desktop.py --hotkey "ctrl c"             # 组合键
    python desktop.py --wallpaper D:\\x.jpg          # 换壁纸

所有会动手的命令都支持 `--dry-run`：只打印打算做什么，不真的动。
每次真实动作都会追加一行到 workspace/desktop.log（审计用）。
"""

import argparse
import ctypes
import json
import os
import sys
import time
import urllib.error
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# 本脚本放在 agents/ 下，截图和日志统一落到仓库根的 workspace/
BASE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "workspace")
os.makedirs(BASE, exist_ok=True)
SHOT_DIR = os.path.join(BASE, "shots")
LOG_PATH = os.path.join(BASE, "desktop.log")

# 闸门地址。daemon 跑在另一台机器上时用环境变量覆盖：
#   set QQ_BRIDGE_GATE=http://192.168.1.10:5702/desktop_grant
GRANT_URL = os.environ.get(
    "QQ_BRIDGE_GATE", "http://127.0.0.1:5702/desktop_grant")
ACTION_LOG_MAX = 2000      # 审计日志行数上限，超了截断（不无限涨）

# 免许可的只读命令：不泄露内容，不产生副作用。
FREE_ACTIONS = {"size", "pos", "help"}


# ------------------------------------------------------------------ 审计
def log_action(action, detail, ok, note=""):
    """每次动手都留痕 —— 出事后能重建她到底点过什么。"""
    try:
        line = "[%s] %-10s %-46s %s %s\n" % (
            time.strftime("%Y-%m-%d %H:%M:%S"),
            action, (detail or "")[:46], "OK" if ok else "FAIL", note)
        lines = []
        if os.path.exists(LOG_PATH):
            try:
                with open(LOG_PATH, "r", encoding="utf-8") as f:
                    lines = f.readlines()
            except Exception:
                lines = []
        lines.append(line)
        if len(lines) > ACTION_LOG_MAX:
            lines = lines[-ACTION_LOG_MAX:]
        with open(LOG_PATH, "w", encoding="utf-8") as f:
            f.writelines(lines)
    except Exception:
        pass


# ------------------------------------------------------------------ 闸门
def check_grant():
    """向 daemon 要许可。连不上 / 超时 / 格式异常 一律当**没有许可**。

    这里刻意不做任何本地缓存，也不读任何本地文件 —— 缓存的许可等于没许可。
    """
    try:
        with urllib.request.urlopen(GRANT_URL, timeout=3) as r:
            data = json.loads(r.read().decode("utf-8"))
        return bool(data.get("granted")), data
    except Exception as e:
        return False, {"granted": False, "reason": "daemon_unreachable: %s: %s"
                       % (type(e).__name__, e)}


def die_no_grant(info):
    msg = (
        "[DENIED] 当前没有桌面操作许可。\n"
        "原因：许可状态由 bridge/daemon.py 的内存持有，只有**主人本人**发消息才会\n"
        "      置为 granted（不设期限，直到被撤销）；任何群友发言都会立刻撤销；\n"
        "      daemon 未运行或连不上时同样默认拒绝。\n"
        "daemon 回报：%s\n" % json.dumps(info, ensure_ascii=False)
    )
    print(msg)
    log_action("denied", json.dumps(info, ensure_ascii=False)[:60], False)
    sys.exit(3)


# ------------------------------------------------------------------ 工具
def _pg():
    """惰性导入 pyautogui，并把 FAILSAFE 开着（鼠标甩到左上角可紧急中断）。"""
    import pyautogui
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.05
    return pyautogui


def _ensure_shot_dir():
    os.makedirs(SHOT_DIR, exist_ok=True)
    return SHOT_DIR


def _fmt(res):
    print(json.dumps(res, ensure_ascii=False, indent=2))


# ------------------------------------------------------------------ 动作
def do_shot(path=None, region=None):
    from PIL import ImageGrab
    _ensure_shot_dir()
    if not path:
        path = os.path.join(SHOT_DIR, "shot_%s.png" % time.strftime("%Y%m%d_%H%M%S"))
    if region:
        x, y, w, h = region
        im = ImageGrab.grab(bbox=(x, y, x + w, y + h))
    else:
        im = ImageGrab.grab()
    im.save(path)
    return {"ok": True, "path": path, "size": list(im.size),
            "note": "用 read_image 看这张图"}


def do_size():
    pg = _pg()
    w, h = pg.size()
    return {"ok": True, "width": w, "height": h}


def do_pos():
    pg = _pg()
    x, y = pg.position()
    return {"ok": True, "x": x, "y": y}


def do_windows():
    import pygetwindow as gw
    out = []
    for w in gw.getAllWindows():
        t = (w.title or "").strip()
        if t:
            out.append({"title": t, "left": w.left, "top": w.top,
                        "width": w.width, "height": w.height})
    return {"ok": True, "count": len(out), "windows": out}


def do_focus(title, dry):
    import pygetwindow as gw
    matches = [w for w in gw.getAllWindows()
               if title.lower() in (w.title or "").lower()]
    if not matches:
        return {"ok": False, "error": "没有匹配的窗口", "query": title}
    w = matches[0]
    if dry:
        return {"ok": True, "dry_run": True, "would_focus": w.title}
    try:
        if w.isMinimized:
            w.restore()
        w.activate()
        time.sleep(0.2)
        return {"ok": True, "focused": w.title}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def do_click(x, y, button="left", clicks=1, dry=False):
    pg = _pg()
    w, h = pg.size()
    if not (0 <= x < w and 0 <= y < h):
        return {"ok": False, "error": "坐标越界", "x": x, "y": y,
                "screen": [w, h]}
    if dry:
        return {"ok": True, "dry_run": True, "would": "%s x%d @ (%d,%d)"
                % (button, clicks, x, y)}
    pg.click(x=x, y=y, button=button, clicks=clicks, interval=0.12)
    return {"ok": True, "button": button, "clicks": clicks, "x": x, "y": y}


def do_move(x, y, dry=False):
    pg = _pg()
    if dry:
        return {"ok": True, "dry_run": True, "would": "move to (%d,%d)" % (x, y)}
    pg.moveTo(x, y, duration=0.15)
    return {"ok": True, "x": x, "y": y}


def do_drag(x1, y1, x2, y2, dry=False):
    pg = _pg()
    if dry:
        return {"ok": True, "dry_run": True,
                "would": "drag (%d,%d) -> (%d,%d)" % (x1, y1, x2, y2)}
    pg.moveTo(x1, y1)
    pg.dragTo(x2, y2, duration=0.4, button="left")
    return {"ok": True, "from": [x1, y1], "to": [x2, y2]}


def do_scroll(n, dry=False):
    pg = _pg()
    if dry:
        return {"ok": True, "dry_run": True, "would": "scroll %d" % n}
    pg.scroll(n)
    return {"ok": True, "scroll": n}


def do_type(text, dry=False):
    """纯 ASCII 走 pyautogui.write；含中文走剪贴板 + Ctrl+V。

    ## 为什么要分两条路
    pyautogui.write 底层是逐字符发键盘扫描码，**非 ASCII 字符它送不进去**
    （中文会直接丢掉或乱码）。中文只能借剪贴板：
      1. 备份当前剪贴板
      2. 写入目标文本
      3. Ctrl+V
      4. 还原剪贴板（不给主人留下垃圾）
    """
    pg = _pg()
    if dry:
        return {"ok": True, "dry_run": True, "would": "type %r" % text[:60]}
    if all(ord(c) < 128 for c in text):
        pg.write(text, interval=0.02)
        return {"ok": True, "typed": len(text), "mode": "keyboard"}
    import pyperclip
    backup = ""
    try:
        backup = pyperclip.paste()
    except Exception:
        backup = ""
    try:
        pyperclip.copy(text)
        time.sleep(0.12)
        pg.hotkey("ctrl", "v")
        time.sleep(0.15)
        return {"ok": True, "typed": len(text), "mode": "clipboard"}
    finally:
        try:
            pyperclip.copy(backup or "")
        except Exception:
            pass


def do_press(key, dry=False):
    pg = _pg()
    if dry:
        return {"ok": True, "dry_run": True, "would": "press %s" % key}
    pg.press(key)
    return {"ok": True, "pressed": key}


def do_hotkey(keys, dry=False):
    pg = _pg()
    parts = [k for k in keys.replace("+", " ").split() if k]
    if not parts:
        return {"ok": False, "error": "热键为空"}
    if dry:
        return {"ok": True, "dry_run": True, "would": "hotkey %s" % parts}
    pg.hotkey(*parts)
    return {"ok": True, "hotkey": parts}


def do_wallpaper(path, dry=False):
    """换壁纸。就是她 10-23 承诺过的「偷偷把你的壁纸换成猫猫大头照」。

    用 SystemParametersInfoW(SPI_SETDESKWALLPAPER=0x0014)，
    带 SPIF_UPDATEINIFILE|SPIF_SENDCHANGE 让它立即生效并写进配置。
    """
    if not os.path.exists(path):
        return {"ok": False, "error": "文件不存在", "path": path}
    if dry:
        return {"ok": True, "dry_run": True, "would": "wallpaper -> %s" % path}
    SPI_SETDESKWALLPAPER = 0x0014
    SPIF_UPDATEINIFILE = 0x01
    SPIF_SENDCHANGE = 0x02
    ok = ctypes.windll.user32.SystemParametersInfoW(
        SPI_SETDESKWALLPAPER, 0, path, SPIF_UPDATEINIFILE | SPIF_SENDCHANGE)
    return {"ok": bool(ok), "path": path}


# ------------------------------------------------------------------ 入口
def build_parser():
    p = argparse.ArgumentParser(
        description="desktop hands: screenshot / mouse / keyboard / wallpaper",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--shot", nargs="?", const="", default=None,
                   metavar="PATH", help="截图（可指定保存路径）")
    p.add_argument("--region", nargs=4, type=int, metavar=("X", "Y", "W", "H"),
                   help="配合 --shot，只截一块区域")
    p.add_argument("--size", action="store_true", help="屏幕尺寸（免许可）")
    p.add_argument("--pos", action="store_true", help="光标位置（免许可）")
    p.add_argument("--windows", action="store_true", help="列出可见窗口")
    p.add_argument("--focus", metavar="TITLE", help="按标题片段激活窗口")
    p.add_argument("--click", nargs=2, type=int, metavar=("X", "Y"))
    p.add_argument("--double", nargs=2, type=int, metavar=("X", "Y"))
    p.add_argument("--right", nargs=2, type=int, metavar=("X", "Y"))
    p.add_argument("--move", nargs=2, type=int, metavar=("X", "Y"))
    p.add_argument("--drag", nargs=4, type=int, metavar=("X1", "Y1", "X2", "Y2"))
    p.add_argument("--scroll", type=int, metavar="N")
    p.add_argument("--type", dest="text", metavar="TEXT")
    p.add_argument("--press", metavar="KEY")
    p.add_argument("--hotkey", metavar="KEYS", help='如 "ctrl c"')
    p.add_argument("--wallpaper", metavar="PATH")
    p.add_argument("--dry-run", action="store_true", help="只说要做什么，不动手")
    return p


def main():
    args = build_parser().parse_args()

    # 先判定这一轮到底要干什么，免许可动作不做闸门检查
    #
    # 🔴 2026-10-23 自审修的一个真 bug：原写法是
    #       if getattr(args, name) not in (None, False)
    #   而 Python 里 `0 == False` 是 True，于是 `--scroll 0` 被判定成
    #   "没给参数" → 直接掉进 print_help 分支，命令被静默吞掉。
    #   实测确认过。现在改成显式判 None / False / ""，
    #   数值 0 要单独放行。
    action = None
    for name in ("shot", "windows", "focus", "click", "double", "right",
                 "move", "drag", "scroll", "text", "press", "hotkey",
                 "wallpaper", "size", "pos"):
        val = getattr(args, name)
        if val is None or val is False:
            continue
        # 🔴 `--shot` 的 nargs="?" + const="" 意味着「不带路径」时 val 就是 ""，
        #   那是**合法用法**（自动生成文件名），不能当"没给参数"。
        #   `--scroll 0` 的 0 同理必须放行（0 == False，不能和 False 合并判断）。
        action = name
        break
    if action is None:
        build_parser().print_help()
        return 0

    if action not in FREE_ACTIONS:
        granted, info = check_grant()
        if not granted:
            die_no_grant(info)

    try:
        if action == "size":
            res = do_size()
        elif action == "pos":
            res = do_pos()
        elif action == "shot":
            res = do_shot(args.shot or None, args.region)
        elif action == "windows":
            res = do_windows()
        elif action == "focus":
            res = do_focus(args.focus, args.dry_run)
        elif action == "click":
            res = do_click(args.click[0], args.click[1], dry=args.dry_run)
        elif action == "double":
            res = do_click(args.double[0], args.double[1], clicks=2,
                           dry=args.dry_run)
        elif action == "right":
            res = do_click(args.right[0], args.right[1], button="right",
                           dry=args.dry_run)
        elif action == "move":
            res = do_move(args.move[0], args.move[1], dry=args.dry_run)
        elif action == "drag":
            res = do_drag(*args.drag, dry=args.dry_run)
        elif action == "scroll":
            res = do_scroll(args.scroll, dry=args.dry_run)
        elif action == "text":
            res = do_type(args.text, dry=args.dry_run)
        elif action == "press":
            res = do_press(args.press, dry=args.dry_run)
        elif action == "hotkey":
            res = do_hotkey(args.hotkey, dry=args.dry_run)
        elif action == "wallpaper":
            res = do_wallpaper(args.wallpaper, dry=args.dry_run)
        else:
            res = {"ok": False, "error": "unknown action"}
    except Exception as e:
        res = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}

    detail = json.dumps({k: v for k, v in vars(args).items()
                         if v not in (None, False, "")}, ensure_ascii=False)
    # 🔴 2026-10-23 自审：`--type` 的正文**绝不能**进审计日志。
    #   实测确认过：打一句"我的密码是hunter2"会原样落进 desktop.log。
    #   她去填密码框时，密码就变成一个明文件躺在硬盘上了。
    #   日志只记"打了几个字"，不记内容。
    if action == "text" and args.text:
        detail = json.dumps({"text_len": len(args.text),
                             "dry_run": args.dry_run}, ensure_ascii=False)
    log_action(action, detail, res.get("ok"), res.get("error", ""))
    _fmt(res)
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
