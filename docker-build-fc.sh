#!/bin/bash
# =============================================================================
# vsr-service FC 3.0 镜像构建 + 推送脚本
# =============================================================================
# 用法：
#   ./docker-build-fc.sh                # 默认构建 + 推送 :1.4-fc
#   ./docker-build-fc.sh 1.5-fc         # 自定义 tag
#   ./docker-build-fc.sh 1.4-fc --no-push  # 只构建不推送
#
# 关键参数：
#   --platform=linux/amd64     强制 x86_64（Mac M 系列必须）
#   --provenance=false         FC 3.0 不识别 SBOM 元数据
#   --load                     buildx 构建后 load 到本地 docker（便于看 :1.4-fc 镜像）
# =============================================================================

set -e

# ===== 参数解析 =====
TAG="${1:-1.4-fc}"
SHOULD_PUSH=true
if [[ "$2" == "--no-push" ]]; then
    SHOULD_PUSH=false
fi

# ===== 关键资源（与 VSR_ECI_CURRENT_STATUS.md 一致） =====
ACR_REGISTRY="crpi-b0qgr6ixjg263lsl.cn-hangzhou.personal.cr.aliyuncs.com"
ACR_NAMESPACE="vsr-deploy"
ACR_REPO="vsr-service"
ACR_USER="18552425674"
ACR_PASS="jhyypy124815"

FULL_TAG="${ACR_REGISTRY}/${ACR_NAMESPACE}/${ACR_REPO}:${TAG}"
LOCAL_TAG="vsr-service:${TAG}"

cd "$(dirname "$0")"

echo "==================================================================="
echo " vsr-service FC 3.0 镜像构建"
echo "==================================================================="
echo "TAG:              ${TAG}"
echo "FULL_TAG:         ${FULL_TAG}"
echo "PLATFORM:         linux/amd64"
echo "PROVENANCE:       false"
echo "DOCKERFILE:       Dockerfile.fc"
echo "PUSH:             ${SHOULD_PUSH}"
echo "==================================================================="

# ===== 1. 构建 =====
echo ""
echo "[1/3] 构建镜像（buildx, linux/amd64, 无 provenance）..."
docker buildx build \
    --platform=linux/amd64 \
    --provenance=false \
    --tag "${LOCAL_TAG}" \
    --tag "${FULL_TAG}" \
    --load \
    --file Dockerfile.fc \
    .

# ===== 2. 检查镜像大小 =====
echo ""
echo "[2/3] 检查镜像大小..."
SIZE=$(docker image inspect "${LOCAL_TAG}" --format '{{.Size}}' 2>/dev/null || echo "0")
SIZE_MB=$((SIZE / 1024 / 1024))
echo "本地镜像大小: ${SIZE_MB}MB"
if [[ ${SIZE_MB} -gt 2048 ]]; then
    echo "⚠️  警告: 镜像 > 2GB，FC 3.0 可能拒绝（建议优化或换 CPU 版 paddlepaddle）"
fi

# ===== 3. 推送 =====
if [[ "${SHOULD_PUSH}" == "true" ]]; then
    echo ""
    echo "[3/3] 登录 ACR + 推送..."
    docker login "${ACR_REGISTRY}" -u "${ACR_USER}" -p "${ACR_PASS}"
    docker push "${FULL_TAG}"

    echo ""
    echo "==================================================================="
    echo " ✅ 完成: ${FULL_TAG}"
    echo "==================================================================="
    echo "下一步：在阿里云 FC 3.0 控制台创建自定义容器函数"
    echo "  - 镜像: ${FULL_TAG}"
    echo "  - 端口: 8000"
    echo "  - 健康检查: GET /vsr/health"
    echo "  - 启动命令: /opt/venv/bin/python /app/vsr-service/wrapper.py"
    echo "  - 环境变量: OSS_INTERNAL_ENDPOINT / OSS_BUCKET / ACCESSKEY_ID / ACCESSKEY_SECRET"
else
    echo ""
    echo "==================================================================="
    echo " ✅ 仅构建完成（--no-push）: ${LOCAL_TAG}"
    echo "==================================================================="
fi