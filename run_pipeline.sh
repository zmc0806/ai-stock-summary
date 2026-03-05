#!/bin/bash
# ============================================================
# A股开盘前情报 + 小红书自动发布 — 一键流水线
#
# 用法：
#   ./run_pipeline.sh                    # 分析 + 自动发布
#   ./run_pipeline.sh --dry-run          # 分析 + 预览文案（不发布）
#   ./run_pipeline.sh --no-publish       # 只分析，不发布
#   ./run_pipeline.sh --auto-cover       # 自动生成封面配图
#   ./run_pipeline.sh --images /path/img.png  # 指定配图
#
# 环境变量（发布时必须设置）：
#   export XHS_PHONE=13800138000
#   export XHS_COOKIES=/Users/yourname/xhs_cookies
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="python3"
REPORT_SCRIPT="$SCRIPT_DIR/intelligence_report_ashare.py"
PUBLISH_SCRIPT="$SCRIPT_DIR/publish_to_xhs.py"
OUTPUT_DIR="$SCRIPT_DIR/reports"
MODEL="${OLLAMA_MODEL:-qwen3:14b}"
LOG_DIR="$SCRIPT_DIR/logs"

# ── 参数解析 ─────────────────────────────────────────────────
DRY_RUN=false
NO_PUBLISH=false
AUTO_COVER=false
EXTRA_IMAGES=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)     DRY_RUN=true;     shift ;;
        --no-publish)  NO_PUBLISH=true;  shift ;;
        --auto-cover)  AUTO_COVER=true;  shift ;;
        --images)      EXTRA_IMAGES="$2"; shift 2 ;;
        --model)       MODEL="$2";       shift 2 ;;
        *) echo "[WARN] 未知参数：$1"; shift ;;
    esac
done

mkdir -p "$OUTPUT_DIR" "$LOG_DIR"

BJ_DATE=$(TZ='Asia/Shanghai' date '+%Y-%m-%d %H:%M CST')
echo ""
echo "════════════════════════════════════════════════════════════════"
echo "  A股开盘前情报系统 + 小红书自动发布流水线"
echo "  北京时间：$BJ_DATE"
echo "  模型：$MODEL"
echo "════════════════════════════════════════════════════════════════"
echo ""

# ── Step 1: 检查 Ollama ──────────────────────────────────────
echo "[1/3] 检查 Ollama 服务..."
if ! curl -s "http://localhost:11434/api/tags" > /dev/null 2>&1; then
    echo "  Ollama 未运行，尝试启动..."
    ollama serve &
    sleep 6
    if ! curl -s "http://localhost:11434/api/tags" > /dev/null 2>&1; then
        echo "  [ERROR] Ollama 启动失败，请手动运行：ollama serve"
        exit 1
    fi
fi
echo "  ✅ Ollama 运行中"

# ── Step 2: 生成情报报告 + XHS Payload ──────────────────────
echo "[2/3] 运行情报分析..."
$PYTHON "$REPORT_SCRIPT" \
    --output-dir "$OUTPUT_DIR" \
    --model "$MODEL"

EXIT_CODE=$?
if [ $EXIT_CODE -ne 0 ]; then
    echo "[ERROR] 情报分析失败（退出码：$EXIT_CODE）"
    exit $EXIT_CODE
fi

PAYLOAD_FILE="$OUTPUT_DIR/xhs_payload_latest.json"
if [ ! -f "$PAYLOAD_FILE" ]; then
    echo "[ERROR] xhs_payload_latest.json 未生成，检查报告脚本"
    exit 1
fi
echo "  ✅ 情报报告生成完成"
echo "  ✅ XHS Payload → $PAYLOAD_FILE"

# ── Step 3: 发布到小红书 ─────────────────────────────────────
if $NO_PUBLISH; then
    echo "[3/3] 跳过发布（--no-publish）"
    echo ""
    echo "  如需发布，运行："
    echo "  python publish_to_xhs.py --payload $PAYLOAD_FILE"
    exit 0
fi

echo "[3/3] 发布到小红书..."

PUBLISH_ARGS="--payload $PAYLOAD_FILE"
$DRY_RUN  && PUBLISH_ARGS="$PUBLISH_ARGS --dry-run"
$AUTO_COVER && PUBLISH_ARGS="$PUBLISH_ARGS --auto-cover"
[ -n "$EXTRA_IMAGES" ] && PUBLISH_ARGS="$PUBLISH_ARGS --images $EXTRA_IMAGES"

$PYTHON "$PUBLISH_SCRIPT" $PUBLISH_ARGS

PUBLISH_EXIT=$?
if [ $PUBLISH_EXIT -eq 0 ]; then
    echo ""
    echo "════════════════════════════════════════════════════════════════"
    echo "  ✅ 全流程完成！"
    echo "  报告目录：$OUTPUT_DIR"
    echo "  完成时间：$(TZ='Asia/Shanghai' date '+%Y-%m-%d %H:%M CST')"
    echo "════════════════════════════════════════════════════════════════"
else
    echo "  [WARN] 发布步骤失败，但报告已生成"
    echo "  手动发布：python publish_to_xhs.py --payload $PAYLOAD_FILE"
fi

exit $PUBLISH_EXIT
