#!/opt/venv/bin/python
"""
vsr-service 启动包装器

职责：
1. 在 venv/site-packages 注入 fake qfluentwidgets 模块（绕开 backend/config.py 的 GUI 依赖，
   不需要真的装 PySide6 + qfluentwidgets 共 200MB+）
2. 调 download_models.py 下模型（subprocess，不依赖 qfluentwidgets）
3. 在当前进程跑 uvicorn（保持 sys.modules 状态让 fake qfluentwidgets 生效）
"""
import sys, types, os, subprocess
from pathlib import Path

# ===== 0. 1.66 改: 去掉所有 cudnn 9.x/8.x hack =====
# 1.58 ~ 1.65 试了 7 种方案 (preload/symlink/cp 到系统路径/FLAGS_cudnn_dir 等), libpaddle.so 内部 dlopen 都 fail
# 1.66 改: 字幕检测从 PaddleOCR 切到 EasyOCR (PyTorch-based), 整个 paddlepaddle-gpu 依赖可以删掉
# 不再需要处理 cudnn 8.x, 这里只留空占位方便以后回滚参考


# ===== 1. 注入 fake qfluentwidgets =====


# ===== 1. 注入 fake qfluentwidgets =====
# backend/config.py 顶部 `from qfluentwidgets import (qconfig, ConfigItem, QConfig, ...)`
# 我们提供同名 fake 类/方法，让 import 成功但行为是 no-op
class _V:
    def __init__(self, *a, **k): pass
class _CI:
    """fake ConfigItem / OptionsConfigItem / RangeConfigItem 等"""
    def __init__(self, *a, **k):
        # 取第 3 个位置参数（默认值）或 'default' 关键字参数作为 value
        self.value = k.get('default')
        if self.value is None and len(a) >= 3:
            self.value = a[2]
class _QC:
    """fake qconfig"""
    def load(self, *a, **k): pass

fake = types.ModuleType('qfluentwidgets')
fake.qconfig = _QC()
for n in ('ConfigItem', 'QConfig', 'OptionsValidator', 'BoolValidator',
          'OptionsConfigItem', 'EnumSerializer', 'RangeValidator',
          'RangeConfigItem', 'ConfigValidator'):
    setattr(fake, n, _CI)
sys.modules['qfluentwidgets'] = fake
# 还要 fake PyQt5（部分 PyQt-Fluent-Widgets 模块可能 import PyQt5）
sys.modules['PyQt5'] = types.ModuleType('PyQt5')
for m in ('QtCore', 'QtGui', 'QtWidgets', 'QtNetwork',
          'QtMultimedia', 'QtMultimediaWidgets'):
    sys.modules['PyQt5.' + m] = types.ModuleType('PyQt5.' + m)

print('[wrapper] fake qfluentwidgets injected', flush=True)


# ===== 1.5 注入 fake numpy._core.tests._natype =====
# 背景：本地 wheels/ 里装的是 numpy 2.2.6 + scipy 1.15.3
#   - scipy 1.15.3 的 scipy._lib.array_api_compat.numpy 用 `from numpy import *`
#   - 触发 numpy.testing._private.utils → `from numpy._core.tests._natype import pd_NA`
#   - 但 numpy 2.0+ 把 numpy._core.tests 子模块整个删了 → ModuleNotFoundError
# 结果：任何 `import scipy.ndimage`（propainter_inpaint 第一行就 import）必炸
# 这是 scipy 1.15.3 的 bug，wheels 重装 scipy 1.16+ 才能根治
# 这里用 monkey patch 注入假模块，让 scipy 能 import 成功
class _NA:
    """scipy 只用 pd_NA 做 NA 标量，给个 None 即可（不会被真正调用）"""
    pass
_nct = types.ModuleType('numpy._core.tests')
_nct_natype = types.ModuleType('numpy._core.tests._natype')
_nct_natype.pd_NA = _NA()
sys.modules['numpy._core.tests'] = _nct
sys.modules['numpy._core.tests._natype'] = _nct_natype
print('[wrapper] fake numpy._core.tests._natype injected', flush=True)


# ===== 1.7 已删除 paddle monkey patch =====
# 之前 1.50-fc 尝试 monkey patch paddle.inference.Config.set_optimization_level
# 但这是 C 扩展方法，Python 覆写 method slot 触发 SIGSEGV (code 139)
# 改用 requirements.txt pin paddleocr<3.0 匹配 paddlepaddle 2.6.x（治本）


# ===== 1.6 修正 backend.config 的 interface 语言 + 注入 fake tr =====
# 背景：backend/config.py:32 把 interface 默认值设成显示文本 'ChineseSimplified'，
#       但 ini 文件名是 ch.ini / en.ini 等（key 是 intefaceTexts 的 value）
#       → 云端没 GUI → interface 永远是无效的 'ChineseSimplified'
#       → f"{config.interface.value}.ini" = "ChineseSimplified.ini" 不存在
#       → tr.read() 静默失败 → tr 是空 ConfigParser
#       → backend/main.py:163 调 tr['Main']['NoSubtitleDetected'] 抛 KeyError('Main')
#       → str(KeyError('Main')) = "'Main'" → 任务失败时存到 error 字段（就是日志里看到的 error: "'Main'"）
# 本地能跑只是因为开发者跑过 GUI 选过语言（写入 config.ini 把值改成 'ch'）
# 修复两步：
#   1. 把 /app 和 /app/backend 加 sys.path（wrapper.py 在 /app/vsr-service/，要 import backend.* 必须加）
#   2. import backend.config 后，把 config.interface 改成 'ch'（简体中文对应的 ini 文件名）
#   3. 用 _SafeTr 替换 backend.config.tr，防止万一 [Main] section 缺失时再次 KeyError
sys.path.insert(0, os.getenv("VSR_PROJECT_DIR", "/app"))
sys.path.insert(0, os.path.join(os.getenv("VSR_PROJECT_DIR", "/app"), "backend"))

class _SafeTrSection(dict):
    """任何 key 都返回自己（占位符），不抛 KeyError"""
    def __getitem__(self, key):
        return key
    def get(self, key, default=None):
        return key

class _SafeTr(dict):
    """dict 替代 ConfigParser，tr['Main'] 永远返回 _SafeTrSection（占位字符串 dict）
    用 try/except 模式：不 override __contains__，避免把 'in' 检查变成永远 True 导致死循环
    """
    def __getitem__(self, key):
        try:
            return dict.__getitem__(self, key)
        except KeyError:
            sec = _SafeTrSection()
            dict.__setitem__(self, key, sec)
            return sec
    def get(self, key, default=None):
        try:
            return dict.__getitem__(self, key)
        except KeyError:
            return default if default is not None else _SafeTrSection()

# 显式 import 触发 backend.config 顶层执行（产生 tr 变量）
import backend.config as _bc
# 把 interface 修正为 'ch'（即 ch.ini，对应简体中文）
fake.qconfig.set = lambda item, value: setattr(item, 'value', value)
fake.qconfig.get = lambda item: getattr(item, 'value', None)
_bc.config.interface.value = 'ch'
# 重新读 tr
import configparser as _cp
_bc.tr = _cp.ConfigParser()
_bc.tr.read(_bc.TRANSLATION_FILE, encoding='utf-8')
# 兜底：万一 [Main] 还是缺失，用 _SafeTr 替
if not _bc.tr.has_section('Main'):
    _bc.tr = _SafeTr()
print(f'[wrapper] backend.config.interface 修正为 ch, tr 来源={_bc.TRANSLATION_FILE}', flush=True)


# ===== 2. download_models.py =====
# 用 subprocess 跑（download_models.py 不依赖 qfluentwidgets，新进程干净没关系）
print('[wrapper] running download_models.py...', flush=True)
_VSR_ROOT = Path(os.getenv("VSR_PROJECT_DIR", "/app"))
r = subprocess.call([sys.executable, str(_VSR_ROOT / "vsr-service" / "download_models.py")])
if r != 0:
    print(f'[wrapper] download_models.py failed (rc={r}), exiting', flush=True)
    sys.exit(r)


# ===== 3. uvicorn in-process =====
# **关键**：必须在当前进程跑 uvicorn，不能用 subprocess 或 os.execvp
# 否则新进程 sys.modules 是干净的，fake qfluentwidgets 失效
print('[wrapper] starting uvicorn in-process...', flush=True)
import uvicorn
uvicorn.run(
    "server:app",
    host="0.0.0.0",
    port=8000,
    app_dir=os.path.join(os.getenv("VSR_PROJECT_DIR", "/app"), "vsr-service"),
    log_level="info",
)
