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

APP_VERSION = "1.12.9"
ADDON_VERSION = (2, 12, 9)      # 2026-09-20 v1.12.9：**界面那几处「一闪就没／白条／跳变」一次修净** —— **窗口从出现的第一眼就是正确的样子**（检索栏候选列表**出现即最终样子**、**暗色一上来就是暗的**）——
                                #   缺陷修复：**同包时也会把材质的字节一并带上**（引用不再悬空）＋ **悬浮小提示不再压住你正在看的那一行**（贴在被悬停那行的旁边、同时只留一个）
                                # + **本版一并含未单独发布的 v1.12.8**（若你上次装的是 v1.12.7 ⇒ 这次会把两版一起带上）


if __name__ == "__main__":
    print("BA_Mod_Maker v%s / Blender addon v%s" % (APP_VERSION, ".".join(map(str, ADDON_VERSION))))

