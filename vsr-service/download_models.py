#!/usr/bin/env python3
"""
容器启动时从阿里云 OSS 下载 VSR 模型到本地 backend/models/

为什么不在镜像里放：
- backend/models/ 602MB（STTN + PaddleOCR）
- 镜像太大会被 PAI 拉取超时
- 模型放 OSS，容器启动按需下载

OSS 前缀：oss://<BUCKET>/vsr-models/
本地目标：/app/backend/models/
"""
import os
import sys
import oss2
import pathlib


OSS_ENDPOINT = os.getenv("OSS_INTERNAL_ENDPOINT", "https://oss-cn-hangzhou-internal.aliyuncs.com")
OSS_BUCKET = os.getenv("OSS_BUCKET")
OSS_PREFIX = "models/"
LOCAL_DIR = pathlib.Path(os.getenv("VSR_PROJECT_DIR", "/app")) / "backend" / "models"

AK = os.getenv("ACCESSKEY_ID")
SK = os.getenv("ACCESSKEY_SECRET")


def main():
    if not all([OSS_BUCKET, AK, SK]):
        print("[download_models] 缺少 OSS 凭据环境变量，跳过模型下载", flush=True)
        sys.exit(0)  # 不阻断启动（开发环境可能用本地模型）

    LOCAL_DIR.mkdir(parents=True, exist_ok=True)

    print(f"[download_models] 连接 OSS: {OSS_BUCKET}", flush=True)
    auth = oss2.Auth(AK, SK)
    bucket = oss2.Bucket(auth, OSS_ENDPOINT, OSS_BUCKET)

    # 列举 OSS 上 vsr-models/ 下所有文件
    print(f"[download_models] 列举 {OSS_PREFIX} ...", flush=True)
    objects = []
    for obj in oss2.ObjectIterator(bucket, prefix=OSS_PREFIX):
        # obj.key 形如 "vsr-models/sttn-det/sttn.pth"
        rel = obj.key[len(OSS_PREFIX):]  # 相对路径
        local_path = LOCAL_DIR / rel
        objects.append((obj.key, local_path, obj.size))

    total_size = sum(s for _, _, s in objects)
    print(f"[download_models] 需下载 {len(objects)} 个文件，共 {total_size/1024/1024:.0f} MB", flush=True)

    for key, local_path, size in objects:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        # 跳过已存在的（按 size 判断）
        if local_path.exists() and local_path.stat().st_size == size:
            print(f"  ✓ {local_path.relative_to(LOCAL_DIR)} 已存在", flush=True)
            continue
        print(f"  ↓ {local_path.relative_to(LOCAL_DIR)} ({size/1024/1024:.1f} MB)...", end="", flush=True)
        bucket.get_object_to_file(key, str(local_path))
        print(" done", flush=True)

    print(f"[download_models] ✓ 模型下载完成: {LOCAL_DIR}", flush=True)


if __name__ == "__main__":
    main()
