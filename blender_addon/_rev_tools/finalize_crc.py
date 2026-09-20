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

# ⛔ 2026-10 修（B 分支）：下面这个路径原来**写死了包名里的 32 位内容哈希**，
#   而游戏 1.2.0.3 之后那份包已经变成 `units_assets_all_1e6c04ce42984f32a0891b92fab010e8.bundle`
#   ⇒ `DEFAULT_BUNDLE` 指向一个**不存在的文件**（旧名字只剩一个 `_unpacked` 残留目录，
#   看着像还在 ✗）。而"错的路径不报错"正是这类事故的共性 ⇒ 现在**按 glob 解析**。
try:
    from bundle_paths import units_bundle
    DEFAULT_BUNDLE = units_bundle() or ""
except Exception:                                                     # noqa: BLE001
    DEFAULT_BUNDLE = ""
# 兜底（glob 找不到时给个提示用的旧名，**不要**拿它当真路径）
_FALLBACK_HINT = r"<游戏>\BrokenArrow_Data\StreamingAssets\aa\PC\units_assets_all_<内容哈希>.bundle"
DEFAULT_CATALOG = os.path.join(
    r"<游戏安装目录>", "BrokenArrow_Data",
    "StreamingAssets", "aa", "catalog.json")


class BundleNotFound(RuntimeError):
    """bundle 没解析出来 —— **明确报错**，绝不退回写死的旧路径"""


def run(bundle=None, catalog=None, verbose=True):
    """算 bundle 的 CRC 并更新 catalog。返回 (crc, updated 列表)。"""
    bundle = bundle or DEFAULT_BUNDLE
    catalog = catalog or DEFAULT_CATALOG
    if not bundle or not os.path.isfile(bundle):
        raise BundleNotFound(
            "没能解析出要算 CRC 的 bundle%s\n"
            "  ⛔ 包名里带 **32 位内容哈希**，游戏更新一次就变（实测 3cc1eb58… → 1e6c04ce…）\n"
            "  ⇒ 别写死名字；用 `bundle_paths.units_bundle()` / 显式传路径\n"
            "  ⇒ 期望形状：%s" % (("：" + str(bundle)) if bundle else "（glob 也没命中）", _FALLBACK_HINT))
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
    try:                                    # GUI/无控制台环境 sys.stdout 可能是 None
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    b = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BUNDLE
    c = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_CATALOG
    run(b, c)
