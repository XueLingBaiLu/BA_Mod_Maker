# -*- coding: utf-8 -*-
r"""bundle CRC 计算 + catalog 更新（Blender 插件与命令行共用）。

用法：
    from finalize_crc import run
    crc, updated = run(bundle_path, catalog_path=None)

    python finalize_crc.py [bundle路径] [catalog路径]
"""
import sys, os, re

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from compute_bundle_crc import compute_bundle_crc
from update_crc import update_catalog

DEFAULT_BUNDLE = r"<游戏安装目录>\BrokenArrow_Data\StreamingAssets\aa\PC\units_assets_all_3cc1eb58d8b7f6cdf82bbfbf3b5b8aaf.bundle"
DEFAULT_CATALOG = r"<游戏安装目录>\BrokenArrow_Data\StreamingAssets\aa\catalog.json"


def run(bundle=None, catalog=None, verbose=True):
    """算 bundle 的 CRC 并更新 catalog。返回 (crc, updated 列表)。"""
    bundle = bundle or DEFAULT_BUNDLE
    catalog = catalog or DEFAULT_CATALOG
    if verbose:
        print("算 CRC（流式，约1-2分钟）...", flush=True)
    crc = compute_bundle_crc(bundle)
    if verbose:
        print("CRC =", crc, flush=True)
    m = re.search(r"([0-9a-fA-F]{32})\.bundle", os.path.basename(bundle))
    if not m:
        raise ValueError("bundle 文件名里没有 32 位 hash：%s" % os.path.basename(bundle))
    hashv = m.group(1).lower()
    if verbose:
        print("hash:", hashv, flush=True)
    if verbose:
        print("更新 catalog.json ...", flush=True)
    updated = update_catalog(catalog, {hashv: crc})
    if verbose:
        print("updated:", updated)
    return crc, updated


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    b = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BUNDLE
    c = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_CATALOG
    run(b, c)
