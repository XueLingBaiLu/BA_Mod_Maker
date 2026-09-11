# -*- coding: utf-8 -*-
"""Rebuild the standalone Windows build (no Python required).

Usage:  python build_exe.py

Downloads PyInstaller wheels from PyPI into a local cache (no pip needed),
extracts them, and builds the onedir distribution in
./dist/Database_Editor_v<VERSION>/ (the folder name carries the version).
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


def build():
    shutil.rmtree(DIST, ignore_errors=True)
    shutil.rmtree(WORK, ignore_errors=True)
    env = dict(os.environ)
    env["PYTHONPATH"] = PYLIBS + os.pathsep + env.get("PYTHONPATH", "")
    cmd = [
        sys.executable, "-X", "utf8", "-m", "PyInstaller",
        "--noconfirm", "--onedir", "--windowed",
        "--name", APP_NAME,
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
        "--add-data", os.path.join(HERE, "_rev_tools", "finalize_crc.py") + ";_rev_tools",
        "--add-data", os.path.join(HERE, "_rev_tools", "stream_save.py") + ";_rev_tools",
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

    # Zip the moved folder in the release directory.
    zip_base = os.path.join(RELEASE, APP_NAME)
    zip_path = shutil.make_archive(zip_base, "zip", RELEASE, APP_NAME)

    exe = os.path.join(dst_dir, APP_NAME + ".exe")
    print("done:", exe)
    print("zipped:", zip_path)
    return exe


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ensure_pylibs()
    build()
