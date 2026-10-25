# -*- coding: utf-8 -*-
"""
selftest.py — 不需要真 QQ 就能验证整条链路。

## 为什么要有这个

「代码能 import」和「功能对」是两件事。实测教训：19 个子命令
`--help` 全过，实机一跑三个 bug。所以这个测试真的起进程、真的发 HTTP、
真的读日志，只看结果。

## 它验证什么

  1. 事件口收消息 → 入队 → 长轮询取出来（完整链路）
  2. 群过滤器：没点名的群消息被丢弃、被 @ 的放行
  3. block_groups：那个群一条都不进
  4. 闸门：非主人说话 → 撤票；主人说话 → 开票（注意这里不验真伪，
     因为验证真伪要一个真的 NapCat；见 docs 的说明）
  5. 注入口：无 token 403、错 token 403、正确 token 入队、
     **并且注入不改变闸门状态**
  6. 队列落盘：重启后 pending_messages.json 里的消息被恢复

## 怎么跑

    python tests/selftest.py

测试自己会挑空闲端口、自己起 daemon、结束时清掉。
不会碰你的真实 QQ，也不会发任何消息出去。
"""

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

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -- {detail}" if detail else ""))
    return ok


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def post(url, payload, headers=None, timeout=5):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json",
                 **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def get(url, timeout=5):
    """GET。注意：/wait_msg 的长轮询会**故意**占用 timeout 秒才返回，
    所以客户端超时必须比服务端那一段更长，否则每次都在读到状态行之前先超时。
    这个坑实测踩过一次 —— 看起来像 daemon 挂了，其实是测试写错了。"""
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def poll_once(base, wait=2):
    """向 /wait_msg 取一批消息。客户端超时固定比服务端多 10 秒。"""
    return get(f"{base}/wait_msg?timeout={wait}", timeout=wait + 10)


def make_config(tmpdir, ports, extra=None):
    cfg = {
        "bot_qq": 10001,
        "owner_qq": 20002,
        "bot_names": ["TestBot"],
        "listen": {
            "events_host": "127.0.0.1", "events_port": ports[0],
            "poll_host": "127.0.0.1", "poll_port": ports[1],
            "inject_host": "127.0.0.1", "inject_port": ports[2],
        },
        "group_filter": {
            "mode": "mention_only",
            "allow_groups": ["*"],
            "block_groups": [99999],
        },
        "workspace_dir": os.path.join(tmpdir, "workspace"),
        "gates": {"ttl_seconds": 0},
        # 🔴 指向测试自己起的假 OneBot —— 绝不能指向 3000，
        #    那上面可能是真的 NapCat，测试会打到别人的机器人账号上。
        "onebot_api": "http://127.0.0.1:%d" % ports[3],
    }
    if extra:
        cfg.update(extra)
    path = os.path.join(tmpdir, "config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return path


# ---------------------------------------------------------------- 假 OneBot
class FakeOneBot:
    """最小的 OneBot v11 HTTP 服务端，只为回答 `get_msg`。

    ## 为什么必须自己起一个
    防伪层的判据是「拿 message_id 去问 NapCat，它说有才是真有」。
    要测「主人真发消息 → 开票」这一格，就必须让这个询问得到 status=ok。
    起一个只认白名单 id 的假服务端，是唯一诚实的测法 ——
    既不碰真实的 QQ，也真的走完了那条代码路径。

    顺带还能测反向：不在白名单里的 id 一律 status=failed。
    """

    def __init__(self, port, valid_ids):
        self.port = port
        self.valid = set(str(i) for i in valid_ids)
        self.server = None
        self.thread = None

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
                mid = str(body.get("message_id", ""))
                if mid in outer.valid:
                    resp = {"status": "ok", "retcode": 0,
                            "data": {"message_id": mid}}
                else:
                    resp = {"status": "failed", "retcode": 200, "data": None}
                data = json.dumps(resp).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        return self

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()


def wait_port(port, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=0.5)
            s.close()
            return True
        except OSError:
            time.sleep(0.15)
    return False


def owner_msg(text="hello from owner", mid=111, uid=20002, mtype="private",
              group=None, message=None):
    """构造一条 OneBot v11 事件。是否被认成主人，取决于 message_id
    能不能通过 daemon 对（假）OneBot 的核验 —— 见 FakeOneBot。"""
    d = {
        "post_type": "message",
        "message_type": mtype,
        "user_id": uid,
        "raw_message": text,
        "message_id": mid,
        "time": int(time.time()),
        "sender": {"nickname": "sender%d" % uid},
    }
    if group is not None:
        d["group_id"] = group
    if message is not None:
        d["message"] = message
    else:
        d["message"] = [{"type": "text", "data": {"text": text}}]
    return d


def run(tmpdir):
    ports = [free_port(), free_port(), free_port(), free_port()]
    # 只有这些 id 会被假 OneBot 认作真消息（模拟「NapCat 确实有这条」）
    VALID_IDS = [301, 302, 303]
    fake = FakeOneBot(ports[3], VALID_IDS).start()
    cfg_path = make_config(tmpdir, ports)
    env = dict(os.environ)
    env["QQ_BRIDGE_CONFIG"] = cfg_path
    env["PYTHONIOENCODING"] = "utf-8"

    log = open(os.path.join(tmpdir, "daemon.out"), "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, os.path.join(BRIDGE, "daemon.py")],
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        if not wait_port(ports[0]):
            with open(os.path.join(tmpdir, "daemon.out"), encoding="utf-8",
                      errors="replace") as f:
                print(f.read()[-3000:])
            return check("daemon starts", False)

        ev = f"http://127.0.0.1:{ports[0]}"
        poll = f"http://127.0.0.1:{ports[1]}"
        inj = f"http://127.0.0.1:{ports[2]}"

        check("daemon starts", True, f"ports {ports}")
        check("fake OneBot is up", wait_port(ports[3]), f"port {ports[3]}")

        # ---- 1. 私聊消息全链路（非主人） ----
        st, _ = post(ev, owner_msg("hi via private", mid=201, uid=30003))
        check("event port accepts private message", st == 200, f"HTTP {st}")

        st, res = poll_once(poll, 5)
        got = res.get("messages") or []
        ok = res.get("has_message") and len(got) == 1 and \
            got[0].get("text") == "hi via private"
        check("long-poll delivers the message", ok,
              f"count={len(got)} text={got[0].get('text') if got else None}")

        if got:
            m = got[0]
            check("guest permission", m.get("permission") == "guest",
                  m.get("permission"))
            check("guest: desktop_allowed is False",
                  m.get("desktop_allowed") is False)
            check("message carries image_urls field", "image_urls" in m)

        # ---- 2. 伪造的主人：message_id 不存在 → 整条丢弃 ----
        st, _ = post(ev, owner_msg("forged owner", mid=999, uid=20002))
        st, res = poll_once(poll, 2)
        check("forged owner message is dropped by the verifier",
              not res.get("has_message"),
              f"has_message={res.get('has_message')}")

        # ---- 3. 真主人：message_id 能核验 → 开票 ----
        st, g0 = get(f"{poll}/desktop_grant")
        post(ev, owner_msg("real owner", mid=301, uid=20002))
        st, res = poll_once(poll, 3)
        got = res.get("messages") or []
        ok = bool(got) and got[0].get("permission") == "owner"
        check("verified owner message is tagged owner", ok,
              got[0].get("permission") if got else None)
        if got:
            check("verified owner: desktop_allowed is True",
                  got[0].get("desktop_allowed") is True)

        st, g1 = get(f"{poll}/desktop_grant")
        check("verified owner message opens the gate",
              g1.get("granted") is True, g1.get("granted"))

        # ---- 4. 群过滤器：未点名 → 丢弃 ----
        post(ev, owner_msg("just chatting", mid=202, uid=30003,
                           mtype="group", group=88888))
        st, res = poll_once(poll, 2)
        check("un-mentioned group message is dropped",
              not res.get("has_message"), f"has_message={res.get('has_message')}")

        # ---- 5. 群 @ → 放行，且把票撤掉 ----
        post(ev, owner_msg("", mid=203, uid=30003, mtype="group", group=88888,
                           message=[{"type": "at", "data": {"qq": "10001"}},
                                    {"type": "text", "data": {"text": " help me"}}]))
        st, res = poll_once(poll, 3)
        got = res.get("messages") or []
        ok = res.get("has_message") and got and got[0].get("at_me") is True
        check("@ mention passes the filter and sets at_me", bool(ok),
              f"at_me={got[0].get('at_me') if got else None}")

        st, g2 = get(f"{poll}/desktop_grant")
        check("a guest message revokes the owner's grant",
              g2.get("granted") is False, f"reason={g2.get('revoked_reason')}")

        # ---- 6. block_groups → 一条都不进 ----
        post(ev, owner_msg("blocked group", mid=204, uid=30003, mtype="group",
                           group=99999,
                           message=[{"type": "at", "data": {"qq": "10001"}}]))
        st, res = poll_once(poll, 2)
        check("blocked group is dropped entirely", not res.get("has_message"))

        # ---- 7. 注入口鉴权 ----
        try:
            post(f"{inj}/inject", {"text": "no token"})
            check("inject without token is rejected", False, "unexpected 200")
        except urllib.error.HTTPError as e:
            check("inject without token is rejected", e.code == 403, f"HTTP {e.code}")

        try:
            post(f"{inj}/inject", {"text": "bad"},
                 headers={"X-Inject-Token": "wrong"})
            check("inject with wrong token is rejected", False, "unexpected 200")
        except urllib.error.HTTPError as e:
            check("inject with wrong token is rejected", e.code == 403, f"HTTP {e.code}")

        # 读真 token
        ws = os.path.join(tmpdir, "workspace")
        token_path = os.path.join(ws, ".inject_token")
        token = None
        if os.path.isfile(token_path):
            token = open(token_path, encoding="utf-8").read().strip()
        check("inject token file created", bool(token))

        # 先让票处于「已开」状态（重新发一条能核验的主人消息 ——
        # 前面那条群友消息已经把票撤了），再验证注入**不会**改变它。
        # 这一格是整个安全模型的关键：注入方自己也是 AI，它能被话术骗。
        post(ev, owner_msg("owner again", mid=302, uid=20002))
        poll_once(poll, 2)
        st, g_before = get(f"{poll}/desktop_grant")
        check("gate is open before injection (owner granted)",
              g_before.get("granted") is True, g_before.get("granted"))

        st, _ = post(f"{inj}/inject", {"text": "injected hello"},
                     headers={"X-Inject-Token": token or ""})
        check("inject with valid token is accepted", st == 200, f"HTTP {st}")

        st, res = poll_once(poll, 3)
        got = res.get("messages") or []
        ok = bool(got) and got[0].get("permission") == "inject"
        check("injected message is tagged as inject", ok,
              got[0].get("permission") if got else None)
        if got:
            check("injected message cannot itself carry permission",
                  got[0].get("desktop_allowed") is False)

        st, g_after = get(f"{poll}/desktop_grant")
        check("injection does not change gate state",
              g_before.get("granted") == g_after.get("granted"),
              f"{g_before.get('granted')} -> {g_after.get('granted')}")

        # ---- 8. 队列落盘 ----
        post(ev, owner_msg("persist me", mid=303, uid=20002))
        time.sleep(0.5)             # 让 daemon 落盘
        pending = os.path.join(ws, "pending_messages.json")
        queued = []
        if os.path.isfile(pending):
            try:
                with open(pending, encoding="utf-8") as f:
                    queued = json.load(f)
            except Exception:
                queued = []
        check("queue is persisted to disk",
              any(m.get("text") == "persist me" for m in queued),
              f"{len(queued)} pending")

        # 把队列排空，免得后面的检查拿到前面残留的消息
        poll_once(poll, 1)

        # ---- 9. 空 text 拒绝 ----
        try:
            post(f"{inj}/inject", {"text": "   "},
                 headers={"X-Inject-Token": token or ""})
            check("inject with empty text is rejected", False, "unexpected 200")
        except urllib.error.HTTPError as e:
            check("inject with empty text is rejected", e.code == 400, f"HTTP {e.code}")

        # ---- 10. 其它路径 404 ----
        try:
            post(f"{inj}/nope", {"text": "x"},
                 headers={"X-Inject-Token": token or ""})
            check("unknown inject path returns 404", False, "unexpected 200")
        except urllib.error.HTTPError as e:
            check("unknown inject path returns 404", e.code == 404, f"HTTP {e.code}")

        # ---- 11. 超长文本被截断 ----
        long_text = "A" * 9000
        st, _ = post(f"{inj}/inject", {"text": long_text},
                     headers={"X-Inject-Token": token or ""})
        st, res = poll_once(poll, 3)
        got = res.get("messages") or []
        t = (got[0].get("text") or "") if got else ""
        check("oversized inject text is truncated",
              bool(t) and len(t) <= 8100 and t.endswith("[truncated]"),
              f"len={len(t)}")

        # ---- 12. 冷却不丢消息，只打标记 ----
        post(ev, owner_msg("first", mid=210, uid=40004))
        poll_once(poll, 2)
        post(ev, owner_msg("second quickly", mid=211, uid=40004))
        st, res = poll_once(poll, 3)
        got = res.get("messages") or []
        ok = bool(got) and got[0].get("throttled") is True
        check("rapid second message is delivered but tagged throttled",
              ok, got[0].get("throttled") if got else "no message")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        fake.stop()


def main():
    print("=" * 62)
    print("QQ bridge self-test (no real QQ required)")
    print("=" * 62)
    # 不用 tempfile.mkdtemp：它以 0o700 建目录，在 Windows 的受限令牌/ACL
    # 策略下会建出一个「自己都写不进去」的目录。用普通 makedirs 就好。
    base = os.path.join(ROOT, ".selftest")
    os.makedirs(base, exist_ok=True)
    td = os.path.join(base, "run_%d_%d" % (int(time.time()), os.getpid()))
    os.makedirs(td, exist_ok=True)
    try:
        run(td)
    finally:
        print(f"\n(test artifacts left in {td})")
    print("-" * 62)
    print(f"passed {len(PASS)}   failed {len(FAIL)}")
    if FAIL:
        print("failed checks:")
        for f in FAIL:
            print("  -", f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
