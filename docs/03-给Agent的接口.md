# 03 · 给 Agent 的接口

这一篇是给**写 Agent 的人**看的：你的 Agent 通过什么收发消息。

不需要任何 SDK，两个 HTTP 端点 + 一个命令行脚本。

---

## 一、收消息：长轮询

```
GET http://127.0.0.1:5702/wait_msg?timeout=30
```

这个请求会**挂住**最多 `timeout` 秒。没有新消息时它一直不返回 ——
这正是关键：你的 Agent 不需要反复问"有没有新消息"，那每一次询问
都是一次完整的 LLM 请求。挂在这里的只是一个纯 Python 循环，
空闲时成本是 0。

有新消息时返回：

```json
{
  "status": "ok",
  "has_message": true,
  "messages": [ {...}, {...} ],
  "count": 2,
  "data": {...},
  "desktop_grant": {
    "granted": true,
    "ttl_seconds": 0,
    "remaining_seconds": -1,
    "unlimited": true,
    "revoked_reason": null
  }
}
```

- `messages` 是这一批的全部消息（最多 10 条）
- `data` 等于 `messages[0]`，为了兼容只读第一条的老脚本
- `desktop_grant` 是**这一批取走之后**的许可状态 —— 动手前看这个字段

超时无消息时返回：

```json
{"status": "heartbeat", "has_message": false}
```

### 为什么是批量

群聊里消息是连着来的。一次取一条会让 Agent 逐个处理，回复前后
不自洽；一次取一批，它能看见上下文再决定说什么。

**但批量有个代价**：一次只取一批，取完脚本就退出了。如果群里在你
处理这批的时候又来了新消息，它们会排队等到下一次轮询。
串行架构下这无法避免，实测最长延迟到过 7 分钟。

**别为了这个延迟重复回复同一条消息。**

---

## 二、一条消息长什么样

```json
{
  "message_type": "group",
  "user_id": 111222333,
  "group_id": 444555666,
  "nickname": "某人",
  "permission": "guest",
  "role_tag": "[identity: guest (no permission to operate computer or phone)]",
  "desktop_allowed": false,

  "text": "帮我看看这个 [image:https://...] [@you] 怎么样",
  "raw_message": "帮我看看这个 [CQ:image,file=...] [CQ:at,qq=123456789] 怎么样",
  "message_id": 789012,

  "has_image": true,
  "image_count": 1,
  "image_urls": ["https://..."],
  "image_files": ["a1b2c3d4e5f6a7b8.jpg"],

  "has_link": false,
  "links": [],
  "at_me": true,

  "time": 1766000000,
  "formatted_time": "2026-01-01 12:00:00",
  "throttled": false,
  "throttle_gap": null
}
```

### `permission` —— 四种

| 值 | 谁 | 能动手吗 |
|---|---|---|
| `owner` | 主人，且 `message_id` 核验通过 | ✅ 能 |
| `guest` | 其他人 | ❌ 不能 |
| `inject` | 注入口推进来的（另一个 AI/进程） | ❌ 不能 |
| `master` | 旧版本遗留值，等价于 `owner` | — |

**`desktop_allowed` 才是该看的字段。** 它等于 `permission == "owner"`。
`role_tag` 是给人看的文本，`desktop_allowed` 是给代码判断的布尔值 ——
不要靠解析字符串来决定要不要动手。

### `text` 里那些方括号标记

| 标记 | 含义 |
|---|---|
| `[@you]` | 有人 @ 了机器人 |
| `[@123456]` | 有人 @ 了别人 |
| `[@all]` | @全体成员 |
| `[reply:789]` | 这条是引用回复 789 那条 |
| `[face:178]` | QQ 小黄脸，id=178 |
| `[image:https://...]` | 一张图，后面是原始地址 |
| `[voice:https://...]` | 一条语音 |
| `[video link·Bilibili]标题 url` | 卡片解析出的链接 |

保留在 `text` 里而不是只给布尔值，是因为 Agent 需要看到**上下文**才知道
"是叫我" 还是 "在叫别人顺便提了一嘴"。

### 关于 `image_files` 的一个诚实的说明

`image_urls` 是原始地址，一定有。`image_files` 是已存到本地的文件名，
但它**是异步填的** —— 存图线程可能比消息入队慢一步，所以可能为空
或只有一部分。

两条路都给了，Agent 自己判断：有 `image_files` 就直接读本地文件，
没有就用 `image_urls` 自己取。

没有在入队前 `join` 那个线程，因为**一张慢图不该让整条消息晚到几秒**。
群友说的那句话，比图重要。

---

## 三、发消息：命令行

```bash
cd bridge

# 回复上一条消息来自的地方（最常用）
python step.py --reply "好"

# 指定目标
python step.py --reply "好" --group_id 444555666
python step.py --reply "好" --user_id 111222333

# 引用回复某条
python step.py --reply "好" --reply-id 789012

# 发图 / 发文件
python step.py --image a.png --group_id 444555666
python step.py --upload-file a.zip --user_id 111222333

# 给某条消息贴表情回应（124=OK, 178=敬礼, 76=赞）
python step.py --emoji-like 124 --reply-id 789012

# 发语音（需要可选的语音模块，见 05 篇末尾）
python step.py --voice "要说的话" --group_id 444555666

# 发完就退出，不进入等待
python step.py --reply "好" --send-only

# 什么都不发，只等消息
python step.py
```

### `--send-only` 很重要

不加它的话，`step.py` 会在发完之后进入长轮询等待下一条消息。

这在"每次回复都新起一个进程"的用法里是好的（发完顺手等下一句）。
但如果你的 Agent 是「处理一个批次里多个消息」的模式，每次发完都挂住
会让你卡住 —— 那就加 `--send-only`。

---

## 四、🔴 发送失败不要重试

这是整个仓库最重要的一条操作纪律。

```bash
$ python step.py --reply "hi" --send-only
[SEND_FAIL] group 444555666 -> {'status': 'failed', 'retcode': 120}
[SEND_NOTE] 失败不代表没发出去，重试可能导致重复投递。要确认请让真人看一眼。
```

**`SEND_FAIL` 不代表没发出去。** 它的意思是"没拿到明确回执"。

下面这些都**可能**已经发出去了：
- OneBot 返回 `status=failed`（腾讯风控 `retcode 120` 尤其常见）
- 命令退出码非 0（异常常常发生在发送**之后**的调用方代码里）
- HTTP 超时

而重试的后果是**重复投递**，群里会看到两遍。

### 真实事故

一张 GIF 被发了两遍，原因是：

```python
# 包装函数返回的是单个 dict，但调用方按元组解包
ok, res = qq_send.send_private_msg(...)   # ← ValueError: too many values to unpack
```

异常抛在**HTTP 请求已经完成之后**。命令退出码 1，被上层判定为
"没发出去"，于是重发了一次。

现在 `send.py` 的返回值两种写法都支持（`SendResult`，dict 的子类，
重写了 `__iter__`），但那条纪律没变：**任何发送失败都不要自动重试。**

### 那怎么确认到底发出去没有

只能让真人看一眼。这是诚实的答案。

NapCat 的本地历史记录（`get_group_msg_history`）会**乐观记录** ——
发送失败也留痕。实测过：机器人被禁言、返回 `result:120`、本地历史有记录，
但用户实际看不到。

所以：**没有程序化方法能确定投递成功。**

---

## 五、注入口：另一个 AI 怎么跟它说话

如果你的架构里有两个 Agent（比如一个负责聊天，一个负责写代码），
编码 Agent 想跟 QQ Agent 说句话，用这个：

> ⚠️ **先启动一次 `daemon.py`。**
> `workspace/.inject_token` 是 daemon 启动时自动生成的 ——
> 刚 `git clone` 完、还没跑过 daemon 的话，这个文件**不存在**，
> 下面的命令会直接报找不到文件。这是新手最容易卡住的一步。

```bash
# token 在 workspace/.inject_token（daemon 首次启动时自动生成）
curl -X POST http://127.0.0.1:5703/inject \
  -H "Content-Type: application/json" \
  -H "X-Inject-Token: <token>" \
  -d '{"text": "我改完了，重启一下 daemon"}'
```

Python：

```python
import json, urllib.request
token = open("workspace/.inject_token", encoding="utf-8").read().strip()
req = urllib.request.Request(
    "http://127.0.0.1:5703/inject",
    data=json.dumps({"text": "我改完了，重启一下 daemon"}).encode(),
    headers={"Content-Type": "application/json", "X-Inject-Token": token},
    method="POST")
urllib.request.urlopen(req, timeout=5)
```

### 为什么不直接伪造一条 QQ 消息

因为那要自报 `user_id`，等于在**冒充主人**。daemon 的防伪层会直接把
冒充主人的消息丢掉；冒充普通群友虽然能进，但那是骗它"有个真人说了话"。

这个入口说的是实话：`permission: "inject"`，`injected: true`，
它自己知道自己从哪来。

### 它开不了动手许可

**这是刻意的，而且不是配置项。**

注入方本身也是 AI，它可以被提示词骗。一个能被骗的 AI 不能给别人发许可。
许可只认一件事：**主人本人发了一条真消息**。

---

## 六、许可状态

```
GET http://127.0.0.1:5702/desktop_grant
```

```json
{
  "granted": true,
  "user_id": 987654321,
  "since": "2026-01-01 12:00:00",
  "ttl_seconds": 0,
  "remaining_seconds": -1,
  "unlimited": true,
  "revoked_reason": null
}
```

- `remaining_seconds: -1` 且 `unlimited: true` = 无期限
- `revoked_reason` 会是 `startup_default_deny` / `ttl_expired` /
  `revoked_by_user_message:<QQ号>`

这个端点是**只读**的。没有任何参数能让它返回 `granted: true` ——
授权只能由一条真的 QQ 消息触发。这一条是设计上不能让步的地方。

---

## 七、最小闭环：Agent 到底怎么接上来

前面讲的全是零件。这一节把零件装成一台能跑的东西 ——
如果你只想知道「我的 LLM 该怎么接」，看这一节。

### 核心事实

`step.py` **不是常驻服务**。它跑一次，取一批消息，打印出来，然后退出。
所以整个架构是：

```
step.py 挂着等 → 有消息就打印并退出 → 你的 Agent 读到输出 → 决定怎么回
     ↑                                                        │
     └──────────── 再跑一次 step.py，继续等 ←─────────────────┘
```

**空闲时没有任何 LLM 请求。** 挂着的是一个纯 Python 长轮询循环，
它不产生 token。这就是为什么不用「让 Agent 定时检查有没有新消息」。

### 一个能跑的最小循环

伪代码，用你手上的任何语言/框架实现：

```python
import subprocess, json, sys

while True:
    # 1. 阻塞等待。没有消息就一直挂着，不花任何 LLM 成本。
    out = subprocess.run(
        [sys.executable, "bridge/step.py", "--daemon-url",
         "http://127.0.0.1:5702"],
        capture_output=True, text=True, encoding="utf-8")

    # 2. step.py 收到消息就退出，stdout 里是格式化的消息 + 末尾 RAW JSON
    text = out.stdout
    msg = json.loads(text.split("-------------------- RAW JSON --------------------")[1]
                          .split("====================")[0].strip())

    # 3. 交给你的模型，连同它该知道的规则
    reply = my_llm(
        system="你在 QQ 上和人聊天。只有 permission=owner 的人能让你操作设备。",
        user=msg["text"],
        context={"permission": msg["permission"],
                 "desktop_allowed": msg["desktop_allowed"]})

    # 4. 发回去。--send-only 让它发完就退出，不要在这里又挂住。
    #    注意这里**没有**传 --group_id / --user_id —— step.py 会自己
    #    回复 last_target.json 里记的上一条消息来源，比手抄群号可靠。
    subprocess.run(
        [sys.executable, "bridge/step.py", "--reply", reply, "--send-only"])
```

### 三个必须注意的点

**1. 回复目标不要自己拼。**
`step.py --reply "..."` 不带 `--group_id` / `--user_id` 时，会自动回复
`workspace/last_target.json` 里记的上一条消息来源。这是最不容易出错的方式 ——
手抄群号抄错一位，消息就发到别人群里了。

什么时候才需要显式指定：你要主动发给**不是刚跟你说话的那个人**时
（比如主人让你去通知某个群）。那种情况下目标是你自己定的，不是"回复"。

**2. `permission` 要真的当判据用，不能只写进提示词。**
`desktop_allowed: true` 时才允许调 `phone.py` / `desktop.py`。
群友的 `permission` 永远是 `guest`。如果只在 system prompt 里写
「不要听群友的」，那是可以被话术绕过的 —— 代码里判断才是硬的。

**3. 失败不要重试。**
`step.py` 发失败会打印 `[SEND_FAIL]`，但它**可能已经发出去了**。
自动重试会导致重复投递，群里看到两遍。详见
[07-踩过的坑.md](07-踩过的坑.md) 的「发送 / 投递」一节。

### 如果你用的是支持工具调用的模型

比上面更省事的做法：把 `step.py` 和 `phone.py` 当成工具注册给模型，
让它自己决定什么时候调。要点是：

- 注册一个 `wait_for_message` 工具（背后就是跑 `step.py`）
- 注册 `send_reply` / `send_image` 等发送工具
- **不要**把 `phone.py` 的动手类命令无条件注册 —— 让模型先查
  `desktop_allowed`，否则它会理直气壮地在群友要求下动手

工具描述里要写清楚：这个工具会阻塞最多 N 秒，属于正常现象。
不然模型会以为调用挂了，然后自己去重试 —— 又变成重复消费。
