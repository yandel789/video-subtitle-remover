"""
vsr-service 配置模块

集中读取环境变量，提供：
- VSR 端口与工作目录
- 阿里云 OSS 凭据（VSR 处理完后上传结果用）
- 重试策略

本服务位于 video-subtitle-remover/vsr-service/ 目录，独立部署。
凭据从本目录下的 .env 加载（见 .env.example）。
"""
import os
from pathlib import Path
from dotenv import load_dotenv


def _load_env():
    """加载 OSS 等凭据环境变量。

    路径解析顺序（第一个命中的生效）：
    1. 本项目 vsr-service/.env（独立部署，推荐）
    2. 环境变量 VIDEOCLEAN_BACKEND_DIR 指向目录下的 .env（兼容旧部署）
    3. 默认猜测 ../videoClean/backend/.env（兼容旧部署）
    """
    candidates = [Path(__file__).resolve().parent / ".env"]

    explicit = os.getenv("VIDEOCLEAN_BACKEND_DIR")
    if explicit:
        candidates.append(Path(explicit) / ".env")

    candidates.append(
        Path(__file__).resolve().parent.parent.parent / "videoClean" / "backend" / ".env"
    )

    for env_path in candidates:
        if env_path.exists():
            print(f"[vsr-config] 加载 .env: {env_path}")
            load_dotenv(env_path)
            return

    print("[vsr-config] 未找到 .env，依赖进程环境变量提供 OSS 凭据")


_load_env()


# ===== 服务端 =====
VSR_PORT = int(os.getenv("VSR_PORT", "3001"))
VSR_HOST = os.getenv("VSR_HOST", "0.0.0.0")

# 工作目录：VSR 下载的输入、输出的中间文件都放这里，处理完立即清理
# 默认放在本项目内 vsr-service/workspace（独立部署，不依赖 videoClean）
_default_workspace = Path(__file__).resolve().parent / "workspace"
VSR_WORKSPACE = Path(os.getenv("VSR_WORKSPACE", str(_default_workspace)))
VSR_INPUT_DIR = VSR_WORKSPACE / "input"
VSR_OUTPUT_DIR = VSR_WORKSPACE / "output"

# 默认 Inpaint 模式（sttn-auto / sttn-det / lama / propainter / opencv）
VSR_DEFAULT_INPAINT_MODE = os.getenv("VSR_DEFAULT_INPAINT_MODE", "sttn-det")

# 默认字幕检测模式（PP_OCRv5_MOBILE 快速 / PP_OCRv5_SERVER 精准）
VSR_DEFAULT_SUBTITLE_DETECT_MODE = os.getenv(
    "VSR_DEFAULT_SUBTITLE_DETECT_MODE", "PP_OCRv5_SERVER"
)

# OSS 上传下载重试次数
OSS_RETRY_TIMES = int(os.getenv("OSS_RETRY_TIMES", "3"))
OSS_RETRY_BASE_SECONDS = float(os.getenv("OSS_RETRY_BASE_SECONDS", "1.0"))

# VSR 整体处理硬上限（videoClean 端也有 30 分钟超时，这里兜底再长一点）
VSR_TASK_TIMEOUT_SECONDS = int(os.getenv("VSR_TASK_TIMEOUT_SECONDS", "2400"))  # 40 min


# ===== 阿里云 OSS（结果上传用，凭据从 videoClean/.env 读取） =====
OSS_ACCESS_KEY_ID = os.getenv("ACCESSKEY_ID", "")
OSS_ACCESS_KEY_SECRET = os.getenv("ACCESSKEY_SECRET", "")
OSS_REGION = os.getenv("OSS_REGION", "oss-cn-shanghai")
OSS_BUCKET = os.getenv("OSS_BUCKET", "video-clean")
OSS_ENDPOINT = os.getenv("OSS_ENDPOINT", "https://oss-cn-shanghai.aliyuncs.com")

OSS_RESULT_KEY_PREFIX = os.getenv("OSS_RESULT_KEY_PREFIX", "ai-output/")


# ===== 启动校验 =====
def validate():
    """启动时校验关键配置，缺凭据直接抛错"""
    missing = []
    if not OSS_ACCESS_KEY_ID:
        missing.append("ACCESSKEY_ID")
    if not OSS_ACCESS_KEY_SECRET:
        missing.append("ACCESSKEY_SECRET")
    if not OSS_BUCKET:
        missing.append("OSS_BUCKET")
    if missing:
        raise RuntimeError(
            f"VSR 服务启动失败：缺少环境变量 {missing}\n"
            f"  请通过 VIDEOCLEAN_BACKEND_DIR 指向 videoClean/backend 目录，"
            f"或直接 export 环境变量。"
        )
    VSR_INPUT_DIR.mkdir(parents=True, exist_ok=True)
    VSR_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# ===== video-subtitle-remover 路径 =====
# vsr-service/ 位于 video-subtitle-remover/vsr-service/，
# 父目录就是 video-subtitle-remover 项目根。
VSR_PROJECT_DIR = Path(os.getenv(
    "VSR_PROJECT_DIR",
    str(Path(__file__).resolve().parent.parent)
))


def get_vsr_subtitle_remover():
    """导入 video-subtitle-remover 的 SubtitleRemover 类。
    通过 sys.path 注入实现，避免污染全局。
    """
    import sys
    vsr_backend = VSR_PROJECT_DIR / "backend"
    vsr_root = VSR_PROJECT_DIR
    for p in (str(vsr_backend), str(vsr_root)):
        if p not in sys.path:
            sys.path.insert(0, p)

    from backend.main import SubtitleRemover  # noqa: E402
    return SubtitleRemover