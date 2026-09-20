# -*- coding: utf-8 -*-
r"""**统一决定"模组工作目录放哪"** —— 一律避开 C 盘（用户的 C 盘很宝贵）。

背景（2026-10 用户反馈）：产品的默认路径原来全用 `os.path.expanduser("~")`
（= `C:\Users\<用户名>\...`），另外 Blender 插件的默认输出还会落到**插件自己所在的目录**
（= `C:\Users\...\AppData\Roaming\Blender Foundation\Blender\<版本>\scripts\addons\...`）
⇒ 导出的 .bamod/.bundle、自建 bundle 工作目录、**单个 6.8 GB 的游戏快照**都可能往 C 盘写 ✗

优先级（第一个可用的胜出）：
  1. 环境变量 **`BAMOD_HOME`**（想强制指定就设它）
  2. **游戏安装所在盘**的 `\BrokenArrow_Mods`（游戏通常装在空间大的盘上，实测本机是 D:）✓
  3. `D:\BrokenArrow_Mods`（D 盘存在但找不到游戏时）
  4. 兜底 `~\BrokenArrow_Mods`（只在上面都不成立时用）

用法：
    from mod_paths import mod_root, workdir, snapshot_root
    print(mod_root())            # → D:\BrokenArrow_Mods
"""
import os

ENV_HOME = "BAMOD_HOME"
DIRNAME = "BrokenArrow_Mods"

# 产品已知的几个游戏目录线索（与 blender_addon.detect_game_dir / 各 CLI 的默认值保持一致）
_GAME_HINTS = (
    r"<游戏安装目录>",
    r"C:\Program Files (x86)\Steam\steamapps\common\broken_arrow",
)


def _game_dir():
    """尽力找游戏目录（找不到返回 None）——只用它的**盘符**，不要求一定存在游戏"""
    for p in _GAME_HINTS:
        if os.path.isdir(p):
            return p
    # Steam 库清单里翻（本机 D:\Steam\steamapps）
    for lib in (r"D:\Steam\steamapps", r"C:\Program Files (x86)\Steam\steamapps"):
        p = os.path.join(lib, "common", "broken_arrow")
        if os.path.isdir(p):
            return p
    return None


def mod_root():
    """→ 模组工作根目录（**绝不默认落 C 盘**，除非实在没别的盘）"""
    env = os.environ.get(ENV_HOME)
    if env:
        return os.path.abspath(env)
    g = _game_dir()
    if g:
        drive = os.path.splitdrive(os.path.abspath(g))[0]      # 例 'D:'
        if drive and drive.upper() != "C:":
            return os.path.join(drive + os.sep, DIRNAME)
    for d in ("D:", "E:", "F:"):
        if os.path.isdir(d + os.sep):
            return os.path.join(d + os.sep, DIRNAME)
    return os.path.join(os.path.expanduser("~"), DIRNAME)


def workdir(*parts):
    """自建 bundle / 导出的工作目录（不存在就建）"""
    p = os.path.join(mod_root(), *parts) if parts else mod_root()
    try:
        os.makedirs(p, exist_ok=True)
    except OSError:
        pass
    return p


def snapshot_root():
    """游戏快照目录（单个 6.8 GB ⇒ 尤其不能放 C 盘）"""
    return workdir("snapshots")


def exports():
    """插件导出的 .bamod/.bundle 默认落点"""
    return workdir("exports")
