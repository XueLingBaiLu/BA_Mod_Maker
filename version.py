# -*- coding: utf-8 -*-
"""单一版本来源 —— BA_Mod_Maker 与 Blender 插件的版本定义。

版本策略（必须遵守）：**每进行一次改动，两个工具的版本都要同时更新**。
  - APP_VERSION    BA_Mod_Maker（exe / 源码包）：vX.Y.Z，改一次 +1 补丁位。
  - ADDON_VERSION  Blender 插件 bl_info：与 APP_VERSION 同步 +1，保持成对。
  - 同步修改 Change Log.txt（三语）并重新打包（auto_build.py / package_zip.py /
    _rebuild_addon_zip.py 都会读取本文件，无需改别处）。

引用方：
  ba_db_tool.py / build_exe.py / package_zip.py  ->  from version import APP_VERSION
  blender_addon/__init__.py                      ->  from .version import ADDON_VERSION
  _rebuild_addon_zip.py 打包前把本文件同步进 blender_addon/version.py。
"""

APP_VERSION = "1.8.53"
ADDON_VERSION = (2, 7, 53)


if __name__ == "__main__":
    print("BA_Mod_Maker v%s / Blender addon v%s" % (APP_VERSION, ".".join(map(str, ADDON_VERSION))))
