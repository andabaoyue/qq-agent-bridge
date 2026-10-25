# -*- coding: utf-8 -*-
"""
manual_device.py — 实机验证「手机那只手」+ 闸门。**需要真手机插着。**

## 为什么这不是自动测试

自动测试能验证「代码能不能跑、参数有没有错、该拒的时候拒没拒」。
它验证不了「tap 有没有真的点到屏幕上」—— 那需要一台连着线的手机。

实测教训：19 个子命令 `--help` 全过，实机一跑三个 bug 全出来。
所以这一份是**手动跑、人看输出**的，不进 CI。

## 它怎么解决「许可」问题

`phone.py` 的动手类命令都要票，票只由「主人发一条真消息」开出。
这里用一个假 OneBot（只认白名单 message_id）代替 NapCat，
然后伪造一条来自主人的消息 —— 这样走的是**真实的核验路径**，
只是被核验的那个"NapCat"是我们自己。

它绝不会去连你真实的 QQ：配置里 `onebot_api` 指向本机的假服务端。

## 用法

    # 终端 A：起测试环境（备用端口，不碰生产那套）
    python tests/manual_device.py up

    # 终端 B：跑命令，注意要指到测试 daemon
    set QQ_BRIDGE_GATE=http://127.0.0.1:15702/desktop_grant
    cd agents
    python phone.py status
    python phone.py look

    # 收工
    python tests/manual_device.py down

测试环境的日志和截图都落在 `.selftest/device/workspace/`，不会污染正式目录。
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRIDGE = os.path.join(ROOT, "bridge")
TEST_ROOT = os.path.join(ROOT, ".selftest", "device")

EVENTS, POLL, INJECT, FAKEBOT = 15701, 15702, 15703, 15704

# 假 OneBot 认这一个 id；用它冒充「NapCat 确实有这条消息」
VALID_MID = 900001
OWNER = 900000001
GUEST = 900000002


def free_or_bail(port):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
    except OSError:
        s.close()
        raise SystemExit(
            f"端口 {port} 被占用 —— 可能上一次没关干净，先跑 "
            f"`python tests/manual_device.py down`，或者改这里的端口常量。")
    finally:
        try:
            s.close()
        except Exception:
            pass


def post(url, payload, timeout=5):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def get(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def wait_port(port, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return True
        except OSError:
            time.sleep(0.15)
    return False


def write_config():
    os.makedirs(TEST_ROOT, exist_ok=True)
    cfg = {
        "bot_qq": 900000000,
        "owner_qq": OWNER,
        "bot_names": ["TestBot"],
        "listen": {
            "events_host": "127.0.0.1", "events_port": EVENTS,
            "poll_host": "127.0.0.1", "poll_port": POLL,
            "inject_host": "127.0.0.1", "inject_port": INJECT,
        },
        "group_filter": {"mode": "mention_only", "allow_groups": [],
                         "block_groups": []},
        "workspace_dir": os.path.join(TEST_ROOT, "workspace"),
        "gates": {"ttl_seconds": 0},
        # 🔴 指向假 OneBot，绝不指向 3000（那上面可能是真的 NapCat）
        "onebot_api": "http://127.0.0.1:%d" % FAKEBOT,
    }
    path = os.path.join(TEST_ROOT, "config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return path


def cmd_up():
    for p in (EVENTS, POLL, INJECT, FAKEBOT):
        free_or_bail(p)

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from selftest import FakeOneBot

    fake = FakeOneBot(FAKEBOT, [VALID_MID]).start()
    print(f"[ok] 假 OneBot 起在 {FAKEBOT}（只认 message_id={VALID_MID}）")

    cfg = write_config()
    env = dict(os.environ)
    env["QQ_BRIDGE_CONFIG"] = cfg
    env["PYTHONIOENCODING"] = "utf-8"
    log_path = os.path.join(TEST_ROOT, "daemon.out")
    log = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, os.path.join(BRIDGE, "daemon.py")],
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)

    with open(os.path.join(TEST_ROOT, "daemon.pid"), "w") as f:
        f.write(str(proc.pid))

    if not wait_port(EVENTS):
        with open(log_path, encoding="utf-8", errors="replace") as f:
            print(f.read()[-2000:])
        fake.stop()
        raise SystemExit("daemon 没起来，看上面的输出")

    print(f"[ok] daemon 起在 {EVENTS}/{POLL}/{INJECT}（pid={proc.pid}）")

    # 先确认默认是关的
    g = get(f"http://127.0.0.1:{POLL}/desktop_grant")
    print(f"[gate] 启动默认 granted={g.get('granted')} "
          f"({g.get('revoked_reason')})")
    if g.get("granted"):
        raise SystemExit("闸门启动就是开的，这不正常")

    # 用一条能通过核验的「主人消息」开票
    post(f"http://127.0.0.1:{EVENTS}", {
        "post_type": "message", "message_type": "private",
        "user_id": OWNER, "raw_message": "open the gate for testing",
        "message_id": VALID_MID, "time": int(time.time()),
        "sender": {"nickname": "owner"},
        "message": [{"type": "text", "data": {"text": "open the gate"}}],
    })
    time.sleep(0.4)
    g = get(f"http://127.0.0.1:{POLL}/desktop_grant")
    if not g.get("granted"):
        raise SystemExit(f"没能开票：{g}")
    print(f"[gate] 已开票 granted=True (owner={g.get('user_id')})")

    print()
    print("=" * 60)
    print("READY. 另一个终端里跑：")
    print(f"  set QQ_BRIDGE_GATE=http://127.0.0.1:{POLL}/desktop_grant")
    print("  cd agents")
    print("  python phone.py status")
    print("=" * 60)
    print("日志：", os.path.join(TEST_ROOT, "workspace", "phone_log.jsonl"))
    print("按 Ctrl+C 结束。")
    print()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        print("\n[down] 收尾…")
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        fake.stop()
        print("[down] done")


def cmd_down():
    pid_file = os.path.join(TEST_ROOT, "daemon.pid")
    if os.path.isfile(pid_file):
        try:
            pid = int(open(pid_file).read().strip())
        except Exception:
            pid = None
        if pid:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True)
            print(f"[down] killed daemon pid {pid}")
        os.remove(pid_file)
    else:
        print("[down] 没有 pid 文件（可能已经关了）")


def cmd_status():
    try:
        g = get(f"http://127.0.0.1:{POLL}/desktop_grant", timeout=2)
        print(json.dumps(g, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"daemon 不在（{type(e).__name__}）")


def main():
    ap = argparse.ArgumentParser(description="实机验证手机手 + 闸门（要真手机）")
    ap.add_argument("action", choices=["up", "down", "status"])
    a = ap.parse_args()
    {"up": cmd_up, "down": cmd_down, "status": cmd_status}[a.action]()


if __name__ == "__main__":
    main()
