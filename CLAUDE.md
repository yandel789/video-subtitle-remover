# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

`video-subtitle-remover` (VSR) — AI tool to remove hardcoded subtitles from videos/images at original resolution. Python 3.12, PyTorch + PaddleOCR, PySide6 GUI.

## Common commands

### Run
```shell
# GUI (default user-facing entry point)
python gui.py

# CLI
python ./backend/main.py -i test/test.mp4 -o test/test_no_sub.mp4 \
    -c 880 990 150 850 -c 880 990 150 850 \
    --inpaint-mode propainter
```

`--inpaint-mode` choices: `sttn-auto` (default), `sttn-det`, `lama`, `propainter`, `opencv`. `-c` is `YMIN YMAX XMIN XMAX` and may be repeated for multiple subtitle regions. Omit `-c` to remove ALL text across the full frame.

### Install (Python env)
`requirements.txt` covers everything except torch/torchvision, which are installed per-platform (see README §4). Pinned working combos:
- CUDA 11.8: `torch==2.7.0` + `torchvision==0.22.0` from `download.pytorch.org/whl/cu118`
- CUDA 12.x / CPU / macOS: same torch version, different index URL or none
- DirectML (Win): also `torch_directml==0.2.5.dev240914`

### Tests / lint
There is **no automated test suite** — `test/` only holds sample input media (`test.mp4`, `test1.mp4`, …). The standard "test" is running the CLI against `test/test.mp4` and eyeballing the output. No linter is configured.

### Docker
```shell
docker build -f docker/Dockerfile --build-arg CUDA_VERSION=11.8 --build-arg HARDWARD_ACCELERATOR=cuda -t vsr .
docker run -it --rm --gpus all -v "$PWD/test:/vsr/test" vsr \
    python backend/main.py -i /vsr/test/test.mp4 -o /vsr/test/test_no_sub.mp4
```
CPU image sets `HARDWARD_ACCELERATION_OPTION = False` in `backend/config.py` at build time.

### Windows packaging (QPT)
The GitHub Actions workflows (`build-windows-*.yml`) use `backend/tools/makedist.py` to produce the 7z release artifacts:
```shell
pip install QPT==1.0b8 setuptools
python backend/tools/makedist.py --cuda 11.8   # or --cuda 12.6 / 12.8 / --directml
```
Output lands in `../vsr_out/Release/`. Pre-built release notes (compute capability ranges per CUDA version) live in README §"预构建包对比说明".

## High-level architecture

```
gui.py  ── spawns ──▶  backend/tools/subtitle_remover_remote_call.py
   │                              │
   │                              ▼
   │              ProcessManager  ── forks ──▶  backend/main.py::SubtitleRemover.run()
   ▼                                                          │
ui/home_interface.py + ui/component/*                         ▼
                                  backend/tools/subtitle_detect.py  (PaddleOCR via HardwareAccelerator)
                                                                │
                                                                ▼
                            backend/inpaint/{sttn_auto,sttn_det,lama,propainter,opencv}_inpaint.py
                                                                │
                                                                ▼
                                backend/tools/video_io.py::{FramePrefetcher, FFmpegVideoWriter}
                                                                │
                                                                ▼
                                          ffmpeg merge of original audio track
```

### Entry points
- **`gui.py`** — PySide6 + qfluentwidgets FluentWindow. Hosts `HomeInterface` (task list + video preview) and `AdvancedSettingInterface` (every tunable exposed in the UI). Forks the actual work into a subprocess via `SubtitleRemoverRemoteCall`; `ProcessManager` kills orphans.
- **`backend/main.py`** — CLI entry. `SubtitleRemover` is the top-level orchestrator: opens video with cv2, writes a temp mp4 via `FFmpegVideoWriter` (libx264 over a pipe, not `cv2.VideoWriter`), then runs the selected mode.

### Five inpaint modes (`backend/inpaint/`)
| Mode | File | Backend | Notes |
|------|------|---------|-------|
| `STTN_AUTO` | `sttn_auto_inpaint.py` | in-house STTN | No subtitle detection; just blanks the user-selected region. Fastest. |
| `STTN_DET` | `sttn_det_inpaint.py` | in-house STTN | Runs OCR first, inpaints only detected boxes. |
| `LAMA` | `lama_inpaint.py` | TorchScript `big-lama.pt` | Best for images/animation. |
| `PROPAINTER` | `propainter_inpaint.py` | RAFT flow + transformer | Highest VRAM, best on motion-heavy video. Splits frame vertically into `split_h = W*3/16` strips via `get_inpaint_area_by_mask`. |
| `OPENCV` | `opencv_inpaint.py` | cv2.inpaint | Trivial fallback. |

### Detection pipeline (`backend/tools/subtitle_detect.py`)
1. Sample frames at `SAMPLE_STEP` (auto: 2/3/4 based on FPS; or user override `subtitleDetectSampleStep`).
2. PaddleOCR text detection → polygon → `(xmin, xmax, ymin, ymax)` boxes.
3. Filter against user-supplied subtitle regions (single-region fast path in `detect_subtitle`).
4. Interpolate: fill gaps ≤ `subtitleDetectFillMaxGapFrames`, carry forward/backward by `subtitleDetectCarry*Frames`.
5. `unify_regions` to merge similar boxes across frames.
6. `find_continuous_ranges_with_same_mask` → split by scene cuts (`backend/scenedetect` ContentDetector) → `filter_and_merge_intervals` ensures each run is ≥ `sttnReferenceLength`.

### Hardware acceleration (`backend/tools/hardware_accelerator.py`)
Singleton that probes, in order: torch-directml → CUDA → MPS → onnxruntime providers (Dml/ROCM/MIGraphX/VitisAI/OpenVINO/Metal/CoreML/CUDA). The `.device` property is lazy because onnxruntime-directml ≥ 1.21.1 conflicts with torch-directml — pinned to `onnxruntime-directml==1.20.1` for that reason (see comment in `hardware_accelerator.py:131`).

### Video I/O (`backend/tools/video_io.py`)
- **`FramePrefetcher`** — background thread that decodes frames into a bounded queue so model inference and cv2.VideoCapture I/O overlap.
- **`FFmpegVideoWriter`** — pipes raw BGR frames into a libx264 subprocess (crf 18, preset fast). cv2's mp4v fourcc was replaced because the output quality was visibly worse.

### Config (`backend/config.py`)
Single `QConfig` instance (`qfluentwidgets`) loaded from `config/config.json`. All user-facing knobs are `RangeConfigItem` / `OptionsConfigItem` here; the GUI bound cards in `advanced_setting_interface.py` edit them at runtime. Key groupings:
- `subtitleDetect*` — OCR sample step, gap-fill, carry-forward/backward, box similarity tolerances, mask dilation, timeline expansion.
- `sttn*` — neighbor stride, reference length, max load (must be > stride × ref length; enforced by `getSttnMaxLoadNum`).
- `propainterMaxLoadNum` — only knob exposed for ProPainter (the rest are hardcoded in `PropainterInpaint.__init__`: `neighbor_length=10`, `mask_dilation=4`, `ref_stride=10`, `raft_iter=20`, `use_fp16=True`).

### Models (`backend/models/`)
Bundled checkpoints: `big-lama/` (LAMA TorchScript), `propainter/` (RAFT + flow completion + ProPainter), `sttn-auto/`, `sttn-det/`, `V5/ch_det{,_fast}/` (PP-OCRv5 mobile/server detection). Large files are split-merged on first use via `merge_big_file_if_not_exists` (fsplit). The `SubtitleDetectMode` enum (`PP_OCRv5_MOBILE` vs `PP_OCRv5_SERVER`) maps to `models/V5/ch_det_fast` vs `models/V5/ch_det` in `model_config.py`.

### Internationalization
Interface strings come from `backend/interface/{ch,chinese_cht,en,es,japan,ko,vi}.ini`, selected via `config.interface`. CLI forces English by setting `config.interface = 'en'` in `main.py`.

## Gotchas
- `KMP_DUPLICATE_LIB_OK=True` is set in `config.py` to silence Intel OpenMP conflicts — do not remove.
- `multiprocessing.set_start_method("spawn")` is required in both `gui.py` and `__main__` of `backend/main.py` because of CUDA/torch child-process behavior; missing on Linux/macOS causes silent deadlocks, especially with Paddle.
- On Windows, `tempfile.NamedTemporaryFile(delete=True)` raises PermissionError on cleanup → both tempfiles are created with `delete=False` and removed in `finally`.
- FFmpeg binaries under `backend/ffmpeg/` are split-merged on first launch per OS; the merger auto-runs in `FFmpegCLI.__init__` and `ModelConfig.__init__`.
- macOS Intel should NOT use MPS — slower than CPU. The README explicitly warns about this.
- `os.name == 'nt'` switches subprocess invocations to `shell=True` because Windows requires it for some FFmpeg calls.
- Scene detection is invoked only for `InpaintMode.PROPAINTER`; other modes skip it entirely.
- `SubtitleDetectMode` enum migration runs at import time (`config.py:119`) to upgrade legacy Chinese-string values in old config.json files.
