#!/usr/bin/env python3
"""
1.66 改: 用 EasyOCR 检测 + 黑框遮罩字幕区域, 生成演示视频
不跑 STTN (太慢, CPU 几小时), 直接遮黑框看 OCR 找的位置对不对
"""
import os
import sys
import types
import urllib.request
import cv2

# 注入 fake qfluentwidgets (跟 test_easyocr_local.py 一样)
class _V:
    def __init__(self, *a, **k): pass
class _CI:
    def __init__(self, *a, **k):
        self.value = k.get('default')
        if self.value is None and len(a) >= 3:
            self.value = a[2]
class _QC:
    def load(self, *a, **k): pass
fake = types.ModuleType('qfluentwidgets')
fake.qconfig = _QC()
for n in ('ConfigItem', 'QConfig', 'OptionsValidator', 'BoolValidator',
          'OptionsConfigItem', 'EnumSerializer', 'RangeValidator',
          'RangeConfigItem', 'ConfigValidator'):
    setattr(fake, n, _CI)
sys.modules['qfluentwidgets'] = fake
sys.modules['PyQt5'] = types.ModuleType('PyQt5')
for m in ('QtCore', 'QtGui', 'QtWidgets', 'QtNetwork',
          'QtMultimedia', 'QtMultimediaWidgets'):
    sys.modules['PyQt5.' + m] = types.ModuleType('PyQt5.' + m)

# 修 backend.config tr
REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "backend"))
import backend.config as _bc
_bc.config.interface.value = 'ch'
fake.qconfig.set = lambda item, value: setattr(item, 'value', value)
fake.qconfig.get = lambda item: getattr(item, 'value', None)
import configparser as _cp
_bc.tr = _cp.ConfigParser()
_bc.tr.read(_bc.TRANSLATION_FILE, encoding='utf-8')
if not _bc.tr.has_section('Main'):
    class _SafeTrSection(dict):
        def __getitem__(self, key): return key
        def get(self, key, default=None): return key
    class _SafeTr(dict):
        def __getitem__(self, key):
            try: return dict.__getitem__(self, key)
            except KeyError:
                sec = _SafeTrSection()
                dict.__setitem__(self, key, sec)
                return sec
        def get(self, key, default=None):
            try: return dict.__getitem__(self, key)
            except KeyError: return default or _SafeTrSection()
    _bc.tr = _SafeTr()

# 修 find_subtitle_frame_no 的 sub_remover=None bug
import backend.tools.subtitle_detect as _sd_mod
_orig_find = _sd_mod.SubtitleDetect.find_subtitle_frame_no
def _patched_find(self, sub_remover=None):
    from backend.tools.inpaint_tools import is_frame_number_in_ab_sections
    from backend.tools.common_tools import get_readable_path
    import cv2 as _cv2
    from tqdm import tqdm
    from backend.config import config, tr
    video_cap = _cv2.VideoCapture(get_readable_path(self.video_path))
    frame_count = video_cap.get(_cv2.CAP_PROP_FRAME_COUNT)
    tbar = tqdm(total=int(frame_count), unit='frame', position=0,
                file=sys.__stdout__, desc='Subtitle Finding')
    current_frame_no = 0
    sampled_results = {}
    if sub_remover:
        sub_remover.append_output(tr['Main']['ProcessingStartFindingSubtitles'])
    while video_cap.isOpened():
        ret, frame = video_cap.read()
        if not ret:
            break
        current_frame_no += 1
        _ab = sub_remover.ab_sections if sub_remover else None
        if not is_frame_number_in_ab_sections(current_frame_no - 1, _ab):
            tbar.update(1)
            continue
        if (current_frame_no - 1) % self.SAMPLE_STEP == 0 or self.SAMPLE_STEP <= 1:
            temp_list = self.detect_subtitle(frame)
            if len(temp_list) > 0:
                sampled_results[current_frame_no] = temp_list
        tbar.update(1)
    video_cap.release()
    # 不做插值, 直接返回采样的
    return sampled_results
_sd_mod.SubtitleDetect.find_subtitle_frame_no = _patched_find

# 下视频
video_path = "/tmp/test.mp4"
if not os.path.exists(video_path):
    print(f"下视频...")
    urllib.request.urlretrieve(
        "https://vsr-service.oss-cn-hangzhou.aliyuncs.com/test/test-video.mp4",
        video_path,
    )

# 1. OCR 检测
print("=" * 50)
print("1. EasyOCR 检测字幕位置")
print("=" * 50)
sd = _sd_mod.SubtitleDetect(video_path, sub_areas=[(1236, 1390, 21, 1053)])
sd.SAMPLE_STEP = 1  # 每帧都检测
print(f"采样间隔: {sd.SAMPLE_STEP}")
_ = sd.text_detector  # 加载模型
result = sd.find_subtitle_frame_no()
print(f"\n找到字幕帧: {len(result)} 帧")
if result:
    sample_frame = sorted(result.keys())[0]
    sample_box = result[sample_frame][0]
    print(f"样例: 帧 {sample_frame}, 字幕区 {sample_box}")

# 2. 用检测到的位置画黑框, 保存视频
print()
print("=" * 50)
print("2. 生成黑框演示视频")
print("=" * 50)
output_path = "/tmp/test_blacked.mp4"
cap = cv2.VideoCapture(video_path)
fps = cap.get(cv2.CAP_PROP_FPS)
width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
print(f"源视频: {width}x{height} @ {fps} FPS, {total} 帧")

fourcc = cv2.VideoWriter_fourcc(*'mp4v')
writer = cv2.VideoWriter(output_path, fourcc, fps, (width, height))

frame_idx = 0
blacken_count = 0
while cap.isOpened():
    ret, frame = cap.read()
    if not ret:
        break
    frame_idx += 1
    if frame_idx in result:
        for xmin, xmax, ymin, ymax in result[frame_idx]:
            cv2.rectangle(frame, (xmin, ymin), (xmax, ymax), (0, 0, 0), -1)
            blacken_count += 1
    writer.write(frame)
    if frame_idx % 30 == 0:
        print(f"  帧 {frame_idx}/{total} (黑框数 {blacken_count})")

cap.release()
writer.release()

print(f"\n输出: {output_path}")
print(f"大小: {os.path.getsize(output_path) // 1024} KB")
print(f"总黑框数: {blacken_count}")
print(f"\n直接用 QuickTime / VLC / mpv 打开 {output_path} 看效果")
