import os
import sys
import ctypes

import cv2
import numpy as np
try:
    from fsplit.filesplit import Filesplit
    HAS_FSPLIT = True
except ImportError:
    Filesplit = None
    HAS_FSPLIT = False  # 大文件分片工具不可用，仅影响超大文件（> 几GB）处理

video_extensions = {
    '.mp4', '.m4a', '.m4v', '.f4v', '.f4a', '.m4b', '.m4r', '.f4b', '.mov',
    '.3gp', '.3gp2', '.3g2', '.3gpp', '.3gpp2', '.ogg', '.oga', '.ogv', '.ogx',
    '.wmv', '.wma', '.asf', '.webm', '.flv', '.avi', '.gifv', '.mkv', '.rm',
    '.rmvb', '.vob', '.dvd', '.mpg', '.mpeg', '.mp2', '.mpe', '.mpv', '.mpg',
    '.mpeg', '.m2v', '.svi', '.3gp', '.mxf', '.roq', '.nsv', '.flv', '.f4v',
    '.f4p', '.f4a', '.f4b'
}

image_extensions = {
    '.jpg', '.jpeg', '.jpe', '.jif', '.jfif', '.jfi', '.png', '.gif',
    '.webp', '.tiff', '.tif', '.psd', '.raw', '.arw', '.cr2', '.nrw',
    '.k25', '.bmp', '.dib', '.heif', '.heic', '.ind', '.indd', '.indt',
    '.jp2', '.j2k', '.jpf', '.jpx', '.jpm', '.mj2', '.svg', '.svgz',
    '.ai', '.eps', '.ico'
}


def is_video_file(filename):
    return os.path.splitext(filename)[-1].lower() in video_extensions


def is_image_file(filename):
    return os.path.splitext(filename)[-1].lower() in image_extensions


def is_video_or_image(filename):
    file_extension = os.path.splitext(filename)[-1].lower()
    # 检查扩展名是否在定义的视频或图片文件后缀集合中
    return file_extension in video_extensions or file_extension in image_extensions

def merge_big_file_if_not_exists(dir, file, man_filename = None):
    if file not in os.listdir(dir):
        if not HAS_FSPLIT:
            # 修改原因：FC 部署时 fsplit 未装，且 OSS download_models 只下分片
            # 不下合并后单文件（bit-lama.pt / ProPainter.pth）。
            # 原行为直接 raise 会导致 ModelConfig.__init__ 中断，FC 任务全部失败。
            # 改为：警告 + 直接返回，不影响 sttn-det 等不依赖大文件的模式。
            # 真用到 big-lama / propainter 的模式（lama / propainter inpaint）会另外报错。
            print(
                f"[common_tools] WARNING: 跳过 {dir}/{file} 合并（fsplit 未装 + 文件不存在），"
                f"该模式如需大模型会另外报错。",
                flush=True,
            )
            return
        # FC 上分片文件存在但 .pt 合并文件没下：调用 fs.merge 也会 raise FileNotFoundError
        # （找不到 fs_manifest.csv）。这里 catch 兜住，仅警告不中断 init。
        try:
            fs = Filesplit()
            if man_filename is not None:
                fs.man_filename = man_filename
            fs.merge(input_dir=dir)
        except Exception as e:
            print(
                f"[common_tools] WARNING: 跳过 {dir}/{file} 合并（fsplit.merge 失败: {e}），"
                f"该模式如需大模型会另外报错。",
                flush=True,
            )
            return

def get_readable_path(path):
    if sys.platform != 'win32':
        return path
    buf = ctypes.create_unicode_buffer(4096)
    ctypes.windll.kernel32.GetShortPathNameW(path, buf, 4096)
    return buf.value

def read_image(path):
    if os.path.getsize(path) > 100*1024*1024: # 100MB
        print(f"Image {path} is too large, skip")
        return None
    img = cv2.imdecode(np.fromfile(path, dtype=np.uint8), -1)
    if img is not None and img.shape[-1] == 4:
        img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    return img