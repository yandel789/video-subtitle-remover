#!/bin/bash
# vsr-service 启动脚本（开发用）
# 用法：./vsr-service/start.sh
#
# 本服务位于 video-subtitle-remover/vsr-service/
# 默认从同级 github 目录下的 videoClean/backend/.env 读取 OSS 凭据。
# 如位置不同，可设置 VIDEOCLEAN_BACKEND_DIR 环境变量。

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VSR_PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "[vsr-service] 启动脚本"
echo "[vsr-service] 位置：$SCRIPT_DIR"
echo "[vsr-service] VSR 项目根：$VSR_PROJECT_DIR"
echo "[vsr-service] 工作目录（默认）：$VSR_PROJECT_DIR/../videoClean/backend/videos/vsr-tmp"
echo ""

# 检查 video-subtitle-remover 项目结构
if [ ! -d "$VSR_PROJECT_DIR/backend" ]; then
  echo "[ERROR] 未找到 video-subtitle-remover/backend: $VSR_PROJECT_DIR/backend"
  echo "        请确认本脚本位于 video-subtitle-remover 仓库内"
  exit 1
fi

# 检查 Python 环境
# PaddlePaddle 3.0.0 wheel 仅支持到 Python 3.12，3.13/3.14 暂无对应版本
# 显式列举 homebrew / 系统路径，避免依赖 PATH 环境
select_python() {
  local candidates=(
    /opt/homebrew/bin/python3.12 /usr/local/bin/python3.12
    /opt/homebrew/bin/python3.11 /usr/local/bin/python3.11
    /opt/homebrew/bin/python3.10 /usr/local/bin/python3.10
    /opt/homebrew/bin/python3.9  /usr/local/bin/python3.9
    python3.12 python3.11 python3.10 python3.9 python3
  )
  for c in "${candidates[@]}"; do
    if [ -x "$c" ] || command -v "$c" &>/dev/null; then
      # 仅接受 3.9 ~ 3.12（用 Python 比较版本最稳）
      local ok=$("$c" -c 'import sys; sys.exit(0 if (3,9) <= sys.version_info[:2] <= (3,12) else 1)' 2>/dev/null && echo y || echo n)
      if [ "$ok" = "y" ]; then
        echo "$c"
        return 0
      fi
    fi
  done
  return 1
}

PYTHON_BIN="$(select_python || true)"
if [ -z "$PYTHON_BIN" ]; then
  echo "[ERROR] 未找到 Python 3.9 ~ 3.12（PaddlePaddle 3.0.0 不支持 3.13/3.14）"
  echo "        请通过 brew install python@3.12 安装"
  exit 1
fi

PY_VERSION=$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "[vsr-service] Python: $PYTHON_BIN (版本 $PY_VERSION)"

# 激活或创建 venv（venv 放在 vsr-service/.venv，与 VSR 项目隔离）
VENV_DIR="$SCRIPT_DIR/.venv"
if [ ! -d "$VENV_DIR" ]; then
  echo "[vsr-service] 创建虚拟环境 $VENV_DIR ..."
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# 安装本服务依赖
echo "[vsr-service] 安装 vsr-service 依赖 ..."
pip install -q --upgrade pip
pip install -q -r "$SCRIPT_DIR/requirements.txt"

# 安装 video-subtitle-remover 依赖（如未安装）
if ! python -c "import paddle" 2>/dev/null; then
  echo "[vsr-service] 首次启动，需要安装 video-subtitle-remover 依赖（PaddlePaddle + PyTorch）..."
  echo "              根据硬件选择版本，详见 $VSR_PROJECT_DIR/README.md"
  read -p "              现在自动安装 CPU 版本吗？(y/N) " -n 1 -r
  echo ""
  if [[ $REPLY =~ ^[Yy]$ ]]; then
    pip install -q paddlepaddle==3.0.0 -i https://www.paddlepaddle.org.cn/packages/stable/cpu/
    pip install -q torch torchvision
    pip install -q -r "$VSR_PROJECT_DIR/requirements.txt"
  else
    echo "[vsr-service] 跳过依赖安装，启动后请自行安装"
  fi
fi

# 检查 VSR 模型（位于 VSR 项目 backend/models/，git clone 已包含）
MODELS_DIR="$VSR_PROJECT_DIR/backend/models"
if [ ! -d "$MODELS_DIR" ] || [ -z "$(ls -A "$MODELS_DIR" 2>/dev/null)" ]; then
  echo "[vsr-service] ⚠️  VSR 模型目录为空：$MODELS_DIR"
  echo "              请确认 video-subtitle-remover 完整 clone，或从以下地址下载模型："
  echo "              https://github.com/YaoFANGUK/video-subtitle-remover/releases"
fi

# 准备工作目录
WORKSPACE="${VSR_WORKSPACE:-$VSR_PROJECT_DIR/../videoClean/backend/videos/vsr-tmp}"
mkdir -p "$WORKSPACE/input" "$WORKSPACE/output"
echo "[vsr-service] 工作目录: $WORKSPACE"

# 启动服务
echo "[vsr-service] 启动 FastAPI 服务 (http://0.0.0.0:3001) ..."
cd "$SCRIPT_DIR"
exec uvicorn server:app --host 0.0.0.0 --port 3001 --reload