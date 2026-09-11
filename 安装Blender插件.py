# -*- coding: utf-8 -*-
r"""BA Mod Maker —— Blender 插件一键安装脚本。

在 Blender 里运行一次即可装好全部依赖：

  脚本工作区（Scripting）→ 打开本文件 → 点「运行脚本」（或全选粘贴进 Python 控制台回车）。

作用：
  1. 把 UnityPy 装进 Blender 自带的 Python（pip）
  2. 从 addon zip 安装并启用「BA Mod Maker」插件
  3. 自动检测并填充游戏 bundle 等偏好

用法：
  把本脚本和 BA_Mod_Maker_blender_addon.zip 放同一目录即可（脚本会自动找 zip）；
  也可以改下面的 ADDON_ZIP 显式指定路径。
"""
import bpy
import os
import sys
import subprocess
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ADDON_ZIP = os.path.join(HERE, "BA_Mod_Maker_blender_addon.zip")
ADDON_MODULE = "blender_addon"


def log(msg):
    print("[安装] " + msg, flush=True)


# 1. 安装 UnityPy ------------------------------------------------------------
def install_unitypy():
    log("安装 UnityPy（用 Blender 自带的 Python）...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", "UnityPy"])
    try:
        import UnityPy
        log("UnityPy 安装成功：" + str(getattr(UnityPy, "__version__", "已导入")))
    except ImportError as e:
        log("警告：pip 装完但 import 失败：" + str(e))


# 2. 安装 + 启用插件 ----------------------------------------------------------
def install_addon():
    if not os.path.isfile(ADDON_ZIP):
        raise FileNotFoundError("找不到插件 zip：" + ADDON_ZIP + "\n请把 BA_Mod_Maker_blender_addon.zip 和本脚本放同一目录。")

    addons_dir = bpy.utils.user_resource("SCRIPTS", path="addons")
    os.makedirs(addons_dir, exist_ok=True)

    log("解压插件到：" + addons_dir)
    with zipfile.ZipFile(ADDON_ZIP) as z:
        z.extractall(addons_dir)

    # 启用（若已启用会报错，忽略）
    try:
        bpy.ops.preferences.addon_enable(module=ADDON_MODULE)
        log("插件已启用：" + ADDON_MODULE)
    except Exception as e:
        log("启用插件（可能已启用）：" + str(e))


# 3. 自动配置偏好 -------------------------------------------------------------
def configure():
    prefs = bpy.context.preferences.addons[ADDON_MODULE].preferences

    # 插件已启用，Blender 已把 addon 目录加进 sys.path，可直接 import
    import blender_addon
    detect_game_dir = blender_addon.detect_game_dir
    detect_bundle = blender_addon.detect_bundle

    gd = detect_game_dir()
    if gd:
        b = detect_bundle(gd)
        if b:
            prefs.bundle = b
            log("游戏 bundle：" + b)
        else:
            log("检测到游戏目录 " + gd + "，但没找到 units bundle，请手动填「游戏 bundle 文件」")
    else:
        log("没自动检测到游戏目录，请手动填「游戏 bundle 文件」")

    if not prefs.out_bundle:
        prefs.out_bundle = os.path.join(os.path.expanduser("~"), "work.bundle")
        log("输出 bundle 默认：" + prefs.out_bundle)


if __name__ == "__main__":
    install_unitypy()
    install_addon()
    configure()
    log("全部装好了。重启 Blender（或直接开 3D 视图侧边栏「BA Mod」面板）即可用。")
    log("首次使用建议：偏好设置里确认「游戏 bundle 文件」和「输出 .bundle」。")
