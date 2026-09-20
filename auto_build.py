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
  - 打包前会**提醒一句**"当前版本跑过发版分层检查没有"（只读台账，不阻塞、不影响打包；
    分层规则见 `.re-kb\tools\release-check-tiers.md`：小版本 ~5 秒 / 中版本 ~4.5 分钟 /
    大版本 ~12 分钟 + 实机）。
"""

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# 参与打包的源文件（改动任意一个都触发重建）
WATCH_FILES = [
    "version.py",
    "_sync_revtools.py",
    "ba_db_tool.py",
    "ba_crypto.py",
    "ba_aes.py",
    "ba_glossary.py",
    "i18n.py",
    # 素材导入（.bamod → bundle）依赖：改这里必须触发重建
    "mod_assets.py",
    # 「我的 bundle」管理器（自建独立 bundle + catalog 新条目/地址）
    "my_bundle.py",
    # ★⑥ v1.8.118：地址命名辅助（DB 字段原值 + 字符数 + 定长回填校验）
    #   —— `my_bundle` 函数内 import 它，改了不重建 ⇒ exe 里还是旧提示 ✗
    "addr_limits.py",
    # ★ v1.8.112：共用 UI 工具（深色主题 / 窗口自适应）与对话框 —— 改了必须重建，
    #   否则 exe 里还是"② ④ 白底"的旧版 ✗
    "ui_fit.py",
    "audio_ui.py",
    "game_snapshot.py",
    "mod_checkup.py",
    # ★ v1.8.113：逆向事实钉板（F2~F9）—— `mod_checkup` 会 import 它成提示文案，
    #   改了不重建 ⇒ exe 里还是**旧极性/旧轴序**的提示（这正是 F2 那类"文案与事实脱节"的复发路径）✗
    "ba_knowledge.py",
    "_rev_tools/my_bundle_catalog.py",
    # ★★ [界面-04] v1.12.2（2026-09-18）：`prefab ↔ 映射路径` 双向检索的**唯一正本**
    #   —— `my_bundle.py` 的 GUI 栏与 `技术资料\scripts\bundle_asset_lookup.py`（CLI 薄壳）都 import 它；
    #   ⛔ 与 `build_exe.py` 的 `--add-data` **成对**：只加一边 ⇒ exe 里静默退回旧行为
    #   （配对由 `测试\test_revtools_wiring.py` 守着）
    "_rev_tools/bundle_asset_lookup_core.py",
    # ★★ 2026-09-20 单模板合并(甲)：GUI 那枚"切换骨架包"按钮已删 ⇒ **运行期不再 import** 本模块；
    #   它仍是"造骨架/验骨架"的**工具**（测试与人工复核用）⇒ 照旧随包
    #   （⛔ 与 build_exe 的 --add-data 成对，配对由 `测试	est_revtools_wiring.py` 守着）
    "_rev_tools/image_skeleton.py",
    # ★B v1.8.119：改包后自检（内部名/CAB 依赖）—— ④ 收尾那枚按钮会 import 它
    "_rev_tools/cab_check.py",
    # ★[配方-05] v1.8.118：数值 mod → 场景 configs.zip（面板 + 打包/自查模块 + 黑名单快照）
    "config_mod.py",
    "_rev_tools/config_override.py",
    "_rev_tools/config_blocked.json",
    # ★ v1.8.113：bundle 路径按 glob 解析（包名带内容哈希，游戏更新就变）
    #   ⛔ 与 build_exe.py 的 --add-data 成对：只加一边 = exe 里静默退回旧行为
    "_rev_tools/bundle_paths.py",
    # ★★ v1.8.113：按 import 闭包审出来的 8 个漏项 + 1 个"有 add-data 却不在监视里"
    #   （`skin_data` 改了不触发重建 ⇒ exe 里是新代码 + 旧模块）✗
    "_rev_tools/skin_data.py",
    # ★★ v1.9.1（★⑮）：流式贴图（Python 侧 exe 也用它导流式贴图）
    "_rev_tools/tex_stream.py",
    "_rev_tools/unitypy_path.py",
    "_rev_tools/extract_model.py",
    "_rev_tools/hub_edit.py",
    "_rev_tools/behavior_meta.py",
    "_rev_tools/behavior_codec.py",
    "_rev_tools/build_turret_direct.py",
    "_rev_tools/turret_swap.py",
    "_rev_tools/fix_dangling_scripts.py",
    # ★★ ★⑳ v1.12.1（`[配方-04]` 产品化）：GUI 入口 `recoil_mod.py` 的 import 闭包
    #   （`add_recoil_point` → `add_turret` → `component_edit`）——
    #   ⛔ 漏进这份清单的后果是"改了它**不会触发重建**"，exe 里永远是新代码 + 旧模块 ✗
    #   （`测试\test_revtools_wiring.py` 判据③ 守着这条）
    "_rev_tools/add_recoil_point.py",
    "_rev_tools/add_turret.py",
    "_rev_tools/component_edit.py",
    # ★★ P-1（2026-09-17）：**实机验证探针**要随包进 `_rev_tools\`，与 `build_exe.py` 的
    #   `--add-data` **成对** —— 装机说明的《附加节》会让用户跑这三支，
    #   而 `tools\realtest\` **不在产品树内** ⇒ 不搬进 `_rev_tools\` 的话，**两 zip 与 exe 都不会带它**
    #   ⇒ 只装 exe 的用户照文档去跑会**找不到脚本**（= `S17-02` 那类问题的翻版）✗
    #   ⛔ 改了不重建 = exe 里仍是旧探针 ⇒ 这份清单必须同步（`测试\test_revtools_wiring.py` 判据③ 守）
    "_rev_tools/probe_coord_label_align.py",
    "_rev_tools/probe_gui_grid_collisions.py",
    "_rev_tools/probe_mybundle_grid_overlap.py",
    # ★ v1.8.113（O4）：等长替换快路径 —— `import_pack` 的保存分支用它
    "_rev_tools/inplace_surgery.py",
    "units_warehouse_small.bundle",
    "_rev_tools/import_pack.py",
    "_rev_tools/pack_model.py",
    "_rev_tools/finalize_crc.py",
    "_rev_tools/stream_save.py",
    # v1.8.75：自动备份开关（默认关）—— 改了必须重建，否则 exe 里还是旧的备份行为
    "_rev_tools/backup_policy.py",
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


def _note_release_check():
    """★ 打包前提醒一句"这版跑过发版分层检查没有"（**绝不阻塞、0 成本**）。

    为什么要这条：一键发版（`publish_all.ps1`）已在打包前**挡**住没检查的版本，
    但"自动打包"这条路会先产出**用户手上的 exe/zip** ⇒ 在这里提一句，
    免得出现"exe 已经发出去了、检查还没跑"。判据只读台账
    （`tools\release\release_history.json`），不重跑任何检查、失败也绝不影响打包。
    分层规则见 `.re-kb\tools\release-check-tiers.md`。
    """
    try:
        import io
        import json
        import re

        ws = HERE
        for _ in range(6):
            if os.path.isdir(os.path.join(ws, ".re-kb")):
                break
            ws = os.path.dirname(ws)
        if not os.path.isfile(os.path.join(ws, "tools", "release", "release_tier.py")):
            return

        ver = None
        try:
            txt = io.open(os.path.join(HERE, "version.py"), encoding="utf-8",
                          errors="replace").read()
            m = re.search(r"""APP_VERSION\s*=\s*["']([^"']+)""", txt)
            ver = m.group(1) if m else None
        except OSError as e:
            _log("（发版检查提醒跳过：读不到 version.py —— %s）" % e)
        if not ver:
            _log("（发版检查提醒跳过：version.py 里没解析出 APP_VERSION）")
            return

        ledger = os.path.join(ws, "tools", "release", "release_history.json")
        hits = []
        if os.path.isfile(ledger):
            try:
                hits = [e for e in json.load(io.open(ledger, encoding="utf-8"))
                        if e.get("version") == ver]
            except (OSError, ValueError):
                hits = []
        if hits:
            e = hits[-1]
            _log("发版检查：v%s 已跑过【%s】%s（%s）" % (
                ver, e.get("tier_label", "?"),
                "✓ 通过" if e.get("result") == "pass" else "✗ 失败",
                e.get("date", "?")))
            if e.get("result") != "pass":
                _log("   ⛔ 上一轮检查是**失败**的 ⇒ 先修再打包/发版")
        else:
            _log("提醒：v%s 还没跑过发版分层检查 ⇒ 打包完双击 发版检查.bat（或 "
                 "python tools\\release\\release_tier.py --run）" % ver)
            _log("      预算：小版本 ~5 秒 / 中版本 ~4.5 分钟 / 大版本 ~12 分钟 + 实机")
    except Exception as e:                                     # noqa: BLE001
        _log("（发版检查提醒跳过：%s）" % e)


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
    # 先把 _rev_tools 两份同步（addon 是唯一源头）：
    # 根目录那份会进 exe，不同步就会出现「插件新、exe 旧」的静默分叉 ✗
    sync = os.path.join(HERE, "_sync_revtools.py")
    if os.path.isfile(sync):
        import subprocess
        _log("同步 _rev_tools …")
        subprocess.run([sys.executable, sync], check=True)
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
    # 清理旧版本发布件：只保留当前版本的目录 + zip。
    # ⛔ 别靠人记得 —— 每版各 ~115MB 会堆满工作区，而且旧 exe 摆在旁边极易被误运行
    #（用户就踩过"跑的是旧 exe ⇒ 修复看起来没生效"）。失败不影响本次打包结果。
    cleaner = os.path.join(HERE, "clean_old_releases.py")
    if os.path.isfile(cleaner):
        import subprocess
        try:
            subprocess.run([sys.executable, cleaner], check=False)
        except OSError as e:
            _log("清理旧版本失败（不影响打包）: %s" % e)
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
    try:                                    # GUI/无控制台环境 sys.stdout 可能是 None
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(
        description="打包或监视源码并自动重新打包 Database_Editor.exe")
    ap.add_argument("-w", "--watch", action="store_true",
                    help="监视源码，改动后自动重新打包")
    ap.add_argument("--debounce", type=float, default=2.0,
                    help="最后一次改动后等待多少秒再重建（默认 2 秒）")
    args = ap.parse_args(argv)
    _note_release_check()
    try:
        if args.watch:
            watch(args.debounce)
        else:
            build_once()
    except KeyboardInterrupt:
        _log("已停止")


if __name__ == "__main__":
    main()
