"""
vsr-service FastAPI 入口

端口：3001
提供接口：
  POST /vsr/remove        提交去字幕任务
  GET  /vsr/progress/{tid}  查询任务进度
  GET  /vsr/health        健康检查（包含模型是否加载完成）

工作流：
  启动 → 加载 VSR 模型（一次性） → 起后台 worker 线程 → 接 HTTP 请求
"""
from __future__ import annotations

import asyncio
import logging
import sys
import traceback
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Path as PathParam
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

import config
import worker

# 让 video-subtitle-remover 的 backend.* 可被 import
sys.path.insert(0, str(config.VSR_PROJECT_DIR))
sys.path.insert(0, str(config.VSR_PROJECT_DIR / "backend"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("vsr-service")


# ===== 启动 / 关闭 =====
@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动时初始化"""
    log.info("VSR 服务启动中...")
    config.validate()

    # 启动后台 worker
    worker.start_worker_thread()

    # 异步加载 VSR 模型（不阻塞启动）
    asyncio.create_task(_load_model_safely())

    yield

    # 关闭时停 worker
    worker.stop_worker_thread()
    log.info("VSR 服务已停止")


async def _load_model_safely():
    """在后台异步加载 VSR 模型，避免阻塞 FastAPI 启动"""
    try:
        log.info("开始加载 VSR 模型（首次较慢，约 10~60 秒）...")
        SubtitleRemover = config.get_vsr_subtitle_remover()
        # 实例化一次，让 Paddle/Torch 模型权重加载到内存
        # 这里只验证可导入，真正实例化放在首次任务执行时（避免冷启动卡死）
        log.info("VSR 模型导入成功，等待首个任务...")
        worker.set_subtitle_remover_class(SubtitleRemover)
        worker.set_model_ready(True)
    except Exception as e:
        log.error(f"VSR 模型加载失败: {e}")
        log.error(traceback.format_exc())
        worker.set_model_ready(False)
        worker.set_load_error(str(e))


app = FastAPI(
    title="VSR Subtitle Remover Service",
    description="本地 AI 去字幕服务（基于 video-subtitle-remover）",
    version="1.0.0",
    lifespan=lifespan,
)


# ===== 请求/响应模型 =====
class RemoveRequest(BaseModel):
    task_id: str = Field(..., min_length=1, max_length=128, description="videoClean 侧任务 UUID")
    video_url: str = Field(..., min_length=1, description="阿里云 OSS 视频公开 URL")
    ymin: int = Field(..., ge=0, description="字幕区域顶部 y 坐标（像素）")
    ymax: int = Field(..., ge=0, description="字幕区域底部 y 坐标（像素）")
    xmin: int = Field(..., ge=0, description="字幕区域左侧 x 坐标（像素）")
    xmax: int = Field(..., ge=0, description="字幕区域右侧 x 坐标（像素）")
    inpaint_mode: Optional[str] = Field(default=None, description="sttn-auto/sttn-det/lama/propainter/opencv")

    @field_validator("ymax")
    @classmethod
    def ymax_gt_ymin(cls, v, info):
        ymin = info.data.get("ymin")
        if ymin is not None and v <= ymin:
            raise ValueError("ymax 必须大于 ymin")
        return v

    @field_validator("xmax")
    @classmethod
    def xmax_gt_xmin(cls, v, info):
        xmin = info.data.get("xmin")
        if xmin is not None and v <= xmin:
            raise ValueError("xmax 必须大于 xmin")
        return v


# ===== 路由 =====
@app.post("/vsr/remove")
async def submit_remove(req: RemoveRequest):
    """提交一个去字幕任务"""
    if not worker.is_model_ready():
        raise HTTPException(
            status_code=503,
            detail=f"VSR 模型未就绪：{worker.get_load_error() or '正在加载中，请稍后再试'}"
        )

    # task_id 重复检测
    existing = worker.get_task(req.task_id)
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"task_id {req.task_id} 已存在，状态={existing.get('status')}"
        )

    inpaint_mode = req.inpaint_mode or config.VSR_DEFAULT_INPAINT_MODE

    try:
        worker.enqueue_task(
            task_id=req.task_id,
            video_url=req.video_url,
            coords={
                "ymin": req.ymin,
                "ymax": req.ymax,
                "xmin": req.xmin,
                "xmax": req.xmax,
            },
            inpaint_mode=inpaint_mode,
        )
    except Exception as e:
        log.error(f"入队失败: {e}")
        raise HTTPException(status_code=500, detail=f"入队失败: {e}")

    return {
        "task_id": req.task_id,
        "status": "queued",
        "inpaint_mode": inpaint_mode,
    }


@app.get("/vsr/progress/{task_id}")
async def get_progress(task_id: str = PathParam(..., min_length=1, max_length=128)):
    state = worker.get_task(task_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"task {task_id} 不存在")

    resp = {
        "task_id": task_id,
        "status": state.get("status", "unknown"),
        "progress": state.get("progress", 0),
    }
    if state.get("status") == "finished":
        resp["output_url"] = state.get("output_url")
    if state.get("status") == "failed":
        resp["error"] = state.get("error")
    return resp


@app.get("/vsr/health")
async def health():
    """健康检查：用于 videoClean 启动时探活"""
    if not worker.is_model_ready():
        err = worker.get_load_error()
        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "model_loaded": False,
                "error": err or "model loading",
                "queue_depth": worker.get_queue_depth(),
            },
        )

    return {
        "status": "ok",
        "model_loaded": True,
        "device": worker.get_device_name(),
        "queue_depth": worker.get_queue_depth(),
        "inpaint_mode": config.VSR_DEFAULT_INPAINT_MODE,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "server:app",
        host=config.VSR_HOST,
        port=config.VSR_PORT,
        reload=False,
        log_level="info",
    )