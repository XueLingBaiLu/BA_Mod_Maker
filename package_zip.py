# -*- coding: utf-8 -*-
"""把源码 + 启动脚本打包成 zip（源码版分发，需本机 Python 运行）。

用法：
  python package_zip.py            # 生成 Database_Editor_v<VERSION>_source.zip

打包内容：
  核心源码         ba_db_tool.py / ba_crypto.py / ba_aes.py / i18n.py
  启动脚本         启动编辑器.bat / Start Editor.bat
  数据与资源       clean_baseline.json / icons/*.png / UABEADump/*
  构建 exe 的工具  build_exe.py / auto_build.py / 自动打包.bat
  本打包工具       package_zip.py / 打包源码.bat
  文档             README.md / Change Log.txt
"""

import os
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
from version import APP_VERSION  # 单一版本来源（version.py）
VERSION = APP_VERSION
ZIP_NAME = "BA_Mod_Maker_v%s_source.zip" % VERSION

# 打进 zip 的文件（相对项目根目录）
FILES = [
    # 核心源码（运行编辑器）
    "ba_db_tool.py",
    "ba_crypto.py",
    "ba_aes.py",
    "ba_glossary.py",
    "i18n.py",
    # 音频功能（文件级导入 + 添加新音效）
    "audio_import.py",
    "audio_add.py",
    "audio_bank.py",
    "audio_preset.py",
    # 单一版本来源（改版本只动这一个文件）
    "version.py",
    # Blender 插件一键安装脚本（配套 BA_Mod_Maker_blender_addon.zip）
    "安装Blender插件.py",
    # 数据与资源
    "clean_baseline.json",
    "localization_map.json",
    "database_glossary.json",
    "generate_glossary.py",
    "数据库词典.md",
    # 启动脚本
    "启动编辑器.bat",
    "Start Editor.bat",
    # 构建 exe 的源码与脚本
    "build_exe.py",
    "auto_build.py",
    "自动打包.bat",
    # 本打包工具
    "package_zip.py",
    "打包源码.bat",
    # 文档
    "README.md",
    "Change Log.txt",
    "Blender工具词典.md",
    "使用教程-BA_Mod_Maker.md",
    "使用教程-Blender插件.md",
]
ICON_DIR = "icons"
UABEADUMP_DIR = "UABEADump"


def build_zip():
    out = os.path.join(HERE, ZIP_NAME)
    if os.path.exists(out):
        os.remove(out)
    added = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in FILES:
            path = os.path.join(HERE, rel)
            if os.path.isfile(path):
                z.write(path, rel)
                print("  +", rel)
                added += 1
            else:
                print("  ! 缺少文件，已跳过:", rel)
        d = os.path.join(HERE, ICON_DIR)
        if os.path.isdir(d):
            for fn in sorted(os.listdir(d)):
                if fn.lower().endswith(".png"):
                    z.write(os.path.join(d, fn), os.path.join(ICON_DIR, fn))
                    added += 1
            print("  +", ICON_DIR + "/*.png")
        # 提取出的游戏图标（单位/武器/弹药/专精/指示），供单位浏览树与关联树使用。
        # 立绘 portraits 体积过大（~470 MB）不打包。
        d = os.path.join(HERE, "icons_extracted")
        if os.path.isdir(d):
            for cat in ("units", "weapons", "ammo", "options", "specs", "indicators"):
                cd = os.path.join(d, cat)
                if not os.path.isdir(cd):
                    continue
                for fn in sorted(os.listdir(cd)):
                    if fn.lower().endswith(".png"):
                        z.write(os.path.join(cd, fn), os.path.join("icons_extracted", cat, fn))
                        added += 1
            print("  + icons_extracted/*")
        # UABEADump helper (exe + classdata.tpk) — required for the unity3d
        # open/export/import features when running from source.
        d = os.path.join(HERE, UABEADUMP_DIR)
        if os.path.isdir(d):
            for fn in sorted(os.listdir(d)):
                fp = os.path.join(d, fn)
                if os.path.isfile(fp):
                    z.write(fp, os.path.join(UABEADUMP_DIR, fn))
                    added += 1
            print("  +", UABEADUMP_DIR + "/*")
        # Pillow (just the PIL package) — running from source needs it so game
        # icons scale with LANCZOS instead of the jagged tk subsample fallback;
        # build_exe.py also bundles this exact folder into the packaged exe.
        pil_dir = os.path.join(HERE, "_unitypy", "PIL")
        if os.path.isdir(pil_dir):
            n = 0
            for root_dir, _dirs, files in os.walk(pil_dir):
                for fn in files:
                    fp = os.path.join(root_dir, fn)
                    rel = os.path.relpath(fp, os.path.join(HERE, "_unitypy"))
                    z.write(fp, os.path.join("_unitypy", rel))
                    n += 1
                    added += 1
            print("  + _unitypy/PIL/* (%d files)" % n)
        # _rev_tools 根目录下的工具脚本（模型 Mod 面板运行所需：mesh/catalog/CRC/换模型等）
        rev_dir = os.path.join(HERE, "_rev_tools")
        if os.path.isdir(rev_dir):
            for fn in sorted(os.listdir(rev_dir)):
                if fn.endswith(".py") or fn == "ComputeBundleCrc.cs":
                    z.write(os.path.join(rev_dir, fn), os.path.join("_rev_tools", fn))
                    added += 1
            print("  + _rev_tools/*.py")
        # Blender 插件（直接构造版：含内置 _rev_tools 子目录）
        ba_dir = os.path.join(HERE, "blender_addon")
        if os.path.isdir(ba_dir):
            for root_dir, _dirs, files in os.walk(ba_dir):
                for fn in sorted(files):
                    if fn.endswith(".pyc") or "__pycache__" in root_dir:
                        continue
                    fp = os.path.join(root_dir, fn)
                    rel = os.path.relpath(fp, ba_dir)
                    z.write(fp, os.path.join("blender_addon", rel))
                    added += 1
            print("  + blender_addon/*（含内置 _rev_tools）")
    print("已生成:", out)


if __name__ == "__main__":
    build_zip()
