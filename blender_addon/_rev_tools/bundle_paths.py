# -*- coding: utf-8 -*-
r"""**bundle 路径解析** —— 按 glob 找，绝不把带 hash 的文件名写死。

为什么必须有这个（2026-10，与 F1「RVA 整体位移」同族的一课）
============================================================
Addressables 的包名形如 `<逻辑名>_<32 位内容哈希>.bundle`，**游戏每更新一次内容哈希就变**。
实测：`units_assets_all_3cc1eb58d8b7f6cdf82bbfbf3b5b8aaf.bundle`
  → 现在磁盘上只有 `units_assets_all_1e6c04ce42984f32a0891b92fab010e8.bundle`
  （旧名字只剩一个 `…3cc1eb58….bundle_unpacked` 的**解包残留目录**，看着像还在 ✗）

⛔ 为什么很危险：`UnityPy.load(<不存在的路径>)` **不报错**，只返回**空 Environment**
   （`.re-kb` 里记过这个坑）⇒ 下游表现成
   * `list(env.objects)[0]` → 光秃秃的 `IndexError`（看着像代码 bug）
   * 涂装/替换算子 → "没导出到任何贴图"（看着像模型没贴图）
   * CRC/导入路径 → 找不到文件或算出别人的值
   ⇒ **错的路径不报错**，和"错的 RVA 不报错"是同一类事故。

用法
====
    from bundle_paths import units_bundle, find_bundle, aa_pc_dir
    p = units_bundle()                       # → 游戏 aa\\PC 下当前的 units 主包（或 None）
    p = units_bundle(pristine=True)          # → `备份\\` 里那份纯净包（导入/手术测试用）
    p = find_bundle("units_assets_all")      # 通用：按前缀找

返回 `None` 时**调用方必须报错**，不要退回到"写死的旧路径"（那正是本模块要消灭的行为）。
"""
import glob
import os

def _find_ws():
    """往**上**找工作区根（认"同时有 备份/ 与 工具制作资源/"的那一层）。

    ⛔ 不能写 `dirname^N(__file__)`：本模块在插件里是
      `blender_addon/_rev_tools/`（深 4 层）、同步到产品根后是 `_rev_tools/`（深 3 层）
      ⇒ 固定层数必然有一边算错（实测第一版就把工作区算成了 `工具制作资源` ✗）
    """
    d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(8):
        if os.path.isdir(os.path.join(d, "工具制作资源")) and \
                (os.path.isdir(os.path.join(d, "备份")) or os.path.isdir(os.path.join(d, "测试"))):
            return d
        nd = os.path.dirname(d)
        if nd == d:
            break
        d = nd
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


WS = _find_ws()

# 游戏安装位置的候选（多盘/中文路径都试一遍）
# ⛔ 常量名与**每个字面量所在的行**都必须带 `DEFAULT`：产品审计 `product_lint.py`
#    用"这一行里有没有 DEFAULT"来区分「故意列的候选路径」与「忘了改的本机路径」⇒
#    写成 6 条裸路径会被整片误报（实测：只改常量名不够，路径行本身也要带标记）✗
DEFAULT_STEAM_ROOT = r"<游戏安装目录>"        # DEFAULT：本机主安装位置
DEFAULT_ROOT_ALTERNATIVES = (
    r"C:\Program Files (x86)\Steam\steamapps\common",    # DEFAULT：常见备选
    r"C:\Program Files\Steam\steamapps\common",          # DEFAULT：常见备选
    r"E:\Steam\steamapps\common",                        # DEFAULT：常见备选
    r"F:\Steam\steamapps\common",                        # DEFAULT：常见备选
    r"G:\Steam\steamapps\common",                        # DEFAULT：常见备选
)
DEFAULT_GAME_ROOTS = [os.path.join(p, "broken_arrow") for p in
                      (DEFAULT_STEAM_ROOT,) + DEFAULT_ROOT_ALTERNATIVES]
# 兼容旧名（本模块内部与外部都可能有引用）
GAME_ROOTS = DEFAULT_GAME_ROOTS
AA_PC = os.path.join("BrokenArrow_Data", "StreamingAssets", "aa", "PC")
PRISTINE_DIRS = [os.path.join(WS, "备份"),
                 os.path.join(WS, "测试")]


def game_root():
    for r in GAME_ROOTS:
        if os.path.isdir(r):
            return r
    return None


def aa_pc_dir():
    """→ 游戏 `BrokenArrow_Data\\StreamingAssets\\aa\\PC`（或 None）"""
    r = game_root()
    if not r:
        return None
    d = os.path.join(r, AA_PC)
    return d if os.path.isdir(d) else None


def _pick(patterns):
    """在所有模式里找**最大的那个真文件**（`.bundle_unpacked` 这类残留目录/文件不算）"""
    hits = []
    for pat in patterns:
        for p in glob.glob(pat):
            if os.path.isfile(p) and p.endswith(".bundle"):
                hits.append(p)
    if not hits:
        return None
    return max(hits, key=os.path.getsize)


def find_bundle(prefix, where=None, pristine=False):
    """按**逻辑名前缀**找 bundle → 路径或 None。

    · `pristine=True` ⇒ 只在 `备份\\` / `测试\\` 里找（**纯净**包，导入/手术测试必须用它；
       游戏目录里那份已经被改过，拿它当基准判据会失真）
    · 否则先找游戏 `aa\\PC`，再退回工作区
    """
    pats = []
    if where:
        pats.append(os.path.join(where, prefix + "*.bundle"))
    elif pristine:
        for d in PRISTINE_DIRS:
            pats.append(os.path.join(d, prefix + "*.bundle"))
    else:
        d = aa_pc_dir()
        if d:
            pats.append(os.path.join(d, prefix + "*.bundle"))
        for d in PRISTINE_DIRS:
            pats.append(os.path.join(d, prefix + "*.bundle"))
    return _pick(pats)


def units_bundle(pristine=False, where=None):
    """模型主包 `units_assets_all_*.bundle`（替代写死的 `…<hash>.bundle`）"""
    return find_bundle("units_assets_all", where=where, pristine=pristine)


def sfx_bundle(pristine=False, where=None):
    """音效包 `sfx_assets_all_*.bundle`"""
    return find_bundle("sfx_assets_all", where=where, pristine=pristine)


if __name__ == "__main__":
    import sys
    try:                                    # GUI/无控制台环境 sys.stdout 可能是 None
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print("工作区：%s" % WS)
    print("游戏根：%s" % game_root())
    print("aa\\PC ：%s" % aa_pc_dir())
    for label, p in (("units（游戏）", units_bundle()),
                     ("units（纯净）", units_bundle(pristine=True)),
                     ("sfx（游戏）", sfx_bundle())):
        print("  %-14s %s" % (label, ("%s  (%.2f GB)" % (p, os.path.getsize(p) / 2 ** 30))
                              if p else "✗ 没找到"))
