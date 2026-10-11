#!/usr/bin/env python3
"""CI 侧：把 MANIFEST 输入（[{local,key},...] JSON）签成 COS 预签名 PUT URL。

产物 cos-presign.json 由 workflow 以 artifact 下发发布机（GITHUB_TOKEN 回写
git push 曾被拒——VoiceStickPlus 2026-10-02 实测，artifact 通道更稳）。

Content-Type / Cache-Control 必须签入 URL 并在直传时原样回放：COS 签名校验按
请求头重建，头不一致 403；不带 Content-Type 则对象落 octet-stream，浏览器拒渲染
HTML（VoiceStickPlus 2026-10-02 整站白屏事故根因）。预签名仅本地计算，凭据不
出 CI；文件本体由发布机直传，CI 永远不碰。
"""
import json
import mimetypes
import os
import pathlib
import sys


def headers_for(key: str) -> dict:
    ct = mimetypes.guess_type(key)[0]
    if key.endswith(".js"):
        ct = "text/javascript"
    ct = ct or "application/octet-stream"
    cc = "no-cache" if key.endswith((".html", ".json", ".xml")) else "max-age=31536000"
    return {"Content-Type": ct, "Cache-Control": cc}


def require_credentials() -> tuple:
    secret_id = (os.environ.get("TENCENT_COS_SECRET_ID")
                 or os.environ.get("TENCENTCLOUD_SECRET_ID"))
    secret_key = (os.environ.get("TENCENT_COS_SECRET_KEY")
                  or os.environ.get("TENCENTCLOUD_SECRET_KEY"))
    if not (secret_id and secret_key):
        sys.exit("::error ::COS secrets missing (TENCENT_COS_SECRET_ID / TENCENT_COS_SECRET_KEY)")
    return secret_id, secret_key


def main() -> None:
    raw = os.environ.get("MANIFEST", "").strip()
    if not raw:
        sys.exit("::error ::MANIFEST input missing")
    plan = json.loads(raw)
    if not isinstance(plan, list) or not plan:
        sys.exit("::error ::MANIFEST must be a non-empty JSON array of {local,key}")

    secret_id, secret_key = require_credentials()
    from qcloud_cos import CosConfig, CosS3Client
    client = CosS3Client(CosConfig(
        Region=os.environ["COS_REGION"], SecretId=secret_id, SecretKey=secret_key,
        Scheme="https"))

    objects = []
    for entry in plan:
        key = entry["key"]
        headers = headers_for(key)
        objects.append({
            "local": entry["local"],
            "key": key,
            "content_type": headers["Content-Type"],
            "cache_control": headers["Cache-Control"],
            "url": client.get_presigned_url(
                Method="PUT", Bucket=os.environ["COS_BUCKET"], Key=key,
                Expired=7200, Headers=headers),
        })
    pathlib.Path("cos-presign.json").write_text(
        json.dumps(objects, indent=2) + "\n", encoding="utf-8")
    print(f"presigned {len(objects)} objects")


if __name__ == "__main__":
    main()
