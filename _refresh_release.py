# -*- coding: utf-8 -*-
r"""刷新发布目录并重新打 zip（改了插件/日志之后必须做，否则发出去的还是旧文件）。

为什么要它：`build_exe.py` 打包 exe 时会**顺便把当时的插件 zip 与 Change Log 复制进发布目录**，
如果之后又改了插件或日志，发布目录里的副本就**过期**了 ⇒ 用户装到的插件少一个修复 ✗
（2026-10 修 addon 版本检查时就遇到这个）。

用法：
    python _refresh_release.py            # 用 version.py 的版本；刷新 + 重打 zip + 自检
    python _refresh_release.py --check    # 只看差异，不动文件
"""
import argparse
import io
import os
import re
import shutil
import sys
import zipfile

sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from version import APP_VERSION                                     # noqa: E402

NAME = "BA_Mod_Maker_v%s" % APP_VERSION
REL = os.path.join(HERE, NAME)
ZIP = os.path.join(HERE, NAME + ".zip")
# 需要从源码目录同步进发布目录的文件
PAIRS = [("BA_Mod_Maker_blender_addon.zip", "BA_Mod_Maker_blender_addon.zip"),
         ("Change Log.txt", "Change Log.txt"),
         ("README.md", "README.md"),
         ("使用教程-BA_Mod_Maker.md", "使用教程-BA_Mod_Maker.md"),
         ("使用教程-Blender插件.md", "使用教程-Blender插件.md"),
         ("数据库词典.md", "数据库词典.md"),
         ("Blender工具词典.md", "Blender工具词典.md")]
EXCLUDE_DIRS = {"model", "__pycache__"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    if not os.path.isdir(REL):
        raise SystemExit("✗ 发布目录不存在：%s（先跑 build_exe.py）" % REL)
    stale = []
    for src, dst in PAIRS:
        s, d = os.path.join(HERE, src), os.path.join(REL, dst)
        if not os.path.exists(s):
            continue
        if not os.path.exists(d) or os.path.getsize(s) != os.path.getsize(d) \
                or open(s, "rb").read() != open(d, "rb").read():
            stale.append((src, d))
    print("发布目录：%s" % REL)
    if not stale:
        print("✓ 需要同步的文件都是最新的（%d 项）" % len(PAIRS))
    else:
        for s, d in stale:
            print("  ⚠ 过期：%s → %s" % (s, os.path.basename(d)))
    if a.check:
        return 1 if stale else 0
    for s, d in stale:
        shutil.copy2(os.path.join(HERE, s), d)
        print("  ✓ 已同步 %s" % os.path.basename(d))
    # 重打 zip
    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED) as z:
        for base, dirs, files in os.walk(REL):
            dirs[:] = [x for x in dirs if x not in EXCLUDE_DIRS]
            for fn in files:
                full = os.path.join(base, fn)
                rel = os.path.relpath(full, REL).replace(os.sep, "/")
                z.write(full, NAME + "/" + rel)
    print("✓ 已重打 %s（%.1f MB，%d 项）"
          % (os.path.basename(ZIP), os.path.getsize(ZIP) / 1048576,
             len(zipfile.ZipFile(ZIP).namelist())))
    # ★ v1.8.112：**根目录那份副本也要一起刷新**（以前只在 `build_exe.py` 里复制一次 ⇒
    #   刷新日志/插件之后，根目录留的还是**旧 zip**；而交付体检只查"存在"，查不出内容过期 ✗
    #   实测踩到：refresh 之后产品目录 zip 119,646,866 字节、根目录还是 122,803,930 的旧件）
    root = os.path.dirname(os.path.dirname(HERE))
    root_zip = None
    if os.path.isdir(root) and os.path.abspath(root) != os.path.abspath(HERE):
        root_zip = os.path.join(root, os.path.basename(ZIP))
        try:
            shutil.copy2(ZIP, root_zip)
            print("✓ 已同步到工作区根：%s（%.1f MB）"
                  % (os.path.basename(root_zip), os.path.getsize(root_zip) / 1048576))
        except Exception as e:                                        # noqa: BLE001
            print("  ⚠ 复制到根目录失败（不影响产品目录）：%s" % e)
            root_zip = None
    # ★ 自检：zip 里的插件必须带上"版本检查已修"的标记（防再发旧文件）
    with zipfile.ZipFile(ZIP) as z:
        inner = None
        for n in z.namelist():
            if n.endswith("blender_addon.zip") or n.endswith("blender_addon/__init__.py"):
                inner = n
                break
        if inner and inner.endswith("blender_addon.zip"):
            import tempfile
            tmp = os.path.join(tempfile.gettempdir(), "_rel_addon.zip")
            with open(tmp, "wb") as f:
                f.write(z.read(inner))
            with zipfile.ZipFile(tmp) as z2:
                src = z2.read("blender_addon/__init__.py").decode("utf-8", "replace")
            os.remove(tmp)
        elif inner:
            src = z.read(inner).decode("utf-8", "replace")
        else:
            src = ""
        ok = ("从磁盘重读一次 version.py" in src) or ("_addon_ver" in src)
        print("  自检：发布包里的插件含最新版本检查修复 = %s" % ("✓" if ok else "✗（可能还是旧文件）"))
    # ★ 自检 2：根目录副本必须与产品目录**逐字节一致**（防"存在但过期"）
    if root_zip:
        same = (os.path.getsize(root_zip) == os.path.getsize(ZIP)
                and open(root_zip, "rb").read() == open(ZIP, "rb").read())
        print("  自检：根目录副本与产品目录一致 = %s" % ("✓" if same else "✗（大小或内容不同）"))
        ok = ok and same
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
