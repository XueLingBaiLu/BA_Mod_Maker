# -*- coding: utf-8 -*-
r"""挑对 `_unitypy` 那一份（**按解释器版本**，不猜、不靠 import 顺序）。

为什么需要这个模块：工作区里有**两份** `_unitypy` 副本，二进制扩展是按解释器编译的：
  · `BA_Mod_Maker/_unitypy`           → `lz4/_version.cp314-win_amd64.pyd`（系统 Python 3.14）
  · `blender_addon/_unitypy`          → `lz4/_version.cp313-win_amd64.pyd`（Blender 的 Python 3.13）
指错的那份会报 `ModuleNotFoundError: No module named 'lz4._version'`。

⛔ 以前各模块各写一句 `sys.path.insert(0, HERE/"../.."/"_unitypy")`：
   · 在 Blender 里把 **cp314** 顶到最前 ⇒ 崩；
   · 反过来，CLI 脚本自己插了 `blender_addon/_unitypy` 时又会把 **cp313** 顶到最前 ⇒ 也崩；
   · 谁的 insert 后执行谁说了算 ⇒ 这是典型的"有时好有时坏"，最难查 ✗。
现在：**看目录里 `lz4/_version.cpXY-*.pyd` 的 tag 对不对得上当前解释器**，
对得上的排前面，并且一律用 `append`（顶不掉别人已经放好的正确路径 ✓）。

用法（放在 `import UnityPy` 之前）：
    import os, sys
    _HERE = os.path.dirname(os.path.abspath(__file__))
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    from unitypy_path import ensure as _ensure_unitypy
    _ensure_unitypy(_HERE)
"""
import glob
import os
import sys


def interpreter_tag():
    """当前解释器的扩展模块 tag，例如 `cp314`（Blender 5.x = cp313）。"""
    return "cp%d%d" % (sys.version_info[:2])


def candidates(here):
    """两份 `_unitypy` 的绝对路径：**先匹配当前解释器的那份**。"""
    here = os.path.abspath(here)
    bases = [os.path.abspath(os.path.join(here, "..", "_unitypy")),      # 插件内（cp313）
             os.path.abspath(os.path.join(here, "..", "..", "_unitypy"))]  # 工具根（cp314）
    tag = interpreter_tag()
    scored = []
    for b in bases:
        if not os.path.isdir(b):
            continue
        has_tag = bool(glob.glob(os.path.join(b, "lz4", "_version.%s-*" % tag)))
        scored.append((0 if has_tag else 1, b))
    scored.sort(key=lambda x: x[0])
    return [b for _s, b in scored]


def ensure(here, verbose=False):
    """把 `_unitypy` 候选目录追加进 `sys.path`（对得上的在前）。返回实际加入的目录。"""
    added = []
    for p in candidates(here):
        if p in sys.path:
            continue
        sys.path.append(p)      # ⛔ append：不能顶掉 unitypy_bridge / 测试脚本已放好的路径
        added.append(p)
    if verbose:
        print("[unitypy_path] 解释器 tag=%s，加入：%s" % (interpreter_tag(), added))
    return added


def which_loaded():
    """已导入的 UnityPy 实际来自哪个目录（排查用：`None` = 还没导入）。"""
    m = sys.modules.get("UnityPy")
    return getattr(m, "__file__", None) if m is not None else None
