#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把最新一批热点**直接推到企微群里**——不是推一条链接，而是把 17 个平台的标题铺开。

为什么要有这个脚本：
    之前群里收到的只是「腾讯文档 / 看板」的链接。看的人要点进去才知道今天有什么热点，
    等于多一步操作，很多人就懒得点了。老板要的是打开群就能看见标题。
    文档和看板照旧生成（那是留档和可视化），只是**推送内容**改成结构化正文。

推送通道：企业微信群机器人 webhook（群里「添加群机器人」即可拿到，无需管理员审批应用）。
    服务器在每小时采集完后直接 curl 一条 POST，7×24 定时，不依赖 Mac 开机。

⚠️ 体积是这里唯一的硬约束：企微 markdown 消息**单条最多 4096 字节（UTF-8）**，
    超了会返回 errcode 40058 整条丢弃。实测一批 51 条标题光正文就 3600+ 字节，
    加上平台名必然超限 —— 所以必须**分片**，且分片**只能在平台边界切**
    （从某个平台的 TOP2 中间断开，群里看到的就是残缺的榜单）。
    本脚本按平台累积到 3200 字节就开新条，实测稳定分成 2 条。

用法:
    python3 push_wecom.py                      # 推最新批次（已推过的自动跳过）
    python3 push_wecom.py --dry-run            # 只打印要发什么，不真发
    python3 push_wecom.py --force              # 已推过也重推
    python3 push_wecom.py --batch "2026-09-22 10:50:00"   # 指定批次
    python3 push_wecom.py --file data/hot_20260922.jsonl --batch "..."  # 指定数据源
    echo '{"webhook":"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"}' \
        > ~/.hot-rank/wecom_webhook.json       # 配置机器人地址

webhook 地址按以下顺序找（第一个命中的生效）：
    1. 环境变量 WECOM_WEBHOOK
    2. ~/.hot-rank/wecom_webhook.json         （本机；deploy_server.sh 会同步到服务器）
    3. 当前目录下的 .wecom_webhook.json       （服务器上跑时的兜底）
没配到就**静默跳过并返回 0** —— 挂在每小时任务里，不能因为没配机器人就让采集失败。

免打扰：默认只在 **09:00~21:00** 推（按批次时间的小时数判断），深夜和清晨不打扰人。
一天 12 条（09:50 ~ 20:50）。可用环境变量 WECOM_PUSH_START_HOUR / WECOM_PUSH_END_HOUR 调整，
调试时用 --anytime 忽略时段。跳过**同样返回 0**，不影响采集。
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

# 单条消息正文上限。企微硬上限 4096，留 900 字节给「(1/2)」标题、平台名和余量。
MAX_BYTES = 3200

# 两条之间歇一下：企微对同一机器人有频率限制（20 条/分钟），分片只有 2~3 条，
# 但连发太快仍可能被限流，1 秒足够。
CHUNK_GAP_SEC = 1

# 每平台取几条。数据源本来就是每平台 TOP3，这里再做一次保险。
TOP_N = 3

STATE_FILE = ".wecom_pushed"

# 每条末尾固定的汇总页入口（用户要求：群里直接可点，不用再翻文档/看板链接）。
# 放在**每一片**而不是只放最后一片：分两条时，只看第一条的人也要能点到入口。
#
# ⚠️ 必须写成 markdown 链接语法 `[文字](url)`，不能裸写 URL：
#    企微的 markdown 不会把裸 URL 自动变成超链接，那样群里看到的就是一行灰字，
#    点不动 —— 用户要的是「点一下就跳转」，所以这里必须是链接语法。
#    （标题行的 clean() 会剥掉 `[]` 等字符，那是正文的事，脚注不受影响。）
FOOT_LINE = "\n> 热榜汇总页：[点击直达](https://rree.cn/)"

# 免打扰时段：只在 07:00 ~ 23:00 推（**两端都含**），深夜和清晨不打扰人。
# 用**批次时间**判断而不是「现在几点」：服务器偶发延迟也不会把 23 点的批次拖到 00:05 还推。
# 端点语义是「最后一个会推的小时」，所以 23 表示 23 点档照推、0 点档起免打扰。
PUSH_START_HOUR = int(os.environ.get("WECOM_PUSH_START_HOUR", 7))
PUSH_END_HOUR = int(os.environ.get("WECOM_PUSH_END_HOUR", 23))


# ---------------- 配置与数据源 ----------------

def find_webhook() -> str:
    """找群机器人地址，找不到返回空串（调用方据此静默跳过）。"""
    env = os.environ.get("WECOM_WEBHOOK", "").strip()
    if env:
        return env
    for path in (os.path.expanduser("~/.hot-rank/wecom_webhook.json"),
                 ".wecom_webhook.json",
                 os.path.join(os.path.dirname(os.path.abspath(__file__)), ".wecom_webhook.json")):
        try:
            with open(path, encoding="utf-8") as f:
                url = (json.load(f).get("webhook") or "").strip()
            if url:
                return url
        except (OSError, ValueError):
            continue
    return ""


def find_latest_file() -> str:
    """定位 latest_batch.json：服务器在仓库根跑，本机在项目子目录跑。"""
    env = os.environ.get("HOT_RANK_LATEST", "").strip()
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    home = os.path.expanduser("~")
    for path in ("data/latest_batch.json",
                 os.path.join(here, "data", "latest_batch.json"),
                 os.path.join(home, "hot-rank", "hot-rank-collector", "data", "latest_batch.json"),
                 os.path.join(home, "hot-rank", "data", "latest_batch.json")):
        if os.path.isfile(path):
            return path
    return ""


def load_platform_order() -> list:
    """按白名单顺序排平台（看得顺眼）。读不到配置就退回数据里的出现顺序。

    只做极简正则解析，不 import yaml —— 这个脚本要在服务器的精简 venv 里跑，
    少一个依赖少一类失败。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (os.path.join(here, "config", "settings.yaml"),
                 os.path.join(here, "..", "hot-rank-collector", "config", "settings.yaml")):
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        block = re.search(r"include_platforms:\s*\n((?:\s*-\s*[\"'].*?[\"'].*\n)+)", text)
        if not block:
            continue
        names = re.findall(r"-\s*[\"']([^\"']+)[\"']", block.group(1))
        if names:
            return names
    return []


# ---------------- 取数 ----------------

def load_batch(path: str, batch: str = "") -> tuple:
    """返回 (批次时间戳, records)。path 可以是 latest_batch.json 或当日 jsonl。"""
    if path.endswith(".jsonl"):
        rows = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        if not rows:
            return "", []
        stamps = sorted({r.get("crawl_time", "") for r in rows if r.get("crawl_time")})
        target = batch or (stamps[-1] if stamps else "")
        return target, [r for r in rows if r.get("crawl_time") == target]

    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    records = data.get("records") or []
    if batch:
        records = [r for r in records if r.get("crawl_time") == batch]
    # crawl_time 缺失时退回 written_at：否则 stamp 为空，免打扰判断会失效（空戳按放行处理）
    stamp = ""
    if records:
        stamp = records[0].get("crawl_time") or data.get("written_at") or ""
    else:
        stamp = data.get("written_at") or ""
    return stamp, records


def group(records: list, order: list) -> list:
    """按平台分组，每平台取前 TOP_N 条（按 rank 排序）。"""
    buckets = {}
    for r in records:
        buckets.setdefault(r.get("platform", ""), []).append(r)
    names = [p for p in order if p in buckets]
    names += [p for p in buckets if p not in names]

    out = []
    for name in names:
        items = sorted(buckets[name], key=lambda r: r.get("rank") or 99)[:TOP_N]
        out.append((name, items))
    return out


# ---------------- 排版 ----------------

def clean(text: str) -> str:
    """去掉会破坏企微 markdown 渲染的字符，并压平换行。

    标题里出现过 `*`、`[`、`]` 会让加粗/链接语法错乱（群里显示成乱码符号），
    所以一律剥掉。emoji 保留 —— 群里带 emoji 更好认。
    """
    text = re.sub(r"[*`\[\]#]", "", str(text or ""))
    return re.sub(r"\s+", " ", text).strip()


def build_chunks(stamp: str, groups: list) -> list:
    """拼成若干条消息。**只能在平台边界切**，绝不从某平台的 TOP2 中间断开。"""
    head = "🔥 **全网热点榜** · {}\n> {} 个平台 · {} 条\n".format(
        stamp[5:16] if len(stamp) >= 16 else stamp,
        len(groups), sum(len(items) for _, items in groups))
    # 页码要打在标题行（而不是末尾）—— 群里一屏看不到底，翻到末尾才发现还有下一条没用
    head_plain = head

    # 先按平台切成块，再贪心合并到 MAX_BYTES 以内
    blocks = []
    for name, items in groups:
        lines = ["\n**{}**".format(clean(name))]
        for i, r in enumerate(items, 1):
            lines.append("{}. {}".format(i, clean(r.get("title"))))
        blocks.append("\n".join(lines))

    # 脚注占的字节要提前预留，否则加了脚注才发现超上限
    room = MAX_BYTES - len(FOOT_LINE.encode("utf-8"))

    # 即使单平台超长（几乎不可能）也要保证它自己成条，宁可超一点也不能截断标题
    chunks, cur = [], ""
    for b in blocks:
        cand = (cur + "\n" + b) if cur else (head + b)
        if cur and len(cand.encode("utf-8")) > room:
            chunks.append(cur)
            cur = head + b
        else:
            cur = cand
    if cur:
        chunks.append(cur)

    chunks = [c + FOOT_LINE for c in chunks]

    if len(chunks) > 1:
        chunks = [c.replace(head_plain,
                            head_plain.rstrip("\n") + "（{}/{}）\n".format(i + 1, len(chunks)), 1)
                  for i, c in enumerate(chunks)]
    return chunks


def in_push_window(stamp: str) -> bool:
    """判断该批次是不是在允许推送的时段（07:00~23:00，**两端都含**）。

    用批次时间戳的小时数判断，而不是「现在几点」——服务器偶尔延迟几分钟，
    23 点的批次拖到 00:0x 才跑完也不该在夜里把人吵醒，反之亦然。
    解析不出来就**放行**（宁可多推一条，也别因为格式问题整条链路静默失效）。
    """
    m = re.search(r"(\d{2}):\d{2}", stamp or "")
    if not m:
        return True
    hour = int(m.group(1))
    if PUSH_START_HOUR <= PUSH_END_HOUR:
        return PUSH_START_HOUR <= hour <= PUSH_END_HOUR
    return hour >= PUSH_START_HOUR or hour <= PUSH_END_HOUR   # 跨零点的情况


def already_pushed(stamp: str, state_path: str) -> bool:
    try:
        with open(state_path, encoding="utf-8") as f:
            return f.read().strip() == stamp
    except OSError:
        return False


def remember(stamp: str, state_path: str) -> None:
    try:
        with open(state_path, "w", encoding="utf-8") as f:
            f.write(stamp)
    except OSError:
        pass


# ---------------- 发送 ----------------

def send(webhook: str, content: str) -> dict:
    body = json.dumps({"msgtype": "markdown", "markdown": {"content": content}},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        webhook, data=body,
        headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with op.open(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(description="把最新热点批次推到企微群")
    ap.add_argument("--file", default="", help="数据源（jsonl 或 latest_batch.json），默认自动找")
    ap.add_argument("--batch", default="", help="指定批次时间戳，默认最新")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不发送")
    ap.add_argument("--force", action="store_true", help="已推过也重推")
    ap.add_argument("--limit", type=int, default=0, help="只取前 N 个平台（调试用）")
    ap.add_argument("--anytime", action="store_true",
                    help="忽略免打扰时段（调试用，正常跑不要加）")
    args = ap.parse_args()

    webhook = find_webhook()
    path = args.file or find_latest_file()
    if not path or not os.path.isfile(path):
        print(json.dumps({"status": "no_data", "hint": "找不到数据文件"}, ensure_ascii=False))
        return 1

    stamp, records = load_batch(path, args.batch)
    if not records:
        print(json.dumps({"status": "no_data", "file": path, "batch": args.batch},
                         ensure_ascii=False))
        return 1

    groups = group(records, load_platform_order())
    if args.limit:
        groups = groups[:args.limit]
    if not groups:
        print(json.dumps({"status": "no_data", "reason": "该批次没有白名单平台"},
                         ensure_ascii=False))
        return 1

    chunks = build_chunks(stamp, groups)

    # 免打扰：09:00~21:00 之外不推。--anytime 可强制（只用于调试）
    quiet = (not args.anytime) and (not in_push_window(stamp))

    if args.dry_run:
        print("批次: {} | 平台 {} 个 | {} 条 | 分成 {} 条消息 | webhook {} | {}".format(
            stamp, len(groups), sum(len(i) for _, i in groups), len(chunks),
            "已配置" if webhook else "**未配置**",
            "**免打扰时段，不会真推**" if quiet else "在推送时段内"))
        for i, c in enumerate(chunks, 1):
            print("\n----- 第 {} 条（{} 字节）-----\n{}".format(
                i, len(c.encode("utf-8")), c))
        return 0

    if not webhook:
        # 没配机器人不算故障：挂在每小时任务里，不能因此让整轮采集判失败
        print(json.dumps({"status": "skipped", "reason": "未配置群机器人 webhook",
                          "hint": "写入 ~/.hot-rank/wecom_webhook.json 或设 WECOM_WEBHOOK"},
                         ensure_ascii=False))
        return 0

    if quiet:
        # 免打扰不算故障，返回 0，别让整轮采集判失败
        print(json.dumps({"status": "skipped",
                          "reason": "非推送时段（只在 {:02d}:00~{:02d}:00 推）".format(
                              PUSH_START_HOUR, PUSH_END_HOUR),
                          "batch": stamp}, ensure_ascii=False))
        return 0

    state = os.path.join(os.path.dirname(os.path.abspath(path)), STATE_FILE)
    if not args.force and already_pushed(stamp, state):
        print(json.dumps({"status": "skipped", "reason": "该批次已推送过",
                          "batch": stamp}, ensure_ascii=False))
        return 0

    for i, chunk in enumerate(chunks, 1):
        if i > 1:
            time.sleep(CHUNK_GAP_SEC)
        try:
            res = send(webhook, chunk)
        except (urllib.error.URLError, OSError, ValueError) as e:
            print(json.dumps({"status": "failed", "chunk": i, "error": repr(e)},
                             ensure_ascii=False))
            return 1
        if res.get("errcode") != 0:
            # 40058 = 单条超长；45009 = 频率限制。都要报出来，别静默吞掉
            print(json.dumps({"status": "failed", "chunk": i,
                              "errcode": res.get("errcode"), "errmsg": res.get("errmsg"),
                              "bytes": len(chunk.encode("utf-8"))}, ensure_ascii=False))
            return 1

    remember(stamp, state)
    print(json.dumps({"status": "ok", "batch": stamp, "chunks": len(chunks),
                      "platforms": len(groups),
                      "rows": sum(len(i) for _, i in groups)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
