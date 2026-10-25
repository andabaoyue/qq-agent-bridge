# qq-agent-bridge

让一个 AI Agent 真正住进你的 QQ 里，并且能操作你的电脑和手机。

这个仓库解决两件事：

1. **接入** —— 你的 AI 怎么收到 QQ 消息、怎么回话（文字 / 图片 / 文件）

> 语音：**收**（转 mp3 后交给 Agent）是通的；**发**只留了接口，
> 需要你自己实现 `workspace/voice_tts.py`（见 [docs/03](docs/03-给Agent的接口.md) 末尾）。
> 仓库不含这部分实现，所以别指望 `--voice` 开箱能用。
2. **动手** —— 你的 AI 怎么截图、点击、打字，操作这台电脑和那台手机

以及一件更要紧的事：

3. **控制** —— 只有你本人能授权它动手。群友说什么都不算。

第 3 点不是附加功能，是这套东西能不能给你用的前提。第 1、2 点随便哪个
能写代码的人都能做出来；难点在于**做完之后你敢让它一直开着**。

---

## 先说清楚这是什么、不是什么

**是**：一个本地桥。QQ 客户端（经 NapCat）把消息 POST 到本机的
一个小 Python 服务上，你的 Agent 从那里取消息、决定怎么回、调命令发出去。

**不是**：
- 不是协议逆向，不碰 QQ 的登录态。它依赖 [NapCat](https://github.com/NapNeko/NapCatQQ) 提供的 OneBot v11 接口。
- 不是托管服务，没有任何云端组件。全部在本机。
- 不保证你的 QQ 号不会被封。用第三方客户端本身就有风险，这个仓库不改变这一点。

---

## 30 秒看懂它长什么样

```
   你的 QQ ←→ NapCat（QQ 客户端插件）←→ 本机 HTTP
                                            │
                          ┌─────────────────┴──────────────────┐
                          │        bridge/daemon.py            │
                          │  5701 收事件   5702 长轮询   5703 注入口 │
                          │  ── 队列落盘 / 权限闸门 / 防伪核验 ──  │
                          └─────────────────┬──────────────────┘
                                            │  你的 Agent 长轮询这里
                                            ▼
                                      你的 AI Agent
                                            │
                          ┌─────────────────┴──────────────────┐
                          │  bridge/step.py    发消息、等消息    │
                          │  agents/phone.py   操作手机          │
                          │  agents/desktop.py 操作电脑          │
                          └────────────────────────────────────┘
                                            │
                                   每次动手前先问 daemon 要票
                                            ▼
                                   GET /desktop_grant
```

关键在最后那一步。`phone.py` 和 `desktop.py` **不是**凭自己判断该不该动手的，
它们每次都要去问 daemon。而 daemon 只会因为一件事开票：**你本人发了一条
真消息**（且那条消息的 `message_id` 能在 NapCat 那里查到）。

任何其他人说一句话，票立刻作废。

---

## 直接开始

```bash
git clone <this repo> qq-agent-bridge
cd qq-agent-bridge

# 1. 依赖
#    核心（daemon + step + send）只用 Python 标准库，什么都不用装。
#    下面这条会装桌面控制要用的 5 个包；如果你暂时不用 desktop.py，跳过它。
pip install -r requirements.txt

# 2. 复制配置，填你自己的两个号
cp bridge/config.example.json bridge/config.json
#    编辑 bridge/config.json：bot_qq = 机器人号，owner_qq = 你的号

# 3. 先跑自测，确认代码在你机器上是好的
python tests/selftest.py     # 28 项，不需要真 QQ
python tests/smoke.py        # 11 项，不需要手机

# 4. 起 daemon
python bridge/daemon.py
```

看到这几行就对了：

```
QQ bridge daemon started
  events  : 127.0.0.1:5701
  polling : 0.0.0.0:5702
  inject  : 127.0.0.1:5703 (local only)
  gate    : owner message grants, no expiry
```

然后去配 NapCat（[docs/01-接入QQ.md](docs/01-接入QQ.md)），让它的
HTTP 上报指向 `http://127.0.0.1:5701`。

---

## 文档

| 我想… | 看这个 |
|---|---|
| 把 QQ 接进来 | [01-接入QQ.md](docs/01-接入QQ.md) |
| 搞懂配置项 | [02-配置.md](docs/02-配置.md) |
| 让 Agent 收发消息 | [03-给Agent的接口.md](docs/03-给Agent的接口.md) |
|  **搞懂权限闸门（最重要）** | [04-权限闸门.md](docs/04-权限闸门.md) |
| 让它操作手机 | [05-手机控制.md](docs/05-手机控制.md) |
| 让它操作电脑 | [06-桌面控制.md](docs/06-桌面控制.md) |
| 我卡住了 | [07-踩过的坑.md](docs/07-踩过的坑.md) |
| 设计上为什么这么做 | [08-设计决策.md](docs/08-设计决策.md) |
| 哪些没验证过 | [09-未验证的部分.md](docs/09-未验证的部分.md) |
| 为什么不做某些事 | [10-为什么不做这些事.md](docs/10-为什么不做这些事.md) |

如果你只读一篇，读 [04-权限闸门.md](docs/04-权限闸门.md)。

---

## 这个仓库里有什么

```
bridge/
  daemon.py            事件接收 + 队列 + 闸门（核心，1700 行）
  step.py              Agent 侧 CLI：发消息 / 等消息
  send.py              底层发送 + 小黄脸名字解析
  config.py            配置加载（一处读，别处不碰文件）
  config.example.json  配置模板
agents/
  phone.py             手机控制（adb 薄封装，14 个子命令）
  desktop.py           桌面控制（截图 / 鼠标 / 键盘）
  who_called.py        查「这次操作是谁触发的」
  utf8_console.py      Windows 控制台编码补丁
docs/                  10 篇，全是踩坑换来的
tests/
  selftest.py          链路自测（28 项，不需要真 QQ）
  smoke.py             冒烟（11 项，不需要手机）
  manual_device.py     实机验证环境（需要真手机）
```

代码大约 4000 行，文档大约一万字。文档比代码长，这是故意的 ——
代码里的每一段注释都在回答「为什么不是另一种写法」，因为那些是真正
花时间的地方，也是最容易在重构中被无声改回去的地方。

---

## 打中文要的那个 APK

仓库里**不含** `ADBKeyboard.apk`。二进制文件不进代码树，理由见
[10-为什么不做这些事.md](docs/10-为什么不做这些事.md)。

需要的话自己下载，**并且自己核对**：

```
上游  https://github.com/senzhk/ADBKeyBoard
文件  https://github.com/senzhk/ADBKeyBoard/raw/master/ADBKeyboard.apk

SHA-256  E698ADEA5633135A067B038F9A0CF41BAA4DE09888713A81593FB2B9682CDC59
大小     17374 bytes
```

```bash
python agents/phone.py setup-input --apk 你的路径/ADBKeyboard.apk
```

> 这是一个**输入法**。IME 能读到你在任何输入框里打的内容，
> 所以在按 `ime enable` 之前，值得想一秒这个文件是从哪来的。
>
> **不装也行。** 打纯 ASCII 走的是 `adb shell input text`，用不到它；
> 只有中文/emoji 才需要。而且如果你的手机本来就有中文输入法，
> 用 `phone.py text` 之外的方式输入也完全没问题。

---

## 三个不打算做的事

写在前面，免得你期待落空：

**1. 不做 OCR。** 装一个 OCR 就能逐条核对 Agent 说的每句话真假，但那样
它不是在探索，是在应付考试。日志只记动作（点了哪、截了哪张图），不记内容 ——
"我看了 X" 这句话，日志证明不了，也不该假装能证明。

**2. 不做内容过滤。** 没有敏感词表，没有自动打标签。这类机制看起来贴心，
但每一项都要回答「标签存哪、谁能改、会不会被绕过」，答不上来就不该写进代码。
诚实地说：这道闸门拦的是「按说明书调用」和「被话术骗了之后照做」，
拦不住一个执意绕过它直接调 adb 的实现。要彻底锁死需要 OS 级沙箱。

**3. 不内置任何具体的人设。** 这个仓库不知道你的 bot 叫什么、怎么说话、
什么时候该插嘴。它只提供通路和闸门。人格是你自己养出来的，不是配置项。

---

## 哪些验证过、哪些没有

这点写清楚，免得你出故障时往错方向查：

| 部分 | 状态 |
|---|---|
| QQ 接入链路（daemon / step / 闸门） | ✅ 28 项自动测试 |
| `phone.py` 核心动作 | ✅ 真机验证过（小米 13 / Android 16，12 项动作 + 闸门三场景） |
| `phone.py wireless` | ❌ 未验证（需要人工配对码） |
| `phone.py setup-input` | ❌ 未验证（已查明不会重装，且中文输入已被间接覆盖） |
| `phone.py diagnose` 报错分支 | ❌ 只验证了"设备正常"那条 |
| `desktop.py` 动作类命令 | ❌ **一行都没跑过**（会打断正在用电脑的人） |
| 语音**发送** | ❌ 未实现（只留了 `text_to_silk()` 接口） |

完整说明和原因见 [docs/09-未验证的部分.md](docs/09-未验证的部分.md)。

**把"没验证"和"验证过"混在一起说，是这类文档最常见的失真方式。**

---

## 来源

这套东西是被真实使用过的，不是设计文档里长出来的。仓库里所有带日期
的注释都来自实际踩到的坑 —— 包括我自己造成的那些。其中三条值得先说：

- 「能跑」不等于「正确」。19 个命令 `--help` 全过，实机一跑三个 bug。
- 「没有弹窗」不等于「没有拦截」。系统可以静默拒绝，连提示都不给。
- 写死一个地址的代价不是"不方便"，是**测试会撒谎**。这个仓库在自测时
  真的因为一个写死的 `127.0.0.1:3000` 让 4 个用例对着本机真实的 QQ 跑了，
  日志上看起来一切正常。

---

## License

MIT. 见 [LICENSE](LICENSE)。
