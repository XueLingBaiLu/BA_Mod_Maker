# -*- coding: utf-8 -*-
"""Rebuild the standalone Windows build (no Python required).

Usage:  python build_exe.py

Downloads PyInstaller wheels from PyPI into a local cache (no pip needed),
extracts them, and builds the onedir distribution in
./dist/Database_Editor_v<VERSION>/ (the folder name carries the version).

★ ① v1.12.1：本脚本在打包前会生成 **Win32 版本资源**（`write_version_file()` ⇒
  `_build\\version_info.txt`，**不进发布件**）并作为 `--version-file` 交给 PyInstaller。
  字段**只从 `version.py` 的 `APP_VERSION` 现算**（单一真相），`CompanyName` 留空。
  ⇒ 出件期判据：`(Get-Item exe).VersionInfo.FileVersion -eq <APP_VERSION>`
  （落在 `动画模块\\test_release_hygiene.py` §4b）。
"""

import html
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(HERE, "_build")
CACHE = os.path.join(BUILD, "cache")
PYLIBS = os.path.join(BUILD, "pylibs")
DIST = os.path.join(BUILD, "dist")
WORK = os.path.join(BUILD, "tmp")
RELEASE = HERE  # the BA Mod Maker project directory
from version import APP_VERSION  # 单一版本来源（version.py）
VERSION = APP_VERSION
APP_NAME = "BA_Mod_Maker_v" + VERSION
#: ★ ① v1.12.1：PyInstaller 的 `--version-file`（Win32 版本资源）。
#: 生成物落 `_build\`，**不进发布件**（只被 PyInstaller 读一次）。
VERSION_FILE = os.path.join(BUILD, "version_info.txt")

WHEELS = [
    # (package name on PyPI, wheel filename filter)
    ("pyinstaller", "py3-none-win_amd64.whl"),
    ("pyinstaller-hooks-contrib", "py3-none-any.whl"),
    ("altgraph", "py3-none-any.whl"),
    ("pefile", "py3-none-any.whl"),
    ("pywin32-ctypes", "py3-none-any.whl"),
    ("packaging", "py3-none-any.whl"),
    ("setuptools", "py3-none-any.whl"),
]


# Index mirrors and file mirrors — try in order (pypi.org first, then
# Chinese mirrors, which are faster/more reliable on some networks).
_INDEX_MIRRORS = [
    "https://pypi.org/simple/%s/",
    "https://pypi.tuna.tsinghua.edu.cn/simple/%s/",
    "https://mirrors.aliyun.com/pypi/simple/%s/",
]
_FILE_MIRRORS = [
    ("https://files.pythonhosted.org", "https://pypi.tuna.tsinghua.edu.cn/packages"),
    ("https://pypi.tuna.tsinghua.edu.cn/packages", "https://mirrors.aliyun.com/pypi/packages"),
]


def _urlopen(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=timeout)


def _simple_index(name):
    last_err = None
    for tmpl in _INDEX_MIRRORS:
        try:
            with _urlopen(tmpl % name, 60) as r:
                page = r.read().decode("utf-8")
            return re.findall(r'href="([^"]+)"', page)
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise last_err if last_err else RuntimeError("no mirror available")


def _ver_key(filename):
    m = re.search(r"-(\d+(?:\.\d+)+)(?:-|\.)", filename)
    if not m:
        return (0,)
    return tuple(int(x) for x in m.group(1).split("."))


def _download(url, dest):
    if not url.startswith("http"):
        url = "https://pypi.org" + url
    fn = url.split("/")[-1].split("#")[0]
    path = os.path.join(dest, fn)
    if os.path.exists(path):
        print("cached:", fn)
        return path
    candidates = [url]
    for old, new in _FILE_MIRRORS:
        if url.startswith(old):
            candidates.append(new + url[len(old):])
    last_err = None
    for cand in candidates:
        try:
            print("downloading:", fn)
            with _urlopen(cand, 600) as r, open(path, "wb") as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
            return path
        except Exception as e:  # noqa: BLE001
            last_err = e
            print("download failed (%s), trying next mirror" % e)
    raise last_err if last_err else RuntimeError("download failed")


def ensure_pylibs():
    os.makedirs(CACHE, exist_ok=True)
    os.makedirs(PYLIBS, exist_ok=True)
    if os.path.exists(os.path.join(PYLIBS, "PyInstaller", "__init__.py")):
        print("PyInstaller already present")
        return
    for name, pattern in WHEELS:
        links = _simple_index(name)
        cands = sorted([html.unescape(l) for l in links if pattern in l],
                       key=_ver_key)
        if not cands:
            raise SystemExit("no wheel found for %s (%s)" % (name, pattern))
        wheel = _download(cands[-1], CACHE)
        with zipfile.ZipFile(wheel) as z:
            z.extractall(PYLIBS)
    print("wheels extracted to", PYLIBS)


def _ver_quad(v):
    r"""`"1.12.1"` → `(1, 12, 1, 0)`（Win32 的 `filevers`/`prodvers` 必须是 4 个整数）。"""
    parts = [int(x) for x in re.findall(r"\d+", v)][:4]
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts)


def write_version_file(path=None, version=None):
    r"""★ ① v1.12.1：生成 PyInstaller 的 `--version-file`（Win32 版本资源）。

    ⛔ 字段**只从 `version.py` 的 `APP_VERSION` 现算**（单一真相）—— 不在这里写死版本号，
      否则改了 `version.py` 而资源没跟着变 ⇒ "属性里显示的版本 ≠ 实际版本"（最难查的那类）。
    ★ `CompanyName` **留空**（用户口径：比编一个中性名更不易错）。
    生成物落 `_build\`，**不进发布件**（PyInstaller 只读一次，不是交付内容）。
    ⇒ 出件期判据：`(Get-Item exe).VersionInfo.FileVersion -eq <APP_VERSION>`。
    """
    ver = version or VERSION
    path = path or VERSION_FILE
    quad = _ver_quad(ver)
    fields = [
        ("CompanyName", ""),                        # ← 留空（用户口径）
        ("FileDescription", "BA_Mod Maker"),
        ("FileVersion", ver),                       # ← 判据读的就是这一格
        ("InternalName", "BA_Mod_Maker"),
        ("OriginalFilename", "BA_Mod_Maker_v%s.exe" % ver),
        ("ProductName", "BA_Mod Maker"),
        ("ProductVersion", ver),
    ]
    body = ",\n         ".join("StringStruct(%r, %r)" % (k, v) for k, v in fields)
    text = (
        "# -*- coding: utf-8 -*-\n"
        "# ★ 由 build_exe.py 的 write_version_file() 自动生成 —— ⛔ 别手改（改 version.py 即可）。\n"
        "VSVersionInfo(\n"
        "  ffi=FixedFileInfo(\n"
        "    filevers=%r,\n"
        "    prodvers=%r,\n"
        "    mask=0x3f,\n"
        "    flags=0x0,\n"
        "    OS=0x40004,\n"
        "    fileType=0x1,\n"
        "    subtype=0x0,\n"
        "    date=(0, 0)\n"
        "  ),\n"
        "  kids=[\n"
        "    StringFileInfo([\n"
        "      StringTable(\n"
        "        '040904B0',\n"
        "        [%s])\n"
        "    ]),\n"
        "    VarFileInfo([VarStruct('Translation', [1033, 1200])])\n"
        "  ]\n"
        ")\n" % (quad, quad, body)
    )
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return path


def strip_pycache(root, dry_run=False):
    r"""★ 2026-09-17（裁定 ①(a)）：删掉 `root` 下所有 **名为 `__pycache__`** 的目录子树。

    ⇒ `(目录数, 文件数)`（`dry_run=True` 时只数不删，供验证脚本使用）。

    ⛔ 判据**只认目录名** `__pycache__`：不碰任何其它文件 —— 尤其**不碰"多出来的文件"**
    （"把不认识的一律清掉"是**假绿**：它会把真内容也删掉而看不出问题）。负向对照见
    `_scratch\_strip_verify\`（那里放一个**真多余文件**，必须**不被删**）。
    """
    ndirs = nfiles = 0
    for dp, dns, _fns in os.walk(root):
        for d in list(dns):
            if d != "__pycache__":
                continue
            target = os.path.join(dp, d)
            n = sum(len(f) for _r, _dd, f in os.walk(target))
            ndirs += 1
            nfiles += n
            if not dry_run:
                shutil.rmtree(target, ignore_errors=True)
            dns.remove(d)
    return ndirs, nfiles


def build():
    shutil.rmtree(DIST, ignore_errors=True)
    shutil.rmtree(WORK, ignore_errors=True)
    vf = write_version_file()                 # ★ ① 先生成 Win32 版本资源（字段取自 version.py）
    print("version resource:", vf)
    env = dict(os.environ)
    env["PYTHONPATH"] = PYLIBS + os.pathsep + env.get("PYTHONPATH", "")
    cmd = [
        sys.executable, "-X", "utf8", "-m", "PyInstaller",
        "--noconfirm", "--onedir", "--windowed",
        "--name", APP_NAME,
        # ★ ① v1.12.1：Win32 版本资源（`VersionInfo.FileVersion` = APP_VERSION）
        #   —— 出件期判据在 `动画模块\test_release_hygiene.py` §3c；字段由 version.py 现算
        "--version-file", vf,
        # 让 PyInstaller 分析 UnityPy 及其依赖（fsspec 等），否则打包版会缺
        # stdlib 模块（如 concurrent.futures / urllib / asyncio），运行时 import UnityPy 报
        # "No module named 'concurrent'"。
        "--paths", os.path.join(HERE, "_unitypy"),
        "--hidden-import", "concurrent.futures",
        "--hidden-import", "configparser",
        "--hidden-import", "importlib.metadata",
        "--hidden-import", "importlib.resources",
        "--hidden-import", "packaging",
        "--hidden-import", "asyncio",
        "--hidden-import", "urllib.request",
        "--hidden-import", "urllib.parse",
        "--hidden-import", "urllib.error",
        "--hidden-import", "http.client",
        "--hidden-import", "http.server",
        "--hidden-import", "ftplib",
        "--hidden-import", "netrc",
        # ★⑥ 2026-10-16：`my_bundle` 里是**函数内** `import addr_limits`（地址命名辅助）
        #   —— 函数内 import 一旦没被收进 exe，用户点「📏 查 DB 原值」就是 ImportError，
        #   而外层的按钮看着完全正常 ✗ ⇒ 显式声明（同 `_rev_tools` 那批的教训）
        "--hidden-import", "addr_limits",
        "--add-data", os.path.join(HERE, "clean_baseline.json") + ";.",
        "--add-data", os.path.join(HERE, "localization_map.json") + ";.",
        "--add-data", os.path.join(HERE, "icons") + ";icons",
        "--add-data", os.path.join(HERE, "UABEADump") + ";UABEADump",
        # Bundle the full UnityPy runtime (_unitypy, 含 PIL) + the .bamod
        # import tool scripts（mod_assets 运行时把它们加进 sys.path）。
        # They land under _internal/<dir>。
        "--add-data", os.path.join(HERE, "_unitypy") + ";_unitypy",
        "--add-data", os.path.join(HERE, "_rev_tools", "catalog_mod.py") + ";_rev_tools",
        "--add-data", os.path.join(HERE, "_rev_tools", "update_crc.py") + ";_rev_tools",
        "--add-data", os.path.join(HERE, "_rev_tools", "compute_bundle_crc.py") + ";_rev_tools",
        # 素材导入（.bamod 包合并 + CRC）
        "--add-data", os.path.join(HERE, "_rev_tools", "import_pack.py") + ";_rev_tools",
        "--add-data", os.path.join(HERE, "_rev_tools", "pack_model.py") + ";_rev_tools",
        "--add-data", os.path.join(HERE, "_rev_tools", "skin_data.py") + ";_rev_tools",
        # ★★ v1.9.1（★⑮）：流式贴图（导出流式贴图 PNG + 生成"流式引用"条目）
        "--add-data", os.path.join(HERE, "_rev_tools", "tex_stream.py") + ";_rev_tools",
        "--add-data", os.path.join(HERE, "_rev_tools", "finalize_crc.py") + ";_rev_tools",
        "--add-data", os.path.join(HERE, "_rev_tools", "stream_save.py") + ";_rev_tools",
        # v1.8.75：自动备份开关（默认关）——import_pack / stream_save 都 import 它
        "--add-data", os.path.join(HERE, "_rev_tools", "backup_policy.py") + ";_rev_tools",
        # 「我的 bundle」管理器（自建独立 bundle + catalog 新条目/地址）
        "--add-data", os.path.join(HERE, "_rev_tools", "my_bundle_catalog.py") + ";_rev_tools",
        # ★ [界面-04] v1.12.2（2026-09-18）：`prefab ↔ 映射路径` 双向检索的**唯一正本** ——
        #   `my_bundle.py`（GUI 那栏）与 `技术资料\scripts\bundle_asset_lookup.py`（CLI 薄壳）都 import 它；
        #   ⛔ 与 `auto_build.py` 的 WATCH_FILES **成对**（配对由 `测试\test_revtools_wiring.py` 守着）
        "--add-data", os.path.join(HERE, "_rev_tools", "bundle_asset_lookup_core.py") + ";_rev_tools",
        # ★ v1.8.113：bundle 路径**按 glob 解析**（包名里带内容哈希，游戏更新就变；
        #   写死名字的 `finalize_crc.DEFAULT_BUNDLE` 已经指向不存在的文件 ✗）
        #   ⛔ 新增 `_rev_tools/*.py` 必须同时加进这里与 auto_build.py 的 WATCH_FILES，
        #      否则 exe 里静默退回旧行为（KB 记过的真实踩坑；`测试\test_revtools_wiring.py` 守着这条）
        "--add-data", os.path.join(HERE, "_rev_tools", "bundle_paths.py") + ";_rev_tools",
        # ★★ v1.8.113：`测试\test_revtools_wiring.py` 按"入口 import 闭包"审出来的
        #    **8 个漏项**（全都不依赖 bpy，属于该进 exe 的运行时模块）：
        #      `import_pack` → `skin_data` → `extract_model` / `unitypy_path`
        #      `import_pack` → `build_turret_direct` → `turret_swap`
        #      `import_pack` → `hub_edit` → `behavior_codec` → `behavior_meta`
        #      `stream_save` → `fix_dangling_scripts`
        #    ⛔ 以前这些靠**函数内 import + try/except 兜底** ⇒ exe 里静默退回旧行为
        #       （"修复看起来没生效"，不报错）—— 这正是 KB 记过的那类踩坑 ✗
        "--add-data", os.path.join(HERE, "_rev_tools", "unitypy_path.py") + ";_rev_tools",
        "--add-data", os.path.join(HERE, "_rev_tools", "extract_model.py") + ";_rev_tools",
        "--add-data", os.path.join(HERE, "_rev_tools", "hub_edit.py") + ";_rev_tools",
        "--add-data", os.path.join(HERE, "_rev_tools", "behavior_meta.py") + ";_rev_tools",
        "--add-data", os.path.join(HERE, "_rev_tools", "behavior_codec.py") + ";_rev_tools",
        "--add-data", os.path.join(HERE, "_rev_tools", "build_turret_direct.py") + ";_rev_tools",
        "--add-data", os.path.join(HERE, "_rev_tools", "turret_swap.py") + ";_rev_tools",
        "--add-data", os.path.join(HERE, "_rev_tools", "fix_dangling_scripts.py") + ";_rev_tools",
        # ★★ ★⑳ v1.12.1（`[配方-04]` 产品化）：**「补后坐力节点」GUI 入口的最小闭包**。
        #   以前 `add_recoil_point.py` **只在源码包里** ⇒ 只装 exe 的用户**跑不了**这条配方
        #   （exe 是 `--windowed`、没有控制台，光塞文件没入口 = 无效分发）。
        #   现在由 `recoil_mod.py`（GUI）在**进程内**调用 `add_recoil_point.run_recipe()`，
        #   所以这三只要跟 `_internal\_rev_tools` 一起随包：
        #     `add_recoil_point` → `add_turret`（克隆机器 + `_default_bundle()`）→ `component_edit`
        #   （`build_turret_direct` / `unitypy_path` / `stream_save` 本来就在上面 ✓）
        #   ⛔ 这两份清单（本文件 + `auto_build.py` 的 WATCH_FILES）由
        #      `测试\test_revtools_wiring.py` 按"入口 import 闭包"守着，漏一边就报红。
        "--add-data", os.path.join(HERE, "_rev_tools", "add_recoil_point.py") + ";_rev_tools",
        "--add-data", os.path.join(HERE, "_rev_tools", "add_turret.py") + ";_rev_tools",
        "--add-data", os.path.join(HERE, "_rev_tools", "component_edit.py") + ";_rev_tools",
        # ★★ P-1（2026-09-17）：**实机验证探针**随包（源在 `tools\realtest\`，**不在产品树内**
        #   ⇒ 两 zip 与 exe 都不会自动带 ⇒ 只装 exe 的用户照《装机说明·附加节》去跑会**找不到脚本** ✗）。
        #   ⛔ 与 `auto_build.py` 的 `WATCH_FILES` **成对**（漏一边 = exe 里静默留旧探针）；
        #      `测试\test_revtools_wiring.py` 的"入口 import 闭包"守的是入口链路，这三支是**独立 CLI**，
        #      不进闭包，因此**必须靠这两处显式列举**（本注释即为此而写）。
        "--add-data", os.path.join(HERE, "_rev_tools", "probe_coord_label_align.py") + ";_rev_tools",
        "--add-data", os.path.join(HERE, "_rev_tools", "probe_gui_grid_collisions.py") + ";_rev_tools",
        "--add-data", os.path.join(HERE, "_rev_tools", "probe_mybundle_grid_overlap.py") + ";_rev_tools",
        # ★⑤ v1.8.118：肖像/图标骨架（往自建包里预置 1×1 占位 Texture2D+Sprite + 补 Sprite(213) 类型）
        "--add-data", os.path.join(HERE, "_rev_tools", "image_skeleton.py") + ";_rev_tools",
        # ★B v1.8.119：改包后自检（内部名/大小 + 谁依赖我动的 CAB）—— `my_bundle` 函数内 import 它
        "--add-data", os.path.join(HERE, "_rev_tools", "cab_check.py") + ";_rev_tools",
        # ★[配方-05] v1.8.118：数值 mod → 场景 configs.zip（打包 / 放进场景 / 出货前自查）
        "--add-data", os.path.join(HERE, "_rev_tools", "config_override.py") + ";_rev_tools",
        #   黑名单快照（28 个"不可覆盖的配置名"）是**数据**，不带上就只能说"这一步没查" ✗
        "--add-data", os.path.join(HERE, "_rev_tools", "config_blocked.json") + ";_rev_tools",
        # ★ v1.8.113：O4 等长替换快路径（字节手术 + 复用原压缩块；`import_pack` 用它）
        "--add-data", os.path.join(HERE, "_rev_tools", "inplace_surgery.py") + ";_rev_tools",
        "--add-data", os.path.join(HERE, "units_warehouse_small.bundle") + ";.",
        # ★★ 2026-09-20 单模板合并(甲)：**不再交付独立的** `portrait_skeleton.bundle`
        #   —— 它的 1×1 占位图标（Texture2D+Sprite+`Sprite(213)`）已**烘进**上面那个
        #   `units_warehouse_small.bundle` ⇒ ⛔ 这行已删（交付件少一件）✓
        "--distpath", DIST,
        "--workpath", WORK,
        "--specpath", WORK,
        os.path.join(HERE, "ba_db_tool.py"),
    ]
    print("building…")
    subprocess.run(cmd, env=env, check=True)

    # Move the onedir folder out of dist/ into the release (root) directory.
    src_dir = os.path.join(DIST, APP_NAME)
    dst_dir = os.path.join(RELEASE, APP_NAME)
    # 保留用户放在发布目录里的 model/（Blender 导出的 .bamod 输出目录），
    # 避免重建 exe 时把用户的模型包一起删掉。
    model_dir = os.path.join(dst_dir, "model")
    preserved_model = None
    if os.path.isdir(model_dir):
        preserved_model = os.path.join(RELEASE, ".model_preserved_tmp")
        if os.path.exists(preserved_model):
            shutil.rmtree(preserved_model, ignore_errors=True)
        shutil.move(model_dir, preserved_model)
    if os.path.exists(dst_dir):
        shutil.rmtree(dst_dir, ignore_errors=True)
    shutil.move(src_dir, dst_dir)
    if preserved_model:
        shutil.move(preserved_model, model_dir)
    shutil.rmtree(DIST, ignore_errors=True)  # leave no dist/ folder behind

    # Ship the user-facing docs next to the exe (also inside the zip).
    for doc in ("Change Log.txt", "README.md", "数据库词典.md", "Blender工具词典.md",
                "使用教程-BA_Mod_Maker.md", "使用教程-Blender插件.md"):
        src = os.path.join(HERE, doc)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(dst_dir, doc))

    # Ship the Blender addon: the ready-to-install zip + the raw addon folder.
    addon_zip = os.path.join(HERE, "BA_Mod_Maker_blender_addon.zip")
    if os.path.isfile(addon_zip):
        shutil.copy2(addon_zip, os.path.join(dst_dir, "BA_Mod_Maker_blender_addon.zip"))
    addon_dir = os.path.join(HERE, "blender_addon")
    if os.path.isdir(addon_dir):
        shutil.copytree(addon_dir, os.path.join(dst_dir, "blender_addon"))

    # ⛔ v1.8.115：**不再分发「任务工具」**（`.bascr` 任务文件的 CLI）。
    #   用户明确说明：**做任务 mod 用的是游戏自带的场景编辑器**，这套命令行工具用不上
    #   ⇒ 目录已删除、`package_zip.py` 的同名规则也一并去掉（原来是"带上节点大典 586 KB"）。
    #   想找回：`_archive\任务工具_已移除_v1.8.114\`（含 README 与自带的那个测试）✓

    # Ship the extracted game icons (unit/weapon/ammo/spec/indicator) as a
    # sibling folder. Portraits (~470 MB) are skipped to keep the zip lean —
    # the unit browser and relation tree only need the small icon categories.
    icon_src = os.path.join(HERE, "icons_extracted")
    if os.path.isdir(icon_src):
        icon_dst = os.path.join(dst_dir, "icons_extracted")
        for cat in ("units", "weapons", "ammo", "options", "specs", "indicators"):
            s = os.path.join(icon_src, cat)
            if os.path.isdir(s):
                shutil.copytree(s, os.path.join(icon_dst, cat))

    # ★★ 2026-09-17（裁定 ①(a)，押下批）：**发布目录里不许带 `__pycache__`**。
    #   为什么放在**这里**（zip 之前、所有 copytree 之后）：上面那几段会把文档、`blender_addon\`（**整体
    #   copytree，连带它的 `__pycache__`**）、`icons_extracted\` 复制进发布目录 ⇒ 清早了会被重新带回来 ✗。
    #   为什么清：`--add-data _unitypy;_unitypy` 把整个 `_unitypy` 带进 `_internal`，
    #   于是发布目录盘上有 **357 条** `__pycache__` —— 分发 zip 走 `_refresh_release.EXCLUDE_DIRS` 会排除它们，
    #   ⇒ **口径分叉**：磁盘上有、包里没有；于是「冻结监测集排除 `**\__pycache__\*`」与"交付面"对不上，
    #   而 `tools\release\export_pack_surface.py --verify` 会报「多余 29」。清掉即三侧口径一致。
    #   ⛔ 只删**目录名为 `__pycache__`** 的子树（⛔ 不是"删所有不认识的文件"——那种做法会把真内容一起删掉，
    #     属"排除一切"式假绿；负向对照见 `_scratch\_strip_verify\`：放一个**真多余文件**必须不被删）。
    stripped = strip_pycache(dst_dir)
    if stripped:
        print("  ✓ 已清除发布目录内 __pycache__：%d 个目录 / %d 个文件（口径对齐 ①(a)）"
              % (stripped[0], stripped[1]))

    # Zip the moved folder in the release directory.
    zip_base = os.path.join(RELEASE, APP_NAME)
    zip_path = shutil.make_archive(zip_base, "zip", RELEASE, APP_NAME)
    # ★ v1.8.99：发布 zip 也**在工作区根留一份**（交付体检要求；以前靠手拷 ⇒ 忘了就假红 ✗）
    _root = os.path.dirname(os.path.dirname(HERE))
    if os.path.isdir(_root) and os.path.abspath(_root) != os.path.abspath(HERE):
        try:
            shutil.copy2(zip_path, os.path.join(_root, os.path.basename(zip_path)))
            print("root copy:", os.path.join(_root, os.path.basename(zip_path)))
        except OSError as e:
            print("⚠ root copy failed (not fatal): %s" % e)

    # ★ v1.8.97：**打完包后再自检一次"发布目录里的副本是不是最新的"**。
    #   打包会把当时的插件 zip / Change Log 复制进发布目录；如果之后又改了插件或日志，
    #   发出去的包里就是旧文件（2026-10 真遇到：修完插件才发现发布目录还是旧的 ✗）。
    refresh = os.path.join(HERE, "_refresh_release.py")
    if os.path.isfile(refresh):
        import subprocess as _sp
        r = _sp.run([sys.executable, refresh], capture_output=True, text=True,
                    encoding="utf-8", errors="replace")
        out = (r.stdout or "") + (r.stderr or "")
        for ln in out.strip().splitlines():
            print("[refresh] " + ln)
        if r.returncode != 0:
            print("[refresh] ⚠ 自检未通过，请检查发布目录内容")

    exe = os.path.join(dst_dir, APP_NAME + ".exe")
    print("done:", exe)
    print("zipped:", zip_path)
    return exe


if __name__ == "__main__":
    try:                                    # GUI/无控制台环境 sys.stdout 可能是 None
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ensure_pylibs()
    build()
