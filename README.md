# 抖音博主视频批量采集与 AI 提纲解析

自动采集抖音博主的全部公开视频，调用豆包 AI 为每个视频生成内容提纲。支持增量采集、断点续跑、实时存档、人审自动暂停。

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt
playwright install chromium

# 2. 配置博主（编辑 targets.json）

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

## 输出文件

```
output/
  {user_id}_scrape.json       # 采集清单
  {user_id}_checkpoint.json   # 采集与解析的原子边界
  {user_id}_outline.jsonl     # 解析结果（逐条实时写入）
  {user_id}_outline_xxx.json  # 最终汇总
```

## 工作机制

- **增量采集**：`processed.db` 记录已知视频 ID，重复运行只拉新视频
- **断点续跑**：已解析视频自动跳过，中断后重跑从断点继续
- **实时存档**：每解析完一个视频立即写入 `.jsonl`，进程崩溃不丢数据
- **人审自动暂停**：豆包触发内容审核时立即停止，保留已有结果供检查
