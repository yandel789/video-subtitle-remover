"""
vsr-service 后台 worker

职责：
- 维护内存任务队列（task_state: dict + task_queue: deque）
- 后台线程串行消费任务（concurrency=1，与 videoClean MAX_PROCESSING=1 对齐）
- 协调 OSS 下载 → VSR 处理 → OSS 上传 → 清理本地文件
- 通过 SubtitleRemover.add_progress_listener 回调更新 task_state 进度
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import threading
import time
import traceback
from collections import deque
from typing import Callable, Optional

import httpx
import oss2

import config

log = logging.getLogger("vsr-worker")


# ===== 模块级状态 =====
_subtitle_remover_class: Optional[Callable] = None  # SubtitleRemover 类（首次任务实例化）
_model_ready: bool = False
_load_error: Optional[str] = None
_device_name: str = "unknown"

# 任务状态: { task_id: {status, progress, video_url, output_url, error, ...} }
_task_state: dict = {}
_task_queue: deque = deque()
_queue_lock = threading.Lock()

_worker_thread: Optional[threading.Thread] = None
_worker_stop_event = threading.Event()


def set_subtitle_remover_class(cls):
    global _subtitle_remover_class
    _subtitle_remover_class = cls


def set_model_ready(ready: bool, error: Optional[str] = None):
    global _model_ready, _load_error
    _model_ready = ready
    _load_error = error


def is_model_ready() -> bool:
    return _model_ready


def get_load_error() -> Optional[str]:
    return _load_error


def get_device_name() -> str:
    return _device_name


def get_task(task_id: str) -> Optional[dict]:
    return _task_state.get(task_id)


def get_queue_depth() -> int:
    with _queue_lock:
        return len(_task_queue)


def enqueue_task(task_id: str, video_url: str, coords: dict, inpaint_mode: str):
    """提交任务到队列"""
    with _queue_lock:
        _task_state[task_id] = {
            "status": "queued",
            "progress": 0,
            "video_url": video_url,
            "coords": coords,
            "inpaint_mode": inpaint_mode,
            "created_at": time.time(),
        }
        _task_queue.append(task_id)
    log.info(f"任务 {task_id} 已入队，当前队列长度={len(_task_queue)}")


# ===== Worker 线程 =====
def start_worker_thread():
    global _worker_thread
    if _worker_thread and _worker_thread.is_alive():
        return
    _worker_stop_event.clear()
    _worker_thread = threading.Thread(target=_worker_loop, name="vsr-worker", daemon=True)
    _worker_thread.start()
    log.info("VSR worker 线程已启动")


def stop_worker_thread():
    _worker_stop_event.set()
    if _worker_thread:
        _worker_thread.join(timeout=5)


def _worker_loop():
    """worker 主循环：串行消费任务队列"""
    log.info("worker 循环开始")
    while not _worker_stop_event.is_set():
        task_id = None
        with _queue_lock:
            if _task_queue:
                task_id = _task_queue.popleft()

        if task_id is None:
            time.sleep(0.5)
            continue

        try:
            _process_one(task_id)
        except Exception as e:
            log.error(f"任务 {task_id} 处理异常: {e}")
            log.error(traceback.format_exc())
            _task_state[task_id] = {
                **_task_state.get(task_id, {}),
                "status": "failed",
                "error": str(e),
            }

    log.info("worker 循环退出")


def _process_one(task_id: str):
    """处理单个任务：OSS 下载 → VSR 处理 → OSS 上传 → 清理"""
    state = _task_state.get(task_id)
    if not state:
        log.warning(f"任务 {task_id} 状态丢失，跳过")
        return

    coords = state["coords"]
    video_url = state["video_url"]
    inpaint_mode = state.get("inpaint_mode") or config.VSR_DEFAULT_INPAINT_MODE

    # 文件路径
    input_path = config.VSR_INPUT_DIR / f"{task_id}.mp4"
    output_path = config.VSR_OUTPUT_DIR / f"{task_id}_no_sub.mp4"

    state["status"] = "processing"
    state["progress"] = 0
    state["started_at"] = time.time()

    try:
        # Step 1: 从 OSS 下载视频
        log.info(f"[{task_id}] 下载 OSS 视频: {video_url[:80]}...")
        _download_oss_with_retry(video_url, input_path)
        log.info(f"[{task_id}] 下载完成: {input_path}")

        # Step 2: 调用 VSR 处理
        log.info(
            f"[{task_id}] VSR 开始处理 (mode={inpaint_mode}): "
            f"coords={coords}"
        )
        _run_vsr(
            task_id=task_id,
            input_path=input_path,
            output_path=output_path,
            coords=coords,
            inpaint_mode=inpaint_mode,
        )
        log.info(f"[{task_id}] VSR 处理完成: {output_path}")

        # Step 3: 上传结果到 OSS
        state["status"] = "uploading"
        state["progress"] = 99
        output_url = _upload_oss_with_retry(output_path, f"{config.OSS_RESULT_KEY_PREFIX}{task_id}_processed.mp4")
        log.info(f"[{task_id}] 上传完成: {output_url}")

        state["status"] = "finished"
        state["progress"] = 100
        state["output_url"] = output_url
        state["finished_at"] = time.time()

    except Exception as e:
        log.error(f"[{task_id}] 失败: {e}")
        log.error(traceback.format_exc())
        state["status"] = "failed"
        state["error"] = str(e)
        state["failed_at"] = time.time()
        # 不抛出，让下一个任务继续
    finally:
        # Step 4: 清理本地文件（输入 + 输出）
        _safe_remove(input_path, label=f"{task_id} input")
        _safe_remove(output_path, label=f"{task_id} output")


def _safe_remove(path, label=""):
    try:
        if path.exists():
            path.unlink()
            log.debug(f"已删除 {label}: {path}")
    except Exception as e:
        log.warning(f"删除 {label} 失败: {e}")


# ===== VSR 处理 =====
def _run_vsr(task_id: str, input_path, output_path, coords: dict, inpaint_mode: str):
    """调用 video-subtitle-remover 的 SubtitleRemover 处理视频"""
    if _subtitle_remover_class is None:
        raise RuntimeError("VSR SubtitleRemover 类未初始化")

    sr = _subtitle_remover_class(str(input_path), gui_mode=False)

    # 坐标换算：videoClean 给的 (ymin, ymax, xmin, xmax) 直接传给 VSR
    sr.sub_areas = [(coords["ymin"], coords["ymax"], coords["xmin"], coords["xmax"])]
    sr.video_out_path = str(output_path)

    # 设置 inpaint 模式（修改 VSR 全局 config）
    try:
        from backend.config import config as vsr_config
        from backend.tools.constant import InpaintMode
        vsr_config.inpaintMode.value = InpaintMode[inpaint_mode.replace("-", "_").upper()]
    except Exception as e:
        log.warning(f"设置 inpaint_mode 失败，使用 VSR 默认值: {e}")

    # 进度回调
    def on_progress(percent, finished):
        # VSR 回调可能从 worker 线程触发，需要安全写 state
        state = _task_state.get(task_id)
        if state is None:
            return
        state["progress"] = max(state.get("progress", 0), int(percent))
        if finished:
            log.info(f"[{task_id}] VSR 报告完成")

    sr.add_progress_listener(on_progress)
    sr.run()


# ===== OSS 下载（带重试） =====
def _download_oss_with_retry(url: str, local_path, max_retries: int = None):
    """从公网 URL 下载文件到本地，支持重试"""
    if max_retries is None:
        max_retries = config.OSS_RETRY_TIMES

    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            _download_file(url, local_path)
            return
        except Exception as e:
            last_err = e
            backoff = config.OSS_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
            log.warning(
                f"下载失败 (attempt {attempt}/{max_retries}): {e}，"
                f"{backoff:.1f}s 后重试"
            )
            if attempt < max_retries:
                time.sleep(backoff)
    raise RuntimeError(f"OSS 下载失败，已重试 {max_retries} 次: {last_err}")


def _download_file(url: str, local_path):
    """流式下载"""
    local_path.parent.mkdir(parents=True, exist_ok=True)
    with httpx.stream("GET", url, follow_redirects=True, timeout=httpx.Timeout(300.0)) as r:
        r.raise_for_status()
        with open(local_path, "wb") as f:
            for chunk in r.iter_bytes(chunk_size=1024 * 256):
                f.write(chunk)


# ===== OSS 上传（带重试） =====
def _upload_oss_with_retry(local_path, oss_key: str, max_retries: int = None) -> str:
    """上传本地文件到阿里云 OSS，返回公开访问 URL"""
    if max_retries is None:
        max_retries = config.OSS_RETRY_TIMES

    if not local_path.exists():
        raise FileNotFoundError(f"本地文件不存在: {local_path}")

    auth = oss2.Auth(config.OSS_ACCESS_KEY_ID, config.OSS_ACCESS_KEY_SECRET)
    bucket = oss2.Bucket(auth, config.OSS_ENDPOINT, config.OSS_BUCKET)

    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            bucket.put_object_from_file(oss_key, str(local_path))
            # 构造公开 URL（与 videoClean 现有约定一致）
            return f"https://{config.OSS_BUCKET}.{config.OSS_REGION}.aliyuncs.com/{oss_key}"
        except Exception as e:
            last_err = e
            backoff = config.OSS_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
            log.warning(
                f"上传失败 (attempt {attempt}/{max_retries}): {e}，"
                f"{backoff:.1f}s 后重试"
            )
            if attempt < max_retries:
                time.sleep(backoff)
    raise RuntimeError(f"OSS 上传失败，已重试 {max_retries} 次: {last_err}")