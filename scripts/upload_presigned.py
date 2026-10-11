#!/usr/bin/env python3
"""本机消费 CI 预签名 URL 清单直传 COS（发布链路第 4/5 步，移植自
VoiceStickPlus scripts/upload_presigned.py）。

清单 = cos-presign.json（artifact 下载所得）：
  [{local,key,url,content_type,cache_control}, ...]

每个对象 PUT 时必须原样回放签入的 Content-Type / Cache-Control（COS 签名
校验按请求头重建，头不一致 403；不带头则对象落 octet-stream——VoiceStickPlus
2026-10-02 整站白屏事故根因）。成功后对公有域名 HEAD 校验状态码与 Content-Type。

由 scripts/publish_cos.py 编排调用，也可独立使用：
    python3 scripts/upload_presigned.py cos-presign.json
"""
import json
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_DOMAIN = "https://dl.davenger.cloud"
RETRIES = 3


def curl(args: list, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(["curl", "-fsS", "--max-time", "300", *args],
                          capture_output=True, text=True, **kwargs)


def put_one(obj: dict, chdir=None) -> None:
    local = Path(obj["local"])
    if not local.is_file():
        raise FileNotFoundError(f"local file missing for key {obj['key']}: {local}")
    headers = []
    if obj.get("content_type"):
        headers += ["-H", f"Content-Type: {obj['content_type']}"]
    if obj.get("cache_control"):
        headers += ["-H", f"Cache-Control: {obj['cache_control']}"]
    for attempt in range(1, RETRIES + 1):
        proc = curl(["-X", "PUT", "-T", str(local), *headers, obj["url"]], cwd=chdir)
        if proc.returncode == 0:
            print(f"uploaded {obj['key']}")
            return
        sys.stderr.write(f"PUT failed (attempt {attempt}/{RETRIES}) {obj['key']}: "
                         f"{proc.stderr.strip()[:200]}\n")
        if attempt < RETRIES:
            time.sleep(5 * attempt)
    raise RuntimeError(f"PUT exhausted retries: {obj['key']}")


def verify_one(domain: str, obj: dict) -> None:
    key = obj["key"]
    proc = subprocess.run(
        ["curl", "-fsSI", "--max-time", "60", f"{domain}/{key}"],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"HEAD not 200: {key}")
    head = proc.stdout.lower()
    if "content-type" not in head:
        raise RuntimeError(f"HEAD missing content-type: {key}")
    expect = obj.get("content_type", "").lower()
    if expect and expect.split(";")[0] not in head:
        raise RuntimeError(f"content-type mismatch for {key}: expect {expect}, got:\n{proc.stdout}")
    print(f"verified {key}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="消费 CI 预签名 URL 清单直传 COS。")
    parser.add_argument("manifest", help="presign 清单 JSON 路径（cos-presign.json）")
    parser.add_argument("--domain", default=DEFAULT_DOMAIN, help="HEAD 校验的公有域名")
    parser.add_argument("--skip-verify", action="store_true", help="跳过上传后的 HEAD 校验")
    args = parser.parse_args()

    objects = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    failed = []
    for obj in objects:
        try:
            put_one(obj)
            if not args.skip_verify:
                verify_one(args.domain, obj)
        except Exception as exc:  # noqa: BLE001 — 汇总后统一退出码
            sys.stderr.write(f"ERROR: {exc}\n")
            failed.append(obj["key"])
    if failed:
        sys.exit(f"Error: {len(failed)} object(s) failed: {failed}")
    print(f"done: {len(objects)} uploaded")


if __name__ == "__main__":
    main()
