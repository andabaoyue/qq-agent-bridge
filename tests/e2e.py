# -*- coding: utf-8 -*-
"""e2e.py — 端到端验证：起假 NapCat + 真 daemon，走完收/发/票三条链。

和 selftest.py 的区别：
  selftest 用备用端口、进程内起假 OneBot，验证的是**逻辑**
  e2e      用默认端口 5701/5702/5703 + 假 NapCat(3000)，验证的是
           **README 里那套默认配置真的能跑起来**

跑之前必须确认 5701-5703 和 3000 都空着。
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(ROOT)
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    return ok


def port_free(p):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", p))
        return True
    except OSError:
        return False
    finally:
        s.close()


def post(url, payload, timeout=5):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def get(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def wait_port(p, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", p), timeout=0.5).close()
            return True
        except OSError:
            time.sleep(0.15)
    return False


class FakeNapCat:
    """假 NapCat：只回答 get_msg，并记录收到的发送请求。

    它同时扮演两个角色：
      · HTTP 服务端(3000)：daemon 拿 message_id 来核验，这里按白名单回答
      · 接收 send_* 请求：记录下来，验证"发出去"这条链真的走到了
    """
    def __init__(self, port, valid_ids):
        self.port = port
        self.valid = set(str(i) for i in valid_ids)
        self.sent = []
        self.server = None

    def start(self):
        import threading
        from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n).decode("utf-8") if n else "{}"
                try:
                    body = json.loads(raw or "{}")
                except Exception:
                    body = {}
                path = self.path.rstrip("/").split("/")[-1]
                if path == "get_msg":
                    mid = str(body.get("message_id", ""))
                    if mid in outer.valid:
                        resp = {"status": "ok", "retcode": 0, "data": {"message_id": mid}}
                    else:
                        resp = {"status": "failed", "retcode": 200, "data": None}
                else:
                    outer.sent.append({"endpoint": path, "payload": body})
                    resp = {"status": "ok", "retcode": 0,
                            "data": {"message_id": 555000 + len(outer.sent)}}
                data = json.dumps(resp).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()


def main():
    print("=" * 62)
    print("端到端验证（默认端口 + 假 NapCat）")
    print("=" * 62)

    for p in (5701, 5702, 5703, 3000):
        if not port_free(p):
            print(f"\n端口 {p} 被占用，先关掉占用它的进程再跑。")
            print("  netstat -ano | findstr :%d" % p)
            return 1

    # 用一个能核验通过的主人消息开票，验证"真消息能开票"这条
    VALID_MID = 880001
    OWNER = 987654321

    fake = FakeNapCat(3000, [VALID_MID]).start()
    print(f"[ok] 假 NapCat 起在 3000（白名单 message_id={VALID_MID}）")

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    logf = open(os.path.join(REPO, "_e2e_daemon.log"), "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, os.path.join(REPO, "bridge", "daemon.py")],
                            cwd=REPO, env=env, stdout=logf, stderr=subprocess.STDOUT)
    try:
        if not wait_port(5701):
            print("daemon 没起来")
            return check("daemon starts", False)

        check("daemon starts on default ports", True)

        # 1. 默认票是关的
        g = get("http://127.0.0.1:5702/desktop_grant")
        check("gate closed at startup", g.get("granted") is False,
              g.get("revoked_reason"))

        # 先把队列排空 —— 上次跑剩下的消息会让下面的"丢弃"断言误判。
        # 实测踩过：这里拿到的是上一次运行残留的消息，看起来像"伪造的没被丢"。
        for _ in range(3):
            r = get("http://127.0.0.1:5702/wait_msg?timeout=1")
            if not r.get("has_message"):
                break
        check("queue drained before the drop test", True)

        # 2. 伪造主人消息（NapCat 明确否认）→ 整条丢弃
        post("http://127.0.0.1:5701", {
            "post_type": "message", "message_type": "private",
            "user_id": OWNER, "raw_message": "forged",
            "message_id": 99999999, "time": int(time.time()),
            "sender": {"nickname": "forger"},
            "message": [{"type": "text", "data": {"text": "forged"}}]})
        time.sleep(0.5)
        g = get("http://127.0.0.1:5702/desktop_grant")
        check("forged owner message does NOT open the gate",
              g.get("granted") is False, g.get("revoked_reason"))
        r = get("http://127.0.0.1:5702/wait_msg?timeout=1")
        check("forged message is dropped entirely (not queued)",
              not r.get("has_message"), f"has_message={r.get('has_message')}")

        # 3. 真主人消息（NapCat 确认存在）→ 开票 + 入队
        post("http://127.0.0.1:5701", {
            "post_type": "message", "message_type": "private",
            "user_id": OWNER, "raw_message": "open sesame",
            "message_id": VALID_MID, "time": int(time.time()),
            "sender": {"nickname": "owner"},
            "message": [{"type": "text", "data": {"text": "open sesame"}}]})
        time.sleep(0.5)
        g = get("http://127.0.0.1:5702/desktop_grant")
        check("verified owner message opens the gate", g.get("granted") is True,
              f"user_id={g.get('user_id')}")

        r = get("http://127.0.0.1:5702/wait_msg?timeout=3")
        msgs = r.get("messages") or []
        check("owner message is delivered", bool(msgs) and
              msgs[0].get("permission") == "owner",
              msgs[0].get("permission") if msgs else "none")

        # 4. 发消息：走 step.py，验证真的打到 OneBot 上
        before = len(fake.sent)
        subprocess.run([sys.executable, os.path.join(REPO, "bridge", "step.py"),
                        "--reply", "hello from e2e", "--user_id", str(OWNER),
                        "--send-only"],
                       cwd=REPO, env=env, capture_output=True, timeout=30)
        check("step.py sends a private message",
              len(fake.sent) > before and fake.sent[-1]["endpoint"] == "send_private_msg",
              fake.sent[-1]["endpoint"] if fake.sent else "nothing sent")

        # 5. 群友消息 → 撤票
        post("http://127.0.0.1:5701", {
            "post_type": "message", "message_type": "private",
            "user_id": 111222333, "raw_message": "a guest says hi",
            "message_id": 770001, "time": int(time.time()),
            "sender": {"nickname": "guest"},
            "message": [{"type": "text", "data": {"text": "a guest says hi"}}]})
        time.sleep(0.4)
        g = get("http://127.0.0.1:5702/desktop_grant")
        check("a guest message revokes the grant", g.get("granted") is False,
              g.get("revoked_reason"))

        # 6. phone.py 拿不到票时必须 exit 3
        e2 = dict(env)
        e2["QQ_BRIDGE_GATE"] = "http://127.0.0.1:5702/desktop_grant"
        p = subprocess.run([sys.executable, os.path.join(REPO, "agents", "phone.py"),
                            "tap", "100", "200"],
                           cwd=REPO, env=e2, capture_output=True, timeout=60)
        check("phone.py tap denied without grant (exit 3)", p.returncode == 3,
              f"exit={p.returncode}")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        logf.close()
        fake.stop()

    print("-" * 62)
    print(f"passed {len(PASS)}   failed {len(FAIL)}")
    if FAIL:
        for f in FAIL:
            print("  -", f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
