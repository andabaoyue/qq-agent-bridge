# -*- coding: utf-8 -*-
"""
config.py — 一处读配置，读不到就给人话级提示。

## 为什么单独抽出来

原来这套桥是写死贴身用的：机器人号、主人号、群号、别名表全都是源码里的
常量。要用到别人电脑上，只能让他去改 Python 源码 —— 那等于让人家做代码
审查才能配置。

现在这些全部挪进 bridge/config.json，源码里一个真实号码都没有。

## 优先级

    1. 环境变量 QQ_BRIDGE_CONFIG 指向的文件
    2. 脚本同目录的 config.json
    3. 脚本上一级的 bridge/config.json

都没有就退出，并打印一份最小可用的模板 —— 不要让人对着 traceback 猜。

## 配置项一览（都有默认值，只有 bot_qq / owner_qq 必须填）

    bot_qq      机器人自己的 QQ 号（数字）
    owner_qq    主人 QQ 号，一个数字或数字数组
    bot_names   群里提到这些词就算在叫机器人
    ...
"""

import json
import os
import sys

_DEFAULT_CONFIG = {
    "bot_qq": None,
    "owner_qq": [],

    # 群里提到这些词就算在叫机器人。用户自己加昵称，不带任何预设。
    "bot_names": [],

    "onebot_api": "http://127.0.0.1:3000",

    "listen": {
        "events_host": "127.0.0.1",
        "events_port": 5701,
        "poll_host": "0.0.0.0",
        "poll_port": 5702,
        "inject_host": "127.0.0.1",
        "inject_port": 5703,
    },

    # 群过滤器。默认只回被点名的消息，其余全部丢弃。
    "group_filter": {
        "mode": "mention_only",
        "allow_groups": [],
        "block_groups": [],
        "question_keywords": [
            "请问", "怎么", "如何", "在哪", "哪个",
            "为什么", "有没有", "懂不懂", "能不能", "可不可以",
        ],
    },

    # 权限闸门。ttl_seconds = 0 表示「主人发过消息就一直有效，直到被撤销」。
    # 想恢复「主人发一条，15 分钟内可动手」，填 900。
    "gates": {
        "ttl_seconds": 0,
    },

    # 冷却
    "cooldowns": {
        "user_seconds": 15,
        "group_image_seconds": 30,
    },

    # 注入口：给「另一个 AI / 另一个进程」往机器人队列里塞消息用的独立通道。
    # 它**永远开不了桌面/手机的门**，这条是硬编码的，不要改成可配置 ——
    # 一旦可配置，注入方（本身也是 AI）就能自己给自己发许可。
    "injection": {
        "enabled": True,
        "token_file": ".inject_token",
        "nickname": "coder-agent",
        "role_tag": "[identity: coder-agent (helper, no desktop permission)]",
    },

    # 收到的图片存到哪。只存原件，不做任何分类或标签。
    "media_inbox": "media_inbox",
    "media_max_bytes": 8388608,
}


def _deep_merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _candidate_paths():
    here = os.path.dirname(os.path.abspath(__file__))
    paths = []
    env = os.environ.get("QQ_BRIDGE_CONFIG")
    if env:
        paths.append(env)
    paths.append(os.path.join(here, "config.json"))
    paths.append(os.path.join(os.path.dirname(here), "bridge", "config.json"))
    return paths


_TEMPLATE = """{
  "_comment": "把 bot_qq 和 owner_qq 改成你自己的，其余可以先不动",
  "bot_qq": 123456789,
  "owner_qq": 987654321,
  "bot_names": ["小助手"]
}"""


def load(required=True):
    """读配置。返回 dict（已和默认值合并）。required=False 时不会退出。"""
    for p in _candidate_paths():
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8-sig") as f:
                    user = json.load(f)
            except Exception as e:
                print(f"[config] 读不了 {p}：{e}", file=sys.stderr)
                if required:
                    sys.exit(2)
                return _DEFAULT_CONFIG
            cfg = _deep_merge(_DEFAULT_CONFIG, user)
            cfg["_source"] = p
            _normalize_id_lists(cfg)
            if required:
                _validate(cfg, p)
            return cfg

    if required:
        print("[config] 找不到配置文件。最少需要这些：\n", file=sys.stderr)
        print(_TEMPLATE, file=sys.stderr)
        print("\n存成 bridge/config.json 再启动。", file=sys.stderr)
        sys.exit(2)
    return _DEFAULT_CONFIG


def _normalize_id_lists(cfg):
    """把「单个数字 / 数组 / 通配符」统一成一致形态。

    踩过的坑：`allow_groups: ["*"]` 在第一次写这个函数时被
    `int("*")` 直接打成 ValueError —— 一个配置通配符让整个进程起不来。
    所以这里显式保留 `"*"`，只把其余项转 int。
    """
    oq = cfg.get("owner_qq")
    if oq is None:
        cfg["owner_qq"] = []
    elif isinstance(oq, (int, str)):
        cfg["owner_qq"] = [oq]
    cfg["owner_qq"] = [int(x) for x in cfg["owner_qq"] if str(x).strip()]

    bq = cfg.get("bot_qq")
    if bq is not None and str(bq).strip():
        cfg["bot_qq"] = int(bq)

    gf = cfg.setdefault("group_filter", {})
    for key in ("allow_groups", "block_groups"):
        out = []
        for x in (gf.get(key) or []):
            s = str(x).strip()
            if not s:
                continue
            out.append("*" if s == "*" else int(s))
        gf[key] = out


def _validate(cfg, path):
    problems = []
    if not cfg.get("bot_qq"):
        problems.append("bot_qq 没填 —— 需要一个真实存在的 QQ 号（机器人自己的）")
    if not cfg.get("owner_qq"):
        problems.append("owner_qq 没填 —— 主人 QQ 号；不填就没人能开上手许可")
    if problems:
        print(f"[config] {path} 有问题：", file=sys.stderr)
        for p in problems:
            print(f"  · {p}", file=sys.stderr)
        sys.exit(2)


def workspace_of(cfg, from_file):
    """日志、队列、媒体都放在仓库根的同一个可写目录里。

    `from_file` 传调用方脚本的 __file__。定位规则：
        <repo>/bridge/xxx.py  或  <repo>/agents/xxx.py
                    ->         <repo>/workspace/

    为什么不用「脚本所在目录」：脚本分在 bridge/ 和 agents/ 两个目录里，
    如果各自建 workspace，日志就会散成两份 —— 排查时得记住去哪找。
    """
    ws = cfg.get("workspace_dir") or "workspace"
    if os.path.isabs(ws):
        os.makedirs(ws, exist_ok=True)
        return ws

    here = os.path.dirname(os.path.abspath(from_file))
    # 若 from_file 就是目录（测试用），直接用它
    if os.path.isdir(from_file):
        here = os.path.abspath(from_file)
    repo = os.path.dirname(here)
    ws = os.path.join(repo, ws)
    os.makedirs(ws, exist_ok=True)
    return ws
