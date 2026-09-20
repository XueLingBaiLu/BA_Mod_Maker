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
import shutil
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
    # 素材导入 + 「我的 bundle」管理器（自建独立 bundle）
    "mod_assets.py",
    "my_bundle.py",
    # ★ v1.8.112：这几项**以前漏在清单外** ⇒ 源码版一打开就 ModuleNotFoundError ✗
    #   （exe 版走 PyInstaller 自动分析，所以只有源码包坏 —— 最难发现的那种）
    #   回归：`测试\test_source_zip_completeness.py`（递归 import 分析 + 负向对照）✓
    "ui_fit.py",              # 窗口自适应 + 深色主题（ba_db_tool 模块级 import）
    "audio_ui.py",            # 音频对话框共用 UI 工具（audio_add/audio_import 用）
    "game_snapshot.py",       # 「游戏文件快照」对话框（v1.8.106 新增）
    "mod_checkup.py",         # 「Mod 自查」（v1.8.107 新增）
    # ★ v1.8.113：逆向事实钉板（F2 画质极性 / F3 命中盒轴序 / F4 方向装甲旁路 /
    #   F5 权威表 / F6 关联表 / F7 音频身份 / F8 机库规则 / F9 调用图陷阱）
    #   —— `mod_checkup` 成提示文案会 import 它；漏了源码版就在自查那一步 ImportError ✗
    "ba_knowledge.py",
    # ★ v1.8.113：`mod_paths.py`（游戏目录探测：Steam/多盘/中文路径）也被同一个
    #   `测试\test_source_zip_completeness.py` 抓出来了 —— 它是 `ba_db_tool.py` 的
    #   模块级 import ⇒ 源码版一启动就 ModuleNotFoundError ✗
    #   ⛔ 治本：本文件底部 `effective_files()` 现在会**自动补全递归 import 闭包**，
    #      以后新加模块不必再靠"记得改这份手写清单" ✓
    "mod_paths.py",           # 游戏目录探测（mod_paths.game_dir()）
    # 单一版本来源（改版本只动这一个文件）
    "version.py",
    # Blender 插件一键安装脚本（配套 BA_Mod_Maker_blender_addon.zip）
    "安装Blender插件.py",
    # 数据与资源
    "clean_baseline.json",
    "localization_map.json",
    "database_glossary.json",
    # ★★ 2026-09-20 单模板合并(甲)：`portrait_skeleton.bundle` **不再交付**
    #   —— 骨架（1×1 占位 Texture2D+Sprite+`Sprite(213)`）已烘进 `units_warehouse_small.bundle`
    #     （该件本清单里已有）⇒ 源码版用户照样"开箱能换图标" ✓
    # ★[配方-05] v1.8.118：数值 mod 面板（GUI）+ 它要用的黑名单快照
    #   ⛔ 快照是**数据**（28 个"不可覆盖的配置名"，随游戏版本变）⇒ 必须随包交付，
    #      否则产品侧只能说"这一步没查"（比假装通过好，但能带上就带上）✓
    "config_mod.py",
    "generate_glossary.py",
    "数据库词典.md",
    # 启动脚本
    "启动编辑器.bat",
    "Start Editor.bat",
    # 构建 exe 的源码与脚本
    "build_exe.py",
    "auto_build.py",
    "自动打包.bat",
    # ★ v1.8.112：`build_exe.py` 会调用它刷新发布目录/重打 zip ⇒ 源码包必须带上，
    #   否则源码用户改了日志后跑打包会在这一步找不到文件 ✗
    "_refresh_release.py",
    # ★ 同一次审计（判据⑤）还查出这三个也漏了 —— 都是**打包流水线的组成部分**，
    #   源码用户跑 `自动打包.bat` / `build_exe.py` 会走到 ✗
    "_rebuild_addon_zip.py",   # 重打 Blender 插件 zip
    "_sync_revtools.py",       # 把 _rev_tools 同步进插件/发布目录
    "clean_old_releases.py",   # 清理旧版本发布件
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

# ★ 治本：源码包的"可达模块"由**递归静态 import 分析**算出来，不再只靠上面那份手写清单。
#   为什么（2026-10 v1.8.112 / v1.8.113 连着两次踩坑）：手写清单靠人"记得加"✗ ⇒ 漏一个模块，
#   发布出去就是 **exe 版正常、源码版 ModuleNotFoundError 一崩到底**（最难发现的那种）。
#   现在的规矩：`FILES` 仍是"人声明的清单"（可读、可审计），但 `effective_files()`
#   会把**闭包补集**并进去 ⇒ 漏写只会漏在"声明"里，不会漏进"交付物"里 ✓
_SEEDS = ("ba_db_tool.py", "build_exe.py", "auto_build.py", "_refresh_release.py",
          "_rebuild_addon_zip.py", "_sync_revtools.py", "package_zip.py",
          "clean_old_releases.py", "generate_glossary.py", "安装Blender插件.py")


def _local_imports(path):
    """一个 .py 里 import 到的**顶层模块名**（含函数内 import —— 那些最容易漏）"""
    import ast
    try:
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
    except Exception:                                                 # noqa: BLE001
        return set()
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for al in n.names:
                out.add(al.name.split(".")[0])
        elif isinstance(n, ast.ImportFrom):
            if n.module and not n.level:
                out.add(n.module.split(".")[0])
    return out


def effective_files():
    """→ (实际打进包的清单, 自动补进去的那批)。

    清单 = `FILES` ∪ 所有种子模块的**本地 import 闭包**（`_SEEDS` 覆盖入口 + 整条打包流水线）。
    """
    have = set(f for f in os.listdir(HERE) if f.endswith(".py"))
    seen, todo = set(), [s for s in _SEEDS if os.path.isfile(os.path.join(HERE, s))]
    while todo:
        f = todo.pop()
        if f in seen:
            continue
        seen.add(f)
        for mod in _local_imports(os.path.join(HERE, f)):
            cand = mod + ".py"
            if cand in have and cand not in seen:
                todo.append(cand)
    declared = set(FILES)
    auto = sorted(seen - declared)
    return list(FILES) + auto, auto


def build_zip():
    out = os.path.join(HERE, ZIP_NAME)
    if os.path.exists(out):
        os.remove(out)
    files, auto = effective_files()
    if auto:
        print("  ★ 自动补全（递归 import 闭包，手写清单里漏声明的）：%s" % ", ".join(auto))
    added = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in files:
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
        # ★★ 2026-09-17：**排除 `__pycache__` / `.pyc`**（下批收紧项 1，判「误打包」）。
        #   为什么（四条判据）：① 源码 zip 的定位是"给源码、用户用自己的解释器跑"；
        #   而 `.pyc` 是**解释器专属**的 —— 本工作区自己就证明了这点：`_rev_tools\unitypy_path.py`
        #   写明有两份 `_unitypy`、二进制扩展按解释器编译（系统 3.14 用 `cp314`、Blender 3.13 用 `cp313`）；
        #   把 `cpython-314.pyc` 发给 3.13 用户**毫无用处**（CPython 校验魔数不符即重编译） ✗
        #   ② 它们的 **mtime 21:08:58–21:10:02** = 本机运行副产物，不是有意准备的交付内容；
        #   ③ 同一份文件里 `blender_addon` 段（L233）**早有同款过滤** ⇒ `_unitypy/PIL` 段**漏了**（本批实证：
        #      源码 zip 里 29 条 `.pyc`，而分发 zip 走 `_refresh_release.py` 的 `EXCLUDE_DIRS` = **0 条**）；
        #   ④ 更硬的后果：`os.walk` 收缓存 ⇒ **条目数随盘上缓存状态漂**
        #      （v1.12.1 的 `2261→2253` 里那 −9 就是盘上 `cpython-313` 的 9 个被清掉所致）⇒ **发版条目数不可复现** ✗
        pil_dir = os.path.join(HERE, "_unitypy", "PIL")
        if os.path.isdir(pil_dir):
            n = 0
            for root_dir, _dirs, files in os.walk(pil_dir):
                for fn in sorted(files):
                    if fn.endswith(".pyc") or "__pycache__" in root_dir:
                        continue
                    fp = os.path.join(root_dir, fn)
                    rel = os.path.relpath(fp, os.path.join(HERE, "_unitypy"))
                    z.write(fp, os.path.join("_unitypy", rel))
                    n += 1
                    added += 1
            print("  + _unitypy/PIL/* (%d files；已排除 __pycache__/.pyc)" % n)
        # _rev_tools 根目录下的工具脚本（模型 Mod 面板运行所需：mesh/catalog/CRC/换模型等）
        rev_dir = os.path.join(HERE, "_rev_tools")
        if os.path.isdir(rev_dir):
            for fn in sorted(os.listdir(rev_dir)):
                # ★ v1.8.118：`config_blocked.json`（[配方-05] 的黑名单快照）是**数据**不是脚本，
                #   ⛔ 漏了它 ⇒ 源码版的数值 mod 面板只能说"这一步没查"（`blocked_names()` 返回 None）✗
                if fn.endswith(".py") or fn == "ComputeBundleCrc.cs" or fn == "config_blocked.json":
                    z.write(os.path.join(rev_dir, fn), os.path.join("_rev_tools", fn))
                    added += 1
            print("  + _rev_tools/*.py（含 config_blocked.json 快照）")
        # ⛔ v1.8.115：**不再打包「任务工具」**（`.bascr` 任务文件 CLI + 586 KB 节点大典）
        #   —— 用户明确说明做任务 mod 用的是**游戏自带的场景编辑器**，这套用不上
        #   ⇒ 目录已删除，这里的分发规则一并去掉（想找回见 `_archive\任务工具_已移除_v1.8.114\`）✓
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

    # ★★ 2026-09-17 断言：**发布 zip 内不许出现任何 `__pycache__` / `.pyc`**（下批收紧项 1）。
    #   为什么用断言而不是"记得别写"：本批实测 `_unitypy/PIL` 段漏了过滤 ⇒ 29 条 `cpython-314.pyc`
    #   进了源码 zip、还把条目数从应有的 2224 顶到 2253（且随盘上缓存状态漂）✗
    #   ⇒ 把口径**在写盘后立刻锁死**：一旦将来有人再加一条 `os.walk` 忘了过滤，这里会当场红、不会静默发出。
    with zipfile.ZipFile(out) as _zf:
        bad = [n for n in _zf.namelist()
               if "__pycache__" in n or n.lower().endswith(".pyc")]
    if bad:
        print("⛔ 断言失败：发布 zip 内出现 %d 条 __pycache__/.pyc（源码包不许带字节码缓存）:" % len(bad))
        for n in bad[:10]:
            print("     -", n)
        if len(bad) > 10:
            print("     …（共 %d 条）" % len(bad))
        raise SystemExit(2)
    print("  ✓ 断言：zip 内无 __pycache__/.pyc（共 %d 条目）" % len(zipfile.ZipFile(out).namelist()))

    print("已生成:", out)
    # ★ v1.8.99：**顺手在工作区根也留一份**。
    #   交付体检（`动画模块/test_release_hygiene.py`）要求"发布件在工作区根有副本"
    #   （用户就是从这两个地方拿的），以前这一步靠手拷 ⇒ 忘了就假红 ✗ ⇒ 现在写进脚本里。
    root = os.path.dirname(os.path.dirname(HERE))
    if os.path.isdir(root) and os.path.abspath(root) != os.path.abspath(HERE):
        dst = os.path.join(root, ZIP_NAME)
        try:
            shutil.copy2(out, dst)
            print("已复制到工作区根:", dst)
        except OSError as e:
            print("⚠ 复制到工作区根失败（不影响本次打包）:", e)
    return out


if __name__ == "__main__":
    build_zip()
