# A股开盘前情报系统 + 小红书自动发布

每天手动运行一次，完成：**全球新闻拉取 → LLM分析 → A股布局简报 → 小红书自动发布**

---

## 文件说明

| 文件 | 作用 |
|------|------|
| `intelligence_report_ashare.py` | 主程序：拉取RSS + 生成情报报告 + 生成XHS文案JSON |
| `publish_to_xhs.py` | 发布程序：读取JSON → 调用xhs_mcp_server → 自动发布小红书 |
| `run_pipeline.sh` | 一键脚本：串联以上两步 |

---

## 安装

```bash
# 1. Python 依赖
pip install requests
pip install Pillow          # 可选：自动生成封面图
pip install xhs-mcp-server  # 小红书发布

# 2. Node 依赖（xhs_mcp_server 需要 chromedriver）
brew install node           # macOS
npx @puppeteer/browsers install chromedriver@stable

# 3. Ollama 本地模型
ollama pull qwen3:14b
```

## 首次登录小红书（只需一次）

```bash
export XHS_PHONE=13800138000
export XHS_COOKIES=/Users/yourname/xhs_cookies

env phone=$XHS_PHONE json_path=$XHS_COOKIES uvx xhs_mcp_server@latest login
```

---

## 每次使用

### 一键流水线（推荐）

```bash
export XHS_PHONE=13800138000
export XHS_COOKIES=/Users/yourname/xhs_cookies

./run_pipeline.sh                              # 分析 + 自动发布
./run_pipeline.sh --auto-cover                 # 自动生成封面图
./run_pipeline.sh --images ~/Desktop/chart.png # 指定配图
./run_pipeline.sh --dry-run                    # 预览，不实际发布
./run_pipeline.sh --no-publish                 # 只分析，不发布
```

### 分步运行

```bash
python intelligence_report_ashare.py           # 生成报告 + xhs_payload_latest.json
python publish_to_xhs.py                       # 发布（读 latest）
python publish_to_xhs.py --auto-cover          # 发布 + 自动封面
python publish_to_xhs.py --dry-run             # 只预览
```

---

## 输出文件

```
reports/
├── ashare_report_20260305_0830.md       ← 完整情报报告
├── xhs_payload_20260305_0830.json       ← 带时间戳的XHS文案
└── xhs_payload_latest.json             ← 最新XHS文案（发布脚本读这个）
```

### xhs_payload.json 格式

```json
{
  "generated_at": "2026-03-05 08:30 CST",
  "title": "今天A股能冲吗？2个外盘信号告诉你 📊",
  "content": "🌍 外盘情况\n昨晚美股...\n\n⚡ 今天重点板块\n...",
  "tags": ["A股", "今日操作", "板块分析", "新能源", "半导体"],
  "image_paths": []
}
```

---

## 情报报告结构

```
A股开盘前布局简报（核心）
├── 今日大盘倾向（偏强/偏弱/震荡）
├── 行业板块轮动
│   ├── 做多机会板块（催化信号 + 代表龙头方向）
│   └── 需规避板块（风险信号 + 代表龙头方向）
├── 北向资金预判
└── 今日核心风险提示

全球宏观快讯
├── 全球宏观情绪
├── 对A股的关键外部信号（含传导路径）
├── 美股/港股收盘参考
└── 大宗商品
```

---

## A股数据源覆盖

| 关注方向 | 数据来源 |
|----------|----------|
| 宏观政策（央行/证监会/发改委） | 新华社EN、中国日报、SCMP China、WTO News |
| 行业板块轮动 | TechCrunch（科技/半导体）、Nikkei Asia、CNA Business |
| 港股联动 | SCMP HK、Nikkei Asia |
| 龙头股方向 | 关键词覆盖：宁德时代/茅台/平安/隆基/赣锋等 |
| 全球传导 | BBC Business、Yahoo Finance、FT、Seeking Alpha |

---

## 配图建议

| 方式 | 操作 |
|------|------|
| 自动封面（深色财经风格） | `--auto-cover`（需要 `pip install Pillow`） |
| 截图（推荐） | 截美股/黄金收盘图，`--images /path/to/screenshot.png` |
| 固定默认配图 | `export XHS_DEFAULT_IMAGE=/path/to/img.png` |

---

## 常见问题

**Q: 发布失败，提示 cookies 无效**
```bash
env phone=$XHS_PHONE json_path=$XHS_COOKIES uvx xhs_mcp_server@latest login
```

**Q: 小红书文案 JSON 解析失败**
正常，模型偶发格式问题。脚本会自动降级为纯文本模式继续发布。

**Q: 换更快的模型**
```bash
OLLAMA_MODEL=qwen3:8b ./run_pipeline.sh
```
