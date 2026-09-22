#!/bin/bash
# 服务器端采集入口 —— 由 crontab 在每小时 50 分调用。
#
# 为什么有这个脚本：
#   原本采集跑在用户的 MacBook Air 上，靠 daemon.py + caffeinate 硬撑着不让机器睡眠。
#   合盖、关机、带出门都会断链，而数据源 rree.cn 没有历史接口，错过的整点永久丢失。
#   GitHub Actions 的 cron 实测一整天零触发（免费版 best-effort，不保证执行），也靠不住。
#   这台开发机已连续运行 12 周以上、有真正可用的 crontab，是最合适的采集宿主。
#
# 数据流（2026-09-21 起服务器全自持，不再依赖 Mac 开机）：
#   服务器采集 -> 写 data/hot_YYYYMMDD.jsonl + latest_batch.json
#              -> 【写腾讯文档】走官方开放平台 OAuth API，7×24 直写
#              -> 生成 docs/data.json（公网看板，GitHub Pages 自动发布）
#              -> git push 回仓库
#   Mac 侧仍会每小时拉一次（sync_cloud.py）并部署内网看板，但那只是锦上添花，
#   文档和公网看板的新鲜度已经完全不需要 Mac 参与。
#
# 采集时刻：每小时 **50 分**（2026-09-21 从整点改过来）。
#       这样整点前数据就已进文档和看板，企微 bot 在 xx:00 推送时是最新的。
#
# 幂等：批次时间戳由 HOT_RANK_BATCH_TIME 显式钉死为「本小时的 50 分」，
#       src/main.py 的 already_collected() 会跳过已采过的批次，
#       所以手动重跑、cron 重复触发都不会产生重复数据。
set -u

# ⚠️ cron 的环境和登录 shell 完全不同：默认 PATH 只有 /usr/bin:/bin，
#    而这台机器的 git 装在 /usr/local/bin/git —— 不显式补 PATH，
#    定时任务会以「git: command not found」静默失败（2026-09-21 实测踩到）。
#    HOME 也一并兜底：git 要靠它找 /root/.gitconfig 里的身份和凭证 helper。
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export HOME="${HOME:-/root}"

# ========== 采集槽位 ==========
# 2026-09-22 起采集时刻改回**整点**（用户要求「每个整点推送一次」）：
# 采完立刻推，群里拿到的就是刚出炉的整点榜单，而不是 10 分钟前那批。
# 改这里必须同步改 crontab（0 * * * *）和 check_gaps.py 的槽位判断，
# 三处对不上会导致缺口误报（tests/test_slot_minute.py 锁着）。
BATCH_MINUTE="00"

# 槽位时间戳**绝不能是未来时刻**：一旦打上未来标签，真正到点的 cron 会被
# already_collected() 幂等跳过 —— 等于用补采时刻的热榜冒充整点且静默不报错。
# 整点档天然安全：当前小时的 00 分必然已经过去（最早也是「此刻」），
# 不像 50 分档那样在 xx:00~xx:49 触发会得到未来值。
# 兜底补采（check_server.py 在任意时刻调 --slot）因此也永远拿到正确的当前档。
current_slot() {
    date "+%Y-%m-%d %H:${BATCH_MINUTE}:00"
}

# 供 check_server.py 等外部脚本取槽位用 —— 单一实现，避免各处算法漂移。
# 必须放在建目录/写日志/加锁**之前**：这只是个纯查询，不该有任何副作用，
# 也不该被「上一轮还在跑」的锁挡住。
if [ "${1:-}" = "--slot" ]; then
    current_slot
    exit 0
fi

ROOT="/data/workspace/hot-rank-monitor"
REPO="$ROOT/hot-rank-cloud"
PY="$ROOT/venv/bin/python"
LOG_DIR="$ROOT/logs"
LOG="$LOG_DIR/hourly.log"
LOCK="$ROOT/.hourly.lock"
TOKEN_FILE="$ROOT/.gh_token"
# 腾讯文档 OAuth 凭证（access_token / client_id / book_id / sheet_id）。
# 由 deploy_server.sh 从本机 ~/.hot-rank/tencent_doc.json 同步过来，chmod 600。
DOC_CRED="${HOT_RANK_DOC_CRED:-$ROOT/.tencent_doc.json}"
export HOT_RANK_DOC_CRED="$DOC_CRED"
REMOTE_HOST="github.com"
REPO_SLUG="shihuayang297/hot-rank-cloud"

mkdir -p "$LOG_DIR"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

# 日志超过 5 MB 就截断保留后半，避免无限增长把磁盘写满
if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG" 2>/dev/null || echo 0)" -gt 5242880 ]; then
    tail -c 2097152 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
    log "日志已截断"
fi

# 防重入：上一轮还没跑完就直接退出，不排队堆积
exec 9>"$LOCK"
if ! flock -n 9; then
    log "上一轮仍在运行，本轮跳过"
    exit 0
fi

# 本批次归属的槽位（计算逻辑见文件顶部的 current_slot）
BATCH="$(current_slot)"
export HOT_RANK_BATCH_TIME="$BATCH"

log "=== 采集任务开始 batch=$BATCH ==="

# 前置依赖自检：缺了就明确报错，而不是走到一半才失败得莫名其妙
for bin in git flock; do
    command -v "$bin" >/dev/null || { log "FATAL 找不到 $bin（PATH=$PATH）"; exit 1; }
done
[ -x "$PY" ] || { log "FATAL venv Python 不存在：$PY"; exit 1; }

cd "$REPO" || { log "FATAL 仓库目录不存在：$REPO"; exit 1; }

# 1. 同步远端（Mac 或 Actions 可能刚推过东西）
if ! git pull --rebase --autostash >> "$LOG" 2>&1; then
    log "WARN git pull 失败，继续用本地状态跑（推送阶段会重试）"
fi

# 2. 采集
if ! "$PY" -m src.main >> "$LOG" 2>&1; then
    log "FAIL 采集失败（详见上方日志与 logs/ 目录），本轮终止"
    exit 1
fi

# 3. 写腾讯文档（官方开放平台 OAuth API，7×24 不依赖 Mac）
#    这一步失败不终止整轮：数据已经落盘、公网看板照常更新，
#    下一轮 tencent_doc.py 会自动把漏掉的批次一并补上（它按文档末尾批次做增量）。
#    常见失败原因：access_token 过期（30 天）——日志里会带 token_days_left 供排查。
if [ -r "$DOC_CRED" ]; then
    if "$PY" tencent_doc.py >> "$LOG" 2>&1; then
        log "腾讯文档写入完成"
    else
        log "WARN 腾讯文档写入失败（不影响采集与公网看板，下轮自动补写）"
    fi
else
    log "WARN 缺少腾讯文档凭证 $DOC_CRED，跳过写文档"
fi

# 4. 生成公网看板数据（内含防回退保护：线上数据不旧于本批次时会跳过）
"$PY" gen_dashboard.py >> "$LOG" 2>&1

# 4.5 把本批次热点**正文**推到企微群（不是推链接，是把 17 个平台的标题铺开）
#     走群机器人 webhook，服务器直发，7×24 不依赖 Mac。
#     没配 webhook 时脚本自己会静默跳过（exit 0），所以这里不需要判断文件是否存在。
#     推送失败也不终止整轮：数据已经落盘，文档和看板照常，下一轮会推新批次。
if "$PY" push_wecom.py >> "$LOG" 2>&1; then
    log "企微群推送完成"
else
    log "WARN 企微群推送失败（不影响采集与看板，详见上方日志）"
fi

# 5. 提交
git add data docs >> "$LOG" 2>&1
if git diff --staged --quiet; then
    log "无新增内容（该整点已采过），跳过提交"
    exit 0
fi
git commit -m "chore: 采集 $BATCH（服务器）" >> "$LOG" 2>&1

# 6. 推送，冲突则 rebase 重试
if [ ! -r "$TOKEN_FILE" ]; then
    log "FAIL 缺少令牌文件 $TOKEN_FILE，数据已提交到本地仓库但未推送"
    exit 1
fi

pushed=0
for i in 1 2 3; do
    if git push origin HEAD:main >> "$LOG" 2>&1; then
        pushed=1
        break
    fi
    log "推送失败（第 $i 次），rebase 后重试"
    git pull --rebase --autostash >> "$LOG" 2>&1
    sleep 5
done

if [ "$pushed" = "1" ]; then
    log "OK 推送成功 batch=$BATCH -> https://$REMOTE_HOST/$REPO_SLUG"
else
    log "FAIL 三次推送均失败，数据留在本地仓库，下轮会一并推上去"
    exit 1
fi
