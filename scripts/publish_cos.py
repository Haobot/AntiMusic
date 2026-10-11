#!/usr/bin/env python3
"""AntiMusic 国内分发发布器：规划清单 → CI 签发预签名 → 本机直传 → 公网校验。

链路（为什么不 CI 直传：海外 runner → 上海 COS 跨境上行 ~8KB/s 且 ~130s 断连，
≥1MB 分块必死，VoiceStickPlus Doc/Ref/cos-distribution.md 定案）：
  1. 本机规划清单 [{local,key},...]（简介网页 + 可选安装包与 .sha256）
  2. gh workflow run cos_presign.yml —— CI 持仓库 Secrets 签发 7200s 预签名 PUT URL
  3. 下载 artifact 得到 cos-presign.json
  4. 本机 curl 直传（原样回放签入的 Content-Type/Cache-Control，头不一致 403）
  5. 公网 HEAD 校验 dl.davenger.cloud；安装包再 GET + SHA-256 比对

用法（仓库根目录执行，gh 已登录）：
    python3 scripts/publish_cos.py --zip build/Release/AntiMusic-1.2-macOS.zip --version 1.2
    python3 scripts/publish_cos.py --site-only           # 仅同步简介网页
    python3 scripts/publish_cos.py --plan-only --zip ... --version 1.2  # 只打印清单

gh/API 请求需本机代理：未设 HTTPS_PROXY 时默认 127.0.0.1:5782；直传与公网校验
阶段自动剥离代理变量，走国内直连。
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import upload_presigned

BUCKET_DOMAIN = "https://dl.davenger.cloud"
PREFIX = "antimusic"
WORKFLOW = "cos_presign.yml"
ARTIFACT = "cos-presign"
REPO = "Haobot/AntiMusic"
GH_PROXY_DEFAULT = "http://127.0.0.1:5782"
# 简介网页镜像（COS 国内主源与 GitHub Pages 备源同源同内容）
SITE_FILES = [
    ("docs/index.html", "index.html"),
    ("docs/icon-512.png", "icon-512.png"),
    ("docs/icon-1024.png", "icon-1024.png"),
]
PROXY_VARS = ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy",
              "ALL_PROXY", "all_proxy")


def gh(args: list) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    for var, value in (("HTTPS_PROXY", GH_PROXY_DEFAULT), ("HTTP_PROXY", GH_PROXY_DEFAULT),
                       ("https_proxy", GH_PROXY_DEFAULT), ("http_proxy", GH_PROXY_DEFAULT)):
        env.setdefault(var, value)
    return subprocess.run(["gh", *args], capture_output=True, text=True, env=env)


def build_plan(zip_path: str, version: str) -> tuple:
    """返回 (清单, 安装包本地 SHA-256 或 None)。"""
    plan = []
    for local, name in SITE_FILES:
        path = Path(local)
        if not path.is_file():
            sys.exit(f"missing site file: {local}")
        plan.append({"local": str(path.resolve()), "key": f"{PREFIX}/{name}"})

    digest = None
    if zip_path:
        if not version:
            sys.exit("--zip 必须配套 --version")
        zip_p = Path(zip_path)
        if not zip_p.is_file():
            sys.exit(f"missing zip: {zip_p}")
        digest = hashlib.sha256(zip_p.read_bytes()).hexdigest()
        key_dir = f"{PREFIX}/v{version}"
        plan.append({"local": str(zip_p.resolve()), "key": f"{key_dir}/{zip_p.name}"})
        sha_p = Path(str(zip_p) + ".sha256")
        sha_p.write_text(f"{digest}  {zip_p.name}\n", encoding="utf-8")
        plan.append({"local": str(sha_p.resolve()), "key": f"{key_dir}/{zip_p.name}.sha256"})
    return plan, digest


def wait_for_run(t0: str, timeout: int = 420) -> int:
    """等 workflow dispatch 出现在 run 列表并跑完，返回 run id。"""
    deadline = time.time() + timeout
    run_id = None
    while time.time() < deadline and run_id is None:
        proc = gh(["run", "list", "--repo", REPO, "--workflow", WORKFLOW,
                   "--limit", "10", "--json", "databaseId,status,conclusion,createdAt"])
        if proc.returncode == 0 and proc.stdout.strip():
            # createdAt 为 UTC ISO 同格式，可字典序比较
            for run in json.loads(proc.stdout):
                if run["createdAt"] >= t0:
                    run_id = run["databaseId"]
                    break
        if run_id is None:
            time.sleep(3)
    if run_id is None:
        sys.exit(f"timeout: run for workflow {WORKFLOW} never appeared (t0={t0})")

    while time.time() < deadline:
        proc = gh(["run", "view", str(run_id), "--repo", REPO,
                   "--json", "status,conclusion"])
        if proc.returncode == 0:
            info = json.loads(proc.stdout)
            if info["status"] == "completed":
                if info["conclusion"] != "success":
                    sys.exit(f"presign run {run_id} failed ({info['conclusion']}), "
                             f"see gh run view {run_id} --repo {REPO}")
                return run_id
        time.sleep(5)
    sys.exit(f"timeout: run {run_id} not finished")


def download_manifest(run_id: int, dest: Path) -> Path:
    proc = gh(["run", "download", str(run_id), "--repo", REPO,
               "--name", ARTIFACT, "--dir", str(dest)])
    if proc.returncode != 0:
        sys.exit(f"artifact download failed: {proc.stderr.strip()}")
    path = dest / "cos-presign.json"
    if not path.is_file():
        got = [str(p) for p in dest.rglob("*")]
        sys.exit(f"cos-presign.json missing after download, got: {got}")
    return path


def strip_proxy() -> None:
    """直传/校验走国内直连，剥离 gh 阶段注入的代理。"""
    for var in PROXY_VARS:
        os.environ.pop(var, None)


def get_sha256(url: str) -> str:
    with tempfile.NamedTemporaryFile(suffix=".dl", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        proc = subprocess.run(["curl", "-fsS", "--max-time", "300", "-o", str(tmp_path), url],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            sys.exit(f"download failed: {url}: {proc.stderr.strip()[:200]}")
        return hashlib.sha256(tmp_path.read_bytes()).hexdigest()
    finally:
        tmp_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="AntiMusic COS 国内分发发布器")
    parser.add_argument("--zip", help="安装包路径（如 build/Release/AntiMusic-1.2-macOS.zip）")
    parser.add_argument("--version", help="版本号（决定键 antimusic/v<版本>/）")
    parser.add_argument("--site-only", action="store_true", help="仅同步简介网页")
    parser.add_argument("--plan-only", action="store_true", help="只打印清单，不触发 CI")
    args = parser.parse_args()

    if args.site_only:
        args.zip = None
    plan, digest = build_plan(args.zip, args.version)
    print(f"plan: {len(plan)} objects")
    for obj in plan:
        print(f"  {obj['key']}  ←  {obj['local']}")
    if args.plan_only:
        print(json.dumps(plan, indent=2, ensure_ascii=False))
        return

    # gh 阶段（需要代理）
    t0 = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest = json.dumps(plan, ensure_ascii=False, separators=(",", ":"))
    proc = gh(["workflow", "run", WORKFLOW, "--repo", REPO, "-f", f"manifest={manifest}"])
    if proc.returncode != 0:
        # 刚推送 workflow 后 API 可见性有滞后，退避重试一次
        print(f"dispatch failed, retrying once: {proc.stderr.strip()[:200]}")
        time.sleep(10)
        proc = gh(["workflow", "run", WORKFLOW, "--repo", REPO, "-f", f"manifest={manifest}"])
        if proc.returncode != 0:
            sys.exit(f"gh workflow run failed: {proc.stderr.strip()}")
    print(f"dispatched {WORKFLOW} (t0={t0})")
    run_id = wait_for_run(t0)
    print(f"presign run {run_id} succeeded")

    with tempfile.TemporaryDirectory(prefix="cos-presign-") as tmp:
        manifest_path = download_manifest(run_id, Path(tmp))
        objects = json.loads(manifest_path.read_text(encoding="utf-8"))

    # 直传阶段（国内直连）
    strip_proxy()
    failed = []
    for obj in objects:
        try:
            upload_presigned.put_one(obj)
            upload_presigned.verify_one(BUCKET_DOMAIN, obj)
        except Exception as exc:  # noqa: BLE001 — 汇总后统一退出
            sys.stderr.write(f"ERROR: {exc}\n")
            failed.append(obj["key"])
    if failed:
        sys.exit(f"Error: {len(failed)} object(s) failed: {failed}")

    # 安装包端到端：从 COS 真下载一次，SHA-256 与本机产物比对
    if digest:
        zip_obj = next(o for o in objects if o["key"].endswith(".zip"))
        got = get_sha256(f"{BUCKET_DOMAIN}/{zip_obj['key']}")
        if got != digest:
            sys.exit(f"SHA-256 mismatch: local {digest} vs COS {got}")
        print(f"download round-trip SHA-256 OK: {digest}")

    print("\n published:")
    for obj in objects:
        print(f"  {BUCKET_DOMAIN}/{obj['key']}")


if __name__ == "__main__":
    main()
