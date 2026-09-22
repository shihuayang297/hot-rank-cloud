#!/usr/bin/env python3
"""通过腾讯文档官方开放平台 API 把热点批次写入在线表格。

这是「7×24 写文档」的核心：不依赖 WorkBuddy 桌面应用的连接器授权，
服务器拿自己的 OAuth 凭证直接调官方接口，所以 Mac 关机也能写。

────────────────────────── 接口形状（2026-09-21 实测得出） ──────────────────────────
官方文档页是 JS 渲染的，抓不到内容；下面每一条都是实测确认过的。

鉴权：三个 Header 缺一不可
    Access-Token / Client-Id / Open-Id
    Open-Id 就是 access_token(JWT) 里的 `sub` 字段，不用另外去拿。

查询子表（拿 sheetID 和当前行数）
    GET /openapi/sheetbook/v2/{bookID}/sheets-info
    bookID **必须带 `300000000$` 前缀**，用短 fileID 会得到 ret 404201。

读取区域
    GET /openapi/spreadsheet/v3/files/{fileID}/values/{range}?sheetId={sheetID}
    注意这里反而用**短 fileID**，且 range 不带 sheetID 前缀、sheetId 走 query，
    参数名大小写敏感（`sheetId` 可以，`sheetID` / `sheet_id` 都会失败）。
    range 必须是区间（`A1:E3`），单格（`A1`）和整列（`A:E`）都报 Range Validate error。

写入区域
    PUT /openapi/sheetbook/v2/{bookID}/values/{sheetID}!A1:E99
    Body: {"values": [[...], ...]}   —— 纯二维字符串数组
    上限 1000 行 / 10000 单元格 → 一批 99 行 × 5 列 = 495 格，**一次调用写完**。
    官方文档写「无法更新不存在的表格区域」，但**实测会自动扩容**
    （rowTotal 200 的表写第 250 行，成功，rowTotal 自动变 250），
    所以不需要先调扩行接口。

────────────────────────────────── 用法 ──────────────────────────────────
    python3 tencent_doc.py --info                 只体检：凭证是否有效、文档末尾批次
    python3 tencent_doc.py                        把 latest_batch.json 追加进文档
    python3 tencent_doc.py --after "T"            补写所有晚于 T 的批次
    python3 tencent_doc.py --from-jsonl f.jsonl   指定数据文件（回填历史用）
    python3 tencent_doc.py --dry-run              只算不写，打印将写什么

退出码：0 成功或无需写入；1 失败
"""
import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

API = "https://docs.qq.com"
BOOK_PREFIX = "300000000$"

# 凭证文件：服务器和本机用同一路径，方便 deploy_server.sh 直接 scp 过去
CRED_PATHS = [
    Path(os.environ.get("HOT_RANK_DOC_CRED", "")) if os.environ.get("HOT_RANK_DOC_CRED") else None,
    Path.home() / ".hot-rank" / "tencent_doc.json",
]

ROOT = Path(__file__).resolve().parent
# 平台白名单过滤（写出层兜底，见 platform_filter.py 的模块说明）
sys.path.insert(0, str(ROOT))
try:
    from platform_filter import filter_records
except ImportError:                                  # 模块缺失时退化为不过滤
    def filter_records(records, whitelist=None):
        return list(records), set()

# 服务器上数据就在脚本旁边的 data/；在 Mac 上跑（回填历史、手动补写）时
# 数据在 hot-rank-collector/data/，用环境变量指过去，不要靠软链——
# 软链会被 deploy_via_server.sh 的 rsync 带进仓库。
DATA_DIR = Path(os.environ.get("HOT_RANK_DATA_DIR") or (ROOT / "data"))

# 文档表头（2026-09-21 起 6 列，爬取时间拆成日期 + 时间两列，便于按日筛选/透视）
#   A 来源平台   B 热点标题   C 热点次序   D 爬取日期   E 爬取时间   F 热度值
# 注意：内部数据模型（jsonl / latest_batch.json）里 crawl_time 仍是
# "YYYY-MM-DD HH:MM:SS" 单字段 —— 去重、新鲜度比较、防回退全依赖它的定长可比性，
# 只在写文档这一层拆开，读回来时再拼回去。
HEADER = ("来源平台", "热点标题", "热点次序", "爬取日期", "爬取时间", "热度值")
LAST_COL = "F"
COL_DATE, COL_TIME = 3, 4          # D / E 的 0-based 下标


# ───────────────────────────── 基础设施 ─────────────────────────────

def load_cred() -> dict:
    """读凭证。返回 dict 含 access_token / client_id / open_id / book_id / sheet_id。"""
    for p in CRED_PATHS:
        if p and p.is_file():
            c = json.loads(p.read_text(encoding="utf-8"))
            # open_id 可以从 access_token 里推出来，省得用户手填
            if not c.get("open_id"):
                c["open_id"] = open_id_from_token(c["access_token"])
            return c
    raise SystemExit(
        "找不到凭证文件。请创建 ~/.hot-rank/tencent_doc.json（chmod 600）：\n"
        '{\n'
        '  "access_token": "eyJ...",\n'
        '  "client_id": "866c...",\n'
        '  "book_id": "DMtZwkVLNwju",\n'
        '  "sheet_id": "BB08J2"\n'
        '}')


def open_id_from_token(token: str) -> str:
    """access_token 是 JWT，payload 里的 sub 就是 Open ID。"""
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))["sub"]


def token_expiry(token: str) -> datetime:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return datetime.fromtimestamp(json.loads(base64.urlsafe_b64decode(payload))["exp"])


def request(cred: dict, method: str, url: str, body=None, retries: int = 3):
    """带重试的 HTTP 调用。腾讯文档偶发 5xx / 限频，重试两次足够。"""
    last = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, method=method)
        req.add_header("Access-Token", cred["access_token"])
        req.add_header("Client-Id", cred["client_id"])
        req.add_header("Open-Id", cred["open_id"])
        req.add_header("User-Agent", "hot-rank/1.0")
        if body is not None:
            req.data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                txt = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            txt = e.read().decode("utf-8", "replace")
        except Exception as e:                                    # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
            if attempt < retries:
                time.sleep(2 * attempt)
                continue
            raise RuntimeError(last) from e
        try:
            return json.loads(txt)
        except json.JSONDecodeError:
            last = txt[:200]
            if attempt < retries:
                time.sleep(2 * attempt)
                continue
            raise RuntimeError(f"响应不是 JSON：{last}")
    raise RuntimeError(last or "unknown")


def check_ret(resp: dict, what: str):
    """v2 接口用 ret/msg，v3 接口用 code/message，两种都要认。"""
    if resp.get("ret") not in (0, None):
        raise RuntimeError(f"{what} 失败：ret={resp['ret']} {resp.get('msg')}")
    if resp.get("code"):
        raise RuntimeError(f"{what} 失败：code={resp['code']} {resp.get('message')}")


# ───────────────────────────── 文档读写 ─────────────────────────────

def sheet_info(cred: dict) -> dict:
    """查询子表，返回 {sheetID, rowCount, columnCount, title}。"""
    book = urllib.parse.quote(BOOK_PREFIX + cred["book_id"], safe="$")
    r = request(cred, "GET", f"{API}/openapi/sheetbook/v2/{book}/sheets-info")
    check_ret(r, "查询子表")
    sheets = (r.get("data") or {}).get("sheetData") or []
    want = cred.get("sheet_id")
    for s in sheets:
        if not want or s.get("sheetID") == want:
            return s
    raise RuntimeError(f"文档里没有 sheet_id={want}，实际有：{[s.get('sheetID') for s in sheets]}")


def read_range(cred: dict, a1: str) -> list:
    """读区域，返回二维文本数组。注意这个接口用短 fileID + sheetId query。"""
    url = (f"{API}/openapi/spreadsheet/v3/files/{cred['book_id']}"
           f"/values/{urllib.parse.quote(a1, safe='')}?sheetId={cred['sheet_id']}")
    r = request(cred, "GET", url)
    check_ret(r, f"读区域 {a1}")
    grid = r.get("gridData") or {}
    out = []
    for row in grid.get("rows", []):
        out.append([(c.get("cellValue") or {}).get("text") for c in row.get("values", [])])
    return out


def row_stamp(row: list) -> str:
    """把一行的「爬取日期 + 爬取时间」两列拼回内部用的完整时间戳。

    兼容旧的 5 列结构（D 列是完整的 "YYYY-MM-DD HH:MM:SS"）：
    如果 D 列本身就带空格和时分秒，直接返回它，不去拼 E 列
    —— 否则迁移期间读旧数据会拼出 "2026-09-21 16:00:00 7"。
    """
    if len(row) <= COL_DATE or not row[COL_DATE]:
        return ""
    d = str(row[COL_DATE]).strip()
    if " " in d:                                   # 旧结构：D 列已是完整时间戳
        return d
    t = str(row[COL_TIME]).strip() if len(row) > COL_TIME and row[COL_TIME] else ""
    return f"{d} {t}".strip()


def tail_state(cred: dict, row_count: int) -> tuple:
    """返回 (末尾真实数据行号, 该行批次时间)；空表返回 (0, "")。"""
    row = find_last_row(cred, row_count)
    if not row:
        return 0, ""
    for r in reversed(read_range(cred, f"A{row}:{LAST_COL}{row}")):
        st = row_stamp(r)
        if st:
            return row, st
    return row, ""


def last_filled_row(cred: dict, row_count: int) -> str:
    """文档末尾那一批的爬取时间（判重用）。

    ⚠️ 不能「从 rowCount 往回读 3 行」：rowCount 统计的是「有格式的行」，
    重建清尾之后末尾会挂一长串空行（实测：数据到 1942 行，rowCount 仍是 4210），
    那 3 行读出来全是 None，本函数就返回 ""。

    而**默认模式正是用它的返回值判重**（`records[0].crawl_time <= t_doc` 就跳过），
    `run_hourly.sh` 每小时无参数调用一次 —— 空值让判重整个失效，
    等于每小时把最新批次重复写 51 行，且不报任何错（2026-09-21 发现）。

    所以先 find_last_row() 定位真正有内容的那一行，再取它的时间戳。
    """
    return tail_state(cred, row_count)[1]


def scan_existing_stamps(cred: dict, row_count: int, chunk: int = 200) -> set:
    """全量扫描文档，返回已存在的 crawl_time 集合。

    为什么需要它：**文档里的行不保证按时间升序**。
    实测这份个人版文档第 2 行是 22:25:03、第 694 行是 12:56:58（迁移前的旧系统
    写入顺序如此）。这种情况下只比对「末尾批次时间」会误判，把已有批次重复写一遍。
    所以回填历史数据时必须全量扫描去重。

    代价是每 200 行一次 API 调用，文档几千行就要十几次 —— 因此**只用于回填**，
    常规每小时追加仍然只读末尾 3 行（那时文档已经是严格追加有序的）。
    """
    stamps = set()
    for start in range(1, row_count + 1, chunk):
        end = min(start + chunk - 1, row_count)
        for row in read_range(cred, f"A{start}:{LAST_COL}{end}"):
            st = row_stamp(row)
            if st:
                stamps.add(st)
    for h in ("爬取时间", "爬取日期", "爬取日期 爬取时间"):
        stamps.discard(h)               # 表头那一行
    return stamps


def split_stamp(stamp: str) -> tuple:
    """"2026-09-21 16:00:00" -> ("2026-09-21", "16:00:00")。

    内部一直用完整时间戳，只有落到表格才拆两列。拆不开时（异常数据）
    整串放日期列、时间列留空，宁可难看也不要丢数据。
    """
    s = str(stamp or "").strip()
    if " " in s:
        d, t = s.split(" ", 1)
        return d.strip(), t.strip()
    return s, ""


def to_row(r: dict) -> list:
    """一条记录 -> 表格一行（6 列，顺序与 HEADER 一致）。"""
    d, t = split_stamp(r.get("crawl_time"))
    return [
        clean(str(r.get("platform") or "")),
        clean(str(r.get("title") or "")),
        "" if r.get("rank") in (None, "") else str(r["rank"]),
        d,
        t,
        "" if r.get("hot_value") in (None, "") else str(r["hot_value"]),
    ]


def put_values(cred: dict, values: list, start_row: int) -> None:
    """把二维数组写到 start_row 起的区域（1-based）。"""
    end_row = start_row + len(values) - 1
    a1 = f"{cred['sheet_id']}!A{start_row}:{LAST_COL}{end_row}"
    book = urllib.parse.quote(BOOK_PREFIX + cred["book_id"], safe="$")
    url = f"{API}/openapi/sheetbook/v2/{book}/values/{urllib.parse.quote(a1, safe='!$:')}"
    r = request(cred, "PUT", url, {"values": values})
    check_ret(r, f"写入 {a1}")


def append_batch(cred: dict, records: list, start_row: int, dry_run: bool = False) -> int:
    """把一批记录追加到 start_row 起的位置（1-based 行号）。返回写入行数。"""
    values = [to_row(r) for r in records]
    if dry_run:
        print(f"  [dry-run] 将写 {len(values)} 行 -> "
              f"{cred['sheet_id']}!A{start_row}:{LAST_COL}{start_row + len(values) - 1}")
        return len(values)
    put_values(cred, values, start_row)
    return len(values)


def clean(s: str) -> str:
    """剥离星外字符（emoji 等 4 字节 UTF-8）。

    走 WorkBuddy 连接器时它们会让参数校验整个失败；官方 API 未必有同样问题，
    但表格里出现 emoji 对后续处理没好处，统一剥掉保持数据干净。
    """
    return "".join(ch for ch in s if ord(ch) <= 0xFFFF).strip()


# ───────────────────────────── 数据来源 ─────────────────────────────

def load_records(args) -> list:
    """按参数取要写的记录，已按 crawl_time 升序。"""
    if args.from_jsonl:
        files = [Path(args.from_jsonl)]
    elif args.after:
        files = sorted(DATA_DIR.glob("hot_*.jsonl"))
    else:
        lb = DATA_DIR / "latest_batch.json"
        if not lb.is_file():
            raise SystemExit(f"找不到 {lb}")
        return json.loads(lb.read_text(encoding="utf-8"))["records"]

    recs = []
    for f in files:
        if not f.is_file():
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                recs.append(json.loads(line))
    recs.sort(key=lambda r: (r["crawl_time"], r.get("platform", ""), r.get("rank") or 0))
    return recs


def apply_whitelist(recs: list) -> tuple:
    """按平台白名单过滤，返回 (保留的记录, 被丢弃的平台名列表)。

    解析层已经过滤过，这里是写出层兜底 —— 2026-09-21 17:50 之前采的 jsonl
    含 33 个平台，而 --rebuild / --after 都会读到那些历史记录。
    不在这儿拦，重建文档时不要的平台会被again写回去。
    """
    kept, dropped = filter_records(recs)
    if not kept and recs:
        # 全滤掉说明白名单配错了（平台名写错之类），宁可不过滤也不要写出空文档
        return recs, []
    return kept, sorted(dropped)


def rebuild(cred: dict, result: dict, args) -> int:
    """用本地 jsonl 全量重写整份文档：第 1 行表头，之后按 crawl_time 升序。

    用途：改表头结构（比如 2026-09-21 把爬取时间拆成日期+时间两列）时，
    既有几千行数据的列位全变了，逐行改不现实，整表重写最干净。

    安全性：数据源是本地 jsonl 完整档案（服务器和 GitHub 各有一份），
    重写只是把同一份数据按新列序再落一遍，不存在信息丢失。
    写入按 500 行一批（6 列 = 3000 单元格，远低于 10000 上限）。
    """
    records = []
    for f in sorted(DATA_DIR.glob("hot_*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    if not records:
        result.update(status="failed", error=f"{DATA_DIR} 下没有 jsonl 数据，拒绝重写")
        print(json.dumps(result, ensure_ascii=False))
        return 1

    # 白名单过滤。重建是白名单最重要的落地时机：历史 jsonl 里有 33 个平台，
    # 不过滤就会把用户明确不要的 16 个平台again写回文档。
    records, dropped = apply_whitelist(records)
    if dropped:
        result["filtered_out"] = dropped

    records.sort(key=lambda r: (r["crawl_time"], r.get("platform", ""), r.get("rank") or 0))
    values = [list(HEADER)] + [to_row(r) for r in records]
    batches = [(i + 1, values[i:i + 500]) for i in range(0, len(values), 500)]

    stamps = sorted({r["crawl_time"] for r in records})
    result.update(rebuild_rows=len(values), rebuild_batches=len(batches),
                  first_stamp=stamps[0], last_stamp=stamps[-1],
                  distinct_batches=len(stamps))

    if args.dry_run:
        result.update(status="dry_run")
        for start, chunk in batches:
            print(f"  [dry-run] 行 {start}~{start + len(chunk) - 1}（{len(chunk)} 行）")
        print(json.dumps(result, ensure_ascii=False))
        return 0

    for start, chunk in batches:
        try:
            put_values(cred, chunk, start)
        except RuntimeError as e:
            result.update(status="failed", failed_at_row=start, error=str(e))
            print(json.dumps(result, ensure_ascii=False))
            return 1

    # 【清尾】新数据比原表短时，必须把多出来的旧行清空。
    #
    # 为什么是必须：PUT values 只覆盖它写到的区域，不会截断表格。
    # 2026-09-21 启用平台白名单后重建，4159 行缩到 1891 行，1892~4159 留着
    # 旧的 33 平台数据。这不只是难看 —— 常规追加靠「读第 0 列找最后非空行」
    # 定起始行，会算到 4160，于是新批次被接在残留后面，文档变成
    # 「新数据 + 旧残留 + 新数据」，而且 last_filled_row 读末尾 3 行拿到的
    # 是残留的时间戳，去重判断也会跟着错。
    #
    # 上一次重建（拆列）恰好行数相等，所以这个坑没暴露。
    # 原表行数：main() 体检时已放进 result["doc_rows"]，这里不重复调接口
    row_count = int(result.get("doc_rows") or 0)
    if row_count > len(values):
        blank = [""] * len(HEADER)
        cleared = 0
        # 每批 800 行 × 6 列 = 4800 格，低于单次 10000 格上限
        for start in range(len(values) + 1, row_count + 1, 800):
            end = min(start + 799, row_count)
            try:
                put_values(cred, [blank] * (end - start + 1), start)
                cleared += end - start + 1
            except RuntimeError as e:
                result.update(status="partial",
                              error=f"清理旧行 {start}~{end} 失败: {e}",
                              cleared_rows=cleared)
                print(json.dumps(result, ensure_ascii=False))
                return 1
        result["cleared_stale_rows"] = cleared
        result["cleared_range"] = f"{len(values) + 1}~{row_count}"

    # 校验：表头 + 末尾行
    head = read_range(cred, f"A1:{LAST_COL}1")
    tail = read_range(cred, f"A{len(values) - 1}:{LAST_COL}{len(values)}")
    result.update(status="ok",
                  header_ok=(head and head[0][:len(HEADER)] == list(HEADER)),
                  tail_stamp=row_stamp(tail[-1]) if tail else None,
                  tail_ok=(row_stamp(tail[-1]) == stamps[-1]) if tail else None)

    # 清尾后必须确认紧随其后的那一行真的空了 —— 否则常规追加仍会定位错。
    if result.get("cleared_stale_rows"):
        nxt = read_range(cred, f"A{len(values) + 1}:{LAST_COL}{len(values) + 1}")
        first_cell = (nxt[0][0] if nxt and nxt[0] else None)
        result["stale_cleared_ok"] = not first_cell

    print(json.dumps(result, ensure_ascii=False))
    return 0


def find_last_row(cred: dict, row_count: int, chunk: int = 200) -> int:
    """返回最后一个**真正有内容**的行号（1-based）；空表返回 0。

    为什么不能用 rowCount + 1 当追加位置：rowCount 统计的是「有格式的行」，
    不等于「有数据的行」。2026-09-21 重建时把 1892~4159 写成空值清尾，
    rowCount 仍是 4159，于是 17:50 那批被追加到 4160，文档中间空了 2268 行。

    往回分块扫第 0 列，遇到第一个非空就停 —— 正常情况第一块（末尾 200 行）
    就能命中，只有刚清过尾的表才会多扫几次。
    """
    row = row_count
    while row > 0:
        start = max(1, row - chunk + 1)
        rows = read_range(cred, f"A{start}:A{row}")
        for offset in range(len(rows) - 1, -1, -1):
            if rows[offset] and rows[offset][0]:
                return start + offset
        row = start - 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--info", action="store_true", help="只体检，不写入")
    ap.add_argument("--after", metavar="T", help="只写晚于该时间戳的批次")
    ap.add_argument("--from-jsonl", metavar="FILE", help="从指定 jsonl 取数据")
    ap.add_argument("--dry-run", action="store_true", help="只计算不实际写入")
    ap.add_argument("--max-batches", type=int, default=0, help="最多写几个批次（0=不限）")
    ap.add_argument("--dedup-scan", action="store_true",
                    help="全量扫描文档已有批次来去重（回填历史用；文档行序混乱时必须加）")
    ap.add_argument("--rebuild", action="store_true",
                    help="用本地 jsonl 全量重写整份文档（表头+全部数据，按时间升序）。"
                         "改表头结构时用，会覆盖文档现有内容")
    args = ap.parse_args()

    cred = load_cred()
    exp = token_expiry(cred["access_token"])
    days_left = (exp - datetime.now()).days

    info = sheet_info(cred)
    row_count = int(info.get("rowCount") or 0)
    last_row, t_doc = tail_state(cred, row_count) if row_count else (0, "")

    result = {
        "book_id": cred["book_id"],
        "sheet_id": cred["sheet_id"],
        "doc_rows": row_count,
        "doc_last_row": last_row,
        "doc_last_batch": t_doc,
        "token_expires": exp.strftime("%Y-%m-%d %H:%M:%S"),
        "token_days_left": days_left,
    }
    if row_count and last_row and last_row != row_count:
        # 末尾挂着空行（通常是重建清尾留下的），报出来免得下次误判
        result["trailing_blank_rows"] = row_count - last_row
    if days_left <= 7:
        result["token_warning"] = (
            f"access_token 仅剩 {days_left} 天（{exp:%Y-%m-%d}）。"
            "没有 Client Secret 无法自动续期，需去开放平台控制台重新生成后更新凭证文件。")

    if args.info:
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0

    if args.rebuild:
        return rebuild(cred, result, args)

    records = load_records(args)

    # 白名单过滤放在按批次筛选**之前**：先剔掉不要的平台，再判断有没有待写批次。
    # 顺序反了的话，若某批次只剩非白名单平台，会被当成"有数据要写"而写出空批次。
    records, dropped = apply_whitelist(records)
    if dropped:
        result["filtered_out"] = dropped

    if args.dedup_scan:
        existing = scan_existing_stamps(cred, row_count)
        result["doc_existing_batches"] = len(existing)
        records = [r for r in records if r["crawl_time"] not in existing]
    elif args.after:
        records = [r for r in records if r["crawl_time"] > args.after]
    elif args.from_jsonl:
        records = [r for r in records if r["crawl_time"] > t_doc]
    elif t_doc and records and records[0]["crawl_time"] <= t_doc:
        # 默认模式（写 latest_batch）：批次时间不晚于文档末尾就说明已经写过了
        result.update(status="up_to_date", reason=f"文档末尾已是 {t_doc}")
        print(json.dumps(result, ensure_ascii=False))
        return 0

    if not records:
        result.update(status="nothing_to_write")
        print(json.dumps(result, ensure_ascii=False))
        return 0

    # 按批次分组，逐批追加，行号连续
    groups = {}
    for r in records:
        groups.setdefault(r["crawl_time"], []).append(r)
    stamps = sorted(groups)
    if args.max_batches:
        stamps = stamps[: args.max_batches]

    # 追加位置 = 最后一个有内容的行 + 1。
    # 不用 row_count + 1：rowCount 是「有格式的行」，清尾留下的空行也算在内。
    last_row = find_last_row(cred, row_count)
    next_row = max(1, last_row + 1)
    result["append_from_row"] = next_row
    if last_row and last_row != row_count:
        # 表里有空行（通常是重建清尾留下的），记一笔便于排查
        result["trailing_blank_rows"] = row_count - last_row
    written = []
    for st in stamps:
        try:
            n = append_batch(cred, groups[st], next_row, args.dry_run)
        except RuntimeError as e:
            result.update(status="failed", failed_at=st, error=str(e),
                          written_batches=written)
            print(json.dumps(result, ensure_ascii=False))
            return 1
        written.append({"crawl_time": st, "rows": n,
                        "start_row": next_row, "end_row": next_row + n - 1})
        next_row += n

    # 抽验：回读最后 2 行，确认 crawl_time 是本次最后一批
    verified = None
    if not args.dry_run and written:
        try:
            tail = read_range(cred, f"A{next_row - 2}:E{next_row - 1}")
            verified = all(len(r) > 3 and r[3] == stamps[-1] for r in tail if any(r))
        except RuntimeError:
            verified = None

    result.update(status="ok", written_batches=written,
                  total_rows=sum(w["rows"] for w in written), verified=verified)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
