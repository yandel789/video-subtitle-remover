import os
import stat
import shutil

import platform
from .common_tools import merge_big_file_if_not_exists
from backend.config import BASE_DIR

class FFmpegCLI:

    """
    进程管理器类，用于管理子进程的生命周期
    使用弱引用避免内存泄漏
    """
    _instance = None

    @classmethod
    def instance(cls):
        """单例模式获取实例"""
        if cls._instance is None:
            cls._instance = FFmpegCLI()
        return cls._instance

    def __init__(self):
        # 系统 ffmpeg (如 FC 镜像的 /usr/bin/ffmpeg) 本就可执行且可能只读，无需/无权 chmod
        try:
            os.chmod(self.ffmpeg_path, stat.S_IRWXU + stat.S_IRWXG + stat.S_IRWXO)
        except (FileNotFoundError, PermissionError, OSError):
            pass

    @property
    def ffmpeg_path(self):
        # 1.68 改: FC/Docker 镜像里打包的 backend/ffmpeg/ 被 Dockerfile 删掉了，
        #   改为优先用系统 PATH 里的 ffmpeg (镜像 apt install，在 /usr/bin/ffmpeg)。
        #   本地开发若存在打包 ffmpeg 仍优先用打包版，保持原行为。
        env_path = os.environ.get('FFMPEG_PATH')
        if env_path and os.path.exists(env_path):
            return env_path
        system = platform.system()
        if system == "Windows":
            ffmpeg_dir = os.path.join(BASE_DIR, 'ffmpeg', 'win_x64')
            merge_big_file_if_not_exists(ffmpeg_dir, 'ffmpeg.exe')
            return os.path.join(ffmpeg_dir, 'ffmpeg.exe')
        elif system == "Linux":
            bundled = os.path.join(BASE_DIR, 'ffmpeg', 'linux_x64', 'ffmpeg')
        else:
            bundled = os.path.join(BASE_DIR, 'ffmpeg', 'macos', 'ffmpeg')
        if os.path.exists(bundled):
            return bundled
        # 打包版不存在 → 用系统 ffmpeg；都没有则返回 bundled 保持原报错信息
        return shutil.which('ffmpeg') or bundled