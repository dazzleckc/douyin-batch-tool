# 抖音博主视频批量采集与 AI 提纲解析

自动采集抖音博主的全部公开视频，调用豆包 AI 为每个视频生成内容提纲。支持增量采集、断点续跑、实时存档、人审自动暂停。

## 快速开始

```bash
# 1. 创建环境并安装依赖（项目当前使用 Python 3.13）
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
playwright install chromium

# 2. 配置博主
cp targets.json.example targets.json
# 编辑 targets.json

# 3. 运行（首次自动弹窗登录抖音和豆包）
python main.py
```

首次运行自动弹出浏览器引导登录，登录态保存在 `*_state.json`，后续无需重复。

## 使用方式

```bash
python main.py                              # 完整流程，使用 targets.json 第一个博主
python main.py --target "博主名称"           # 按名称指定
python main.py --user https://www.douyin.com/user/xxx  # 直接指定 URL
python main.py --phase scrape               # 仅采集视频列表
python main.py --from-checkpoint output/xxx_checkpoint.json --phase ai  # 仅解析已有清单
python main.py --limit 10                   # 限制数量
python main.py --show-browser               # 显示浏览器（调试用）
```

## 配置

编辑 `targets.json`：

```json
[
  {
    "name": "博主名称",
    "url": "https://www.douyin.com/user/xxx"
  }
]
```

不传参数时默认使用第一条。无需配置 `.env`，所有参数有合理默认值。

## 验证

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q main.py src
.venv/bin/python main.py --help
```

自动化测试覆盖批次增量 JSONL、checkpoint/partial/DB 恢复顺序、采集结果一致性和豆包输入框兼容。它们不能替代真实抖音与豆包页面的小样本验证；登录态、页面结构、验证码和风控仍属于外部运行条件。

## 输出文件

```
output/
  {user_id}_scrape.json       # 采集清单
  {user_id}_checkpoint.json   # 采集与解析的原子边界
  {user_id}_outline.jsonl     # 解析结果事实源（逐条追加）
  {user_id}_increment_{batch_id}.jsonl  # 单个增量批次的成功提纲
  {user_id}_partial_*.json    # 中断时的阶段性存档
  {user_id}_{YYYYMMDD}.json   # 当日最终汇总
```

### 累计 JSONL 与增量 JSONL

- `{user_id}_outline.jsonl` 是累计、append-only 的事实源，包含该博主历次成功落盘的提纲，恢复流程仍以它为准。
- `{user_id}_increment_{batch_id}.jsonl` 是可直接交给下游 Skill 或模型的批次数据集，只包含该批新视频首次成功持久化的提纲。
- `batch_id` 在采集到新视频时生成并写入 checkpoint；同一批次中断后从该 checkpoint 恢复时保持不变。批次全部成功后 checkpoint 会持久化完成标记；每个视频仍保存自己的批次归属，因此历史失败重试仍会写回原批次文件。
- 每条增量记录包含 `aweme_id`、`url`、`title`、`outline`、`status`、`timestamp` 和 `batch_id`。文件内按 `aweme_id` 去重，只有 `status == "success"` 且提纲非空的记录会进入。
- 历史 checkpoint、partial 或累计 JSONL 没有 `batch_id` 时仍可恢复，但这些历史记录不会被误标为新批次增量。

下游使用时可直接读取目标批次的 `*_increment_*.jsonl`，无需再从累计文件按日期或标题猜测边界。AI 阶段结束或安全中断后，CLI 会显示当前未完成 checkpoint 批次已经成功落盘的完整记录数和文件路径；同一批次中断恢复时，即使幂等去重导致本次没有重复写入，也仍会报告已有增量文件。批次全部成功后会标记完成，因此后续无新增运行显示增量数量为 `0`，不会把旧批次伪装成新更新。当前批次没有任何成功提纲，或恢复的只是缺少批次元数据的历史记录时，也显示 `0` 且不会创建空增量文件。失败、`skipped` 和空提纲不会进入增量文件，失败记录重试成功后只追加一次。

## 工作机制

- **增量采集**：`processed.db` 记录已知视频 ID，重复运行只拉新视频
- **原子边界**：采集完成先保存 checkpoint，AI 阶段可用 `--from-checkpoint` 独立重跑
- **断点恢复**：启动 AI 阶段时合并历史 partial，并从 append-only JSONL 恢复已落盘提纲
- **落盘顺序**：成功结果依次持久化到累计 JSONL、所属批次增量 JSONL，再把 DB 状态标记为 `parsed`；任一 JSONL 写入失败都不会提前推进 DB 状态
- **采集一致性保护**：只读取博主作品区视频，并核对页面作品数与 checkpoint/新增候选的差额；遇到疑似推荐区混入、漏采或元数据未渲染时停止且不更新 checkpoint，同时保存完整诊断页面到 `output/debug_scraper_page.html`
- **人审自动暂停**：豆包触发内容审核时先保存已收集结果，再停止处理

## 运行边界

- 工具依赖抖音和豆包网页结构及有效登录态；页面改版、验证码或风控都可能中断自动化。
- `douyin_state.json`、`doubao_state.json` 含登录态，`targets.json` 可能含个人目标信息；这些文件已忽略，不要提交或外发。
- `processed.db`、`output/` 和 `*_state.json` 是本地运行数据。排障或清理前先备份，避免破坏断点恢复链。
