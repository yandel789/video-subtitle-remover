# vsr-service

本地视频去字幕服务，基于 [video-subtitle-remover](https://github.com/YaoFANGUK/video-subtitle-remover) 封装，提供 HTTP 接口供 [videoClean](https://github.com/your-org/videoClean) 后端调用。

本服务位于 `video-subtitle-remover/vsr-service/` 目录，与 VSR 项目本体一同管理。OSS 凭据复用 videoClean 的 `backend/.env`，避免重复配置。

## 架构

```
┌────────────────────────────────────────────────────────────┐
│ videoClean backend (3000)                                  │
│   taskQueue → vsrClient.remove() ──HTTP──▶ vsr-service    │
└────────────────────────────────────────────────────────────┘
                                                 │
                                                 ▼
┌────────────────────────────────────────────────────────────┐
│ vsr-service (3001, Python FastAPI)                         │
│   本目录：video-subtitle-remover/vsr-service/              │
│                                                            │
│   POST /vsr/remove   → 入队                                │
│   GET  /vsr/progress/{tid} → 进度查询                       │
│   GET  /vsr/health   → 健康检查                            │
│                                                            │
│   后台 worker 线程（concurrency=1）:                       │
│     下载 OSS → SubtitleRemover.run() → 上传 OSS → 清理    │
└────────────────────────────────────────────────────────────┘
```

## 环境要求

- Python 3.9 ~ 3.12（PaddlePaddle 3.0.0 不支持 3.13/3.14）
- 磁盘：VSR 仓库自带模型约 5GB，工作目录预留 10GB
- 可选：NVIDIA GPU（CUDA 加速，强烈推荐）
- 依赖：已克隆 [videoClean](https://github.com/your-org/videoClean) 到同级目录（默认假设 `~/github/videoClean`）

## 目录布局

```
github/
├── videoClean/                          ← 业务项目
│   └── backend/
│       ├── .env                         ← OSS 凭据来源
│       └── videos/vsr-tmp/              ← VSR 工作目录（共享）
│           ├── input/
│           └── output/
└── video-subtitle-remover/              ← 本仓库（含 VSR 代码 + 模型权重）
    ├── backend/
    │   ├── main.py                      ← VSR 核心（SubtitleRemover 类）
    │   └── models/                      ← VSR 模型权重（git clone 已包含）
    │       ├── big-lama/
    │       ├── sttn-auto/
    │       ├── sttn-det/
    │       ├── propainter/
    │       └── V5/
    └── vsr-service/                     ← 本服务（FastAPI HTTP 封装）
        ├── server.py
        ├── worker.py
        ├── config.py
        ├── requirements.txt
        ├── start.sh
        └── README.md
```

## 快速开始

### 1. 启动 vsr-service

```bash
cd ~/github/video-subtitle-remover/vsr-service
./start.sh
```

启动脚本会自动：
- 创建 `.venv/` 虚拟环境
- 安装 `requirements.txt` 中的依赖（fastapi、uvicorn、httpx、oss2）
- 询问是否安装 VSR 的 PaddlePaddle + PyTorch（首次需选 y）
- 启动 uvicorn 在 3001 端口

**期望输出**：
```
[vsr-config] 加载 .env: /Users/xxx/github/videoClean/backend/.env
[vsr-service] 工作目录: /Users/xxx/github/videoClean/backend/videos/vsr-tmp
[vsr-service] 启动 FastAPI 服务 (http://0.0.0.0:3001) ...
INFO:     Uvicorn running on http://0.0.0.0:3001
```

### 3. 验证

```bash
curl http://localhost:3001/vsr/health
# {"status":"ok","model_loaded":true,"device":"cpu","queue_depth":0,"inpaint_mode":"sttn-auto"}
```

⚠️ 首次会返回 503 + `model_loaded=false`，因为模型加载需要 30~120 秒。等 `INFO` 日志显示模型加载完再查。

### 4. 启动 videoClean

```bash
cd ~/github/videoClean/backend
npm run dev
```

## 配置

通过环境变量配置（推荐放 `videoClean/backend/.env`，本服务自动加载）：

```bash
# 服务端
VSR_PORT=3001                                  # 端口
VSR_WORKSPACE=.../backend/videos/vsr-tmp       # 工作目录
VSR_DEFAULT_INPAINT_MODE=sttn-auto             # 默认算法
OSS_RETRY_TIMES=3                              # OSS 重试次数

# 阿里云 OSS（结果上传用）
ACCESSKEY_ID=...
ACCESSKEY_SECRET=...
OSS_REGION=oss-cn-shanghai
OSS_BUCKET=video-clean
OSS_ENDPOINT=https://oss-cn-shanghai.aliyuncs.com

# 可选：自定义 videoClean backend 目录（默认 ../videoClean/backend）
# VIDEOCLEAN_BACKEND_DIR=/custom/path/to/videoClean/backend

# 可选：自定义 VSR 项目根（默认本脚本的父目录）
# VSR_PROJECT_DIR=/custom/path/to/video-subtitle-remover
```

## API 文档

启动后访问 `http://localhost:3001/docs` 看 FastAPI 自动生成的接口文档。

## 与 videoClean 配合

1. 启动 `vsr-service`（端口 3001）
2. 启动 videoClean 后端（端口 3000）
3. 小程序端 `useAliyun=true` 的去字幕任务会自动走 VSR 链路

## 常见问题

**Q: `/vsr/health` 返回 503 model_loaded=false？**
A: 模型目录为空或模型文件损坏。检查 `~/github/video-subtitle-remover/backend/models/` 目录（应有 big-lama/、sttn-auto/、sttn-det/、propainter/、V5/）。

**Q: 处理一个 2GB 视频要多久？**
A: GPU（RTX 3060）：约 2~5 分钟；CPU：30~60 分钟。

**Q: 启动报"未找到 videoClean .env"？**
A: 设置 `VIDEOCLEAN_BACKEND_DIR` 指向 videoClean 的 backend 目录，或直接 export OSS 凭据。

**Q: 如何选择 inpaint 模式？**
- `sttn-auto`：默认，对真人视频效果最好，速度快
- `sttn-det`：自动检测字幕位置（如果用户没框选）
- `lama`：对动画/图片效果好
- `propainter`：剧烈运动场景，显存占用大

通过 `VSR_DEFAULT_INPAINT_MODE` 配置，或在 `POST /vsr/remove` 请求中指定 `inpaint_mode` 字段。

**Q: 与 VSR 主项目的关系？**
本服务是 VSR 项目的"运行封装"，不修改 VSR 核心代码（`backend/` 目录保持原样）。升级 VSR 时只需 `git pull` 即可，本服务代码与 VSR 核心解耦。