# -*- coding: utf-8 -*-
# 重建 BA_Mod_Maker_blender_addon.zip（含插件文件 + 内置 UnityPy 运行时）
# 1) 把根目录 version.py（单一版本来源）同步进 blender_addon/version.py；
# 2) 把 bl_info 里的版本字面量同步成 ADDON_VERSION（Blender 的 ast 解析要求纯字面量）。
import shutil
import zipfile
import os
import re
import sys

here = os.path.dirname(os.path.abspath(__file__))
addon = os.path.join(here, "blender_addon")
out = os.path.join(here, "BA_Mod_Maker_blender_addon.zip")

# 0) 先把两个 _rev_tools 目录同步（addon 是唯一源头）——
#    否则根目录那份会悄悄变旧，出现「改了插件、exe 还是旧逻辑」这类难查的 bug ✗
_sync = os.path.join(here, "_sync_revtools.py")
if os.path.isfile(_sync):
    import subprocess
    r = subprocess.run([sys.executable, _sync, "--check"], capture_output=True)
    if r.returncode == 2:
        print("检测到 _rev_tools 分叉，正在同步…")
        subprocess.run([sys.executable, _sync], check=True)
    elif r.returncode != 0:
        print("_rev_tools 同步检查异常（继续打包）：%s" % r.stdout.decode("utf-8", "replace")[-300:])

# 1) 同步版本单一来源（exe 与插件版本成对更新，见 version.py 的策略说明）
src_version = os.path.join(here, "version.py")
dst_version = os.path.join(addon, "version.py")
if os.path.isfile(src_version):
    shutil.copy2(src_version, dst_version)

sys.path.insert(0, here)
from version import ADDON_VERSION  # noqa: E402

# 2) bl_info 版本字面量同步（__init__.py 里的 "version": (...) 行）
init_path = os.path.join(addon, "__init__.py")
with open(init_path, "r", encoding="utf-8") as f:
    src = f.read()
new_src, n = re.subn(r'("version"\s*:\s*)\([^)]*\)',
                     r'\g<1>%s' % repr(tuple(ADDON_VERSION)), src, count=1)
if n != 1:
    raise SystemExit("bl_info 的 version 行没找到，无法同步版本字面量")
with open(init_path, "w", encoding="utf-8") as f:
    f.write(new_src)
print("bl_info version 已同步为", ADDON_VERSION)

files = []
# ⛔ 2026-10：这是**写死的白名单** ⇒ 新增的顶层模块不加进来就会**静默漏进 zip** ✗
#   实测踩到：`mod_paths.py`（默认路径 D 盘优先）没进 zip，装上后只能走插件内的兜底逻辑
for name in ("README.md", "unitypy_bridge.py", "__init__.py", "mount_dict.py", "version.py",
             "mod_paths.py"):
    files.append(name)
files += ["_rev_tools/" + f for f in sorted(os.listdir(os.path.join(addon, "_rev_tools")))
          if f.endswith(".py") or f.endswith(".json")]
# 内置 UnityPy 运行时（_unitypy：UnityPy/lz4/brotli/fsspec，随 zip 分发，免 pip）
if os.path.isdir(os.path.join(addon, "_unitypy")):
    for root, _, names in os.walk(os.path.join(addon, "_unitypy")):
        for n in names:
            if n.endswith((".pyc", "__pycache__")):
                continue
            full = os.path.join(root, n)
            rel = os.path.relpath(full, addon).replace(os.sep, "/")
            files.append(rel)
z = zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED)
for f in files:
    z.write(os.path.join(addon, f.replace("/", os.sep)), "blender_addon/" + f)
z.close()
print("addon zip:", out)
print("文件数:", len(files), "大小 KB:", os.path.getsize(out) // 1024)
