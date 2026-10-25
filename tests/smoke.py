# -*- coding: utf-8 -*-
"""
smoke.py — agents 侧的冒烟测试：不接手机、不接 QQ，只验证「命令能跑、参数没崩」。

## 这个测试能证明什么、不能证明什么

**能**：脚本能启动、argparse 定义没冲突、--help 不炸、缺手机时给的是人话而不是
     traceback、闸门拒绝时退出码是 3 而不是 1。

**不能**：不能证明 tap 真的点到了屏幕上。那需要一台连着线的手机。
    实测教训：19 个子命令 `--help` 全过，实机一跑三个 bug 全出来。
    所以这个测试**不声称**验证了真机行为 —— 它只挡「装不上」这一类错误。

## 关于闸门

phone.py / desktop.py 的动手类命令都要票。这里故意让它们**拿不到票**
（daemon 没跑），验证它们正确拒绝 —— 这是 fail-closed 该有的样子。
"""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENTS = os.path.join(ROOT, "agents")

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" +
          (f"  -- {detail}" if detail else ""))
    return ok


def run(args, timeout=90):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("QQ_BRIDGE_GATE", None)
    # 指向一个必然没人监听的端口，保证「拿不到票」是可复现的
    env["QQ_BRIDGE_GATE"] = "http://127.0.0.1:1/desktop_grant"
    return subprocess.run(
        [sys.executable, os.path.join(AGENTS, "phone.py")] + args,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, env=env, cwd=ROOT)


def run_desktop(args, timeout=90):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["QQ_BRIDGE_GATE"] = "http://127.0.0.1:1/desktop_grant"
    return subprocess.run(
        [sys.executable, os.path.join(AGENTS, "desktop.py")] + args,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, env=env, cwd=ROOT)


def main():
    print("=" * 62)
    print("agents smoke test (no phone / no QQ required)")
    print("=" * 62)

    # ---- phone.py ----
    r = run(["--help"])
    check("phone.py --help works", r.returncode == 0 and "usage" in
          (r.stdout or "").lower(), f"rc={r.returncode}")

    for sub in ("devices", "diagnose"):
        r = run([sub])
        # 这两个免票。没有手机时应该给人话或明确"没检测到设备"，
        # 关键是不能抛 traceback。
        txt = (r.stdout or "") + (r.stderr or "")
        no_traceback = "Traceback" not in txt
        check(f"phone.py {sub} does not crash (free action)",
              no_traceback, f"rc={r.returncode}")

    # 动手类命令：没票必须 exit 3，而且说的是人能看懂的话
    r = run(["tap", "100", "200"])
    check("phone.py tap is denied without a grant (exit 3)",
          r.returncode == 3, f"rc={r.returncode}")
    check("denial message is human readable",
          "DENIED" in (r.stdout or ""), (r.stdout or "")[:60])

    r = run(["look"])
    check("phone.py look is denied without a grant (exit 3)",
          r.returncode == 3, f"rc={r.returncode}")

    r = run(["key", "back"])
    check("phone.py key is denied without a grant (exit 3)",
          r.returncode == 3, f"rc={r.returncode}")

    # ---- desktop.py ----
    r = run_desktop(["--help"])
    check("desktop.py --help works", r.returncode == 0 and "usage" in
          (r.stdout or "").lower(), f"rc={r.returncode}")

    r = run_desktop(["--size"])
    check("desktop.py --size is a free action", r.returncode in (0, 1),
          f"rc={r.returncode}")

    r = run_desktop(["--click", "10", "10"])
    check("desktop.py click is denied without a grant (exit 3)",
          r.returncode == 3, f"rc={r.returncode}")

    # ---- who_called.py ----
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run(
        [sys.executable, os.path.join(AGENTS, "who_called.py")],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60, env=env, cwd=ROOT)
    check("who_called.py runs without a log file", r.returncode == 0,
          (r.stdout or "").strip()[:50])

    print("-" * 62)
    print(f"passed {len(PASS)}   failed {len(FAIL)}")
    if FAIL:
        for f in FAIL:
            print("  -", f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
