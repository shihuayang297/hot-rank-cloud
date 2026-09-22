# 云端兜底采集器（GitHub Actions）

> 解决的问题：MacBook Air 睡眠 / 关机 / 被带出门时，热榜数据永久丢失。
> rree.cn **没有历史接口**（已验证：传任意历史时间戳都返回当前榜单），
> 错过的整点补不回来，所以只能靠一个不会关机的地方持续采集。

## 它做什么 / 不做什么

| | 说明 |
|---|---|
| ✅ 做 | 每小时采集 rree.cn，数据提交到 GitHub 仓库，保证**数据零丢失** |
| ❌ 不做 | 写腾讯文档、刷看板。这两件事需要 WorkBuddy 的连接器授权，导不到云端 |

所以完整链路是：**云端保证数据不丢 → 你的 Mac 开机后自动补写文档和看板**。
你不在时文档不会实时更新，但一条数据都不会少。

## 部署步骤（约 10 分钟，只需做一次）

### 1. 建仓库

到 GitHub 新建一个仓库，例如 `hot-rank-cloud`。
**建议设为 Public** —— 热榜是公开信息不涉及隐私，而且本机拉取数据时无需任何 token，
最省事。若坚持 Private，本机拉取要额外配 PAT（见文末）。

### 2. 上传本目录的文件

把 `cloud-collector/` 里的全部内容传到仓库根目录（网页端拖拽上传即可，不需要装 git）：

```
.github/workflows/collect.yml
config/settings.yaml
src/...
requirements.txt
```

注意 `.github` 是隐藏目录，网页拖拽可能看不到。若上传不了，就在仓库网页上
用 "Add file → Create new file"，文件名直接填 `.github/workflows/collect.yml`，
把内容粘进去。

### 3. 允许 Actions 写仓库

仓库 → Settings → Actions → General → 拉到底 "Workflow permissions"
→ 选 **Read and write permissions** → Save。

不改这一步，采集能跑但提交会失败（403）。

### 4. 手动跑一次验证

仓库 → Actions → 左侧 "热点榜单整点采集" → 右侧 "Run workflow"。
跑完后仓库里应该出现 `data/hot_YYYYMMDD.jsonl`，约 99 行。

### 5. 告诉本机去哪拉数据

编辑 `~/hot-rank/sync_cloud.py`，把 `REPO` 改成你的 `用户名/仓库名`；
或者在 `~/.zshrc` 里加一行：

```bash
export HOT_RANK_CLOUD_REPO="你的用户名/hot-rank-cloud"
```

之后本机每小时的同步任务会自动拉取并合并，无需手动操作。
也可以随时手动跑：

```bash
~/hot-rank/hot-rank-collector/venv/bin/python ~/hot-rank/sync_cloud.py --dry-run
```

## 关于 Actions 的调度精度

GitHub 的 cron 是「不早于」语义，排队高峰期会延迟几分钟到几十分钟，偶尔跳过。
所以 workflow 里**每小时排了两次**（0 分和 20 分）：

- 第一次通常能赶在整点附近
- 若被延迟，第二次兜底
- 采集脚本会检查该整点是否已有数据，**重复触发直接跳过**，不会写重复批次

云端的时间戳一律**向下取整到所属整点**（`align_tolerance_minutes: 60`）——
14:26 才跑起来的那轮，语义上仍是「14 点的榜单」，记作 `14:00:00`。
本机守护进程用的是 5 分钟容差（它能精确整点触发），两者配置不同，不要互相复制。

## 数据冲突怎么处理

同一整点本机和云端都采到时，**本机数据优先**（本机是精确整点采的，云端是排队后补的）。
合并逻辑在 `sync_cloud.py`，由 `tests/test_sync_cloud.py` 的 6 个用例锁住。

## 私有仓库的额外配置

如果仓库设为 Private，本机拉取需要带 token：

1. GitHub → Settings → Developer settings → Personal access tokens → Fine-grained
2. 只勾选该仓库的 `Contents: Read-only`
3. 在 `~/.zshrc` 加 `export HOT_RANK_CLOUD_TOKEN="github_pat_xxx"`
4. `sync_cloud.py` 的 `fetch_cloud()` 里给 request 加上
   `Authorization: Bearer <token>` 头（当前版本未实现，需要时告诉我，我来加）

## 成本

GitHub Actions 对公开仓库完全免费。私有仓库每月有 2000 分钟免费额度，
本任务单次约 1~3 分钟、每天最多 48 次，约 2000~4000 分钟/月，
**私有仓库会超额**，这也是推荐用公开仓库的原因之一。
