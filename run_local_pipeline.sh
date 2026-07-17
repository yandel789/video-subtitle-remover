#!/bin/bash
# 本地全流程：在 Mac 上模拟 FC 环境，build 1:1 镜像 → run → 端到端测试
# 全部在本地解决后，才 push 到 ACR → FC 部署

set -e
cd "$(dirname "$0")"

# 配置
IMAGE_LOCAL="vsr-test:local"  # 本地 docker 镜像 tag
TASK_ID="local-e2e-$(date +%s)"
OSS_VIDEO="https://vsr-service.oss-cn-hangzhou.aliyuncs.com/test/test-video.mp4"

# ========== Step 1: Build 镜像 ==========
echo "============================================================"
echo "[Step 1/4] Build 镜像（1.5h 首次 / 1-2min 增量）"
echo "============================================================"
docker build \
  --platform=linux/amd64 \
  --provenance=false \
  -f Dockerfile.fc \
  -t "$IMAGE_LOCAL" \
  . 2>&1 | tail -8

if ! docker image inspect "$IMAGE_LOCAL" > /dev/null 2>&1; then
  echo "[FAIL] 镜像 build 失败" >&2
  exit 1
fi
echo "✓ 镜像 ready: $IMAGE_LOCAL"
echo ""

# ========== Step 2: Run 容器（后台） ==========
echo "============================================================"
echo "[Step 2/4] Run 容器（监听 :8000）"
echo "============================================================"

# 先停可能残留的容器
docker rm -f vsr-test-local 2>/dev/null || true

docker run -d \
  --platform=linux/amd64 \
  --name vsr-test-local \
  -p 8000:8000 \
  -e VSR_PROJECT_DIR=/app \
  -e VSR_PORT=8000 \
  -e VSR_HOST=0.0.0.0 \
  -e OSS_INTERNAL_ENDPOINT=https://oss-cn-hangzhou.aliyuncs.com \
  -e OSS_REGION=oss-cn-hangzhou \
  -e OSS_BUCKET=vsr-service \
  -e OSS_RESULT_KEY_PREFIX=ai-output/ \
  -e ACCESSKEY_ID=xxxx \
  -e ACCESSKEY_SECRET=xxxxx \
  -e VSR_DEFAULT_INPAINT_MODE=sttn-det \
  -e VSR_DEFAULT_SUBTITLE_DETECT_MODE=PP_OCRv5_SERVER \
  "$IMAGE_LOCAL"

# 自动清理（脚本退出时停容器）
trap 'echo ""; echo "停容器..."; docker stop vsr-test-local 2>/dev/null; docker rm vsr-test-local 2>/dev/null' EXIT

echo "等待 uvicorn 起来（最长 3 分钟，CPU 下模型加载 + paddle init 比较慢）"

# ========== Step 3: 等 health 200 ==========
echo ""
echo "============================================================"
echo "[Step 3/4] 等 /vsr/health 返回 200"
echo "============================================================"
for i in $(seq 1 36); do
  sleep 5
  H=$(curl -s --max-time 3 http://localhost:8000/vsr/health 2>/dev/null || echo "fail")
  echo "[$i*5s] $H" | head -c 200
  echo
  if echo "$H" | grep -q '"status":"ok"'; then
    echo "✓ 健康检查通过"
    break
  fi
  if [ $i -eq 36 ]; then
    echo "[FAIL] 3 分钟还没起来，看容器日志："
    docker logs vsr-test-local 2>&1 | tail -50
    exit 1
  fi
done

# ========== Step 4: 端到端测试 ==========
echo ""
echo "============================================================"
echo "[Step 4/4] 提交去字幕任务 + 轮询"
echo "============================================================"
RESPONSE=$(curl -s -X POST http://localhost:8000/vsr/remove \
  -H "Content-Type: application/json" \
  -d "{
    \"task_id\": \"$TASK_ID\",
    \"video_url\": \"$OSS_VIDEO\",
    \"ymin\": 1236, \"ymax\": 1390, \"xmin\": 21, \"xmax\": 1053,
    \"inpaint_mode\": \"sttn-det\"
  }")
echo "提交响应: $RESPONSE"

echo ""
echo "开始轮询进度（最多 30 分钟，CPU 处理 3MB 视频预计 5-15 分钟）"
for i in $(seq 1 180); do
  sleep 10
  R=$(curl -s --max-time 10 "http://localhost:8000/vsr/progress/$TASK_ID")
  STATUS=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null || echo "?")
  PROG=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('progress',''))" 2>/dev/null || echo "?")
  echo "[$i $(date +%H:%M:%S)] status=$STATUS progress=$PROG"
  if [ "$STATUS" = "finished" ]; then
    OUTPUT_URL=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('output_url',''))" 2>/dev/null)
    ERROR=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('error',''))" 2>/dev/null)
    echo ""
    echo "============================================================"
    echo "✅ 端到端测试通过！"
    echo "   output_url: $OUTPUT_URL"
    echo "============================================================"
    if [ -n "$OUTPUT_URL" ]; then
      OUTPUT_FILE="/tmp/output-$(date +%s).mp4"
      echo "下载到 $OUTPUT_FILE"
      curl -s -o "$OUTPUT_FILE" "$OUTPUT_URL"
      ls -lh "$OUTPUT_FILE"
      echo "本地打开看下，字幕去掉了没"
    fi
    exit 0
  fi
  if [ "$STATUS" = "failed" ]; then
    ERROR=$(echo "$R" | python3 -c "import sys,json; print(json.load(sys.stdin).get('error',''))" 2>/dev/null)
    echo ""
    echo "============================================================"
    echo "❌ 端到端测试失败"
    echo "   error: $ERROR"
    echo "============================================================"
    echo ""
    echo "容器最后 50 行日志："
    docker logs vsr-test-local 2>&1 | tail -50
    exit 1
  fi
done

echo "[FAIL] 30 分钟还没跑完，看日志"
docker logs vsr-test-local 2>&1 | tail -50
exit 1
