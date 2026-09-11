# -*- coding: utf-8 -*-
"""自动打包脚本：源码一改动，就自动重建 BA_Mod_Maker exe（版本取 version.py）。

用法：
  python auto_build.py             # 只打包一次（等价于 build_exe.py，先重建插件 zip）
  python auto_build.py --watch     # 先打包一次，然后监视源码、改动后自动重建
  python auto_build.py -w          # --watch 的简写
  python auto_build.py -w --debounce 3   # 停止改动 3 秒后才触发重建（默认 2 秒）

监视的文件：
  version.py（版本单一来源：改版本触发重建，且 exe/插件版本成对更新）、
  ba_db_tool.py, ba_crypto.py, ba_aes.py, ba_glossary.py, i18n.py, mod_assets.py,
  _rev_tools/（import_pack/finalize_crc/stream_save/pack_model/update_crc/
  compute_bundle_crc/catalog_mod）, blender_addon/（__init__.py/mount_dict.py）,
  clean_baseline.json, database_glossary.json, 数据库词典.md, icons/*.png

注意：
  - 重建会覆盖 dist 输出，如果旧 exe 正在运行，请先关闭它，
    否则 Windows 会因文件被占用而无法覆盖。
  - 按 Ctrl+C 停止监视。
"""

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# 参与打包的源文件（改动任意一个都触发重建）
WATCH_FILES = [
    "version.py",
    "ba_db_tool.py",
    "ba_crypto.py",
    "ba_aes.py",
    "ba_glossary.py",
    "i18n.py",
    # 素材导入（.bamod → bundle）依赖：改这里必须触发重建
    "mod_assets.py",
    "_rev_tools/import_pack.py",
    "_rev_tools/pack_model.py",
    "_rev_tools/finalize_crc.py",
    "_rev_tools/stream_save.py",
    "_rev_tools/update_crc.py",
    "_rev_tools/compute_bundle_crc.py",
    "_rev_tools/catalog_mod.py",
    # Blender 插件（重建插件 zip 后随发布目录分发）
    "blender_addon/__init__.py",
    "blender_addon/mount_dict.py",
    # 词典（GUI 词典窗口 + 随发布目录分发的 数据库词典.md）
    "generate_glossary.py",
    "database_glossary.json",
    "数据库词典.md",
    "Change Log.txt",
    "clean_baseline.json",
]
# 图标目录（整个目录一起监视）
WATCH_DIRS = ["icons"]


def _now():
    return time.strftime("%H:%M:%S")


def _log(msg):
    print("[%s] %s" % (_now(), msg), flush=True)


def snapshot():
    """返回所有被监视文件的 {路径: (mtime_ns, size)} 快照。"""
    state = {}
    for rel in WATCH_FILES:
        path = os.path.join(HERE, rel)
        try:
            st = os.stat(path)
            state[path] = (st.st_mtime_ns, st.st_size)
        except OSError:
            state[path] = None
    for rel in WATCH_DIRS:
        d = os.path.join(HERE, rel)
        if not os.path.isdir(d):
            continue
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for fn in names:
            path = os.path.join(d, fn)
            try:
                st = os.stat(path)
                state[path] = (st.st_mtime_ns, st.st_size)
            except OSError:
                pass
    return state


def _watched_count():
    n = len(WATCH_FILES)
    d = os.path.join(HERE, "icons")
    if os.path.isdir(d):
        try:
            n += len(os.listdir(d))
        except OSError:
            pass
    return n


def _build():
    # 先重建 Blender 插件 zip：build_exe 会把它原样打进发布目录，
    # 不重建的话插件改动（hub_edit/copy_full/import_pack 等）不会进入分发包。
    rebuild_addon = os.path.join(HERE, "_rebuild_addon_zip.py")
    if os.path.isfile(rebuild_addon):
        import subprocess
        _log("重建插件 zip ...")
        subprocess.run([sys.executable, rebuild_addon], check=True)
    # 复用 build_exe.py 里已有的打包逻辑（含 PyInstaller 缓存）
    import build_exe
    build_exe.ensure_pylibs()
    exe = build_exe.build()
    _log("打包完成: %s" % exe)
    return exe


def build_once():
    _log("开始打包…")
    _build()


def watch(debounce=2.0):
    _log("开始打包（首次）…")
    _build()
    _log("正在监视 %d 个文件，改动后将自动重新打包（Ctrl+C 停止）"
         % _watched_count())

    last = snapshot()
    dirty = False
    dirty_since = 0.0
    while True:
        time.sleep(0.5)
        cur = snapshot()
        if cur == last:
            # 没有新改动：若之前有改动且已“安静”了 debounce 秒，就重建
            if dirty and time.time() - dirty_since >= debounce:
                dirty = False
                _log("检测到改动，开始重新打包…")
                try:
                    _build()
                except Exception as e:  # noqa: BLE001
                    _log("打包失败: %s" % e)
                last = snapshot()
            continue
        # 有新改动：记录快照并重置“安静”计时器
        last = cur
        dirty = True
        dirty_since = time.time()


def main(argv=None):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(
        description="打包或监视源码并自动重新打包 Database_Editor.exe")
    ap.add_argument("-w", "--watch", action="store_true",
                    help="监视源码，改动后自动重新打包")
    ap.add_argument("--debounce", type=float, default=2.0,
                    help="最后一次改动后等待多少秒再重建（默认 2 秒）")
    args = ap.parse_args(argv)
    try:
        if args.watch:
            watch(args.debounce)
        else:
            build_once()
    except KeyboardInterrupt:
        _log("已停止")


if __name__ == "__main__":
    main()
