# -*- coding: utf-8 -*-
"""BA Mod Maker —— Blender 插件（模型提取 → 挂载点编辑 → 构建写回 .bamod）。

工作流：
  1. 模型导入：浏览 bundle 内全部 prefab，一键提取（挂载点树 = Empty，网格带顶点组）。
  2. 挂载点：可视化查看/添加/删除所有种类挂载点（含子挂载点），词典说明用途。
  3. 工具：构建写回（.bamod 素材包 / 完整 bundle）、权重转移、CRC 计算。
  4. 词典：挂载点 / 组件 / 模板 / 地址与常量（数据结构、地址映射收纳于此）。
  5. 动画：读取 AnimationHub 行为列表，每个行为一个折叠栏（默认折叠）。

版本策略：版本号在 version.py（单一来源）；每次改动 BA_Mod_Maker 与插件版本成对更新。
"""
# 版本：单一来源 version.py。Blender 的 addon 扫描器用 ast.literal_eval 解析 bl_info，
# 必须是**纯字面量**（不能引用 ADDON_VERSION 变量）——所以下面的 version 是字面量，
# 由 _rebuild_addon_zip.py 打包时从 version.py 自动同步（AUTO-SYNC 标记行）。
# 运行时再 import version.py 做一致性核对，未同步时打印警告。
# ★ 过渡说明（2026-09-18 中枢令 · 由子⑦ 落）：**用户面措辞＝中枢给定候选甲**（与 子⑤ exe 侧同批，
#   ⛔ 不许只改一边）；第三处是 v1.12.3 已发 Change Log（⛔ 不改已发件，下批写更正）。
#   位置 = 模块级常量（运行期可从 `blender_addon.TRANSITION_NOTE` 读到），并在「③ 工具」面板显示
TRANSITION_NOTE = "本功能仍在 exe 侧正常提供；后续形态正在评估中，如有调整会提前说明。"

bl_info = {
    "name": "BA Mod Maker（断箭模型工具）",
    "author": "BA Mod Maker",
    "version": (2, 12, 9),  # AUTO-SYNC from version.py
    "blender": (4, 0, 0),
    "location": "3D View > 侧边栏 > BA Mod",
    "description": "提取游戏模型、可视化编辑挂载点、构建写回（全类型模型）",
    "category": "Import-Export",
}

try:
    from .version import ADDON_VERSION
    _addon_ver = tuple(ADDON_VERSION)
    if _addon_ver != tuple(bl_info["version"]):
        # ⛔ v1.8.97 修：**装插件时这里会误报**。安装脚本是"先解压覆盖文件、再 enable"，
        #    而 `.version` 可能还是本次会话早先 import 的旧模块（sys.modules 缓存）⇒
        #    bl_info 是新的、ADDON_VERSION 是旧的 ⇒ 打出"版本不一致，请重新打包"的假警告 ✗
        #    ⇒ 报警前**从磁盘重读一次 version.py**，只有磁盘上也不一致才说。
        try:
            import importlib as _il
            import os as _os
            import re as _re
            _vp = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "version.py")
            with open(_vp, encoding="utf-8", errors="replace") as _f:
                _m = _re.search(r"ADDON_VERSION\s*=\s*\(([^)]*)\)", _f.read())
            if _m:
                _disk = tuple(int(x.strip()) for x in _m.group(1).split(",") if x.strip())
                if _disk == tuple(bl_info["version"]):
                    _addon_ver = _disk      # 磁盘上一致 ⇒ 只是内存里旧了，不报警
                    _il.reload(__import__(__name__ + ".version", fromlist=["x"]))
        except Exception:  # noqa: BLE001
            pass
    if _addon_ver != tuple(bl_info["version"]):
        print("[BA Mod] 警告：bl_info 版本 %s 与 version.py %s 不一致，"
              "请用 _rebuild_addon_zip.py 重新打包" % (bl_info["version"], _addon_ver))
except ImportError:
    ADDON_VERSION = tuple(bl_info["version"])

import bpy
import glob
import os
import sys          # ★ 2026-10 补：默认路径兜底/迁移要用 sys.path（原来没导入 ⇒ 一跑到就 NameError ✗）
import time

from mathutils import Matrix, Quaternion, Vector, Euler

# Unity Y-up -> Blender Z-up 的基变换：C @ [x,y,z]^T = [x,-z,y]^T（正交，C^-1 = C^T）
_UNITY_TO_BLENDER_M = Matrix(((-1, 0, 0, 0), (0, 0, -1, 0), (0, 1, 0, 0), (0, 0, 0, 1)))  # ★㓓：det=−1（Unity 左手 ↔ Blender 右手之间必须差一个翻轴）

from . import unitypy_bridge
from . import mount_dict
from .mount_dict import MOUNT_CATEGORIES, category_of


# ---------------------------------------------------------------------------
# 路径自动检测
# ---------------------------------------------------------------------------
_STEAM_ROOTS = [
    "D:/Steam/steamapps/common",
    "C:/Program Files (x86)/Steam/steamapps/common",
    "C:/Program Files/Steam/steamapps/common",
    "E:/Steam/steamapps/common",
    "F:/Steam/steamapps/common",
]
_WORKSPACE_CANDIDATES = [
    "<仓库目录>",
    "<工具目录>",
]

# 打包版 exe 的发布目录根（BA_Mod_Maker_v*/_internal 内含 _unitypy + _rev_tools）
_RELEASE_ROOTS = [
    "<工作目录>",
    "<工作目录>/工具制作资源",
    "D:/BA_Mod_Maker",
]


def detect_game_dir():
    for root in _STEAM_ROOTS:
        d = os.path.join(root, "broken_arrow")
        if os.path.isdir(os.path.join(d, "BrokenArrow_Data")):
            return d
    return None


def detect_workspace():
    """找 _unitypy + _rev_tools 所在目录。

    优先级：
      1. 插件自身目录（zip 自带 _unitypy/_rev_tools，完全自包含）；
      2. 开发工作区（_WORKSPACE_CANDIDATES，开发机上改代码即时生效）；
      3. 打包版 exe 的 _internal 目录（BA_Mod_Maker_v*/_internal）。
    """
    own = os.path.dirname(os.path.abspath(__file__))
    if os.path.isdir(os.path.join(own, "_unitypy")) and os.path.isdir(os.path.join(own, "_rev_tools")):
        return own
    for c in _WORKSPACE_CANDIDATES:
        if os.path.isdir(os.path.join(c, "_unitypy")) and os.path.isdir(os.path.join(c, "_rev_tools")):
            return c
    for root in _RELEASE_ROOTS:
        if not os.path.isdir(root):
            continue
        try:
            # 只认真实目录：过滤掉 BA_Mod_Maker_v*.zip 等同名前缀的文件，
            # 否则字符串排序会把 "...v1.6.0.zip" 排在 "...v1.6.0" 目录前面，
            # 导致路径被自动填充进 zip 文件里。
            names = sorted((n for n in os.listdir(root)
                            if n.startswith("BA_Mod_Maker_v") and os.path.isdir(os.path.join(root, n))),
                           reverse=True)
        except OSError:
            continue
        for name in names:
            internal = os.path.join(root, name, "_internal")
            if os.path.isdir(os.path.join(internal, "_unitypy")) and \
                    os.path.isdir(os.path.join(internal, "_rev_tools")):
                return internal
    d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        if os.path.isdir(os.path.join(d, "_unitypy")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None


def detect_output_dir():
    """输出目录：优先 BA_Mod_Maker_v* 发布目录下的 model 文件夹（自动创建）。

    找发布目录的顺序：插件自身所在目录链上的 BA_Mod_Maker_v* → _RELEASE_ROOTS 下
    版本号最新的 BA_Mod_Maker_v*。找不到就退回插件目录。
    """
    def _release_of(d):
        cur = d
        for _ in range(6):
            if os.path.isdir(cur) and os.path.basename(cur).startswith("BA_Mod_Maker_v"):
                return cur
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
        return None

    own = os.path.dirname(os.path.abspath(__file__))
    rel = _release_of(own)
    if not rel:
        for root in _RELEASE_ROOTS:
            if not os.path.isdir(root):
                continue
            try:
                # 只认真实目录：BA_Mod_Maker_v*.zip 也会匹配前缀且排序靠前，
                # 必须过滤掉，否则输出目录会被填进 zip 文件里（并生成 zip 名的假目录）。
                names = sorted((n for n in os.listdir(root)
                                if n.startswith("BA_Mod_Maker_v") and os.path.isdir(os.path.join(root, n))),
                               reverse=True)
            except OSError:
                continue
            if names:
                rel = os.path.join(root, names[0])
                break
    if rel:
        model = os.path.join(rel, "model")
        try:
            os.makedirs(model, exist_ok=True)
        except OSError:
            pass
        return model
    return own


def detect_bundle(game_dir):
    if not game_dir:
        return None
    pc = os.path.join(game_dir, "BrokenArrow_Data", "StreamingAssets", "aa", "PC")
    cands = glob.glob(os.path.join(pc, "units_assets_all_*.bundle"))
    return cands[0] if cands else None


# ---------------------------------------------------------------------------
# 偏好设置
# ---------------------------------------------------------------------------
def _bp():
    """`_rev_tools/backup_policy.py` —— 自动备份开关（与 BA_Mod_Maker 共用配置）。"""
    try:
        import backup_policy
        return backup_policy
    except Exception:  # noqa: BLE001
        return None


def _on_auto_backup_update(self, context):
    """勾选变化 ⇒ 立刻写进共享配置（`%APPDATA%\\BA Mod Maker\\settings.json`）。"""
    b = _bp()
    if b is not None:
        b.set_backup_enabled(bool(self.auto_backup))


def _migrate_paths_off_c(prefs=None, log=print):
    r"""[路径-02] 启动时把**落在 C 盘 / 插件目录内**的输出路径自动迁到 D 盘。

    为什么（用户反馈）：`out_bundle` / `unitypy_dir` 这些**旧值**一旦是 C 盘（或者指向插件安装目录
    内部），用户会在 C 盘上堆出 GB 级产物（实测被白占 6.95 GB），而且插件升级/重装后路径就失效 ✗
    v2.7.113 只改了**默认值**，**已存在的老值不会被自动修** ⇒ 本条补上"启动时迁移一次"。

    判据（只动**输出类**路径，**绝不碰用户手选的游戏文件路径**）：
      · 路径以 `C:` 开头（Windows），**或**位于插件安装目录内部 ⇒ 认定为"该迁"
      · 迁到当前默认值（`_default_export_dir()`，D 盘优先）✓
      · **原有的值先记进日志**（迁了什么、从哪到哪），随时能看 ✓
    ⛔ 只迁空值/坏值之外的那些 **输出**字段：`bundle` 是**输入**（游戏文件），**不动** ✓
    `prefs` 不给就去 `bpy.context.preferences` 里取 —— **留参数是为了可测**（无界面自测里插件可能
    还没"启用"，拿不到偏好设置就完全没法验这条）✓
    返回迁移过的字段列表。
    """
    p = prefs
    try:
        if p is None:
            import bpy
            add = bpy.context.preferences.addons.get(__name__)
            p = add.preferences if add is not None else None
        if p is None:
            return []
    except Exception:                                            # noqa: BLE001
        return []
    addon_dir = os.path.dirname(os.path.abspath(__file__))
    # ⛔ "插件目录"取**产品根目录**（`blender_addon` 的上一级），不只 `blender_addon/`：
    #   用户的输出落进产品目录里同样会被"升级/重装"冲掉，而且 `_rev_tools`/缓存都在那一层 ✗
    prod_dir = os.path.dirname(addon_dir)
    default_out = _default_export_dir()
    moved = []
    for field in ("out_bundle", "unitypy_dir", "rev_dir"):
        try:
            cur = getattr(p, field, "") or ""
        except Exception:                                        # noqa: BLE001
            continue
        if not cur:
            continue
        ap = os.path.abspath(cur)
        # ⛔ 用 `splitdrive` 而不是 `ap[1:3]`：`"C:\x"` 的 `[1:3]` 是 `":\\"`（第一版就错在这里，
        #    结果"C 盘判定"恒为 False ⇒ 迁移**静默什么也不做**，测试当场报红）✗
        drv = os.path.splitdrive(ap)[0].upper()
        on_c = (drv == "C:")
        inside_addon = (ap.lower().startswith(addon_dir.lower())
                        or ap.lower().startswith(prod_dir.lower()))
        if not (on_c or inside_addon):
            continue
        # 目标：out_bundle 是**文件**，其余是目录
        new = os.path.join(default_out, "mod.bamod") if field == "out_bundle" else os.path.dirname(
            default_out)
        try:
            setattr(p, field, new)
        except Exception as e:                                   # noqa: BLE001
            log("⚠ 路径迁移失败（%s）：%s" % (field, e))
            continue
        moved.append(field)
        log("路径已迁移 %s：%s → %s（%s）"
            % (field, cur, new, "在 C 盘" if on_c else "在插件目录内"))
    return moved


def _default_export_dir():
    """插件导出的默认目录 —— **绝不落 C 盘**（2026-10 用户反馈修）

    优先级：`BAMOD_HOME` → 产品统一入口 `mod_paths.exports()`（D 盘优先）→
    游戏所在盘 `\\BrokenArrow_Mods\\exports` → `~\\BrokenArrow_Mods\\exports`
    """
    try:
        # ★ 先试**同目录**（`mod_paths.py` 随插件一起装 ⇒ 发布包里也在）✓
        try:
            import mod_paths as _mp                         # noqa: PLC0415
            return _mp.exports()
        except ImportError:
            pass
        _prod = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if _prod not in sys.path:
            sys.path.insert(0, _prod)
        import mod_paths                                     # noqa: PLC0415
        return mod_paths.exports()
    except Exception:                                        # noqa: BLE001
        env = os.environ.get("BAMOD_HOME")
        if env:
            return env
        try:
            g = detect_game_dir()
        except Exception:                                    # noqa: BLE001
            g = None
        drv = os.path.splitdrive(os.path.abspath(g))[0] if g else ""
        if drv and drv.upper() != "C:":
            return os.path.join(drv + os.sep, "BrokenArrow_Mods", "exports")
        return os.path.join(os.path.expanduser("~"), "BrokenArrow_Mods", "exports")


class BAModPreferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    unitypy_dir: bpy.props.StringProperty(name="UnityPy 目录 (_unitypy)", subtype="DIR_PATH")
    rev_dir: bpy.props.StringProperty(name="工具脚本目录 (_rev_tools)", subtype="DIR_PATH")
    bundle: bpy.props.StringProperty(name="游戏 bundle 文件", subtype="FILE_PATH")
    out_bundle: bpy.props.StringProperty(
        name="输出（.bamod 素材包 / .bundle）", subtype="FILE_PATH")
    new_prefab: bpy.props.StringProperty(
        name="新 prefab 内部路径",
        description="**留空 = 原地替换复制源那个 prefab**（改现有单位用这个，最常用）。\n"
                    "填别的路径 = 注册成一个**新 prefab**，原来的不动（克隆成新单位）。\n"
                    "⛔ v1.8.72 之前留空是**坏的**：包里的 prefab_path 变成空串 ⇒ 导入时"
                    "**不写容器条目** ⇒ 游戏仍加载原 prefab、改动完全不可见 ✗",
        default="")
    new_name: bpy.props.StringProperty(
        name="新 prefab 名字",
        description="只用于『新建 prefab』流程的显示名；copy-full（完整复制）模式不读它",
        default="")
    search: bpy.props.StringProperty(name="搜索", default="")
    mount_new_name: bpy.props.StringProperty(name="挂载点名字", default="")
    cat_filter: bpy.props.EnumProperty(
        name="分类",
        items=lambda self, ctx: _anim_cached_items(
            "catfilter",
            lambda: [("ALL", "全部", "")]
            + [(c["id"], c["name"], c["desc"]) for c in MOUNT_CATEGORIES]),
    )
    only_lod0: bpy.props.BoolProperty(name="只导入 LOD0", default=True)
    copy_full: bpy.props.BoolProperty(
        name="完整复制模式（保留原组件/材质/动画）",
        description="勾选后：① 导入过的 prefab 会整段字节复制，④ 只改网格和挂点，其余组件原样保留",
        default=True)
    apply_anim: bpy.props.BoolProperty(
        name="构建时写回动画（AnimationHub）",
        description="勾选后构建时把 ④ 面板的动画行为写进模型的 AnimationHub。\n"
                    "面板为空时回退读文本块 bamod_anim 的 JSON。\n"
                    "⛔ 面板里加了行为却没勾这项 ⇒ 构建出来没有动画（构建会直接拦下来提醒）",
        default=False)
    build_pack: bpy.props.BoolProperty(
        name="导出 .bamod 素材包（分发用，不写完整 bundle）",
        description="勾选后构建只输出小包（KB~MB 级），用 BA_Mod_Maker 的素材导入合并进游戏",
        default=True)
    auto_backup: bpy.props.BoolProperty(
        name="写回前自动备份（GB 级副本 · 默认关）",
        description="⛔ v1.8.75 起默认**关**：写 bundle 时不再顺手复制一份 `<bundle>.bak`。\n"
                    "units bundle 3.42GB、data.unity3d 13.7GB —— 留一份就是整份副本，\n"
                    "实测这样白占过 6.95GB。\n"
                    "关掉**不影响安全**：写回走「先写 .tmp → 成功后原子替换」，中途失败\n"
                    "只会留下临时文件，原文件完好。\n"
                    "本开关与 BA_Mod_Maker 的『文件 → 写入前自动备份』是**同一份配置**。",
        default=False,
        update=_on_auto_backup_update)

    def draw(self, context):
        layout = self.layout
        layout.operator("ba_mod.autodetect", icon="AUTO")
        layout.separator()
        layout.prop(self, "unitypy_dir")
        layout.prop(self, "rev_dir")
        layout.separator()
        layout.prop(self, "bundle")
        layout.prop(self, "out_bundle")
        layout.prop(self, "new_prefab")
        layout.prop(self, "new_name")
        layout.separator()
        # v1.8.75：默认不备份（bundle 3.42GB / data.unity3d 13.7GB，留一份就是整份副本）
        layout.prop(self, "auto_backup")


def _prefs(context):
    return context.preferences.addons[__name__].preferences


def _normalize_name(nm):
    nm = nm.split(".")[0]
    if " (" in nm and nm.rstrip().endswith(")"):
        nm = nm[:nm.rfind(" (")]
    return nm

# ---------------------------------------------------------------------------
# 场景状态（prefab 列表 / 挂载点列表）
# ---------------------------------------------------------------------------
_ADDRESS_MAP_CACHE = {}


def _load_address_map(bundle_path):
    """catalog.json → {内部路径: 映射地址}，供 prefab 列表显示短地址。"""
    if bundle_path in _ADDRESS_MAP_CACHE:
        return _ADDRESS_MAP_CACHE[bundle_path]
    mapping = {}
    try:
        cat_path = os.path.join(os.path.dirname(os.path.dirname(bundle_path)), "catalog.json")
        if os.path.isfile(cat_path):
            import catalog_mod
            cat = catalog_mod.Catalog(cat_path)
            iids = cat.cat.get("m_InternalIds", [])
            for ki, (typ, addr) in enumerate(cat.keys):
                if typ != 0:
                    continue
                for ei in cat.buckets[ki]["entries"]:
                    e = cat.entries[ei]
                    if e[0] < len(iids):
                        mapping.setdefault(iids[e[0]], addr)
    except Exception as e:  # noqa: BLE001
        # ⛔ 失败时**不要缓存空结果**：以前 swallow 掉并缓存 {} ⇒ catalog 读失败一次后
        #    整个会话都退回短名显示，用户完全不知道地址表没读到 ✗
        print("[BA Mod] catalog 地址表读取失败（不缓存，下次重试）：%s: %s"
              % (type(e).__name__, e))
        return mapping
    _ADDRESS_MAP_CACHE[bundle_path] = mapping
    return mapping


def _short_name(internal_path):
    """内部路径 → 短名（最后一段，去扩展名）。"""
    p = (internal_path or "").replace("\\", "/").rstrip("/")
    name = p.split("/")[-1] if p else ""
    if "." in name:
        name = name.rsplit(".", 1)[0]
    return name or p


class PREFAB_Item(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty(name="映射地址", description="Addressables 映射地址")
    path: bpy.props.StringProperty(name="内部路径", description="bundle 内 .prefab 完整路径")
    pid: bpy.props.StringProperty()  # pathID 是 int64，超出 Blender int32 范围，用字符串


class MOUNT_Item(bpy.types.PropertyGroup):
    obj: bpy.props.StringProperty()
    name: bpy.props.StringProperty()
    cat: bpy.props.StringProperty()
    depth: bpy.props.IntProperty()


class PREFAB_UL(bpy.types.UIList):
    bl_idname = "BA_MOD_UL_prefabs"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {"DEFAULT", "COMPACT"}:
            layout.prop(item, "name", text="", emboss=False, icon="OBJECT_DATA")


class MOUNT_UL(bpy.types.UIList):
    bl_idname = "BA_MOD_UL_mounts"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {"DEFAULT", "COMPACT"}:
            row = layout.row()
            row.prop(item, "name", text="", emboss=False, icon="EMPTY_DATA")
            cat = next((c for c in MOUNT_CATEGORIES if c["id"] == item.cat), None)
            row.label(text=cat["name"] if cat else item.cat)


# ---------------------------------------------------------------------------
# 自动检测
# ---------------------------------------------------------------------------
class BAMOD_OT_AutoDetect(bpy.types.Operator):
    bl_idname = "ba_mod.autodetect"
    bl_label = "自动检测并填充路径"

    def execute(self, context):
        prefs = _prefs(context)
        msg = []
        ws = detect_workspace()
        if ws:
            prefs.unitypy_dir = os.path.join(ws, "_unitypy")
            prefs.rev_dir = os.path.join(ws, "_rev_tools")
            msg.append("工作区")
        gd = detect_game_dir()
        if gd:
            b = detect_bundle(gd)
            if b:
                prefs.bundle = b
            msg.append("游戏bundle")
        outdir = detect_output_dir()
        if outdir:
            msg.append("输出目录")
        if not prefs.out_bundle:
            ext = ".bamod" if prefs.build_pack else ".bundle"
            # ⛔ 2026-10 修：原先是 `outdir or os.path.dirname(__file__)` —— 后者是**插件自己的目录**，
            #   而插件装在 `C:\Users\...\AppData\Roaming\Blender Foundation\Blender\<版本>\scripts\addons\…`
            #   ⇒ 导出的 .bamod/.bundle（几十 MB）默认全落 **C 盘**（用户反馈"默认路径怎么都去 C 盘了"）✗
            #   ⇒ 改成：**非 C 盘的** outdir 优先，否则用产品统一入口 `mod_paths`（D 盘优先）✓
            out_dir = outdir
            if out_dir and os.path.splitdrive(os.path.abspath(out_dir))[0].upper() == "C:":
                out_dir = None
            if not out_dir:
                out_dir = None
                try:                                   # 产品目录下的统一入口（发布包里也带）
                    _prod = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                    if _prod not in sys.path:
                        sys.path.insert(0, _prod)
                    import mod_paths                       # noqa: PLC0415
                    out_dir = mod_paths.exports()
                except Exception:                          # noqa: BLE001
                    # 兜底：环境变量 → 游戏盘的 \BrokenArrow_Mods → 用户目录
                    out_dir = os.environ.get("BAMOD_HOME")
                    if not out_dir:
                        _g = detect_game_dir()
                        _drv = os.path.splitdrive(os.path.abspath(_g))[0] if _g else ""
                        out_dir = os.path.join(_drv + os.sep, "BrokenArrow_Mods", "exports") \
                            if _drv and _drv.upper() != "C:" \
                            else os.path.join(os.path.expanduser("~"), "BrokenArrow_Mods", "exports")
                try:
                    os.makedirs(out_dir, exist_ok=True)
                except OSError:
                    pass
            prefs.out_bundle = os.path.join(out_dir, "units_turret_mod" + ext)
        if msg:
            self.report({"INFO"}, "已填充：" + "、".join(msg))
        else:
            self.report({"WARNING"}, "没找到，请手动填")
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# ① 模型导入
# ---------------------------------------------------------------------------
class BAMOD_OT_RefreshPrefabs(bpy.types.Operator):
    bl_idname = "ba_mod.refresh_prefabs"
    bl_label = "刷新 prefab 列表"
    bl_description = "从 bundle 读取全部 prefab（约 1 分钟，之后可搜索）"

    def execute(self, context):
        prefs = _prefs(context)
        if not prefs.bundle:
            self.report({"ERROR"}, "请先填 bundle 路径")
            return {"CANCELLED"}
        unitypy_bridge.add_paths(prefs.rev_dir, prefs.unitypy_dir)
        try:
            from extract_model import list_prefabs
            t0 = time.time()
            prefs_list = list_prefabs(prefs.bundle)
        except Exception as e:
            self.report({"ERROR"}, "读取失败：%s" % e)
            return {"CANCELLED"}
        lst = context.scene.bamod_prefabs
        lst.clear()
        q = prefs.search.strip().lower()
        addr_map = _load_address_map(prefs.bundle)
        for nm, pid in prefs_list:
            addr = addr_map.get(nm)
            if not addr or addr == nm:
                addr = _short_name(nm)
            if q and q not in addr.lower() and q not in nm.lower():
                continue
            it = lst.add()
            it.name = addr
            it.path = nm
            it.pid = str(pid)
        self.report({"INFO"}, "已列出 %d 个 prefab（%.1f 秒）" % (len(prefs_list), time.time() - t0))
        return {"FINISHED"}


class BAMOD_OT_ImportPrefab(bpy.types.Operator):
    bl_idname = "ba_mod.import_prefab"
    bl_label = "导入所选模型（挂载点+网格）"
    bl_description = "提取 prefab：挂载点树（Empty）+ 蒙皮网格（带顶点组）"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        prefs = _prefs(context)
        lst = context.scene.bamod_prefabs
        idx = context.scene.bamod_prefab_index
        if idx < 0 or idx >= len(lst):
            self.report({"ERROR"}, "请先刷新并选择一个 prefab")
            return {"CANCELLED"}
        it = lst[idx]
        pid = int(it.pid)
        unitypy_bridge.add_paths(prefs.rev_dir, prefs.unitypy_dir)
        try:
            import extract_model
            import importlib
            importlib.reload(extract_model)
            tree, order, meshes, tis = extract_model.extract_prefab(prefs.bundle, pid)
        except Exception as e:
            self.report({"ERROR"}, "提取失败：%s" % e)
            return {"CANCELLED"}
        if not tree:
            self.report({"ERROR"}, "层级为空")
            return {"CANCELLED"}
        # 世界矩阵（Blender 系，父在前拓扑序连乘）。四元数需 (x,y,z,w) -> (w,x,y,z)。
        def _local_mat(node):
            rot = node["rot"]
            q = Quaternion((rot[3], rot[0], rot[1], rot[2]))
            return Matrix.LocRotScale(Vector(node["pos"]), q, Vector(node["scale"]))
        world = {}
        for gpid in order:
            node = tree[gpid]
            lm = _local_mat(node)
            world[gpid] = (world[node["parent"]] @ lm) if (node["parent"] and node["parent"] in world) else lm

        # ParentConstraint 覆盖：节点跟随源挂载点（+偏移）。第一遍后源的世界矩阵已算好，
        # 再跑一遍让约束节点及其子孙继承覆盖后的矩阵。
        overrides = {}
        for gpid in order:
            cons = tree[gpid].get("constraint")
            if cons and cons.get("source") in world:
                to = cons["t_off"]; ro = cons["r_off"]
                b_off = Vector((to[0], -to[2], to[1]))          # Unity 平移偏移 -> Blender
                b_eul = Euler((ro[0], -ro[2], ro[1]), "XYZ")    # Unity 欧拉偏移 -> Blender
                off = Matrix.LocRotScale(b_off, b_eul.to_quaternion(), None)
                overrides[gpid] = world[cons["source"]] @ off
        if overrides:
            world2 = {}
            for gpid in order:
                node = tree[gpid]
                if gpid in overrides:
                    world2[gpid] = overrides[gpid]
                else:
                    lm = _local_mat(node)
                    world2[gpid] = (world2[node["parent"]] @ lm) if (node["parent"] and node["parent"] in world2) else lm
            world = world2

        def mount_ancestor(gpid):
            """向上找最近的挂载点 GO（跳过网格容器/LOD 结构节点）。"""
            cur = gpid
            while cur:
                node = tree.get(cur)
                if not node:
                    return None
                if node["is_mount"]:
                    return cur
                cur = node["parent"]
            return None

        bpy_objs = {}
        n_mounts = 0
        for gpid in order:
            node = tree[gpid]
            if not node["is_mount"]:
                continue  # 网格容器/LOD 结构节点不是挂载点，不建空物体
            nm = node["name"]
            e = bpy.data.objects.new(nm, None)
            anc = mount_ancestor(node["parent"])
            if anc and anc in bpy_objs:
                lm = world[anc].inverted() @ world[gpid]
                e.parent = bpy_objs[anc]
            else:
                lm = world[gpid]
            loc, quat, sca = lm.decompose()
            e.location = loc
            e.rotation_mode = "QUATERNION"
            e.rotation_quaternion = quat
            e.scale = sca
            e.empty_display_size = 0.35
            e.empty_display_type = "PLAIN_AXES"
            e["ba_mount"] = True
            e["ba_mount_cat"] = category_of(nm)
            context.collection.objects.link(e)
            bpy_objs[gpid] = e
            n_mounts += 1
        # 姿势基线（⑥ 步兵姿势：重置/姿势库用）
        if bpy_objs:
            import json as _json
            base = {}
            for _gpid, e in bpy_objs.items():
                q = e.rotation_quaternion
                base[_normalize_name(e.name)] = [q.x, q.y, q.z, q.w,
                                                 e.location.x, e.location.y, e.location.z]
            context.scene["ba_pose_baseline"] = _json.dumps(base, ensure_ascii=False)
        # 步兵骨骼显示：创建 Armature（rest 姿势 = 导入姿势），让网格随姿势变形
        arm_obj = None
        is_infantry = ("/Infantry/" in (it.path or "")) or n_mounts >= 20
        if is_infantry and bpy_objs:
            try:
                arm_data = bpy.data.armatures.new("%s_Armature" % _normalize_name(os.path.basename(it.path)))
                arm_obj = bpy.data.objects.new(arm_data.name, arm_data)
                context.collection.objects.link(arm_obj)
                context.view_layer.objects.active = arm_obj
                bpy.ops.object.mode_set(mode="EDIT")
                eb = arm_data.edit_bones
                bone_pids = {}
                for gpid in order:
                    node = tree[gpid]
                    if not node["is_mount"] or gpid not in bpy_objs:
                        continue
                    b = eb.new(_normalize_name(node["name"]))
                    # 直接指定骨骼矩阵（head/tail/roll 与导入姿势完全一致）：
                    # 这样 rest 姿势 == 导入姿势，姿势同步（basis = rest⁻¹ @ empty_world）
                    # 在未改动时恒为单位矩阵——不会像 head/tail 推断朝向那样引入扭转。
                    # 注意：eb.new() 的骨骼长度为零，matrix 赋值对零长度骨骼不重建
                    # tail 方向（方向退化、roll 错误），必须先给单位长度再赋矩阵。
                    b.head = (0.0, 0.0, 0.0)
                    b.tail = (0.0, 0.0, 1.0)
                    b.matrix = world[gpid]
                    if node["parent"] in bone_pids:
                        b.parent = bone_pids[node["parent"]]
                    bone_pids[gpid] = b
                bpy.ops.object.mode_set(mode="OBJECT")
                context.scene["ba_armature_name"] = arm_obj.name
                print("[导入] 已创建骨骼 Armature：%s（%d 骨）" % (arm_obj.name, len(arm_data.bones)))
            except Exception as _e:
                print("[导入] Armature 创建失败（姿势仍可用空物体编辑）：%s" % _e)
                if arm_obj is not None:
                    bpy.data.objects.remove(arm_obj, do_unlink=True)
                arm_obj = None
        mesh_objs = []
        n_mesh = 0
        n_vert = 0
        n_skip_lod = 0
        skip_names = []
        for m in meshes:
            if prefs.only_lod0 and not m["is_lod0"]:
                # ★★ v1.11.0（★⑯ 补充）：以前**静默跳过** ⇒ 用 `copy_full` 新加的零件（本例旋翼
                #   `Ah_1z`）不在 prefab 的 LODGroup 里 ⇒ 默认根本导不进来、用户"找不到那个网格、
                #   没法选中"，却看不到任何提示 ✗ ⇒ 至少**明确说一句**（跳过几个、叫什么、
                #   怎么让它们出来）✓
                n_skip_lod += 1
                skip_names.append(tree[m["go"]]["name"] if m.get("go") in tree else "?")
                continue
            try:
                geo = extract_model.extract_mesh_geometry(prefs.bundle, m["mesh"])
            except Exception as e:
                print("[导入] 网格 %d 读取失败: %s" % (m["mesh"], e))
                continue
            # ⛔ 槽位与 SMR.m_Bones **一一对应**：空槽（0）命名成 bone_N，
            #    绝不能跳过 —— 跳过就等于把后面所有骨骼名往前挪，权重全错 ✗（v1.8.76）
            bone_names = [tree[b]["name"] if (b and b in tree) else "bone_%d" % i
                          for i, b in enumerate(m["bones"])]
            # 物体名优先用 GO 名（更直观），内部网格名（如 blast7/Browning_body）仅作兜底
            mesh_name = (tree[m["go"]]["name"] if m["go"] in tree else "") or geo["name"] or "mesh"
            me = bpy.data.meshes.new(mesh_name)
            tris = geo["triangles"]
            faces = [tuple(tris[i:i + 3]) for i in range(0, len(tris), 3)]
            me.from_pydata(geo["positions"], [], faces)
            me.update()
            if geo["uv"]:
                uv_layer = me.uv_layers.new(name="UVMap")
                for poly in me.polygons:
                    for li in range(poly.loop_start, poly.loop_start + poly.loop_total):
                        vi = me.loops[li].vertex_index
                        if vi < len(geo["uv"]):
                            uv_layer.data[li].uv = geo["uv"][vi]
            me.update()
            obj = bpy.data.objects.new(mesh_name, me)
            mm = world.get(m["go"])
            # 导出用矩阵：root 空间（scale=1）——网格在 GO 层级的真实变换，写回时要用它
            export_mm = mm
            rb = m.get("root_bone")
            rbp = m.get("root_bind_pose")
            if mm is not None and rb and rb in world and rbp:
                # 静止位姿 = world[root_bone] @ bindpose_root（bind pose 由 Unity 系转到 Blender 系）。
                # 这只用于"显示"（把蒙皮网格缩到骨骼 root_scale 比例，与挂载点对齐）。
                Mu = Matrix((rbp[0:4], rbp[4:8], rbp[8:12], rbp[12:16]))
                Mb = _UNITY_TO_BLENDER_M @ Mu @ _UNITY_TO_BLENDER_M.transposed()
                mm = world[rb] @ Mb
            if mm is not None:
                obj.matrix_world = mm
            if export_mm is not None:
                obj["ba_mesh_export_matrix"] = [export_mm[i][j] for i in range(4) for j in range(4)]
            # 皮肤功能：记录渲染器 pid 与默认材质 pid（皮肤扫描/应用用）
            # 注意：pid 是 int64（可能为负），Blender 的 ID 属性 int 只有 32 位 → 必须存字符串
            obj["ba_renderer_pid"] = str(m["renderer"])
            obj["ba_materials"] = [str(x) for x in (m.get("materials") or [])]
            # ★v1.12.5（★㉖-续·B·甲）：记下**源材质的来历包** —— 导出时若它与"当前 bundle"不同
            #   （＝跨包情形），就把该材质与其贴图的**源字节**一起带进清单（见 `_collect_mat_srcs`）
            obj["ba_src_bundle"] = prefs.bundle or ""
            context.collection.objects.link(obj)
            for bn in bone_names:
                if bn not in obj.vertex_groups:
                    obj.vertex_groups.new(name=bn)
            for vi in range(len(geo["positions"])):
                if vi >= len(geo["weights"]) or vi >= len(geo["bones"]):
                    continue
                b0, b1 = geo["bones"][vi]
                w0, w1 = geo["weights"][vi]
                if b0 < len(bone_names) and w0 > 0:
                    obj.vertex_groups[bone_names[b0]].add([vi], w0, "REPLACE")
                if b1 != b0 and b1 < len(bone_names) and w1 > 0:
                    obj.vertex_groups[bone_names[b1]].add([vi], w1, "ADD")
            n_mesh += 1
            n_vert += len(geo["positions"])
            mesh_objs.append(obj)
        if arm_obj is not None:
            for obj in mesh_objs:
                mod = obj.modifiers.new("BA_Armature", "ARMATURE")
                mod.object = arm_obj
            print("[导入] 已给 %d 个网格挂上骨骼修改器" % len(mesh_objs))
        for t in tis:
            print("[导入] TI: %s index=%d 武器=%d" % (tree[t["go"]]["name"], t["index"], len(t["weapons"])))
        # 记录复制源：④ 构建时用「完整复制」模式（整段字节复制原 prefab，只改网格/挂点）
        context.scene["ba_copy_source_pid"] = str(pid)
        context.scene["ba_copy_source_path"] = it.path
        # ⛔ **清掉与"上一个 prefab"绑定的场景状态**：不然换模型后 ④ 面板的
        #    `bamod_anim_order`（rid 顺序）、⑤ 面板的 `ba_applied_skin_id`（当前皮肤）
        #    仍是**上一个模型**的值 —— 界面显示 B 模型、实际按 A 的数据走 ✗
        for _k in ("bamod_anim_order", "bamod_anim_types",
                   "bamod_anim_node_pids", "bamod_anim_node_names",
                   "ba_applied_skin_id", "ba_skin_hint", "ba_pose_baseline",
                   "ba_last_output_bundle"):
            if _k in context.scene:
                del context.scene[_k]
        context.scene.bamod_anims.clear()
        # ⛔ v1.8.67：文本块 `bamod_anim` 也要一起清 —— 它是**上一个模型**的动画 JSON，
        #    面板为空时构建会回退去读它 ⇒ 换模型后一构建就把上个模型的动画
        #    （连着它的 pid）写进新模型的 AnimationHub ✗（实测能复现的静默错数据）。
        _txt = bpy.data.texts.get("bamod_anim")
        if _txt is not None:
            _txt.clear()
        print("[导入] 已记录复制源：%s (root=%s)" % (it.path, pid))
        # ⛔ v1.8.53 修复 ✓✓：此处原有「收集 AnimationClip 列表（⑥ 从动画读取姿势）」的收尾打印 ✗
        #   `if is_infantry: print("... %s 个" % _n_clips)` ✗
        #   ⇒ 但 v1.8.52 删掉 ⑥ 段时，给 `_n_clips` 赋值的 `_collect_pose_clips(...)` 一并被删 ✗
        #   ⇒ 留下 **`NameError: _n_clips 未定义`** ✗✗ —— 且第 488 行的判定是
        #      `is_infantry = ("/Infantry/" in path) or **n_mounts >= 20**` ✗
        #   ⇒ **挂载点 ≥20 的载具（如 F15EX）也会走进来** ✗ ⇒ 导入时报错 ✗
        #   ⇒ 症状：**模型其实已导入成功** ✓（该行在建模之后 ✓），但 Blender 弹红字 ✗、
        #      且后面的「导入完成」提示与 `return {"FINISHED"}` 都跑不到 ✗
        #   ⇒ 整块已删除 ✓（步兵动画功能已下线，本就无需再收集剪辑列表 ✓）
        self.report({"INFO"}, "导入完成：%d 挂载点 / %d 网格 / %d 顶点" % (n_mounts, n_mesh, n_vert))
        if n_skip_lod:
            # ★⑯ 补充：把"被 LOD0 过滤掉"的零件**明说出来**（否则用户以为模型没导进来）
            msg = ("有 %d 个网格因**不在 prefab 的 LODGroup 里**被「只导入 LOD0」跳过了：%s%s"
                   " ⇒ 若其中就有你要改的零件（例如移植过来的旋翼），到插件偏好里**关掉"
                   "「只导入 LOD0」**再导入一次即可（它们本来就不是 LOD 层级的一部分）"
                   % (n_skip_lod, "、".join(skip_names[:6]),
                      "…" if n_skip_lod > 6 else ""))
            print("[导入] ⚠ " + msg)
            self.report({"WARNING"}, msg)
        # ★★ 追加令「甲」：**导入时消偏** —— 把父级逆变换整条链折平（自顶向下、连同子孙）
        #   ⇒ `matrix_parent_inverse` 恒为单位 ⇒ **视图位置从第一步就等于游戏位置** ✓
        #   ⛔ 只动 mpi（`game_matrix` ＝导出器口径逐对象不变）⇒ ⛔ 不改导出器行为（硬要求 a）
        #   ⛔ 逐对象核算，任何一处对不上就回滚并点名（⛔ 不静默）
        try:
            _nrm = normalize_parent_inverse_topdown(
                list(bpy_objs.values()) + list(mesh_objs) + ([arm_obj] if arm_obj else []))
            import json as _json
            context.scene["ba_import_normalize"] = _json.dumps(
                {"checked": _nrm["checked"],
                 "fixed": len(_nrm["fixed"]),
                 "fixed_names": [x[0] for x in _nrm["fixed"]],
                 "moved_m": [[x[0], round(x[3], 6)] for x in _nrm["fixed"]],
                 "skipped": [[n, w] for n, w in _nrm["skipped"]]}, ensure_ascii=False)
            if _nrm["fixed"] or _nrm["skipped"]:
                print("[导入] 父级逆变换折平：检查 %d 个对象 ⇒ 折平 %d 处%s"
                      % (_nrm["checked"], len(_nrm["fixed"]),
                         ("" if not _nrm["skipped"] else
                          "，跳过 %d 处（%s）" % (len(_nrm["skipped"]),
                                                 "; ".join("%s：%s" % (n, w) for n, w in _nrm["skipped"][:3])))))
                for _nm, _vb, _va, _mv in _nrm["fixed"]:
                    print("      · %-28s 视图移动 %.4f m ⇒ 现在等于游戏位置" % (_nm, _mv))
        except Exception as _e:                               # noqa: BLE001
            print("[导入] ⚠ 父级逆变换折平失败（%s: %s）⇒ 视图可能与游戏不一致，请跑一次『一致性自检』"
                  % (type(_e).__name__, _e))
        return {"FINISHED"}

# ---------------------------------------------------------------------------
# ② 挂载点
# ---------------------------------------------------------------------------
def _refresh_mounts(context):
    lst = context.scene.bamod_mounts
    lst.clear()
    prefs = _prefs(context)
    catf = prefs.cat_filter
    for o in bpy.data.objects:
        if o.type != "EMPTY" or not o.get("ba_mount"):
            continue
        nm = _normalize_name(o.name)
        cat = o.get("ba_mount_cat", category_of(nm))
        if catf != "ALL" and cat != catf:
            continue
        depth = 0
        p = o.parent
        while p:
            depth += 1
            p = p.parent
        it = lst.add()
        it.obj = o.name
        it.name = nm
        it.cat = cat
        it.depth = depth


class BAMOD_OT_RefreshMounts(bpy.types.Operator):
    bl_idname = "ba_mod.refresh_mounts"
    bl_label = "刷新挂载点列表"

    def execute(self, context):
        _refresh_mounts(context)
        return {"FINISHED"}


class BAMOD_OT_SelectMount(bpy.types.Operator):
    bl_idname = "ba_mod.select_mount"
    bl_label = "选中/定位"

    def execute(self, context):
        _refresh_mounts(context)
        lst = context.scene.bamod_mounts
        idx = context.scene.bamod_mount_index
        if 0 <= idx < len(lst):
            o = bpy.data.objects.get(lst[idx].obj)
            if o:
                bpy.ops.object.select_all(action="DESELECT")
                o.select_set(True)
                context.view_layer.objects.active = o
                return {"FINISHED"}
        self.report({"ERROR"}, "列表为空，请刷新")
        return {"CANCELLED"}


class BAMOD_OT_AddMount(bpy.types.Operator):
    bl_idname = "ba_mod.add_mount"
    bl_label = "添加挂载点"
    bl_description = "在选中物体（或光标）处创建挂载点；名字可查词典"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        prefs = _prefs(context)
        nm = prefs.mount_new_name.strip()
        if not nm:
            self.report({"ERROR"}, "请填挂载点名字")
            return {"CANCELLED"}
        parent = context.active_object
        e = bpy.data.objects.new(nm, None)
        e.empty_display_size = 0.35
        e.empty_display_type = "PLAIN_AXES"
        e["ba_mount"] = True
        e["ba_mount_cat"] = category_of(nm)
        warn = None
        if parent and parent.type == "EMPTY" and parent.get("ba_mount"):
            e.parent = parent
        elif parent and parent.type == "EMPTY":
            # 是 Empty 但不是挂载点（LOD/网格容器节点）——它们不进挂载点树，
            # 构树时会被跳过 ⇒ 新挂载点会变成"根"、脱离 prefab 层级 ✗
            e.parent = parent
            warn = "父 %s 不是挂载点（没有 ba_mount 标记），构建时可能被当成独立根节点" % parent.name
        elif parent and parent.type == "MESH":
            # ⛔ 网格不进挂载点树 ⇒ 父子关系会丢，构建时新节点变成根节点、挂不到车体上 ✗
            e.location = parent.matrix_world.translation
            warn = ("选中了**网格** %s —— 挂载点的父级必须是**挂载点 Empty**，"
                    "已改为放在该位置（请改选挂载点 Empty 再添加一次）" % parent.name)
        else:
            e.location = context.scene.cursor.location
            # ⛔ v1.8.77：什么都没选中时，新挂载点**没有父级** ⇒ 构建时它会被写成
            #    prefab 里的**独立根节点**（和车体平级），转起来不会跟着车动 ✗
            #    以前这里不提示，用户只能靠"游戏里旋翼飘在原点"自己发现。
            warn = ("当前**没有选中任何物体** ⇒ 新挂载点没有父级，会落在 3D 游标处并成为"
                    "**独立根节点**，构建后**不跟着车体动**。请先在**大纲视图**里点中父挂载点"
                    "（如 `body`）再点「添加挂载点」")
        context.collection.objects.link(e)
        _refresh_mounts(context)
        cat = next((c for c in MOUNT_CATEGORIES if c["id"] == category_of(nm)), None)
        if warn:
            self.report({"WARNING"}, "已添加 %s，但：%s" % (nm, warn))
        else:
            self.report({"INFO"}, "已添加 %s（%s）%s"
                        % (nm, cat["name"] if cat else "挂载点",
                           "，父级 = %s" % e.parent.name if e.parent else ""))
        return {"FINISHED"}


class BAMOD_OT_RemoveMount(bpy.types.Operator):
    bl_idname = "ba_mod.remove_mount"
    bl_label = "删除选中挂载点"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        o = context.active_object
        if not o or o.type != "EMPTY" or not o.get("ba_mount"):
            self.report({"ERROR"}, "请选中一个挂载点（Empty）")
            return {"CANCELLED"}
        # ⛔ 递归收集**全部后代**：旧实现只删直接子级 ⇒ 三级链
        #    （Rotorangle_0 → Rotorangle_1 → X）里的孙辈会残留，而且失去父级后
        #    变成"根挂载点"，进而影响构树取根（构建出错的模型）✗
        kids = []
        stack = list(o.children)
        while stack:
            c = stack.pop()
            if c.type == "EMPTY" and c.get("ba_mount"):
                kids.append(c)
            stack.extend(c.children)
        nm = o.name
        bpy.data.objects.remove(o, do_unlink=True)
        for k in kids:
            if k.name in bpy.data.objects:      # 可能已被父级连带删除
                bpy.data.objects.remove(k, do_unlink=True)
        _refresh_mounts(context)
        self.report({"INFO"}, "已删除 %s 及其子挂载点 %d 个" % (nm, len(kids)))
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# ③ 工具
# ---------------------------------------------------------------------------
class BAMOD_OT_ComputeCRC(bpy.types.Operator):
    bl_idname = "ba_mod.compute_crc"
    bl_label = "计算 CRC 并更新 catalog"
    bl_description = "对偏好里的输出 bundle 计算 CRC 并写入游戏 catalog.json（1-2 分钟）"

    def execute(self, context):
        prefs = _prefs(context)
        unitypy_bridge.add_paths(prefs.rev_dir, prefs.unitypy_dir)
        try:
            import finalize_crc
            import importlib
            importlib.reload(finalize_crc)
        except ImportError:
            self.report({"ERROR"}, "找不到 finalize_crc.py（_rev_tools 目录）")
            return {"CANCELLED"}
        # CRC 针对「部署到游戏目录」的 bundle；.bamod / 临时输出不算
        gd = detect_game_dir()
        target = (detect_bundle(gd) if gd else None) or prefs.bundle
        if not target or not target.endswith(".bundle"):
            self.report({"ERROR"}, "请把构建产物复制到游戏目录的 units_assets_all_*.bundle 后再算 CRC")
            return {"CANCELLED"}
        try:
            msg = finalize_crc.run(target)
        except Exception as e:
            self.report({"ERROR"}, "CRC 失败：%s" % e)
            return {"CANCELLED"}
        self.report({"INFO"}, "CRC 完成：%s" % msg)
        return {"FINISHED"}


class BAMOD_OT_TransferWeights(bpy.types.Operator):
    bl_idname = "ba_mod.transfer_weights"
    bl_label = "权重转移（原 → 新模型）"
    bl_description = "用 KD-tree 最近邻把参考网格的顶点组权重转到新模型"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        target = context.active_object
        if target is None or target.type != "MESH":
            self.report({"ERROR"}, "请选中你的新模型（网格）")
            return {"CANCELLED"}
        sources = [o for o in bpy.data.objects if o.type == "MESH" and o != target and o.data.vertices]
        if not sources:
            self.report({"ERROR"}, "场景里没有参考网格（先导入游戏模型）")
            return {"CANCELLED"}
        source = max(sources, key=lambda o: (len(o.vertex_groups), len(o.data.vertices)))
        try:
            from mathutils.kdtree import KDTree
            src_me = source.data
            dst_me = target.data
            src_mw = source.matrix_world
            dst_mw = target.matrix_world
            src_pos = [src_mw @ v.co for v in src_me.vertices]
            src_groups = [{g.group: g.weight for g in v.groups} for v in src_me.vertices]
            gname_by_index = {g.index: _normalize_name(g.name) for g in source.vertex_groups}
            tg = {}
            for gi, gname in gname_by_index.items():
                g = target.vertex_groups.get(gname)
                if g is None:
                    g = target.vertex_groups.new(name=gname)
                tg[gi] = g
            kd = KDTree(len(src_pos))
            for i, p in enumerate(src_pos):
                kd.insert(p, i)
            kd.balance()
            for v in dst_me.vertices:
                co, si, _ = kd.find(dst_mw @ v.co)
                for gi, w in src_groups[si].items():
                    if w > 0:
                        tg[gi].add([v.index], w, "REPLACE")
            target.data.update()
        except Exception as e:
            self.report({"ERROR"}, "权重转移失败：%s" % e)
            return {"CANCELLED"}
        self.report({"INFO"}, "权重转移完成（源：%s）" % source.name)
        return {"FINISHED"}

# ---------------------------------------------------------------------------
# 面板
# ---------------------------------------------------------------------------
class BAMOD_OT_CopyPath(bpy.types.Operator):
    """把 ① 面板里选中 prefab 的完整容器路径复制到剪贴板。"""

    bl_idname = "ba_mod.copy_path"
    bl_label = "复制路径"
    bl_description = ("复制 ① 里所选 prefab 的完整容器路径到剪贴板"
                      "（可直接粘到「新 prefab 内部路径」）")
    bl_options = {"REGISTER"}

    text: bpy.props.StringProperty(default="")

    def execute(self, context):
        t = (self.text or "").strip()
        if not t:
            self.report({"WARNING"}, "没有可复制的路径")
            return {"CANCELLED"}
        context.window_manager.clipboard = t
        self.report({"INFO"}, "已复制路径：%s" % t)
        return {"FINISHED"}


class BAMOD_PT_Import(bpy.types.Panel):
    bl_label = "① 模型导入"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "BA Mod"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        prefs = _prefs(context)
        scene = context.scene
        layout.operator("ba_mod.refresh_prefabs", icon="FILE_REFRESH")
        layout.prop(prefs, "search", icon="VIEWZOOM")
        layout.prop(prefs, "only_lod0")
        layout.template_list("BA_MOD_UL_prefabs", "", scene, "bamod_prefabs",
                             scene, "bamod_prefab_index", rows=6)
        if 0 <= scene.bamod_prefab_index < len(scene.bamod_prefabs):
            it = scene.bamod_prefabs[scene.bamod_prefab_index]
            if it.path:
                box = layout.box()
                r = box.row(align=True)
                r.label(text="实际路径：", icon="FILE_TEXT")
                _op = r.operator("ba_mod.copy_path", text="复制路径", icon="COPYDOWN")
                _op.text = it.path
                box.label(text=it.path)
        layout.operator("ba_mod.import_prefab", icon="IMPORT")


class BAMOD_PT_Mounts(bpy.types.Panel):
    bl_label = "② 挂载点"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "BA Mod"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        prefs = _prefs(context)
        scene = context.scene
        row = layout.row(align=True)
        row.prop(prefs, "cat_filter", text="")
        row.operator("ba_mod.refresh_mounts", text="", icon="FILE_REFRESH")
        layout.template_list("BA_MOD_UL_mounts", "", scene, "bamod_mounts",
                             scene, "bamod_mount_index", rows=8)
        row = layout.row(align=True)
        row.operator("ba_mod.select_mount", icon="RESTRICT_SELECT_OFF")
        row.operator("ba_mod.remove_mount", icon="X")
        layout.separator()
        layout.prop(prefs, "mount_new_name")
        layout.operator("ba_mod.add_mount", icon="ADD")
        lst = scene.bamod_mounts
        idx = scene.bamod_mount_index
        if 0 <= idx < len(lst):
            it = lst[idx]
            cat = next((c for c in MOUNT_CATEGORIES if c["id"] == it.cat), None)
            box = layout.box()
            box.label(text="%s  —  %s" % (it.name, cat["name"] if cat else it.cat))
        # ★⑬ 挑挂载点时最需要"游戏坐标"（视图不可信时的唯一依据）✓
        layout.separator()
        _draw_game_coord(layout, context)


# ---------------------------------------------------------------------------
# ★⑫/★⑬ 游戏坐标（父链复合后的位置）—— 显示 / 编辑 / 摊平父级逆变换
#
# 为什么需要（飞行ACV 实机教训，都是**实测**结论）：
#   · Blender 视图里的"位置"是**局部 `loc`**；游戏读的是**沿父链复合后**的坐标。
#     导出器**只累加各级 `loc`、完全忽略 `matrix_parent_inverse`**
#     （实测：`Rotorangle_0` 的逆变换偏移 = `-body.loc`，构建产物里该节点世界位置 = `body.loc + loc`）
#     ⇒ 两者只有在"链条无旋转 + 无父级逆变换"时才相等 ✗
#   · 后果：**视图里在 A 处、游戏里在 B 处** —— "旋翼绕隐藏轴转"那轮排查全程被它误导
#     （改完数值看视图，永远判不出对错）。
#   · 网格还有一层：网格位置由导入时记下的 `obj["ba_mesh_export_matrix"]` 决定（不是 `matrix_world`）
#     ⇒ 面板对网格必须显示**这个属性**，否则会给出让人误判的数。
# ---------------------------------------------------------------------------
def _basis_matrix(obj):
    """对象的 `matrix_basis`（= loc/rot/scale 本身；**不含** `matrix_parent_inverse`）。"""
    return obj.matrix_basis.copy()


def game_matrix(obj):
    r"""从根到 `obj` 沿父链连乘 `matrix_basis`（**不含** `matrix_parent_inverse`）。

    ⛔ 通用式是**矩阵连乘**，不是"各级 loc 相加" —— 只有"全链无旋转"时两者才相等
      （本项目恰好如此，所以早期用 Σloc 也得到同样结果）✓
    """
    chain = []
    o = obj
    while o is not None:
        chain.append(o)
        o = o.parent
    m = Matrix.Identity(4)
    for o in reversed(chain):
        m = m @ _basis_matrix(o)
    return m


def game_coord(obj):
    """★ 游戏里读到该节点的世界位置 = `game_matrix(obj).translation`。"""
    if obj is None:
        return None
    return game_matrix(obj).translation.copy()


def parent_game_matrix(obj):
    """父链（到父级为止）复合后的矩阵 —— 反算 `loc` 就用它。"""
    m = Matrix.Identity(4)
    chain = []
    o = obj.parent if obj is not None else None
    while o is not None:
        chain.append(o)
        o = o.parent
    for o in reversed(chain):
        m = m @ _basis_matrix(o)
    return m


def has_nonuniform_scale_ancestor(obj):
    """祖先链里有没有**非均匀缩放**（→ 摊平会把剪切丢掉，实测误差可达 0.2466）。

    返回第一个有问题的对象名（没有则 None）。
    """
    o = obj
    while o is not None:
        s = o.scale
        if abs(s[0] - s[1]) > 1e-4 or abs(s[1] - s[2]) > 1e-4 or abs(s[0] - s[2]) > 1e-4:
            return o.name
        o = o.parent
    return None


def has_nonidentity_parent_inverse(obj):
    """这个对象自己有没有非单位 `matrix_parent_inverse`（→ 视图不可信）。"""
    if obj is None:
        return False
    m = obj.matrix_parent_inverse
    for r in range(4):
        for c in range(4):
            want = 1.0 if r == c else 0.0
            if abs(m[r][c] - want) > 1e-6:
                return True
    return False


def normalize_parent_inverse_topdown(objs, verify=True):
    r"""把父级逆变换**折平**：自顶向下遍历给定子树，逐个把 `matrix_parent_inverse` 置为单位。

    ★ 为什么要在**导入时**做（追加令「甲」）：导出器**只累加各级 loc、完全忽略
      `matrix_parent_inverse`** ⇒ 链上只要有一处非单位逆变换，Blender 里看到的**位置**就与游戏不一致；
      导入时把这条链折平 ⇒ **视图从第一步就等于游戏位置** ✓（用户原话要的就是这个）。

    ⛔ 语义（与硬要求 (d) 对齐）：**只把 `mpi` 置单位，⛔ 不动 `loc/rot/scale`**
      ⇒ `game_matrix()`（＝导出器口径）**逐对象逐元素不变** ✓，变的只是"视图位置"，而那正是要修的。
      ⚠ 另一种写法（`basis := mpi @ basis; mpi := I`）会**保住视图**、却把游戏位置改掉 ⇒ ⛔ 不是本函数语义。

    ⛔ 全链：给定对象**连同其全部子孙**一起遍历（父级有偏移时子级也一并处理，⛔ 不只平一层），
      执行顺序＝**父先子后**。
    ⛔ 跳过规则沿用现成算子：祖先链里有**非均匀缩放**时跳过（逆变换可能含剪切）并点名。
    ⛔ 任何时候都不静默：`verify=True` 时逐对象核算"折前/折后 `game_matrix` 相同"，
      对不上就**回滚该对象**并记进 skipped。

    返回 dict：{"checked": N, "fixed": [(名, 折前视图, 折后视图, 位移量 m)], "skipped": [(名, 原因)]}
    """
    seen, stack = [], list(objs or [])
    while stack:
        o = stack.pop()
        if o is None or o in seen:
            continue
        seen.append(o)
        stack.extend([c for c in (list(getattr(o, "children", []) or []))])

    def _depth(o):
        d, p = 0, o.parent
        while p is not None:
            d += 1
            p = p.parent
        return d

    seen.sort(key=_depth)                      # 父先子后
    fixed, skipped = [], []
    for o in seen:
        if not has_nonidentity_parent_inverse(o):
            continue
        bad = has_nonuniform_scale_ancestor(o)
        if bad:
            skipped.append((o.name, "祖先 %s 是非均匀缩放 ⇒ 逆变换可能含剪切，跳过" % bad))
            continue
        keep = o.matrix_parent_inverse.copy()
        gm_before = game_matrix(o)
        view_before = o.matrix_world.translation.copy()
        o.matrix_parent_inverse = Matrix.Identity(4)          # ★ 就是这一笔：只动 mpi
        try:
            bpy.context.view_layer.update()
        except Exception:                                     # noqa: BLE001
            pass
        view_after = o.matrix_world.translation.copy()
        if verify:
            gm_after = game_matrix(o)
            if not all(abs(gm_before[r][c] - gm_after[r][c]) <= 1e-9
                       for r in range(4) for c in range(4)):
                o.matrix_parent_inverse = keep                # ⛔ 回滚：绝不静默留错
                skipped.append((o.name, "折平后 game_matrix 变了（已回滚）"))
                continue
        fixed.append((o.name, tuple(view_before), tuple(view_after),
                      float((view_after - view_before).length)))
    return {"checked": len(seen), "fixed": fixed, "skipped": skipped}





def mesh_geo_center(obj):
    r"""网格的**几何中心**（全部顶点世界坐标的平均，再按 `ba_mesh_export_matrix` 变换）。

    ★ 这就是用户 `Shift+S → 游标 → 选中项` 量到的那个数；
    ⛔ 网格的**对象原点根本不是几何中心**（实测：`Ah_1z.001` 的原点在 `(0,0,0)`，而桨盘顶点重心
      在 z=3.78 处，差 3.78 m）⇒ 只显示"对象原点"会把用户带偏。
    """
    if obj is None or obj.type != "MESH" or not obj.data:
        return None
    em = obj.get("ba_mesh_export_matrix")
    if em and len(em) == 16:
        m = Matrix((em[0:4], em[4:8], em[8:12], em[12:16]))
    else:
        m = obj.matrix_world
    n = len(obj.data.vertices)
    if not n:
        return None
    acc = Vector((0.0, 0.0, 0.0))
    for v in obj.data.vertices:
        acc += m @ v.co
    return acc / n


def subtree_root_for_align(obj):
    r"""★⑬ 的"对齐要加到哪个节点"：沿父链往上找**宿主骨骼**，返回**接缝节点**。

    判定（与 `自制mod\_tools\spin_axis_check.py` 同一套）：沿父链往上走，
    第一个拥有 ≥2 个子物体的节点 = 宿主骨骼 ⇒ **它的下一个节点**就是接缝节点（移植子树的最顶）。
    ⛔ **别加到更下面的节点** —— 会双倍/反向偏（旋翼那轮就踩过）。
    """
    o = obj
    prev = obj
    while o is not None:
        if len(o.children) >= 2:
            return prev
        prev = o
        o = o.parent
    return obj


def _draw_game_coord(layout, context, node_name=None, box=None):
    r"""★⑬ 画「游戏坐标」板块（**只读显示 + 一键编辑按钮**）。

    ⛔ 为什么编辑用**算子弹窗**而不是面板里的三个数字框：`draw()` 里若每次都把存储属性从
      `obj` 重新读一遍，用户正在输入的数字会被**立刻回弹覆盖**（Blender 面板经典坑）✗
      ⇒ 算子 + `invoke_props_dialog` 天然没有这个问题，而且能按对象自动预填当前值 ✓
    返回：`(文本, 是否一致)`。
    """
    obj = context.active_object
    if obj is None:
        return None, True
    col = box or layout.box()
    g = game_coord(obj)
    view = obj.matrix_world.translation
    mesh = mesh_geo_center(obj)
    # ★[界面-02] ① 对齐：三行**必须同 icon**。原实现首行带 `icon="ORIENTATION_GLOBAL"`、
    #   后两行不带 ⇒ 首行文字整体右移**一个图标宽**，三列数字在视觉上对不齐（用户点名）。
    #   ⇒ 三行统一同一个 icon（同宽 ⇒ 文字左边界对齐）＋ 数字一律**定宽 `%+9.4f`**
    #   （含符号位、宽度固定 ⇒ 小数点与 X/Y/Z 三列都对齐）。
    _ci = "ORIENTATION_GLOBAL"
    col.label(text="游戏坐标: X %+9.4f  Y %+9.4f  Z %+9.4f" % (g[0], g[1], g[2]), icon=_ci)
    col.label(text="视图坐标: X %+9.4f  Y %+9.4f  Z %+9.4f" % (view[0], view[1], view[2]), icon=_ci)
    if mesh is not None:
        col.label(text="网格几何中心: X %+9.4f  Y %+9.4f  Z %+9.4f" % (mesh[0], mesh[1], mesh[2]),
                  icon=_ci)
    # ★[界面-02] ② 置灰**原因写进界面本身**，并把**已有入口挪到数字正下方**（数字视线内）。
    #   产品不是"改不动"，而是**故意不在这三行做内联输入**：`draw()` 每次都会从 `obj` 重读，
    #   用户正在输入的数字会被**立刻回弹覆盖**（Blender 面板经典坑，见本函数 docstring）。
    #   ⇒ 编辑能力走算子弹窗 `ba_mod.set_game_coord`（`invoke_props_dialog`，按对象自动预填当前值）；
    #   本次改写只负责让用户**看见它**（能力本来就有，只是原来被压在三条告警之后 = 看不见）。
    col.label(text="（三行只读：面板内直接编辑会被 draw() 回弹覆盖 ⇒ 改坐标用下面按钮）", icon="INFO")
    row = col.row(align=True)
    row.operator("ba_mod.set_game_coord", text="✏ 输入游戏坐标…", icon="DRIVER")
    if mesh is not None:
        row.operator("ba_mod.align_to_mesh_center", text="对齐到网格中心", icon="SNAP_ON")
    same = (g - view).length < 1e-4
    if not same:
        col.label(text="⚠ 两者不等 ⇒ 视图不可信（该对象带父级逆变换）", icon="ERROR")
        if has_nonuniform_scale_ancestor(obj):
            col.label(text="⚠ 祖先链里有非均匀缩放 ⇒ 摊平会丢剪切、已禁用", icon="ERROR")
        else:
            col.operator("ba_mod.flatten_parent_inverse", icon="MODIFIER")
    col.label(text="（游戏坐标 = 父链复合后的位置，和「位置」那个 loc 不是一个东西）",
              icon="INFO")
    # ★25（v1.12.4 候选）：一键**场景级**一致性自检 —— 与上面的『摊平父级逆变换』**同面板并排**
    #   （同族问题一次给用户：先自检看有几个不一致，再逐个摊平）
    col.separator()
    col.operator("ba_mod.selfcheck_xform", icon="CHECKMARK")
    # ★㉔ 档1(1-d)：自检结果**在 UI 里可见**（汇总 ＋ 前 N 条明细 ＋ 判不了 ＋ 边界句）
    _sr = None
    try:
        import json as _json2
        _raw2 = context.scene.get(XFORM_SELFCHECK_KEY)
        _sr = _json2.loads(_raw2) if _raw2 else None
    except Exception:                                               # noqa: BLE001
        _sr = None
    if _sr:
        _n_bad, _n_unk = _sr.get("bad", 0), _sr.get("unknown", 0)
        _n_disp = _sr.get("disp_export", 0)
        col.label(text="自检：一致 %d ／ 显示≠导出 %d ／ 不一致 %d ／ 判不了 %d（分母 %d）"
                  % (_sr.get("clean", 0), _n_disp, _n_bad, _n_unk, _sr.get("total", 0)),
                  icon=("ERROR" if _n_bad else ("QUESTION" if (_n_unk or _n_disp) else "CHECKMARK")))
        for _di in (_sr.get("disp_items") or [])[:5]:
            col.label(text="  ◐ %s：%s（**需人判**）" % (_di.get("name"), _di.get("worst") or ""),
                      icon="QUESTION")
        for _it in (_sr.get("items") or [])[:5]:
            col.label(text="  ✗ %s：%s" % (_it.get("name"), _it.get("worst") or ""), icon="DOT")
        if _n_unk:
            col.label(text="  ? 判不了：%s" % ", ".join(_sr.get("unknown_names") or []),
                      icon="QUESTION")
        col.label(text="⛔ Blender 侧口径一致 ≠ 实机一致（约束/动画不复现）", icon="INFO")
    return ("X %.4f  Y %.4f  Z %.4f" % (g[0], g[1], g[2])), same


class BAMOD_OT_SetGameCoord(bpy.types.Operator):
    """★⑬ 输入**游戏坐标**（父链复合后的位置），插件内部反算 `loc`。"""
    bl_idname = "ba_mod.set_game_coord"
    bl_label = "设置游戏坐标（父链复合后的位置）"
    bl_description = ("输入的目标是**游戏里读到的世界位置**；插件用父链矩阵反算 loc。"
                      "⚠ 父级逆变换没清空时视图仍会对不上 ⇒ 先点「摊平父级逆变换」")
    bl_options = {"REGISTER", "UNDO"}

    gx: bpy.props.FloatProperty(name="X", default=0.0)
    gy: bpy.props.FloatProperty(name="Y", default=0.0)
    gz: bpy.props.FloatProperty(name="Z", default=0.0)
    apply_to_seam: bpy.props.BoolProperty(
        name="整额加到「接缝节点」", default=False,
        description="勾上则把 Δ=(目标−当前) 整额加到移植子树最顶那个节点（宿主骨骼的下一个），"
                    "而不是只改这一个节点")

    @classmethod
    def poll(cls, context):
        return context.active_object is not None

    def invoke(self, context, event):
        obj = context.active_object
        g = game_coord(obj) or Vector((0, 0, 0))
        self.gx, self.gy, self.gz = g[0], g[1], g[2]
        return context.window_manager.invoke_props_dialog(self, width=320)

    def execute(self, context):
        obj = context.active_object
        target = Vector((self.gx, self.gy, self.gz))
        tgt_obj = subtree_root_for_align(obj) if self.apply_to_seam else obj
        cur = game_coord(tgt_obj) or Vector((0, 0, 0))
        delta = target - cur
        if delta.length < 1e-9:
            self.report({"INFO"}, "游戏坐标没变")
            return {"FINISHED"}
        wp = parent_game_matrix(tgt_obj)
        try:
            new_loc = wp.inverted() @ (game_coord(tgt_obj) + delta)
        except ValueError:
            self.report({"ERROR"}, "父链矩阵不可逆（含退化缩放？）⇒ 没法反算 loc")
            return {"CANCELLED"}
        tgt_obj.location = new_loc
        # ⛔ 直写 `obj.location` 会覆盖约束/驱动的结果 ⇒ 带约束时要提示（不静默）
        if len(tgt_obj.constraints):
            self.report({"WARNING"}, "该对象有 %d 个约束：直接写 location 会覆盖约束结果"
                                     % len(tgt_obj.constraints))
        self.report({"INFO"}, "已设 %s 的游戏坐标 = (%.4f, %.4f, %.4f)"
                    % (tgt_obj.name, target[0], target[1], target[2]))
        return {"FINISHED"}


class BAMOD_OT_AlignToMeshCenter(bpy.types.Operator):
    """★⑬ 一键：把节点轴心对齐到**选中网格的几何中心**。"""
    bl_idname = "ba_mod.align_to_mesh_center"
    bl_label = "把节点轴心对齐到选中网格的几何中心"
    bl_description = ("Δ = 网格几何中心 − 节点游戏坐标，整额加到移植子树最顶那个节点"
                      "（⛔ 别加到更下面的节点，会双倍/反向偏）")
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return (context.active_object is not None
                and any(o.type == "MESH" for o in context.selected_objects))

    def execute(self, context):
        meshes = [o for o in context.selected_objects if o.type == "MESH"]
        if not meshes:
            self.report({"ERROR"}, "先选中一个网格")
            return {"CANCELLED"}
        c = mesh_geo_center(meshes[0])
        if c is None:
            self.report({"ERROR"}, "这个网格没有顶点（算不出几何中心）")
            return {"CANCELLED"}
        node = context.active_object if context.active_object.type != "MESH" else subtree_root_for_align(meshes[0])
        tgt = subtree_root_for_align(node)
        cur = game_coord(tgt)
        delta = c - cur
        tgt.location = parent_game_matrix(tgt).inverted() @ (cur + delta)
        self.report({"INFO"}, "已把 %s 对齐到 %s 的几何中心（Δ=(%.4f, %.4f, %.4f)）"
                    % (tgt.name, meshes[0].name, delta[0], delta[1], delta[2]))
        return {"FINISHED"}


class BAMOD_OT_FlattenParentInverse(bpy.types.Operator):
    r"""★⑫ **摊平父级逆变换**：让 Blender 视图坐标 = 游戏坐标（**纯显示层，不改导出结果**）。

    ⛔ 千万别用 Blender 的「物体 → 应用 → 父级逆变换」(`object.parent_inverse_apply`)：
      它把逆变换**烧进 `loc`** ⇒ 视图是对齐了，但**导出结果跟着变 ⇒ 实机反而会偏** ✗
    ✓ 正确做法 = `parent_clear(type='CLEAR_INVERSE')` 的等价操作：**只把 `matrix_parent_inverse`
      归零、`loc` 一个字节都不动** ⇒ 导出器本来就不读它 ⇒ **构建产物完全不变** ✓
    ⚠ 代价（实测，用户要知情）：**视图里对象会跳到游戏里的位置** —— 那正是这次操作的目的 ✓
    ⚠ 安全阀：祖先链里有**非均匀缩放**时**跳过并警告**（逆变换含剪切，`loc/rot/scale` 表达不了，
      实测误差 0.2466）✗
    ⛔ 只对**选中对象**生效（不全局改）：那些部件本来就是新对象、无历史包袱 ✓
    """
    bl_idname = "ba_mod.flatten_parent_inverse"
    bl_label = "摊平父级逆变换（让视图 = 游戏坐标）"
    bl_description = ("把选中对象的『父级逆变换』归零（不改 loc）⇒ 视图位置从此等于游戏位置。"
                      "导出结果不受影响（导出器本来就不读它）")
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return any(has_nonidentity_parent_inverse(o) for o in context.selected_objects)

    def execute(self, context):
        done, skipped, jumped = [], [], []
        for o in list(context.selected_objects):
            if not has_nonidentity_parent_inverse(o):
                continue
            bad = has_nonuniform_scale_ancestor(o)
            if bad:
                skipped.append("%s（祖先 %s 是非均匀缩放 ⇒ 逆变换可能含剪切，跳过）" % (o.name, bad))
                continue
            before_view = o.matrix_world.translation.copy()
            o.matrix_parent_inverse = Matrix.Identity(4)
            after_view = o.matrix_world.translation.copy()
            if (after_view - before_view).length > 1e-6:
                jumped.append("%s（视图跳了 %.4f m ⇒ 现在视图=游戏）"
                              % (o.name, (after_view - before_view).length))
            done.append(o.name)
        for m in skipped:
            self.report({"WARNING"}, m)
        for m in jumped:
            self.report({"INFO"}, m)
        if not done:
            self.report({"WARNING"}, "选中的对象里没有带父级逆变换的（没改任何东西）")
            return {"CANCELLED"}
        self.report({"INFO"}, "已摊平 %d 个对象：%s（loc 未改动 ⇒ 导出结果不变）"
                    % (len(done), ", ".join(done[:6])))
        return {"FINISHED"}


# ★㉔ 档1：自检算子口径常量（⛔ 只读算子；三项阈值写在这里 ⇒ 引用时连口径一起引）
XFORM_SELFCHECK_KEY = "ba_xform_selfcheck"          # 1-d：UI 明细落点（场景属性，JSON 串）
XFORM_THRESH = {"pos_m": 1e-4, "rot_deg": 0.05, "scale_ratio": 1e-4}   # 位置 m ／ 朝向 ° ／ 缩放比
XFORM_BOUNDARY = ("⛔ 边界：约束驱动节点（Aim/RotationConstraint）与动画在 Blender 侧**不复现** ⇒ "
                  "本条只证「**Blender 侧口径一致**」，≠ 实机一致（最终姿态以游戏内为准）")


def xform_delta(obj):
    r"""★㉔ 档1(1-a)：逐对象算「视图」与「将写回的值」的**三项**偏差（⛔ 只读）。

    返回 `(delta, why)`：
      delta = {"pos_m": 米, "rot_deg": 度, "scale_ratio": 最大比值偏差}（单项算不出 ⇒ 该项 None）
      why   = **算不出**时的原因串 ⇒ 调用方必须把这类对象计进「判不了 N」（⛔ 不静默丢）
    ⛔ 不改任何 loc/rot/scale、不动 `matrix_parent_inverse`。
    """
    try:
        gm = game_matrix(obj)
    except Exception as e:                                            # noqa: BLE001
        return None, "%s: %s" % (type(e).__name__, e)
    view = obj.matrix_world
    d = {"pos_m": None, "rot_deg": None, "scale_ratio": None}
    try:
        d["pos_m"] = float((gm.translation - view.translation).length)
    except Exception:                                                 # noqa: BLE001
        pass
    try:
        q = gm.to_quaternion().rotation_difference(view.to_quaternion())
        a = float(q.angle)
        d["rot_deg"] = float((a if a <= 3.14159265358979 else (6.28318530717959 - a)) * 57.29577951308232)
    except Exception:                                                 # noqa: BLE001
        pass
    try:
        sv, sg = view.to_scale(), gm.to_scale()
        d["scale_ratio"] = float(max(abs(sg[i] - sv[i]) / max(abs(sg[i]), 1e-9) for i in range(3)))
    except Exception:                                                 # noqa: BLE001
        pass
    return d, None


def xform_display_export_delta(obj):
    r"""★㉔ 档1(1-b·**甲**)：只对**带 `ba_mesh_export_matrix` 的网格**算「**显示 ≠ 导出**」三项差。

    为什么单列一类：网格的**显示**矩阵 `matrix_world` 与**导出**用的矩阵 `ba_mesh_export_matrix`
      在"带 root_bone 的网格"上**本来就不同**（导入时按 `world[rb] @ Mb` 做了**仅供显示**的调整）——
      实测 AH-1Z 旋翼 `Ah_1z` 差 **0.014238 m**，而 `lod_0`／US-ACV 网格**等价**。
    ⇒ 若照字面把它算进"不一致"，**原版模型（D1）会变红**；若直接忽略，又**把真差异吞掉**。
    ⇒ 甲：**单列一类 M ＝「需人判的显示偏差」**（⛔ 本函数**不**、任何地方也**不许**自动判"设计内"）：
      只有"**件内显式登记**"或"**人逐条判**"才能把它当设计内（见 doc 与本件说明页）。

    返回 `(delta, why, has_export_matrix)`：没有 `ba_mesh_export_matrix` ⇒ `(None, None, False)`（不参与 M）。
    ⛔ 只读。
    """
    em = None
    try:
        em = obj.get("ba_mesh_export_matrix")
    except Exception:                                                 # noqa: BLE001
        em = None
    if not em or len(em) != 16:
        return None, None, False
    try:
        from mathutils import Matrix as _M
        m = _M((em[0:4], em[4:8], em[8:12], em[12:16]))
        view = obj.matrix_world
        d = {"pos_m": None, "rot_deg": None, "scale_ratio": None}
        d["pos_m"] = float((m.translation - view.translation).length)
        try:
            q = m.to_quaternion().rotation_difference(view.to_quaternion())
            a = float(q.angle)
            d["rot_deg"] = float((a if a <= 3.14159265358979 else (6.28318530717959 - a)) * 57.29577951308232)
        except Exception:                                             # noqa: BLE001
            pass
        try:
            sv, se = view.to_scale(), m.to_scale()
            d["scale_ratio"] = float(max(abs(se[i] - sv[i]) / max(abs(se[i]), 1e-9) for i in range(3)))
        except Exception:                                             # noqa: BLE001
            pass
        return d, None, True
    except Exception as e:                                            # noqa: BLE001
        return None, "%s: %s" % (type(e).__name__, e), True


class BAMOD_OT_SelfcheckXform(bpy.types.Operator):
    """★25 一键一致性自检：**逐对象**比「视图」与「导出器将写进产物的值」。

    判据（★25 定的硬项）：
        `obj.matrix_world`  **vs**  `game_matrix(obj)`（＝从根沿父链连乘 `matrix_basis`，**不含**
        `matrix_parent_inverse` —— 与导出器同口径）
    ⇒ **逐项 diff 必须 = 0**；只要有一处 > 1e-4 m 就**点名**该对象并给出偏差与修法（旁边那个
      「摊平父级逆变换」按钮），⛔ **不许全绿**。
    ★ 负向对照＝任何一个带非单位 `matrix_parent_inverse` 的对象（例如 `Rotorangle_0`）都必须被抓出来。
    ⛔ 本算子**只读**：不修改任何对象的 loc/rot/scale，也不动父级逆变换。
    """
    bl_idname = "ba_mod.selfcheck_xform"
    bl_label = "一致性自检（视图 = 将写回的值？）"
    bl_description = ("逐对象比『视图位置』与『导出器会写进产物的位置』（★24/★25 同族）："
                      "不一致就点名，并提示用旁边『摊平父级逆变换』。⛔ 不修改任何东西")
    bl_options = {"REGISTER"}

    def execute(self, context):
        import json as _json
        rows, bad, unknown, disp = [], [], [], []
        for o in bpy.data.objects:
            d, why = xform_delta(o)
            if d is None:
                unknown.append((o.name, why))
                print("[xform-selfcheck] ⚠ %s **算不出**（%s）⇒ 计进「判不了」（⛔ 不静默丢）"
                      % (o.name, why))
            else:
                rows.append((o.name, d))
                worst = None
                if d["pos_m"] is not None and d["pos_m"] > XFORM_THRESH["pos_m"]:
                    worst = "位置 %.4f m" % d["pos_m"]
                if d["rot_deg"] is not None and d["rot_deg"] > XFORM_THRESH["rot_deg"]:
                    worst = (worst + " ／ " if worst else "") + "朝向 %.3f°" % d["rot_deg"]
                if d["scale_ratio"] is not None and d["scale_ratio"] > XFORM_THRESH["scale_ratio"]:
                    worst = (worst + " ／ " if worst else "") + "缩放比偏差 %.6f" % d["scale_ratio"]
                if worst:
                    bad.append((o.name, d, worst))
            # ★1-b(甲)：网格的「显示 ≠ 导出」单列一类（M）—— ⛔ 不算进"不一致"，但也**不许藏**
            de, dwhy, has_em = xform_display_export_delta(o)
            if has_em:
                if de is None:
                    disp.append((o.name, None, "读不出（%s）⇒ 需人判" % (dwhy or "未知")))
                else:
                    dw = None
                    if de["pos_m"] is not None and de["pos_m"] > XFORM_THRESH["pos_m"]:
                        dw = "位置差 %.4f m" % de["pos_m"]
                    if de["rot_deg"] is not None and de["rot_deg"] > XFORM_THRESH["rot_deg"]:
                        dw = (dw + " ／ " if dw else "") + "朝向差 %.3f°" % de["rot_deg"]
                    if de["scale_ratio"] is not None and de["scale_ratio"] > XFORM_THRESH["scale_ratio"]:
                        dw = (dw + " ／ " if dw else "") + "缩放比差 %.6f" % de["scale_ratio"]
                    if dw:
                        disp.append((o.name, de, dw))
        total = len(rows) + len(unknown)          # ★1-c：**判不了计入分母**（⛔ 不许静默丢）
        _row_names = {r[0] for r in rows}
        # ★(a)：一致 N ＝ **既不是不一致、也不是"显示≠导出"、也不是判不了**（三计数互斥、与分母可加：
        #   N + M + K + 判不了 = 分母）—— 第一版没扣 M，导致 AH-1Z 出现「一致 129 ｜ 显示≠导出 1」（129+1≠129）✗
        n_clean = len(rows) - len(bad) - len([1 for n, _d, _w in disp if n in _row_names])
        # ⚠ 措辞必须保住门禁解析器认的形状：`比较对象 N 个 ｜ 不一致 N 个`（**带「个」**）
        print("[xform-selfcheck] 比较对象 %d 个 ｜ 不一致 %d 个 ｜ 一致 %d ｜ 显示≠导出 %d（需人判）｜ "
              "判不了 %d ｜ 分母 %d"
              % (total, len(bad), n_clean, len(disp), len(unknown), total))
        print("[xform-selfcheck] 口径＝位置>%.0e m／朝向>%.2f°／缩放比>%.0e ｜ ⛔ 显示≠导出**不自动判设计内**"
              % (XFORM_THRESH["pos_m"], XFORM_THRESH["rot_deg"], XFORM_THRESH["scale_ratio"]))
        for n, d, w in sorted(bad, key=lambda x: -(x[1]["pos_m"] or 0.0))[:24]:
            print("   ✗ %-30s %s ⇒ 用『摊平父级逆变换』修（先选中它）" % (n, w))
        for n, _d, w in disp[:12]:
            print("   ◐ %-30s 显示≠导出：%s ⇒ **需人判**（⛔ 不自动当设计内）" % (n, w))
        for n, why in unknown[:12]:
            print("   ? %-30s **判不了**：%s" % (n, why))
        if unknown:
            print("   ⓘ 判不了 %d 个（分母已含）⇒ ⛔ 不得据此判绿其余部分" % len(unknown))
        rep = {"total": total, "clean": n_clean, "bad": len(bad), "unknown": len(unknown),
               "disp_export": len(disp), "thresh": dict(XFORM_THRESH), "boundary": XFORM_BOUNDARY,
               "items": [{"name": n, "worst": w, "pos_m": d["pos_m"], "rot_deg": d["rot_deg"],
                          "scale_ratio": d["scale_ratio"]} for n, d, w in bad[:5]],
               "disp_items": [{"name": n, "worst": w} for n, _d, w in disp[:5]],
               "unknown_names": [n for n, _w in unknown[:5]]}
        try:
            context.scene[XFORM_SELFCHECK_KEY] = _json.dumps(rep, ensure_ascii=False)
        except Exception as e:                                          # noqa: BLE001
            print("⚠ [xform-selfcheck] 结果写场景属性失败（UI 明细会看不到）：%s: %s"
                  % (type(e).__name__, e))
        if total == 0:
            # ★㉔ D4 空集守卫：0 对象 ⇒ **判不了**，⛔ 不许报绿
            self.report({"WARNING"}, "一致性自检：**0 个对象 ⇒ 判不了**（空场景／没有可比对象；⛔ 不报绿）")
            return {"FINISHED"}
        _disp_txt = "；".join("%s（%s）" % (n, w) for n, _d, w in disp[:5])
        if bad or unknown:
            # ⚠ 只有"不一致 / 判不了"才走 WARNING；**M（显示≠导出）不影响是否报绿** ——
            #   但按中枢收紧(a)：**M 必须在结论行里点数＋点名**（⛔ 不许藏，也⛔ 不许自动当设计内）
            parts = ["比较对象 %d 个 ｜ 一致 %d ｜ **显示≠导出 %d** ｜ **不一致 %d 个**"
                     % (total, n_clean, len(disp), len(bad))]
            if unknown:
                parts.append("**判不了 %d 个**（分母 %d）" % (len(unknown), total))
            if bad:
                parts.append("不一致：%s" % "；".join("%s（%s）" % (n, w) for n, _d, w in bad[:5]))
            if disp:
                parts.append("显示≠导出（**需人判**，⛔ 不自动当设计内）：%s" % _disp_txt)
            if unknown:
                parts.append("判不了：%s" % ", ".join(n for n, _w in unknown[:5]))
            self.report({"WARNING"}, "一致性自检：" + "。".join(parts))
            return {"FINISHED"}
        self.report({"INFO"}, "一致性自检：**全绿**（比较对象 %d 个 ｜ 一致 %d ｜ 显示≠导出 %d ｜ 不一致 0；"
                    "位置/朝向/缩放三项一致）%s  ⓘ 全部对象都可判"
                    % (total, n_clean, len(disp),
                       ("；**显示≠导出（需人判，⛔ 不自动当设计内）**：" + _disp_txt) if disp else ""))
        return {"FINISHED"}




# ---------------------------------------------------------------------------
# ④ 构建写回（场景 -> prefab）
# ---------------------------------------------------------------------------
_SRC_ENV_CACHE = {}       # ★v1.12.5：源 bundle 路径 -> (env, objs, by_pid)（⛔ 只在本进程内复用）


def _src_env(bundle):
    r"""按需加载"源 bundle"并缓存（跨包带源时才用；⛔ 不写盘、⛔ 不改包）。"""
    if bundle in _SRC_ENV_CACHE:
        return _SRC_ENV_CACHE[bundle]
    import UnityPy
    env = UnityPy.load(bundle)
    objs = list(list(env.objects)[0].assets_file.objects.values()) if list(env.objects) else []
    _SRC_ENV_CACHE[bundle] = (env, objs, {o.path_id: o for o in objs})
    return _SRC_ENV_CACHE[bundle]


def _read_src_material_bytes(src_bundle, pid, name):
    r"""★v1.12.5（★㉖-续·B·甲）：从**源 bundle** 读出该材质 raw ＋ 每张图的 raw ＋ **真实 resS 字节**。

    返回 dict（可直接当 `mat_srcs` 的一项）：成功含 `raw_b64`；失败含 `why`（⛔ 不静默）。
    ⚠ 贴图是**流式**的（`m_StreamData → archive:/CAB-…/….resS`）⇒ 必须连带**字节**，
      只带对象 raw 的话目标包里没有那个 CAB ⇒ 游戏里照样缺图（这是本作最容易踩的坑）。
    """
    import base64 as _b64
    out = {"pid": int(pid), "name": name}
    try:
        _env, objs, by_pid = _src_env(src_bundle)
    except Exception as _e:                                            # noqa: BLE001
        out["why"] = "源 bundle 打不开（%s: %s）" % (type(_e).__name__, _e)
        return out
    o = by_pid.get(int(pid))
    if o is None and name:
        for _o in objs:
            if getattr(_o.type, "name", "") != "Material":
                continue
            try:
                if getattr(_o.read(), "m_Name", None) == name:
                    o = _o
                    break
            except Exception:                                          # noqa: BLE001
                continue
    if o is None:
        out["why"] = "源 bundle 里找不到该材质（pid=%s / 名 %r）" % (pid, name)
        return out
    try:
        out["name"] = getattr(o.read(), "m_Name", None) or name
        out["raw_b64"] = _b64.b64encode(o.get_raw_data()).decode("ascii")
    except Exception as _e:                                            # noqa: BLE001
        out["why"] = "源材质 raw 取不出（%s: %s）" % (type(_e).__name__, _e)
        return out
    texs = []
    try:
        _env2, _objs2, by_pid2 = _src_env(src_bundle)
        d = o.read()
        sp = getattr(d, "m_SavedProperties", None)
        envs = list(getattr(sp, "m_TexEnvs", []) or []) if sp is not None else []
        import tex_stream as _TS
        for te in envs:
            try:
                t = te[1].m_Texture
                tp = int(getattr(t, "m_PathID", 0) or 0) if t is not None else 0
            except Exception:                                          # noqa: BLE001
                tp = 0
            if not tp:
                continue
            row = {"prop": str(te[0]), "src_pid": tp, "name": None, "raw_b64": None,
                   "bytes_b64": None, "why": None}
            to = by_pid2.get(tp)
            if to is None:
                row["why"] = "源 bundle 里没有这张图"
                texs.append(row)
                continue
            try:
                row["name"] = getattr(to.read(), "m_Name", None)
                row["raw_b64"] = _b64.b64encode(to.get_raw_data()).decode("ascii")
            except Exception as _e:                                    # noqa: BLE001
                row["why"] = "图 raw 取不出（%s: %s）" % (type(_e).__name__, _e)
                texs.append(row)
                continue
            try:
                info = _TS.stream_info(to)
                nb = _TS.read_stream_bytes(src_bundle, info, sf=to.assets_file)
                if nb:
                    row["bytes_b64"] = _b64.b64encode(bytes(nb)).decode("ascii")
                else:
                    row["why"] = "该图没有流式字节可取（可能本来就是内联图）"
            except Exception as _e:                                    # noqa: BLE001
                row["why"] = "流式字节读取失败（%s: %s）" % (type(_e).__name__, _e)
            texs.append(row)
    except Exception as _e:                                            # noqa: BLE001
        out["why"] = "列源材质的贴图依赖失败（%s: %s）" % (type(_e).__name__, _e)
        return out
    out["texs"] = texs
    return out


def _collect_mat_srcs(obj, pids, names):
    r"""★v1.12.5（★㉖-续·B·甲）→ `mat_srcs` 列表（**向后兼容**：同包时只有 pid/名，老消费方不用管）。"""
    try:
        prefs = _prefs(bpy.context)
        cur = prefs.bundle or ""
    except Exception:                                                  # noqa: BLE001
        cur = ""
    src = ""
    try:
        src = obj.get("ba_src_bundle") or bpy.context.scene.get("ba_copy_source_bundle") or ""
    except Exception:                                                  # noqa: BLE001
        src = ""
    out = []
    for i, pid in enumerate(pids or []):
        nm = names[i] if i < len(names) else None
        # ★★ v1.12.9（★㉖-续·D **第二处根因**）：**同包也必须带字节**
        #   旧写法（本条修前）：`if not src or not cur or normcase(src)==normcase(cur): out.append({pid,name}); continue`
        #     ⇒ 「导入包 ＝ 构建包」时**只带 pid/名、⛔ 不带字节** ⇒ 消费侧拿不到材质 raw ⇒ 产物 `m_Materials` 可能悬空。
        #   新写法：源包为空（来历不明/同包）⇒ **退回当前包当源**，**一律**经 `_read_src_material_bytes` 现场取「材质 raw ＋ 其贴图」。
        _s = src or cur
        if not _s:
            out.append({"pid": int(pid), "name": nm})                  # 连当前包都没有 ⇒ 只带 pid/名（消费侧安全网仍在）
            continue
        ent = _read_src_material_bytes(_s, pid, nm)
        if ent.get("why"):
            print("⚠ 跨包源材质 %r（pid=%s）带源失败：%s ⇒ 清单里已标注（copy_full 会走安全网）"
                  % (nm, pid, ent["why"]))
        else:
            print("✓ 跨包源材质 %r（pid=%s）已带源：材质 raw ＋ 图 %d 张（含流式字节 %d 张）"
                  % (ent.get("name"), pid, len(ent.get("texs") or []),
                     sum(1 for x in (ent.get("texs") or []) if x.get("bytes_b64"))))
        out.append(ent)
    return out


class BAMOD_OT_BuildModel(bpy.types.Operator):
    bl_idname = "ba_mod.build_model"
    bl_label = "构建写回（挂载点树 + 网格 → prefab）"
    bl_description = "把场景里的挂载点树和蒙皮网格写成新 prefab（全类型模型，约 2 分钟）"
    bl_options = {"REGISTER", "UNDO"}

    def _collect_tree(self):
        """收集挂载点树：[(name, parent_name, pos_unity, rot_unity, scale_unity)] 父在前。"""
        mounts = [o for o in bpy.data.objects if o.type == "EMPTY" and o.get("ba_mount")]
        if not mounts:
            return None, "场景里没有挂载点（Empty）"
        roots = [o for o in mounts if o.parent is None or o.parent not in mounts]
        if not roots:
            return None, "挂载点树没有根"
        root = roots[0]
        wrapper = None
        # 剥掉顶层包装节点（原版 prefab 的容器资产名如 "RU_BMPT2"，其子才是 "root"）
        if _normalize_name(root.name) != "root":
            wrapper = root
            for c in root.children:
                if c in mounts and _normalize_name(c.name) == "root":
                    root = c
                    break
        order = []
        def walk(o):
            order.append(o)
            for c in o.children:
                if c in mounts:
                    walk(c)
        walk(root)
        tree = []
        for o in order:
            nm = _normalize_name(o.name)
            p = _normalize_name(o.parent.name) if (o.parent and o.parent in mounts
                                                   and o.parent is not wrapper) else None
            pos = o.location
            rot = o.rotation_quaternion  # (w, x, y, z)
            scale = o.scale
            tree.append((nm, p, (pos.x, pos.z, -pos.y),
                         (rot.x, rot.z, -rot.y, rot.w),
                         (scale.x, scale.z, scale.y)))
        return tree, None

    def _collect_one_mesh(self, obj, root, tree):
        """收集单个网格：位置转 root 空间 + Unity 坐标，骨骼按顶点组映射到树。"""
        import bmesh
        me = obj.data
        bm = bmesh.new()
        bm.from_mesh(me)
        bmesh.ops.triangulate(bm, faces=bm.faces)
        bm.to_mesh(me)
        bm.free()
        me.update()
        name_index = {nm: i for i, (nm, _, _, _, _) in enumerate(tree)}
        # 顶点组名 -> 树索引（只保留骨骼树里的名字）
        gname_to_bone = {}
        for g in obj.vertex_groups:
            nm = _normalize_name(g.name)
            if nm in name_index:
                gname_to_bone[g.index] = name_index[nm]
        if not gname_to_bone:
            return None
        body_idx = name_index.get("body", 0)
        root_inv = root.matrix_world.inverted()
        # 用导入时记下的"导出矩阵"（root 空间，scale=1）；显示矩阵可能带了 root_scale 缩放
        em = obj.get("ba_mesh_export_matrix")
        if em and len(em) == 16:
            mw = root_inv @ Matrix((em[0:4], em[4:8], em[8:12], em[12:16]))
        else:
            mw = root_inv @ obj.matrix_world
        positions = []
        triangles = []
        uv = []
        bones = []
        weights = []
        for v in me.vertices:
            p = mw @ v.co
            positions.append((-p.x, p.z, -p.y))  # ★㓓：导入映射的逆（同 det=−1 那一套）
            vg = sorted([(g.group, g.weight) for g in v.groups if g.group in gname_to_bone],
                        key=lambda kv: -kv[1])
            if len(vg) >= 2:
                bones.append((gname_to_bone[vg[0][0]], gname_to_bone[vg[1][0]]))
                weights.append((vg[0][1], vg[1][1]))
            elif len(vg) == 1:
                b0 = gname_to_bone[vg[0][0]]
                bones.append((b0, b0))
                weights.append((vg[0][1], 0.0))
            else:
                bones.append((body_idx, body_idx))
                weights.append((1.0, 0.0))
        for p in me.polygons:
            for k in range(3):
                triangles.append(me.loops[p.loop_start + k].vertex_index)
        uv_layer = me.uv_layers.active.data if me.uv_layers.active else None
        for p in me.polygons:
            for k in range(3):
                if uv_layer:
                    u, v = uv_layer[p.loop_start + k].uv
                    uv.append((u, v))
                else:
                    uv.append((0.0, 0.0))
        used = sorted({b for b, _ in bones} | {b for _, b in bones})
        bone_names = [tree[i][0] for i in used]
        remap = {old: new for new, old in enumerate(used)}
        bones = [(remap[b0], remap[b1]) for b0, b1 in bones]
        # ★26 完整修法：源材质 pid/名（供 copy_full 按 pid 绑定；读不到就留空并由打包侧出声）
        _src_mat_pids, _src_mat_names = [], []
        try:
            for _x in (obj.get("ba_materials") or []):
                try:
                    _src_mat_pids.append(int(_x))
                except Exception as _e:                                 # noqa: BLE001
                    print("⚠ %s 的源材质 pid 读不出（%s）⇒ 略过该项" % (obj.name, type(_e).__name__))
            for _slot in (obj.material_slots or []):
                if _slot.material is None:
                    continue
                _nm = _slot.material.name
                _src_mat_names.append(_nm.split("_", 1)[1] if _nm.startswith("SKIN0_") else _nm)
        except Exception as _e:                                         # noqa: BLE001
            print("⚠ 收集 %s 的源材质失败（%s: %s）⇒ 该网格材质将按缺失处理" % (obj.name, type(_e).__name__, _e))
        return {
            "name": _normalize_name(obj.name),
            "positions": positions, "triangles": triangles, "uv": uv,
            "bones": bones, "weights": weights, "bone_names": bone_names,
            # ★26 完整修法（2026-09-18 中枢批准）：把**源材质的 pid/名**随网格带入清单
            #   ⇒ copy_full 侧据此**按 pid 绑定**（⛔ 不再继承"本车材质"）。
            #   `ba_materials` = 导入时记下的源包材质 pid；材质名去掉插件的 `SKIN0_` 前缀即源名。
            "mats": _src_mat_pids, "mat_names": _src_mat_names,
            # ★v1.12.5（★㉖-续·B·甲）：**导出侧带源** —— 跨包时把源材质与其贴图依赖的源字节一并带上
            #   （同包只带 pid/名；copy_full 侧按 pid 命中 ⇒ ⛔ 不重复带入）
            "mat_srcs": _collect_mat_srcs(obj, _src_mat_pids, _src_mat_names),
        }

    def _collect_meshes(self, root, tree):
        """收集所有蒙皮网格（各自有匹配骨骼的顶点组）。"""
        name_index = {nm: i for i, (nm, _, _, _, _) in enumerate(tree)}
        meshes = []
        for obj in bpy.data.objects:
            if obj.type != "MESH" or not obj.data.vertices:
                continue
            if not any(_normalize_name(g.name) in name_index for g in obj.vertex_groups):
                continue
            m = self._collect_one_mesh(obj, root, tree)
            if m:
                meshes.append(m)
        if not meshes:
            return None, "场景里没有带骨骼顶点组的网格（先给网格建顶点组并赋权）"
        return meshes, None

    def execute(self, context):
        prefs = _prefs(context)
        tree, err = self._collect_tree()
        if err:
            self.report({"ERROR"}, err)
            return {"CANCELLED"}
        mounts = [o for o in bpy.data.objects if o.type == "EMPTY" and o.get("ba_mount")]
        roots = [o for o in mounts if o.parent is None or o.parent not in mounts]
        meshes, err = self._collect_meshes(roots[0], tree)
        if err:
            self.report({"ERROR"}, err)
            return {"CANCELLED"}
        if not prefs.bundle:
            self.report({"ERROR"}, "请先在偏好里填 bundle 路径")
            return {"CANCELLED"}
        out = prefs.out_bundle if prefs.out_bundle else prefs.bundle
        # 后缀一致性：导出包模式强制 .bamod；写完整 bundle 模式强制 .bundle
        # 后缀一致性：导出包模式强制 .bamod；写完整 bundle 模式强制 .bundle
        # ⛔ 用 `os.path.splitext`（只切最后一个扩展名），**不能用 `out.split(".")[0]`** ——
        #    那是对**整条路径**切割，目录名里只要有一个点（`D:\my.mods\x`、`v1.8.59.old`…）
        #    就会被截成 `D:\my` ✗（实测确认）。
        if prefs.build_pack:
            out = out if out.endswith(".bamod") else os.path.splitext(out)[0] + ".bamod"
        else:
            out = out if out.endswith(".bundle") else os.path.splitext(out)[0] + ".bundle"
        unitypy_bridge.add_paths(prefs.rev_dir, prefs.unitypy_dir)
        # ⛔ v1.8.67 防呆：面板里明明有行为、但「应用动画」没勾 ⇒ 构建出来**没有任何动画、
        #    也不报错** ✗（最恶劣的失败方式）。直接拦下来，让用户勾上或确认不要动画。
        if (not prefs.apply_anim) and len(context.scene.bamod_anims) > 0:
            self.report({"ERROR"},
                        "④ 面板里有 %d 条动画行为，但「应用动画」没勾选 —— "
                        "勾上再构建，否则这些动画**不会写进模型**"
                        % len(context.scene.bamod_anims))
            return {"CANCELLED"}
        try:
            source_pid = context.scene.get("ba_copy_source_pid")
            # v1.8.90：⑧ 面板改过的组件。⛔ 它**不受「应用动画」开关管**（那是动画的事），
            #    但只要它有内容、而构建又没走 copy-full 那条路，改动就会被**静默丢掉** ✗
            #    —— 这正是本插件最不能接受的失败方式，所以下面直接拦下来。
            comp_edits = _comp_edits(context)
            if comp_edits and not (source_pid and prefs.copy_full and prefs.build_pack):
                self.report({"ERROR"},
                            "⑧ 面板里有 %d 个组件的改动，但这次构建不会走「复制源」那条路"
                            "（需要：① 导入过 prefab + 勾选「复制源整体重建」+「导出为 .bamod 包」）"
                            "—— 这样构建出来这些改动**不会生效**，已中止"
                            % len(comp_edits))
                return {"CANCELLED"}
            if source_pid and prefs.copy_full and prefs.build_pack:
                import copy_full as cf
                import importlib
                importlib.reload(cf)
                hub_json = None
                if prefs.apply_anim:
                    import json as _json
                    if len(context.scene.bamod_anims) > 0:
                        hub_json = _json.dumps(_anims_to_dict(context), ensure_ascii=False)
                    else:
                        _txt = bpy.data.texts.get("bamod_anim")
                        if _txt is not None:
                            hub_json = _txt.as_string()
                log, root_pid = cf.build_copy(
                    prefs.bundle, out, prefs.new_prefab, prefs.new_name,
                    int(source_pid), tree, meshes,
                    source_path=context.scene.get("ba_copy_source_path", ""),
                    hub_json=hub_json, comp_edits=comp_edits)
            else:
                import build_model as bm
                import importlib
                importlib.reload(bm)
                if prefs.build_pack:
                    log, root_pid = bm.build_model(
                        prefs.bundle, "", prefs.new_prefab, prefs.new_name,
                        tree, meshes, pack_path=out)
                else:
                    log, root_pid = bm.build_model(
                        prefs.bundle, out, prefs.new_prefab, prefs.new_name,
                        tree, meshes)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.report({"ERROR"}, "构建失败：%s" % e)
            return {"CANCELLED"}
        print(log)
        # ⛔ 不要用输出覆盖输入源（旧写法 `prefs.bundle = out`）：
        #    之后「刷新 prefab 列表 / 读取动画 / ⑤ 皮肤 / 算 CRC」会**悄悄改读刚写出的文件**，
        #    用户完全看不出来 ✗。输出路径单独记一份，提示用户自己决定要不要切。
        if not prefs.build_pack:
            context.scene["ba_last_output_bundle"] = out
            self.report({"INFO"}, "构建完成：%s（输入源未改动；如需继续以它为源，"
                                  "请手动把偏好里的 bundle 指向它）" % out)
        else:
            context.scene["ba_last_output_bundle"] = out
        self.report({"INFO"}, "构建完成：%s" % (log.split("|")[0].strip(),))
        return {"FINISHED"}


class BAMOD_PT_Tools(bpy.types.Panel):
    bl_label = "③ 工具"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "BA Mod"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        prefs = _prefs(context)
        layout.prop(prefs, "build_pack")
        layout.prop(prefs, "copy_full")
        # ★ 过渡说明（2026-09-18 中枢令）：整块复制/构建这一族的迁移预告，运行期在面板可见
        layout.label(text=TRANSITION_NOTE, icon="INFO")
        layout.operator("ba_mod.build_model", icon="EXPORT")
        # v1.8.75：写回前会不会多出一份 GB 级 .bak —— 一眼可见（与 BA_Mod_Maker 共享配置）
        _buf = _bp()
        _bk = bool(_buf.backup_enabled()) if _buf is not None else bool(prefs.auto_backup)
        layout.label(text="自动备份：%s" % ("开 · 会留 .bak 副本" if _bk
                                           else "关 · 不留 .bak（原子替换仍保护原文件）"),
                     icon="CHECKBOX_DEHLT" if _bk else "CHECKBOX_HLT")
        layout.prop(prefs, "auto_backup")
        layout.separator()
        layout.operator("ba_mod.transfer_weights", icon="MOD_VERTEX_WEIGHT")
        layout.separator()
        layout.operator("ba_mod.compute_crc", icon="FILE_TICK")


ANIM_TYPES_BASE = [
    ("AxisRandom", "AxisRandom", "随机扫掠（炮塔/天线小幅摆动）"),
    ("MathConnect", "MathConnect", "跟随（约束到另一节点）"),
    ("Torque", "Torque", "旋翼自转（直升机主桨/尾桨）"),
    ("FloatEffect", "FloatEffect", "空中浮动（机体上下起伏）"),
    ("WaterFloatEffect", "WaterFloatEffect", "水面浮动"),
]
_ANIM_CLASS_CACHE = []
_ANIM_STATIC_ITEMS = []      # 顺序固定的类型下拉（见 _anim_static_items 的说明）
# 动态枚举 items 的缓存（见 _anim_cached_items 的说明）
_ANIM_ITEM_CACHE = {}


def _anim_cached_items(key, build):
    r"""按 `key` 缓存枚举 items；命中时返回**同一个列表对象**。

    ⛔ 为什么必须缓存（v1.8.71，用户实测「模板栏全是乱码」的根因）：
       Blender 把动态 items 回调返回的字符串**按引用**存进 RNA。回调若每次都
       `return [(...), ...]` 新建一份列表 + 新建字符串对象，上一帧的条目在 C 侧
       就可能已经失效 ⇒ 下拉/字段里显示成**乱码** ✗。
       实测对照（同一个对话框里）：
         · 「行为类型」`pick` → 返回缓存的**同一个列表对象** ⇒ **一直是正常的** ✓
         · 「模板」`preset` → 每次重新构造 ⇒ **全是乱码** ✗
       这就是判据 ⇒ 所有动态枚举一律走本函数缓存 ✓。
    """
    hit = _ANIM_ITEM_CACHE.get(key)
    if hit is not None:
        return hit
    items = [(str(a), str(b), str(c)) for a, b, c in build()]
    # 只留最近 64 组，免得"元数据驱动的字段枚举"把内存撑爆
    if len(_ANIM_ITEM_CACHE) > 64:
        _ANIM_ITEM_CACHE.clear()
    _ANIM_ITEM_CACHE[key] = items
    return items


def _anim_all_classes():
    """dump 里**全部** IAnimationBehaviour 类（24 个）。首次约 1~2 秒，之后走缓存。

    只在 `read_animation` / 面板按钮里预热，**不在绘制回调里同步加载**（否则面板会卡）。
    """
    global _ANIM_CLASS_CACHE
    if not _ANIM_CLASS_CACHE:
        try:
            import behavior_meta as BM
            _ANIM_CLASS_CACHE = BM.behaviour_classes(_anim_reg())
        except Exception as e:  # noqa: BLE001
            print("[动画] 行为类清单加载失败：%s" % e)
            _ANIM_CLASS_CACHE = []
    return _ANIM_CLASS_CACHE


def _anim_type_items(self, context):
    """"类型"下拉（**带缓存**，见 `_anim_cached_items`）。

    缓存键 = 「场景里读到的类型串 + 当前各行为的 raw_type + dump 类清单」——
    这三样任一变化就重建，否则返回同一个列表对象 ✓（乱码就是这么修掉的）。
    """
    extra = []
    try:
        extra.append(context.scene.get("bamod_anim_types", "") or "")
    except Exception:  # noqa: BLE001
        extra.append("")
    try:
        extra += [b.raw_type for b in context.scene.bamod_anims if b.raw_type]
    except Exception:  # noqa: BLE001
        pass
    key = ("types", tuple(extra), tuple(_ANIM_CLASS_CACHE))
    return _anim_cached_items(key, lambda: _anim_build_type_items(self, context))


def _anim_build_type_items(self, context):
    """行为类型下拉：内置 5 种 + 当前读到的**未破译类型**（原样保留字节）。

    ⛔ 必须把未破译类型也列进 items：读到的 hub 里可能有 HideMesh / SpawnVFX /
    AnimatorConnect 等十几个类，若下拉不接受它们，读取时会全部落回 AxisRandom ✗
    ⇒ 一按「构建写回」就把原 prefab 的行为**写坏**（v1.8.57 前就是这个行为）。
    ⛔ 两个来源都要查：`bamod_anim_types`（本次读取收集的）**以及**集合里已有的
    `raw_type` —— 后者是必须的：回填时是「先 add 进集合、再赋 type」，若只认前者，
    第一次遇到未破译类型就会 `enum "X" not found` 直接报错（实测踩坑）。
    """
    items = list(ANIM_TYPES_BASE)
    known = {k for k, _, _ in items}
    extra = []
    try:
        extra += (context.scene.get("bamod_anim_types", "") or "").split(",")
    except Exception:
        pass
    try:
        for b in context.scene.bamod_anims:
            if b.raw_type:
                extra.append(b.raw_type)
    except Exception:
        pass
    for t in extra:
        t = (t or "").strip()
        if t and t not in known:
            items.append((t, t, "未破译行为（字节原样保留）"))
            known.add(t)
    # ⛔ v1.8.67：**先把行为词典里的 24 类全部列上**（纯静态数据、零开销）。
    #    以前只有"预热过 dump 之后"才列全 24 类 ⇒ 刚开面板只看到 5 类，
    #    用户以为只能加这 5 种 ✗。词典列全后，即使 dump 还没加载完，
    #    下拉里也已经有全部 24 类（中文名 + 用途说明）。
    try:
        for t in _anim_bd().CLASS_INFO:
            if t not in known:
                items.append((t, t, ""))
                known.add(t)
    except Exception as e:  # noqa: BLE001
        print("[动画] 行为词典加载失败（不影响已缓存类型）：%s" % e)
    # dump 里**全部**行为类（含当前模型没用到的）—— 这样"新增任意行为"不需要改代码 ✓
    for t in _ANIM_CLASS_CACHE:
        if t not in known:
            items.append((t, t, "来自 IL2CPP dump 的行为类（可新增）"))
            known.add(t)
    # v1.8.67：**显示名换成行为词典里的中文名**（标识仍是真实类名 —— 写回字节要用它）。
    # 19 个原本只能看到裸类名的行为，现在下拉里直接写清楚用途 ✓
    try:
        fixed = []
        for ident, label, desc in items:
            if not ident:
                continue
            try:
                i = _anim_bd().info(ident)
            except Exception:  # noqa: BLE001
                i = {}
            if i.get("cn") and i["cn"] != ident:
                label = "%s（%s）" % (i["cn"], ident)
                desc = (i.get("desc") or desc)
                if i.get("danger"):
                    desc = "⚠ 会改动模型部件 · " + desc
            fixed.append((ident, label, (desc or "")[:1024]))
        items = fixed
    except Exception as e:  # noqa: BLE001
        print("[动画] 类型下拉中文化失败（不影响使用）：%s" % e)
    return items


def _anim_field_update(self, context):
    """字段控件改动 → 写回行为的 values_json（面板只是视图，JSON 才是权威）。"""
    _anim_write_field(self, context)


def _anim_enum_items(self, context):
    """枚举行下拉：选项来自 IL2CPP dump 里的枚举成员名（每个字段自己的选项表）。

    ⛔ 回调里**绝不能读 `self.e`** —— 那只会在枚举求值过程中再次触发本回调 ⇒
    无限递归（实测 RecursionError ✗）。当前值改用普通 IntProperty `i` 来判断。
    ⛔ v1.8.71：走 `_anim_cached_items` 缓存 —— 动态枚举**必须**返回同一个列表对象，
    否则下拉会显示乱码（见 `_anim_cached_items` 的说明）。
    """
    import json as _json
    try:
        opts = _json.loads(self.eopts or "[]")
    except Exception:
        opts = []

    def build():
        out, seen = [], set()
        for it in opts:
            try:
                name, val = it[0], int(it[1])
            except Exception:  # noqa: BLE001
                continue
            k = str(val)
            if k in seen:
                continue
            seen.add(k)
            out.append((k, "%s (%d)" % (name, val), ""))
        if not out:
            out = [("0", "0", "")]
        cur = str(int(self.i or 0))
        if cur not in seen:
            out.append((cur, "%s (未定义)" % cur, ""))
        return out

    return _anim_cached_items(("enum", self.eopts or "", str(int(self.i or 0))), build)


class ANIM_Field(bpy.types.PropertyGroup):
    """**通用**字段行（按元数据生成，任何行为类的任何字段都用它）。

    JSON 路径存在 `path` 里（列表下标也支持）；`kind` 决定用哪个控件。
    嵌套组 / 数组 / 字典本身也是一行（kind=group/list/dict），数组项带增删复制按钮。
    """
    path: bpy.props.StringProperty(default="[]")
    depth: bpy.props.IntProperty(default=0)
    label: bpy.props.StringProperty(default="")
    kind: bpy.props.StringProperty(default="")
    cs: bpy.props.StringProperty(default="")
    owner: bpy.props.IntProperty(default=0)
    # v1.8.90：这一行属于**哪个列表** —— "anim"=④ 动画行为 / "comp"=⑧ 组件。
    # 有了它，字段控件、数组增删、吸管三个算子都能**共用同一套代码**，
    # 不必为组件再造一份几乎一样的实现（造两份的下场就是改一处忘一处 ✗）。
    src: bpy.props.StringProperty(default="anim")
    b: bpy.props.BoolProperty(default=False, update=_anim_field_update)
    i: bpy.props.IntProperty(default=0, update=_anim_field_update)
    f: bpy.props.FloatProperty(default=0.0, update=_anim_field_update)
    f2: bpy.props.FloatProperty(default=0.0, update=_anim_field_update)
    f3: bpy.props.FloatProperty(default=0.0, update=_anim_field_update)
    s: bpy.props.StringProperty(default="", update=_anim_field_update)
    e: bpy.props.EnumProperty(items=_anim_enum_items, update=_anim_field_update)
    eopts: bpy.props.StringProperty(default="[]")
    fcount: bpy.props.IntProperty(default=0)
    felem: bpy.props.StringProperty(default="float")
    pid: bpy.props.StringProperty(default="0")
    note: bpy.props.StringProperty(default="")
    list_index: bpy.props.IntProperty(default=-1)
    list_path: bpy.props.StringProperty(default="[]")
    # 填充期间置 True：一个 vec 行要写 f/f2/f3 三次，中途会把"半成品向量"写进 JSON ✗
    muted: bpy.props.BoolProperty(default=False)


class ANIM_Behavior(bpy.types.PropertyGroup):
    array: bpy.props.EnumProperty(name="数组", items=[
        ("universal", "universal", "通用（战场+军械库）"),
        ("demo", "demo", "军械库演示"),
        ("game", "game", "战场游戏"),
        ("preDeath", "preDeath", "死亡前"),
        ("death", "death", "死亡"),
    ])
    type: bpy.props.EnumProperty(name="类型", items=_anim_type_items)
    # ⛔ v1.8.87：原来的 `open`（**假折叠栏**开关）已删除 —— 每条行为现在是 Blender 的
    #    **真子面板**（`DEFAULT_CLOSED`），折叠状态由 Blender 自己管，不需要场景属性 ✗
    rid: bpy.props.StringProperty(name="rid", default="")  # 原始 SerializeReference id（保留，不重编号）
    lod: bpy.props.IntProperty(name="LOD", default=1, min=0)
    speed: bpy.props.FloatProperty(name="转速", default=20.0)
    min_time: bpy.props.FloatProperty(name="最小间隔(秒)", default=2.0)
    max_time: bpy.props.FloatProperty(name="最大间隔(秒)", default=10.0)
    x_source: bpy.props.StringProperty(name="X源", default="")
    x_min: bpy.props.FloatProperty(name="X最小角", default=0.0)
    x_max: bpy.props.FloatProperty(name="X最大角", default=0.0)
    y_source: bpy.props.StringProperty(name="Y源", default="turret_0")
    y_min: bpy.props.FloatProperty(name="Y最小角", default=-45.0)
    y_max: bpy.props.FloatProperty(name="Y最大角", default=45.0)
    z_source: bpy.props.StringProperty(name="Z源", default="")
    z_min: bpy.props.FloatProperty(name="Z最小角", default=0.0)
    z_max: bpy.props.FloatProperty(name="Z最大角", default=0.0)
    freq: bpy.props.FloatProperty(name="频率", default=0.0)
    damper: bpy.props.FloatProperty(name="阻尼", default=0.0)
    reaction: bpy.props.FloatProperty(name="反应", default=0.0)
    root: bpy.props.StringProperty(name="根", default="")
    target: bpy.props.StringProperty(name="目标", default="")
    freeze_x: bpy.props.BoolProperty(name="冻结X", default=False)
    freeze_y: bpy.props.BoolProperty(name="冻结Y", default=False)
    freeze_z: bpy.props.BoolProperty(name="冻结Z", default=False)
    shots_b64: bpy.props.StringProperty(
        name="shots", default="", description="WeaponShotForces 原始字典字节（base64，原样保留）")
    # ---- Torque / FloatEffect（v1.8.57 新增）----
    dir_x: bpy.props.FloatProperty(name="方向X", default=0.0)
    dir_y: bpy.props.FloatProperty(name="方向Y", default=-1.0)
    dir_z: bpy.props.FloatProperty(name="方向Z", default=0.0)
    values_str: bpy.props.StringProperty(
        name="参数", default="", description="FloatEffect 的 8 个 f32（逗号分隔）")
    # ---- 原样往返 ----
    src_json: bpy.props.StringProperty(
        name="原始 JSON", default="",
        description="读取时的原始行为 JSON；写回时作为基底，只覆盖面板里改过的字段。"
                    "这样未破译字段（pid、命名空间等）不会丢")
    raw_type: bpy.props.StringProperty(name="原始类型", default="")
    # ---- 通用字段视图（元数据驱动，任意类/任意字段）----
    values_json: bpy.props.StringProperty(
        name="字段值 JSON", default="{}",
        description="该行为的**字段值**（由元数据编解码器解析得到）；面板上的通用字段"
                    "编辑器直接读写它，构建时再由编解码器写回字节")
    # ⛔ `fields` 不在这里声明：`CollectionProperty(type=ANIM_Field)` 要求 ANIM_Field
    #    **已经注册**（否则报 "missing bl_rna … may not be registered"），而类体是在
    #    import 时求值的、那时谁都没注册 ✗。 ⇒ 在 register() 里注册完两个类之后再挂。
    # ⛔ v1.8.87：原来的 `show_fields`（**假折叠栏**开关）已删除 —— 「通用字段」现在是
    #    嵌套在行为子面板里的**真子面板**（默认折叠）✓
    raw_only: bpy.props.BoolProperty(
        name="仅原始字节", default=False,
        description="该实例的布局无法完全按元数据解析（资产比类定义旧等）⇒ 只保留原始"
                    "字节，仍能字节级往返，但不出可编辑字段")
    conv: bpy.props.BoolProperty(
        name="便捷属性优先", default=False,
        description="True = 这条行为由「新增 / 一键」创建，**顶部那几个便捷输入框"
                    "（目标节点 / 转速 / 方向…）是权威**，每次写回时覆盖对应字段；\n"
                    "False = `values_json`（通用字段编辑器）是权威。\n"
                    "⛔ 两者不能同时生效：以前 `values_json` 一旦非空就**忽略**便捷属性 ⇒\n"
                    "   「+ 旋翼自转」之后填目标节点名**完全没有效果**，用户只能去通用字段表里翻 ✗。\n"
                    "   只要在通用字段里改过一次，就自动切到 False（交回给字段表）。")


def _anim_codec():
    """取通用编解码模块（不reload，避免每次重绘都重解析 dump）。"""
    import behavior_meta as BM
    import behavior_codec as BC
    import behavior_ui as BU
    return BM, BC, BU


def _anim_reg():
    """按需加载 IL2CPP dump 的元数据注册表（首次约 1~2 秒，之后走缓存）。"""
    BM, BC, BU = _anim_codec()
    return BM.load()


def _anim_behavior_bytes(b):
    """该行为的原始数据字节（hex 存 src_json 的 rawB64 或内存字段里）。"""
    import base64
    import json as _json
    try:
        item = _json.loads(b.src_json or "{}")
    except Exception:
        return None
    rb = item.get("rawB64")
    if not rb:
        return None
    try:
        return base64.b64decode(rb)
    except Exception:
        return None


def _anim_rebuild_fields(context, beh_index, src="anim"):
    """按元数据把字段值拍平进 `b.fields`（读取后、改动后都调它）。

    `src="comp"` 时同一套逻辑服务 ⑧ 的组件（`COMP_Item` 有 `cls`/`raw_type`/`values_json`，
    形状与行为一致 ⇒ 不需要第二份实现）。
    """
    import json as _json
    scene = context.scene
    b = _flds_of(scene, src, beh_index)
    if b is None:
        return
    b.fields.clear()
    if src == "anim":
        _anim_heal_type(b)      # 先把 b.type 从权威的 raw_type 对齐（修枚举下标漂移）
    try:
        BM, BC, BU = _anim_codec()
        reg = _anim_reg()
        cls = b.raw_type or b.type
        vals = (_anims_to_dict_values(b) if src == "comp"
                else _anim_sync_values_from_props(context, b))
        rows = BU.json_to_slots(reg, cls, vals)
    except Exception as e:  # noqa: BLE001
        print("[动画] 字段视图生成失败：%s" % e)
        b.raw_only = True
        return
    for r in rows:
        f = b.fields.add()
        f.muted = True      # 填充期间屏蔽 update 回调（避免中途态写回 JSON）
        f.owner = beh_index
        f.src = src
        f.path = _json.dumps(r["path"])
        f.depth = r.get("depth", 0)
        # 中文标签：来自行为词典（dump.cs 的字段名 + [Tooltip] 语义），
        # 词典里没有的字段退回原来的「下划线转空格」✓
        # ⛔ v1.8.91 优先级修正：词典**查不到**时它只会退化成「下划线转空格」，
        #    那会盖掉**字段行自带的语义标签**（曲线那三个整数 preInfinity/postInfinity/
        #    rotationOrder 就被显示成英文原名 ✗ —— draw 冒烟测试抓到的）。
        #    规则：词典给出的是"真名字"就用词典的；只是退化兜底时，字段行的标签优先 ✓
        cn = r.get("label", "")
        fname = r.get("name") or ""
        fallback = fname.replace("_", " ").strip() or fname
        try:
            bd = _anim_bd().label(cls, r.get("path") or [], fname)
            if bd and bd != fallback:
                cn = bd
        except Exception:  # noqa: BLE001
            pass
        f.label = "%s  ·  %s" % (cn, r.get("cs", ""))
        f.note = ""
        try:
            f.note = _anim_bd().tip(cls, r.get("path") or [], r.get("name") or "")
        except Exception:  # noqa: BLE001
            pass
        f.kind = r.get("kind", "")
        f.cs = r.get("cs", "")
        f.fcount = r.get("count", 0)
        f.felem = r.get("elem", "float")
        f.pid = str(r.get("pid", 0))
        slots = r.get("slots") or {}
        f.f = slots.get("f", 0.0)
        f.f2 = slots.get("f2", 0.0)
        f.f3 = slots.get("f3", 0.0)
        f.i = slots.get("i", 0)
        f.b = bool(slots.get("b"))
        f.s = slots.get("s", "")
        # 节点引用：数字 pid 换成挂载点名（可读、可选）
        if f.kind == "node":
            f.s = _anim_pid_name(context, slots.get("s", "0"), src)
        if f.kind == "enum":
            f.eopts = _json.dumps(r.get("options") or [])
            f.e = str(int(slots.get("i", 0)))
        if f.kind in ("list", "dict") and r.get("path"):
            f.list_path = _json.dumps(r["path"])
        f.muted = False     # 填充完毕，恢复回调


def _node_key(src, which):
    r"""节点名 ↔ pid 两张表存在**哪个场景键**里。

    ⛔ v1.8.90：④ 和 ⑧ **必须各存一份**，不能共用。
       ④ 读动画时写的是**全 bundle** 的名字表（原因见 `BAMOD_OT_ReadAnimation`：
       武器/挂点会引用**别的 prefab** 里的节点，只用本 prefab 的会把外部节点名判成"没有" ✗）。
       ⑧ 扫描写的是**本 prefab** 的表。若共用同一个键，两边会互相覆盖 ——
       先后点两次就能把对端的数据冲掉，而且**完全看不出异常**（只是吸管有时认不出）✗
    """
    if src == "comp":
        return "bamod_comp_node_names" if which == "n" else "bamod_comp_node_pids"
    return "bamod_anim_node_names" if which == "n" else "bamod_anim_node_pids"


def _anim_pid_name(context, pid, src="anim"):
    """pid → 挂载点名（认不出就显示 pid 数字，仍可编辑）。"""
    import json as _json
    try:
        m = _json.loads(context.scene.get(_node_key(src, "n"), "{}"))
    except Exception:
        m = {}
    return m.get(str(pid), str(pid))


def _anim_name_pid(context, name, src="anim"):
    """挂载点名 → pid；认不出则返回 None（表示"保持原 pid 不变"）。"""
    import json as _json
    try:
        m = _json.loads(context.scene.get(_node_key(src, "p"), "{}"))
    except Exception:
        m = {}
    if name in m:
        return int(m[name])
    try:
        return int(name)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 节点名标注（v1.8.67）—— 让**构建期**才分配的 pid 也能正确解析
# ---------------------------------------------------------------------------
def _anim_node_maps(context):
    """`(name -> pid, pid -> name)` 两张表（读动画/导入模型时写入场景）。"""
    import json as _json
    try:
        n2p = _json.loads(context.scene.get("bamod_anim_node_pids", "{}"))
    except Exception:
        n2p = {}
    try:
        p2n = _json.loads(context.scene.get("bamod_anim_node_names", "{}"))
    except Exception:
        p2n = {}
    return n2p, p2n


def _anim_safe_name(context, pid):
    """pid → 挂载点名，**只在"名字↔pid 一一对应"时才返回**，否则返回 None。

    ⛔ 为什么必须判歧义：写回时 `copy_full._pid_of(name)` 只按名字查表，
        prefab 里若有两个同名 GameObject，名字反查到的可能是**另一个** pid ⇒
        本来逐字节原样保留的行为会被悄悄改指到别的节点 ✗。
        只在 `n2p[name] == pid` 时才标注名字，就不会出现这种漂移。
    """
    if not pid:
        return None
    n2p, p2n = _anim_node_maps(context)
    nm = p2n.get(str(int(pid)))
    if not nm:
        return None
    if int(n2p.get(nm, 0)) != int(pid):
        return None        # 同名歧义 ⇒ 不标注，保持原 pid
    return nm


def _anim_annotate_names(context, vals):
    """给值树里所有能唯一确定名字的 PPtr 打上 `__name__`（构建期据此重解析 pid）。

    返回标注了几处。未标注的保持原 pid 不动（所以**老数据的字节级往返不受影响**）。
    """
    n2p, p2n = _anim_node_maps(context)
    if not n2p and not p2n:
        return 0
    hits = [0]

    def walk(o):
        if isinstance(o, dict):
            if o.get("__pptr__"):
                if not o.get("__name__"):
                    nm = _anim_safe_name(context, o.get("pathID"))
                    if nm:
                        o["__name__"] = nm
                        hits[0] += 1
                return
            for v in list(o.values()):
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(vals)
    return hits[0]


def _anim_local_names(context):
    """本 prefab 内**可用**的节点名集合（用于"当前模型有没有这个节点"的判定）。

    ⛔ 为什么不能只看 `bamod_anim_node_pids`：那张表是**全 bundle** 的
       （`Hierarchy(objs)` 覆盖整个 bundle 的每个 Transform），因为武器/炮塔等
       会引用**别的 prefab** 里的节点，`copy_full._pid_of` 也刻意留了全 bundle 兜底。
       后果：ACV 上写 `Rotorangle_0` 也能"解析成功"——解析到的是**某架直升机**的旋翼节点，
       而那个节点根本不在 ACV 的 prefab 里 ⇒ 进游戏是坏引用 ✗，而面板毫无提示。
       ⇒ 给用户看的"当前模型有没有这个节点"必须用**prefab 内**的名单。

    ⛔ v1.8.70：还要并上**场景里已存在的挂载点**（② 面板新建的 Empty）。
       否则「给自建旋翼节点加 Torque」这条**推荐用法**，一点「校验」就被报
       "节点不在本模型里（引用会失效）"—— 而构建其实会**新建**这个节点、引用完全有效 ✗。
       那是**假警告**，会把用户带偏。
    """
    import json as _json
    names = set()
    s = context.scene.get("bamod_anim_local_names")
    if s:
        try:
            names |= set(_json.loads(s))
        except Exception:  # noqa: BLE001
            pass
    try:
        for o in bpy.data.objects:
            if o.type == "EMPTY" and o.get("ba_mount"):
                names.add(_normalize_name(o.name))
    except Exception:  # noqa: BLE001
        pass
    if names:
        return names
    n2p, _ = _anim_node_maps(context)
    return set(n2p)


def _anim_missing_nodes(context, vals):
    """值树里引用了名字、但**本 prefab 没有**的节点名集合。"""
    local = _anim_local_names(context)
    if not local:
        return set()
    miss = set()
    for _p, v in _iter_ppt_vals(vals or {}):
        nm = v.get("__name__")
        if nm and nm not in local:
            miss.add(nm)
    return miss


def _copy_source_name(context):
    r"""当前「复制源」的短名 —— 即**最后一次 ① 导入**的那个 prefab。

    ⛔ 这个值同时决定「读取动画」读谁、以及「构建写回」把模型写进哪个 prefab。
       用户实测踩过坑：先导 ACV、再导捐赠机 ⇒ 读到的是**捐赠机的行为**
       （US_ACV 只有 9 条、US_AH-1Z Viper 有 14 条），构建也会写到捐赠机上 ✗
       ⇒ 面板上必须**一直显示它**，别让用户猜。
    """
    p = context.scene.get("ba_copy_source_path", "") or ""
    return _short_name(p) if p else ""


def _selected_prefab_name(context):
    """① 面板当前**选中**的 prefab 短名（用于「读取①所选模型的动画」）。"""
    try:
        lst = context.scene.bamod_prefabs
        i = context.scene.bamod_prefab_index
        if 0 <= i < len(lst):
            return lst[i].name or _short_name(lst[i].path)
    except Exception:  # noqa: BLE001
        pass
    return ""


def _anim_bd():
    """行为词典（24 类的中文名/说明/字段标签/模板）。

    ⛔ `_rev_tools` 必须已在 sys.path —— 现在 `register()` 里就会加（见那里的说明）；
    万一没有（老版本装的插件、或路径被改），这里也兜一次，否则 ④ 面板会**静默退化成
    只有 5 个内置类型、没有中文标签**（用户看不出是加载失败）。
    """
    import os
    import sys
    try:
        import behavior_dict  # noqa: F401
    except ImportError:
        here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_rev_tools")
        if os.path.isdir(here) and here not in sys.path:
            sys.path.insert(0, here)
    import behavior_dict
    return behavior_dict


def _anim_touch(context):
    """动过动画面板 = 用户想让动画生效 ⇒ 自动勾上「应用动画」。

    ⛔ 为什么必须自动勾：`apply_anim` 默认是关的，用户在 ④ 面板辛苦加了半天行为、
        一构建——**什么都没发生、也没有任何报错** ✗（最恶劣的失败方式）。
        现在只要碰过动画（读取/新增/复制/模板/导入），构建时就一定会写回。
    """
    try:
        _prefs(context).apply_anim = True
    except Exception:  # noqa: BLE001
        pass
    # v1.8.87：行为列表变过 ⇒ 重建「每条行为一个真子面板」（带签名去重，重复调用很便宜 ✓）
    try:
        _anim_rebuild_panels(context)
    except Exception as e:  # noqa: BLE001
        print("[动画] 重建行为子面板失败：%s" % e)


def _anim_static_items():
    r"""**顺序固定不变**的行为类型下拉（给"选类型"的对话框用）。

    ⛔ 为什么不能用 `_anim_type_items`（动态）做"选哪个类"的控件：
       Blender 的 `EnumProperty` 内部**存的是下标**，每次读值都用**当时的 items 列表**
       把下标翻回字符串。而 `_anim_type_items` 的列表长度会随场景变化
       （读到的 hub 里有新类，就追加进 `bamod_anim_types`）⇒ 同一个下标在不同时刻
       指向**不同的类** ✗。
       实测症状：对话框里选 `Afterburner`，`execute` 里读到的是 `PositionRandom`
       （生成的行为字段全是 PositionRandom 的）—— 用户看到的就是"**类型是乱码**"。
    本函数返回**同一个列表对象**（缓存），顺序来自 `behavior_dict` 的静态类表，
    不含任何场景相关项 ⇒ 下标永远稳定 ✓。
    """
    global _ANIM_STATIC_ITEMS
    if not _ANIM_STATIC_ITEMS:
        try:
            bd = _anim_bd()
            _ANIM_STATIC_ITEMS = [(str(a), str(b), str(c))
                                  for a, b, c in bd.type_items(sorted(bd.CLASS_INFO))]
        except Exception as e:  # noqa: BLE001
            print("[动画] 静态类型表加载失败（退回内置 5 类）：%s" % e)
            _ANIM_STATIC_ITEMS = [(str(a), str(b), str(c)) for a, b, c in ANIM_TYPES_BASE]
    return _ANIM_STATIC_ITEMS


def _anim_heal_type(b):
    """把 `b.type` 从权威的 `b.raw_type` 重新解析一遍（修正下标漂移）。

    ⛔ `b.type` 是动态 items 的枚举，内部存下标；列表一变（场景里出现了新类），
       原来那个下标就可能指向**别的类** ⇒ 面板显示错、便捷属性也按错的类走 ✗。
       `b.raw_type` 是字符串，**永远是权威**（写字节也用它）。
       这里在每次重建字段视图时对齐一次，漂移就自愈了。
    """
    rt = (b.raw_type or "").strip()
    if not rt:
        return
    try:
        if b.type != rt:
            b.type = rt
    except Exception:  # noqa: BLE001 - 表里没有这个类时只能保持原样
        pass


def _anim_array_items():
    bd = _anim_bd()
    return getattr(bd, "_ARRAY_TIP", {})


def _flds_of(scene, src, index):
    """按 `src` 取"拥有字段表的那个对象"：④ 的行为 或 ⑧ 的组件。

    ⛔ 两个列表长度不同、下标各自独立 ⇒ 任何拿 `owner` 当**行为下标**用的地方
       都必须先过这里。反过来也一样（组件行被 ④ 的算子误当行为取 ⇒ 越界或改错对象 ✗）
    """
    try:
        coll = scene.bamod_comps if src == "comp" else scene.bamod_anims
        i = int(index)
        if 0 <= i < len(coll):
            return coll[i]
    except Exception:  # noqa: BLE001
        pass
    return None


def _anim_write_field(f, context):
    """一个字段控件改动 → 写回它所属对象（行为 / 组件）的 values_json。"""
    import json as _json
    if getattr(f, "muted", False):
        return          # 正在批量填充：忽略回调，避免把中途态写进 JSON
    scene = context.scene
    b = _flds_of(scene, getattr(f, "src", "anim"), f.owner)
    if b is None:
        return
    # ⛔ 用户在「通用字段」表里改过东西 ⇒ 字段表接管，便捷属性不再覆盖它 ✓
    #    （组件没有"便捷属性"这套，`conv` 恒为 False，这里判一下只是在共用代码时保持语义）
    if getattr(f, "src", "anim") == "anim":
        b.conv = False
    try:
        vals = _json.loads(b.values_json or "{}")
    except Exception:
        vals = {}
    BM, BC, BU = _anim_codec()
    try:
        path = _json.loads(f.path)
    except Exception:
        return
    row = {"kind": f.kind, "count": f.fcount, "elem": f.felem}
    if f.kind == "node":
        pid = _anim_name_pid(context, f.s, getattr(f, "src", "anim"))
        old = _json.loads(b.values_json or "{}")
        cur = BU.jget(old, path) or {}
        if pid is None:
            pid = int((cur or {}).get("pathID", 0) or 0)
        v = {"__pptr__": True, "fileID": int((cur or {}).get("fileID", 0) or 0),
             "pathID": int(pid)}
        # ⛔ v1.8.67：把**名字**一起存进值里。真实 pid 要构建期才定得下来
        #    （用户自己新建的挂载点如 Rotorangle_0 当时根本还没有 pid）——
        #    只存 pid 的话这里只能存 0 ⇒ 构建出来指向空引用 ✗。
        #    写了名字，构建期 `behavior_codec.emit(..., pid_of)` 就能解析到真 pid ✓。
        if f.s and not str(f.s).lstrip("-").isdigit():
            v["__name__"] = str(f.s)
    elif f.kind == "enum":
        v = int(f.e or 0)
    else:
        v = BU.slots_to_value(row, {"b": f.b, "i": f.i, "f": f.f, "f2": f.f2,
                                    "f3": f.f3, "s": f.s})
    if v is None:
        return
    BU.jset(vals, path, v)
    b.values_json = _json.dumps(vals, ensure_ascii=False)


def _anims_to_dict_values(b):
    """行为的字段值（JSON）→ dict，交给编解码器写字节。"""
    import json as _json
    try:
        return _json.loads(b.values_json or "{}")
    except Exception:
        return {}


def _anim_sync_values_from_props(context, b):
    """把「便捷属性」（一键/新增按钮设的 target/speed/dir/lod…）同步进 `values_json`。

    ⛔ 权威归属（v1.8.67 修正）——由 `b.conv` 决定，二者不会同时生效：
      · `b.conv = True`（行为是「新增 / 一键」造出来的）：
        顶部那套便捷输入框是**权威**，每次取字段值时把它们映射到元数据里的真实字段名
        （`_target` / `_speed` / `_direction` / `_lodGroup`…）覆盖回去。
        ⇒ 用户填了目标节点名、改了转速，构建时**真的会生效** ✓。
      · `b.conv = False`（读过动画 / 从模板或文件导入 / 在通用字段表里改过）：
        `values_json` 是权威，直接原样返回（不动用户改过的任何字段）。
      · `values_json` 为空（真的什么都没有）时：按元数据生成完整默认值兜底。

    ⛔ 旧写法的坑：只要 `values_json` 非空就**直接返回**，而「+ 旋翼自转」按钮恰恰会
       预填一份默认值 ⇒ 之后用户在面板上填的目标节点、转速**全都被静默忽略** ✗
       （只有"一键"按钮能用，因为它走后建的是空 values_json）。
    """
    import json as _json
    vals = _anims_to_dict_values(b)
    cls = b.raw_type or b.type
    BM, BC, BU = _anim_codec()
    reg = _anim_reg()
    if not vals:
        try:
            vals = BC.make_defaults(reg, cls)
        except Exception:  # noqa: BLE001
            return {}
        if not b.conv:
            return vals
    elif not b.conv:
        return vals                    # 字段表权威：一个字都不动

    pid = _anim_name_pid(context, b.target) if b.target else None

    def _pptr(p, name=None):
        v = {"__pptr__": True, "fileID": 0, "pathID": int(p or 0)}
        # v1.8.67：连同名字一起存 —— 构建期才拿得到真实 pid（见 _anim_annotate_names）
        if name and not str(name).lstrip("-").isdigit():
            v["__name__"] = str(name)
        return v

    try:
        if cls == "Torque":
            vals["_lodGroup"] = b.lod
            vals["_target"] = _pptr(pid, b.target)
            vals["_direction"] = [b.dir_x, b.dir_y, b.dir_z]
            vals["_speed"] = b.speed
        elif cls in ("FloatEffect", "WaterFloatEffect"):
            vals["_lodGroup"] = b.lod
            vals["_rootBone"] = _pptr(pid, b.target)
            vv = _anim_values_parse(b.values_str)
            names = [f["name"] for f in reg.ser_fields(cls)]
            for k, nm in enumerate([n for n in names if n.startswith("_") and
                                    n not in ("_lodGroup", "_rootBone")]):
                if k < len(vv):
                    vals[nm] = vv[k]
        elif cls == "AxisRandom":
            vals["_inspectorName"] = ""
            vals["_speed"] = b.speed
            vals["_minimalTime"] = b.min_time
            vals["_maximalTime"] = b.max_time
            for ax, src, lo, hi in (("_x", b.x_source, b.x_min, b.x_max),
                                    ("_y", b.y_source, b.y_min, b.y_max),
                                    ("_z", b.z_source, b.z_min, b.z_max)):
                vals[ax] = {"Source": _pptr(_anim_name_pid(context, src) if src else 0, src),
                            "MinimalAngle": lo, "MaximalAngle": hi}
        elif cls == "MathConnect":
            vals["_lodGroup"] = b.lod
            vals["_settings"] = {"_frequency": b.freq, "_damper": b.damper,
                                 "_reaction": b.reaction, "_defaultSerialized": True}
            vals["_root"] = _pptr(_anim_name_pid(context, b.root) if b.root else 0, b.root)
            vals["_target"] = _pptr(pid, b.target)
            vals["_freezeXPos"] = b.freeze_x
            vals["_freezeYPos"] = b.freeze_y
            vals["_freezeZPos"] = b.freeze_z
        else:
            # 其余 19 个类没有"便捷属性"这一层 —— 保持 values_json 原样
            return vals
    except Exception as e:  # noqa: BLE001
        print("[动画] %s 便捷属性同步失败（保持原字段值）：%s" % (cls, e))
        return _anims_to_dict_values(b)
    b.values_json = _json.dumps(vals, ensure_ascii=False)
    return vals


def _fill_anims_from_dict(context, d, append=False, name_map=None):
    """把 hub_to_dict() 的结果填进 ④ 面板集合（读取动画与离线测试共用）。

    append=True  → **追加**（用于「从别的 prefab 抄行为」：不动已有条目）
    name_map     → `{源 prefab 的 transform pid: 节点名}`；给了它就把源 pid 换成
                   **当前模型**的 pid（按名字匹配），并写下 `__name__` 供构建期解析。
                   跨 prefab 抄行为必须走这条：源 pid 在目标 prefab 里毫无意义 ✗

    返回本次读到的类型集合（写进 scene["bamod_anim_types"]，供下拉使用）。
    """
    import json as _json
    if not append:
        context.scene.bamod_anims.clear()
    seen_types = set()
    # 第一遍：先把本次要出现的**全部类型**登记进 scene（下拉 items 要用），
    #          否则遇到未破译类型时 `b.type = typ` 会因枚举里没有该项而报错。
    for arr, items in d.items():
        if arr.startswith("_"):
            continue
        for it in items:
            seen_types.add(it.get("type") or "AxisRandom")
    prev = (context.scene.get("bamod_anim_types", "") or "").split(",") if append else []
    context.scene["bamod_anim_types"] = ",".join(
        sorted(seen_types | {t for t in prev if t}))
    # 文件内条目顺序：不存下来的话写回会按「数组顺序」重排 ⇒ 与原字节不一致
    if not append:
        context.scene["bamod_anim_order"] = ",".join(str(r) for r in d.get("_order", []))
    # 第二遍：真正填字段
    for arr, items in d.items():
        if arr.startswith("_"):
            continue
        for it in items:
            b = context.scene.bamod_anims.add()
            b.array = arr
            b.rid = str(it.get("_rid", "")) if it.get("_rid") is not None else ""
            typ = it.get("type") or "AxisRandom"
            seen_types.add(typ)
            b.raw_type = typ
            # 原始 JSON 存起来当写回基底（未破译字段/pid/命名空间不丢）
            try:
                b.src_json = _json.dumps(it, ensure_ascii=False)
            except Exception:
                b.src_json = ""
            b.lod = it.get("lod", 0)
            if typ == "AxisRandom":
                b.type = "AxisRandom"
                b.speed = it.get("speed", 20)
                b.min_time = it.get("minTime", 2)
                b.max_time = it.get("maxTime", 10)
                for ax in ("x", "y", "z"):
                    a = it.get("axes", {}).get(ax, {})
                    setattr(b, ax + "_source", a.get("source", ""))
                    setattr(b, ax + "_min", a.get("min", 0))
                    setattr(b, ax + "_max", a.get("max", 0))
            elif typ == "MathConnect":
                b.type = "MathConnect"
                b.freq = it.get("freq", 0)
                b.damper = it.get("damper", 0)
                b.reaction = it.get("reaction", 0)
                b.root = it.get("root", "")
                b.target = it.get("target", "")
                f = it.get("freeze", {})
                b.freeze_x = f.get("x", False)
                b.freeze_y = f.get("y", False)
                b.freeze_z = f.get("z", False)
                b.shots_b64 = it.get("shotsRawB64", "") or ""
            elif typ == "Torque":
                b.type = "Torque"
                b.target = it.get("target", "")
                dr = it.get("direction", {})
                b.dir_x = dr.get("x", 0.0)
                b.dir_y = dr.get("y", -1.0)
                b.dir_z = dr.get("z", 0.0)
                b.speed = it.get("speed", 1080.0)
            elif typ in ("FloatEffect", "WaterFloatEffect"):
                b.type = typ
                b.target = it.get("target", "")
                b.values_str = ", ".join("%g" % v for v in (it.get("values") or []))
            else:
                # 未破译类型：下拉里已经有这一项（由 bamod_anim_types / raw_type 提供）
                b.type = typ
    # 第三遍：用**元数据编解码器**把原始字节解析成命名字段 → 通用字段编辑器
    start = len(context.scene.bamod_anims) - sum(
        len(v) for k, v in d.items() if not k.startswith("_")) if append else 0
    for i in range(max(0, start), len(context.scene.bamod_anims)):
        b = context.scene.bamod_anims[i]
        raw = _anim_behavior_bytes(b)
        vals = None
        if raw:
            try:
                BM, BC, BU = _anim_codec()
                reg = _anim_reg()
                cls = b.raw_type or b.type
                vals = BC.parse(reg, cls, raw)
                # ⛔ **必须验证"写回 == 原始字节"**：全库穷举（607 个 prefab / 3288 个实例）
                #    里有 18 个 SpawnVFX 实例，解析能过、但 emit(parse(x)) ≠ x ✗。
                #    若还让它们显示成可编辑字段，用户一改就会**静默改写**原始字节。
                #    不一致 ⇒ 退回"仅原始字节"（仍然字节级往返，只是不出字段）。
                if BC.emit(reg, cls, vals) != raw:
                    print("[动画] %s 该实例写回与原字节不一致（%d 字节）⇒ 只保留原始字节"
                          % (cls, len(raw)))
                    vals = None
            except Exception as e:  # noqa: BLE001
                print("[动画] %s 通用解析回退为原始字节：%s" % (b.raw_type, e))
        if vals is None:
            b.raw_only = True
            b.values_json = "{}"
        else:
            if name_map:
                _anim_retarget_vals(context, vals, name_map)
            b.raw_only = ("__n__" in vals and vals.get("__n__") == 0)
            b.values_json = _json.dumps(vals, ensure_ascii=False)
        _anim_rebuild_fields(context, i)
    # v1.8.87：列表变了就重建动态子面板（读动画/导入/从文本同步都走这里 ✓）
    try:
        _anim_rebuild_panels(context)
    except Exception as e:  # noqa: BLE001
        print("[动画] 重建行为子面板失败：%s" % e)
    return seen_types


def _anim_retarget_vals(context, vals, name_map):
    """跨 prefab 抄行为时，把值树里的**源 prefab pid** 换成当前模型的 pid。

    ⛔ 不做这一步会怎样：抄过来的 Torque 指向源 prefab 的 Transform pid，
       而目标 prefab 里那个 pid 根本不存在（或是别的节点）⇒ 行为指错/失效 ✗。
       做法：用 `name_map`（源 pid → 节点名）取出名字 → 按**名字**在当前节点表里查 pid。
       名字查不到（目标模型没这个节点）时写 0 并保留 `__name__`，
       面板会显示成名字、构建期也还能再试一次解析。
    """
    hits = [0]

    def walk(o):
        if isinstance(o, dict):
            if o.get("__pptr__"):
                nm = o.get("__name__")
                if not nm:
                    nm = name_map.get(int(o.get("pathID", 0) or 0))
                if nm:
                    o["__name__"] = nm
                    cur = _anim_name_pid(context, nm)
                    o["pathID"] = int(cur or 0)
                    hits[0] += 1
                return
            for v in list(o.values()):
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(vals)
    return hits[0]


def _anim_values_parse(s):
    out = []
    for t in (s or "").replace("，", ",").split(","):
        t = t.strip()
        if not t:
            continue
        try:
            out.append(float(t))
        except ValueError:
            pass
    return out


def _anims_to_dict(context):
    """面板集合 → hub JSON dict。

    ⛔ 基底用 `src_json`（读取时保存的原始 JSON），只覆盖面板里暴露的字段：
    这样 pid（`sourcePID`/`targetPID`）、命名空间（`_ns`，本作里 ProceduralDirt 是空串 ✗）、
    未破译行为的原始字节（`rawB64`）全都不会丢。
    v1.8.57 之前是「按类型从零重建」，任何没被面板覆盖的类都会被写成 AxisRandom ✗
    ⇒ 一按构建就把原 prefab 的 AnimationHub 写坏。
    """
    import json as _json
    d = {"universal": [], "demo": [], "game": [], "preDeath": [], "death": []}
    for b in context.scene.bamod_anims:
        item = None
        if b.src_json:
            try:
                base = _json.loads(b.src_json)
                if isinstance(base, dict) and base.get("type") == b.type:
                    item = base
            except Exception:
                item = None
        if item is None:
            item = {"type": b.type}
        if b.rid:
            item["_rid"] = int(b.rid)
        item["type"] = b.type
        item["lod"] = b.lod
        # ⛔ **通用路径**：字段值（values_json）拍成 `__vals__` 交给**构建期**编码。
        #    为什么不能在面板就编码成 `__bytes__`：节点引用的 pid 要构建期才定得下来
        #    （自己新建的挂载点如 Rotorangle_0 在"读动画"那一刻**还没有 pid**）——
        #    提前烘成字节只能烘 0 ⇒ 进游戏指向空引用（旋翼不转）✗ 实测确认。
        #    现在把值（含 `__name__` 挂载点名）留着，由 `hub_edit._emit_entry` 在
        #    构建期调 `behavior_codec.emit(..., pid_of=…)` 解析出真 pid ✓。
        #    附带好处：文本块 bamod_anim 里的 JSON 变成**可读可手改**的字段值，
        #    而不是一长串 hex。
        used_generic = False
        vals = _anim_sync_values_from_props(context, b)
        if vals and not b.raw_only:
            _anim_annotate_names(context, vals)
            item["__vals__"] = vals
            used_generic = True
        if not used_generic and item.get("rawB64"):
            # 完全解析不了的实例：把读到的**原始字节**原样带去构建期
            # （`rawB64` 是 base64、`__bytes__` 是 hex —— 单位别搞混，实测踩过）。
            import base64 as _b64
            try:
                item["__bytes__"] = _b64.b64decode(item["rawB64"]).hex()
                used_generic = True
            except Exception:  # noqa: BLE001
                pass
        if not used_generic and b.type == "AxisRandom":
            item["name"] = item.get("name", "")
            item["speed"] = b.speed
            item["minTime"] = b.min_time
            item["maxTime"] = b.max_time
            ax = item.get("axes") or {}
            item["axes"] = {
                k: dict(ax.get(k) or {}) for k in ("x", "y", "z")}
            for k, src, lo, hi in (("x", b.x_source, b.x_min, b.x_max),
                                   ("y", b.y_source, b.y_min, b.y_max),
                                   ("z", b.z_source, b.z_min, b.z_max)):
                item["axes"][k]["source"] = src
                item["axes"][k]["min"] = lo
                item["axes"][k]["max"] = hi
        elif b.type == "MathConnect":
            item["freq"] = b.freq
            item["damper"] = b.damper
            item["reaction"] = b.reaction
            item.setdefault("objBool", True)
            item["root"] = b.root
            item["target"] = b.target
            item["freeze"] = {"x": b.freeze_x, "y": b.freeze_y, "z": b.freeze_z}
            if b.shots_b64:
                item["shotsRawB64"] = b.shots_b64
        elif b.type == "Torque":
            item["target"] = b.target
            item["direction"] = {"x": b.dir_x, "y": b.dir_y, "z": b.dir_z}
            item["speed"] = b.speed
        elif b.type in ("FloatEffect", "WaterFloatEffect") and not used_generic:
            item["target"] = b.target
            vals = _anim_values_parse(b.values_str)
            if vals:
                item["values"] = vals
        d[b.array].append(item)
    order_str = context.scene.get("bamod_anim_order", "")
    if order_str:
        d["_order"] = [int(x) for x in order_str.split(",") if x]
    return d


class BAMOD_OT_ReadAnimation(bpy.types.Operator):
    bl_idname = "ba_mod.read_animation"
    bl_label = "读取动画"
    bl_description = "解析复制源的 AnimationHub，把动画行为填进下面的面板"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        prefs = _prefs(context)
        source_pid = context.scene.get("ba_copy_source_pid")
        if not source_pid:
            self.report({"ERROR"}, "请先①导入一个 prefab（记录复制源）")
            return {"CANCELLED"}
        unitypy_bridge.add_paths(prefs.rev_dir, prefs.unitypy_dir)
        try:
            import importlib, base64, struct
            import UnityPy
            import copy_full, hub_edit
            importlib.reload(copy_full)
            importlib.reload(hub_edit)
            env = UnityPy.load(prefs.bundle)
            objs = list(list(env.objects)[0].assets_file.objects.values())
            from analyze_common import Hierarchy
            h = Hierarchy(objs, {o.path_id: o for o in objs})
            src_objects, _, _ = copy_full.collect_prefab_objects(
                prefs.bundle, int(source_pid),
                want_path=context.scene.get("ba_copy_source_path", ""))
            hub_raw = None
            for o in src_objects:
                if o["type_name"] == "MonoBehaviour":
                    raw = base64.b64decode(o["raw"])
                    if len(raw) >= 28 and struct.unpack_from("<q", raw, 20)[0] == hub_edit.HUB_SCRIPT:
                        hub_raw = raw
                        break
            def name_of(pid):
                return h.name(h.tr_go.get(pid, 0)) if pid else ""
            # ⛔ 必须把「节点名 ↔ Transform pid」两张表写进场景：
            #    通用字段编辑器里的**节点引用行**靠它们把 pid 显示成可读名字、
            #    并在吸管选节点时把名字解析回 pid。以前只读不写 ⇒ 吸管是**静默空操作**
            #    （界面显示成名字、实际 pid 没变）✗ —— 实测确认。
            import json as _json2
            _n2p, _p2n = {}, {}
            for _trpid, _gpid in h.tr_go.items():
                _nm = h.name(_gpid)
                if not _nm:
                    continue
                _p2n[str(_trpid)] = _nm
                _n2p.setdefault(_nm, _trpid)
            context.scene["bamod_anim_node_pids"] = _json2.dumps(_n2p, ensure_ascii=False)
            context.scene["bamod_anim_node_names"] = _json2.dumps(_p2n, ensure_ascii=False)
            # 本 prefab **自己**的节点名（用于"当前模型没有这个节点"的准确提示）。
            # ⛔ 上面两张表是全 bundle 的（武器会引用别的 prefab 的节点），
            #    拿它判"有没有"会把外挂 prefab 的节点名误判成"有" ✗。
            _local = set()
            for _o in src_objects:
                if _o["type_name"] == "GameObject":
                    _nm = h.name(_o["pid"])
                    if _nm:
                        _local.add(_nm)
            context.scene["bamod_anim_local_names"] = _json2.dumps(
                sorted(_local), ensure_ascii=False)
            print("[动画] 本 prefab 节点名 %d 个（bundle 级名字表 %d 个）"
                  % (len(_local), len(_n2p)))
            if hub_raw is None:
                # ⛔ v1.8.67：原本这里**直接报错退出** ⇒ 没有 AnimationHub 的模型
                #    （静物、建筑、自建模型）永远做不了动画 ✗。
                #    现在：清空面板 + 保留刚建好的节点表（吸管/名字解析照常可用），
                #    让用户从零加行为；构建时 `copy_full` 会**新建**一个 AnimationHub
                #    挂到根节点上 ✓。
                context.scene.bamod_anims.clear()
                context.scene["bamod_anim_types"] = ""
                context.scene["bamod_anim_order"] = ""
                self.report({"WARNING"},
                            "这个 prefab 没有 AnimationHub —— 已清空面板。"
                            "现在可以用模板/新增做动画，**构建时会自动新建一个 AnimationHub**")
                return {"FINISHED"}
            d = hub_edit.hub_to_dict(hub_raw, name_of)
            context.scene["bamod_anim_order"] = ",".join(str(r) for r in d.get("_order", []))
            # 同步 JSON 到文本块（方便高级用户用 JSON 改）
            txt = bpy.data.texts.get("bamod_anim")
            if txt is None:
                txt = bpy.data.texts.new("bamod_anim")
            txt.clear()
            txt.write(hub_edit.hub_to_json(hub_raw, name_of, indent=1))
            # 填进面板集合（与离线测试共用同一段逻辑）
            seen_types = _fill_anims_from_dict(context, d)
            _anim_touch(context)
            # ⛔ 必须把**读的是哪个模型**报出来：用户实测踩过"以为在读 ACV、
            #    其实读到的是捐赠机的 14 条"这种坑，光看数字分辨不出来 ✗
            _srcn = _copy_source_name(context) or source_pid
            self.report({"INFO"}, "已读取【%s】的动画：%d 个行为（%d 种类型）"
                        % (_srcn, len(context.scene.bamod_anims), len(seen_types)))
            print("[动画] 读取来源 = %s（复制源 = 最后一次 ① 导入的 prefab）"
                  % context.scene.get("ba_copy_source_path", source_pid))
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.report({"ERROR"}, "读取动画失败：%s" % e)
        return {"FINISHED"}


def _anim_pick_changed(self, context):
    r"""类型下拉一改就写回 `type` —— `type` 始终是**唯一权威**。

    ⛔ 为什么不能让 `pick` 自己当权威：Blender 的枚举属性**总是有值**（默认第一项），
       于是 `self.pick or self.type` 会永远返回 `pick` ⇒ 脚本/测试传进来的 `type`
       **被静默忽略**，新增出来的类全变成下拉的第一项
       （实测：24 个类全变成 AudioPlayer，进而 emit 报 `float() ... not NoneType`）✗。
       所以反过来：下拉只是"选择器"，值写进 `type`；`execute` 只读 `type`。
    """
    try:
        self.type = self.pick
    except Exception:  # noqa: BLE001
        pass


class BAMOD_OT_AddAnimation(bpy.types.Operator):
    r"""新增**任意一条**动画行为（24 个类全部可选）。

    ⛔ v1.8.68：以前面板上是 5 个写死的 `+ 类型` 快捷键 + 一个 `+ 其它类型`，
       而 `+ 其它类型` 背后是个 `StringProperty`（**根本没有下拉可点**）——
       点一下就直接塞一条 `AxisRandom`，用户看到一条自己没选过的类型、
       也不知道去哪儿改（这就是"类型是乱码"的来源）✗。
       现在：点按钮弹对话框，里面是**带中文名的真下拉**（24 项）+ 目标数组。

    ⛔ `type` 保持 `StringProperty`、下拉另用 `pick`（静态 items）：
       Blender 枚举内部存**下标**，动态 items 一变下标就指到别的类
       （实测：选 Afterburner 拿到 PositionRandom）⇒ 下拉必须用静态表。
       `type` 保留是为了兼容脚本/测试里的 `bpy.ops.ba_mod.add_animation(type="Torque")`。
    """
    bl_idname = "ba_mod.add_animation"
    bl_label = "新增动画行为"
    bl_description = "选一个行为类型加进 ④ 面板（24 个类全在列表里，可先选数组）"
    bl_options = {"REGISTER", "UNDO"}

    type: bpy.props.StringProperty(default="AxisRandom")

    pick: bpy.props.EnumProperty(
        name="行为类型", items=lambda self, ctx: _anim_static_items(),
        update=_anim_pick_changed,
        description="按中文名挑；括号里是游戏里的真实类名")
    array: bpy.props.EnumProperty(
        name="放进哪个数组", items=[
            ("auto", "按类型自动",
             "Torque/FloatEffect → game，AxisRandom → demo，其余 → universal"),
            ("universal", "universal", "通用（战场 + 军械库都生效）"),
            ("demo", "demo", "军械库演示（**战场不播**）"),
            ("game", "game", "战场游戏"),
            ("preDeath", "preDeath", "死亡前（被打后）"),
            ("death", "death", "死亡时"),
        ], default="auto")

    def invoke(self, context, event):
        # 弹对话框前：把下拉同步到当前 `type`（不在静态表里就保持默认，不影响 type）
        try:
            self.pick = self.type
        except Exception:  # noqa: BLE001
            pass
        return context.window_manager.invoke_props_dialog(self, width=520)

    def draw(self, context):
        layout = self.layout
        col = layout.column()
        col.prop(self, "pick", text="类型")
        col.prop(self, "array", text="数组")
        cls = self.type
        try:
            i = _anim_bd().info(cls)
        except Exception:  # noqa: BLE001
            i = {}
        if i.get("desc"):
            box = col.box()
            box.label(text=i["desc"], icon="INFO")
            if i.get("nown"):
                box.label(text="实测常用：%s" % i["nown"], icon="CHECKMARK")
            if i.get("danger"):
                box.label(text=_anim_bd().DANGER_TIP, icon="ERROR")

    def execute(self, context):
        import json as _json
        cls = self.type or "AxisRandom"
        _anim_all_classes()          # 预热 dump 里的行为类清单（下拉要用，只加载一次）
        b = context.scene.bamod_anims.add()
        # ⛔ **顺序很重要**：typed 枚举 `type` 只接受它已知的项；先把真类型写进
        #    `raw_type`（items 回调会扫描集合里各行为的 raw_type 并把它列进下拉），
        #    再赋 `type` 才不会 `enum "X" not found` ✗
        b.raw_type = cls
        b.type = cls
        # 「新增」出来的行为：顶部便捷输入框是权威（填目标节点/转速立刻生效）✓
        b.conv = True
        # 默认数组按类型给：**加错数组会白折腾** ——
        # `demo` 只在军械库预览里播、战场上不播（旋翼会"预览里转、实战不转"）；
        # `AxisRandom`（天线/炮塔微晃）放 demo 才和原版一致。
        # v1.8.68：对话框里选过（非 auto）就以用户选的为准。
        b.array = (self.array if self.array and self.array != "auto" else
                   {"Torque": "game", "FloatEffect": "game",
                    "WaterFloatEffect": "game",
                    "AxisRandom": "demo"}.get(cls, "universal"))
        # 命名空间/程序集从 dump 取（`ProceduralDirt` 的命名空间是**空串**，不能写死 ✗）
        ns, asm = "BrokenArrow.Client.Ecs.AnimationBehaviors", "BrokenArrow"
        try:
            ti = _anim_reg().get(cls)
            if ti is not None:
                ns = ti.ns or ""
        except Exception:  # noqa: BLE001
            pass
        b.src_json = _json.dumps({"type": cls, "_ns": ns, "_asm": asm},
                                 ensure_ascii=False)
        # 字段默认值按元数据生成（结构完整才能写出合法字节）
        b.values_json = "{}"
        try:
            import behavior_codec as BC
            reg = _anim_reg()
            b.values_json = _json.dumps(BC.make_defaults(reg, cls),
                                        ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            # ⛔ 生成不了默认值就**别留下一个坏条目**（它会写出空数据/错数据）✗
            #    删掉刚加的那条并明确报错，让用户知道这个类暂时加不了。
            idx = len(context.scene.bamod_anims) - 1
            context.scene.bamod_anims.remove(idx)
            self.report({"ERROR"}, "无法为 %s 生成默认值：%s" % (cls, e))
            return {"CANCELLED"}
        _anim_rebuild_fields(context, len(context.scene.bamod_anims) - 1)
        _anim_touch(context)
        self.report({"INFO"}, "已新增 [%s] %s" % (b.array, cls))
        return {"FINISHED"}


class BAMOD_OT_RemoveAnimation(bpy.types.Operator):
    bl_idname = "ba_mod.remove_animation"
    bl_label = "删除动画行为"
    bl_options = {"REGISTER", "UNDO"}
    index: bpy.props.IntProperty(default=0)

    def execute(self, context):
        if 0 <= self.index < len(context.scene.bamod_anims):
            context.scene.bamod_anims.remove(self.index)
            # ⛔ 删完必须**重建全部字段视图**：字段行里的 `owner` 是"第几个行为"的**下标**，
            #    删掉一条会让后面所有行的 owner 指错 ⇒ 之后编辑会写到**别的行为**上
            #    （实测：删第 0 条后改第 0 条的字段，值落到了第 2 条上 ✗）
            for i in range(len(context.scene.bamod_anims)):
                _anim_rebuild_fields(context, i)
            _anim_rebuild_panels(context)      # v1.8.87：真子面板要跟着少一个
        return {"FINISHED"}


class BAMOD_OT_PickAnimNode(bpy.types.Operator):
    bl_idname = "ba_mod.pick_anim_node"
    bl_label = "取当前选中物体"
    bl_description = "把场景里当前选中的物体名填进这个行为的目标节点（先选中挂载点再点）"
    bl_options = {"REGISTER", "UNDO"}
    index: bpy.props.IntProperty(default=0)

    def execute(self, context):
        o = context.active_object
        if o is None:
            self.report({"ERROR"}, "先在场景里选中一个物体")
            return {"CANCELLED"}
        if not (0 <= self.index < len(context.scene.bamod_anims)):
            return {"CANCELLED"}
        b = context.scene.bamod_anims[self.index]
        b.target = _normalize_name(o.name)
        self.report({"INFO"}, "目标已设为 %s" % b.target)
        return {"FINISHED"}


def _anim_add(anims, typ, array, **kw):
    """往面板集合里加一条行为（供一键按钮复用）。

    ⛔ `conv = True`：一键按钮走的是**便捷属性**（target/speed/dir），
       必须让它们成为权威，否则构建时会被 `values_json` 里的默认值盖掉 ✗。
    """
    b = anims.add()
    b.conv = True
    b.type = typ
    b.array = array
    for k, v in kw.items():
        setattr(b, k, v)
    return b


class BAMOD_OT_AddRotorTorque(bpy.types.Operator):
    bl_idname = "ba_mod.add_rotor_torque"
    bl_label = "补旋翼自转"
    bl_description = (
        "按真机直升机的实测约定往 AnimationHub 里加 Torque：game 数组 1080°/s，"
        "preDeath 数组 540°/s（被打下来时减速）。\n"
        "默认加「主旋翼 Rotorangle_0 + 尾桨 Rotorangle_1」——与 US_UH60M 完全一致；"
        "取消「同时加尾桨」就只加主旋翼（2 条 → 1 个旋翼，外面看就是单旋翼布局）。\n"
        "⛔ 单旋翼完全没问题：游戏不模拟反扭矩，没有尾桨不会打转，只是外观不一样。\n"
        "若你用的是 rot_blade_1 / tail（美系武装直升机命名），把名字改一下即可")
    bl_options = {"REGISTER", "UNDO"}

    main_node: bpy.props.StringProperty(name="主旋翼节点", default="Rotorangle_0")
    tail_node: bpy.props.StringProperty(name="尾桨节点", default="Rotorangle_1")
    add_tail: bpy.props.BoolProperty(
        name="同时加尾桨", default=True,
        description="取消勾选 = 只加主旋翼（只 2 条 Torque，不动尾桨节点）")
    speed: bpy.props.FloatProperty(name="转速(°/s)", default=1080.0)
    predeath_speed: bpy.props.FloatProperty(name="被打后转速", default=540.0)
    russian: bpy.props.BoolProperty(
        name="俄系转向（主旋翼 +Y）", default=False,
        description="美系主旋翼绕 -Y 转、俄系绕 +Y 转（俯视顺/逆时针不同）")

    def execute(self, context):
        anims = context.scene.bamod_anims
        my = 1.0 if self.russian else -1.0
        # (目标节点, 方向)：主旋翼永远加；尾桨看开关（名字留空也视为不加）
        rotors = [(self.main_node, (0.0, my, 0.0))]
        if self.add_tail and (self.tail_node or "").strip():
            rotors.append((self.tail_node, (-1.0, 0.0, 0.0)))
        for arr, sp in (("game", self.speed), ("preDeath", self.predeath_speed)):
            for node, d in rotors:
                _anim_add(anims, "Torque", arr, lod=2, target=node,
                          dir_x=d[0], dir_y=d[1], dir_z=d[2], speed=sp)
        self.report({"INFO"}, "已加 %d 条 Torque（%s；game %g°/s + preDeath %g°/s）"
                    % (len(rotors) * 2,
                       "主旋翼 + 尾桨" if len(rotors) > 1 else "只主旋翼",
                       self.speed, self.predeath_speed))
        return {"FINISHED"}


class BAMOD_OT_AddFloatEffect(bpy.types.Operator):
    bl_idname = "ba_mod.add_float_effect"
    bl_label = "补空中浮动"
    bl_description = ("加一条 FloatEffect（直升机机体滞空起伏）。默认值取自 "
                      "US_MH60X/US_UH60M/RU_MI24V_VP/US_AH64D 四架真机的实测均值；"
                      "目标节点默认 root —— 实测这四架的 FloatEffect（以及 ACV 原本的 "
                      "WaterFloatEffect）**全部指向 root**")
    bl_options = {"REGISTER", "UNDO"}
    target: bpy.props.StringProperty(name="目标节点", default="root")

    def execute(self, context):
        _anim_add(context.scene.bamod_anims, "FloatEffect", "game", lod=1,
                  target=self.target, values_str="0.35, 1.3, 0.25, 5.3, 0.33, 1.3, 0.25, 1.6")
        self.report({"INFO"}, "已加 FloatEffect（目标 %s）" % (self.target or "自身"))
        return {"FINISHED"}


class BAMOD_OT_AnimPickNode(bpy.types.Operator):
    bl_idname = "ba_mod.anim_pick_node"
    bl_label = "取当前选中物体作为节点"
    bl_description = "把场景里当前选中的挂载点名字填进这个字段（节点引用）"
    bl_options = {"REGISTER", "UNDO"}
    beh: bpy.props.IntProperty(default=0)
    field: bpy.props.IntProperty(default=0)
    src: bpy.props.StringProperty(default="anim")   # anim=④行为 / comp=⑧组件

    def execute(self, context):
        o = context.active_object
        if o is None:
            self.report({"ERROR"}, "先在场景里选中一个物体")
            return {"CANCELLED"}
        # ⛔ 先校验下标再取对象：draw 时记下的 beh 可能在点击前失效（行为被删/重复执行），
        #    旧写法先 `[...]` 后校验 ⇒ 越界直接抛异常 ✗
        b = _flds_of(context.scene, self.src, self.beh)
        if b is None:
            self.report({"ERROR"}, "行为已不存在（列表变了），请重新展开该行为")
            return {"CANCELLED"}
        if not (0 <= self.field < len(b.fields)):
            self.report({"ERROR"}, "字段行已失效，请重新展开")
            return {"CANCELLED"}
        f = b.fields[self.field]
        f.s = _normalize_name(o.name)
        _anim_write_field(f, context)
        pid = _anim_name_pid(context, f.s, self.src)
        if pid is None:
            self.report({"WARNING"},
                        "场景里的名字 %s 不在本 prefab 的节点表里 —— 节点引用未改变"
                        % f.s)
        else:
            self.report({"INFO"}, "%s ← %s" % (f.label.split("  ·  ")[0], f.s))
        return {"FINISHED"}


class BAMOD_OT_AnimListEdit(bpy.types.Operator):
    bl_idname = "ba_mod.anim_list_edit"
    bl_label = "数组/字典项编辑"
    bl_description = "给数组加一项 / 删一项 / 复制一项（通用：任何行为的任何数组）"
    bl_options = {"REGISTER", "UNDO"}
    beh: bpy.props.IntProperty(default=0)
    field: bpy.props.IntProperty(default=0)
    index: bpy.props.IntProperty(default=-1)
    mode: bpy.props.StringProperty(default="add")   # add / del / dup
    src: bpy.props.StringProperty(default="anim")   # anim=④行为 / comp=⑧组件

    def execute(self, context):
        import json as _json
        scene = context.scene
        # ⛔ 先校验再取（同 anim_pick_node：draw 时的下标可能已失效）
        b = _flds_of(scene, self.src, self.beh)
        if b is None:
            self.report({"ERROR"}, "行为已不存在（列表变了），请重新展开该行为")
            return {"CANCELLED"}
        if not (0 <= self.field < len(b.fields)):
            self.report({"ERROR"}, "字段行已失效，请重新展开")
            return {"CANCELLED"}
        f = b.fields[self.field]
        BM, BC, BU = _anim_codec()
        reg = _anim_reg()
        vals = _anims_to_dict_values(b)
        try:
            path = _json.loads(f.list_path or f.path)
        except Exception:
            return {"CANCELLED"}
        cur = BU.jget(vals, path)
        if f.kind == "list":
            arr = list(cur) if isinstance(cur, list) else []
            if self.mode == "add":
                elem = _anim_elem_default(reg, b, path)
                arr.append(elem)
            elif self.mode == "dup" and 0 <= self.index < len(arr):
                arr.insert(self.index + 1, _json.loads(_json.dumps(arr[self.index])))
            elif self.mode == "del" and 0 <= self.index < len(arr):
                arr.pop(self.index)
            BU.jset(vals, path, arr)
        elif f.kind == "dict":
            # v1.8.84：字典的 wire 是 [键数][键…][值数][值…]，**没有** a/b 两个前缀字 ✗
            d = dict(cur) if isinstance(cur, dict) else {"__dict__": True,
                                                         "keys": [], "values": []}
            keys = list(d.get("keys") or [])
            vals_ = list(d.get("values") or [])
            if self.mode == "add":
                keys.append(_anim_elem_default(reg, b, path, "key"))
                vals_.append(_anim_elem_default(reg, b, path, "val"))
            elif self.mode == "dup" and 0 <= self.index < len(keys):
                keys.insert(self.index + 1, _json.loads(_json.dumps(keys[self.index])))
                vals_.insert(self.index + 1, _json.loads(_json.dumps(vals_[self.index])))
            elif self.mode == "del" and 0 <= self.index < len(keys):
                keys.pop(self.index)
                vals_.pop(self.index)
            d["keys"], d["values"] = keys, vals_
            BU.jset(vals, path, d)
        else:
            return {"CANCELLED"}
        b.values_json = _json.dumps(vals, ensure_ascii=False)
        _anim_rebuild_fields(context, self.beh, self.src)
        return {"FINISHED"}


def _anim_elem_default(reg, b, path, which=None):
    """数组/字典新项的默认值：按元数据里该元素的类型生成完整结构。"""
    import behavior_codec as BC
    BM, _BC, BU = _anim_codec()
    cls = b.raw_type or b.type
    # 沿路径找到对应 wire
    wire = {"kind": "inline", "fields": reg.ser_fields(cls)}
    for seg in path:
        if isinstance(seg, int):
            wire = wire.get("elem") or wire.get("val") or {"kind": "prim", "size": 4}
        elif seg == "keys":
            wire = wire.get("key") or {"kind": "prim", "size": 4}
        elif seg == "values":
            wire = wire.get("val") or {"kind": "prim", "size": 4}
        else:
            fld = None
            for f in (wire.get("fields") or []):
                if f["name"] == seg:
                    fld = f
                    break
            wire = reg.wire(fld["type"]) if fld else {"kind": "prim", "size": 4}
    if which == "key":
        wire = wire.get("key") or {"kind": "prim", "size": 4}
    elif which == "val":
        wire = wire.get("val") or {"kind": "prim", "size": 4}
    elif wire.get("kind") == "list":
        wire = wire.get("elem") or {"kind": "prim", "size": 4}
    try:
        return BC._default_for(reg, wire)
    except Exception:  # noqa: BLE001
        return 0


# ===========================================================================
# ④ 动画 —— 高灵活度编辑（v1.8.67）
# ---------------------------------------------------------------------------
# 这一节补上"一键生成"之外的全部动作：**复制 / 移动 / 从零做 / 跨模型抄 /
# 存成文件复用 / 校验**。全部走元数据驱动那条路（任意行为类、任意字段），
# 不针对某个类写死。
# ===========================================================================

# ANIM_Behavior 上"要跟着一起复制"的属性（排除 fields/rna_type 这类不可写的）
_ANIM_CLONE_SKIP = {"rna_type", "fields", "name"}   # v1.8.87：open/show_fields 已删


def _anim_clone_props(src, dst):
    """把一个 ANIM_Behavior 的**全部标量属性**拷进另一个（CollectionProperty 不能整体复制）。"""
    for prop in ANIM_Behavior.bl_rna.properties:
        if prop.identifier in _ANIM_CLONE_SKIP:
            continue
        try:
            setattr(dst, prop.identifier, getattr(src, prop.identifier))
        except Exception:  # noqa: BLE001 - 只读/计算属性跳过
            pass


def _anim_clone(context, src_index, array=None):
    """复制第 src_index 条行为到末尾，返回新下标。返回 None 表示下标无效。

    ⛔ `rid` 必须**清空**：rid 是 Unity ManagedReferencesRegistry 的键，全局唯一。
       直接照抄会让两条行为抢同一个 rid ⇒ 绑定错乱/反序列化异常 ✗。
       清空后由 `hub_edit.dict_to_hub` 的分配器给一个未占用的新 rid ✓。
    """
    scene = context.scene
    if not (0 <= src_index < len(scene.bamod_anims)):
        return None
    src = scene.bamod_anims[src_index]
    dst = scene.bamod_anims.add()
    _anim_clone_props(src, dst)
    dst.rid = ""
    if array:
        dst.array = array
    _anim_rebuild_fields(context, len(scene.bamod_anims) - 1)
    return len(scene.bamod_anims) - 1


class BAMOD_OT_DupAnimation(bpy.types.Operator):
    bl_idname = "ba_mod.dup_animation"
    bl_label = "复制动画行为"
    bl_description = (
        "复制这一条动画行为（字段值、原始基底、字节兜底全带上）。\n"
        "可指定复制份数与目标数组 —— 例如把 game 里的旋翼自转复制一份到 preDeath。\n"
        "⛔ rid 会自动重新分配（照抄 rid 会让两条行为抢同一个引用 id）")
    bl_options = {"REGISTER", "UNDO"}

    index: bpy.props.IntProperty(default=0)
    count: bpy.props.IntProperty(name="份数", default=1, min=1, max=64)
    array: bpy.props.EnumProperty(name="目标数组", items=[
        # ⛔ 标识符不能是空串（Blender 不接受）—— 用 "keep" 表示"保持原数组"
        ("keep", "保持原数组", "复制到同一个数组"),
        ("universal", "universal", "通用（战场+军械库）"),
        ("demo", "demo", "军械库演示"),
        ("game", "game", "战场游戏"),
        ("preDeath", "preDeath", "死亡前"),
        ("death", "death", "死亡"),
    ], default="keep")

    def invoke(self, context, event):
        # 弹对话框填份数/目标数组（面板里点的是"复制这一条"）
        return context.window_manager.invoke_props_dialog(self, width=420)

    def execute(self, context):
        if not (0 <= self.index < len(context.scene.bamod_anims)):
            self.report({"ERROR"}, "行为已不存在（列表变了）")
            return {"CANCELLED"}
        last = None
        for _ in range(self.count):
            # "keep" = 保持原数组（不是空串 —— Blender 枚举不接受空标识符）
            last = _anim_clone(context, self.index,
                               self.array if self.array != "keep" else None)
        _anim_touch(context)
        b = context.scene.bamod_anims[last]
        self.report({"INFO"}, "已复制 %d 份 → [%s] %s（原第 %d 条）"
                    % (self.count, b.array, b.type, self.index + 1))
        return {"FINISHED"}


class BAMOD_OT_MoveAnimation(bpy.types.Operator):
    bl_idname = "ba_mod.move_animation"
    bl_label = "移动到其它数组"
    bl_description = (
        "换数组 = 换生效时机，**内容一点不改**：\n"
        "  universal 战场+军械库 / demo 只在军械库演示 / game 只在战场 /\n"
        "  preDeath 被打后 / death 死亡时。\n"
        "⛔ 最常见的坑：把旋翼自转放进 demo ⇒「预览里转、实战不转」")
    bl_options = {"REGISTER", "UNDO"}

    index: bpy.props.IntProperty(default=0)
    array: bpy.props.EnumProperty(name="目标数组", items=[
        ("universal", "universal", "通用（战场+军械库）"),
        ("demo", "demo", "军械库演示（战场不播）"),
        ("game", "game", "战场游戏"),
        ("preDeath", "preDeath", "死亡前（被打后）"),
        ("death", "death", "死亡时"),
    ], default="game")

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=420)

    def execute(self, context):
        if not (0 <= self.index < len(context.scene.bamod_anims)):
            self.report({"ERROR"}, "行为已不存在（列表变了）")
            return {"CANCELLED"}
        b = context.scene.bamod_anims[self.index]
        old = b.array
        b.array = self.array
        _anim_rebuild_panels(context)          # v1.8.87：标题里的数组名要跟着变
        self.report({"INFO"}, "%s：%s → %s" % (b.type, old, self.array))
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# 模板组：把「自己制作动画」变成"选模板 → 填节点名 → 生成"
# ---------------------------------------------------------------------------
def _anim_preset_items(self, context):
    """模板下拉（行为词典提供；走缓存，**必须返回同一个列表对象**）。

    ⛔ v1.8.71：以前这里每次都 `return _anim_bd().preset_items()`（新建列表 +
    新建字符串）⇒ 用户实测「**模板栏全是乱码**」✗。原因见 `_anim_cached_items`。
    顺带修掉旧兜底项的空标识符（Blender 不允许 `""` 作标识符）。
    """
    def build():
        try:
            return _anim_bd().preset_items()
        except Exception as e:  # noqa: BLE001
            print("[动画] 模板清单加载失败：%s" % e)
            return [("__unavailable__",
                     "（模板不可用：behavior_dict.py 未能导入）", "")]
    return _anim_cached_items("presets", build)


def _anim_wire_field(reg, wire, name):
    """在 wire（inline）里找名为 name 的字段的线格式。"""
    if not isinstance(wire, dict) or wire.get("kind") != "inline":
        return None
    for f in (wire.get("fields") or []):
        if f["name"] == name:
            return reg.wire(f["type"])
    return None


def _anim_slot_pptr(tv, slots):
    """模板里的节点占位符 → PPtr 值。"""
    nm = ""
    if isinstance(tv, str) and tv.startswith("@"):
        nm = (slots.get(tv[1:]) or "").strip()
    if nm and nm.isdigit():
        return {"__pptr__": True, "fileID": 0, "pathID": int(nm)}
    return {"__pptr__": True, "fileID": 0, "pathID": 0,
            **({"__name__": nm} if nm else {})}


def _anim_preset_values(reg, cls, tpl, slots):
    """模板 → 完整的字段值字典。

    基底用 `BC.make_defaults`（结构完整才写得出合法字节），再按模板覆盖。
    ⛔ 逐字段查 wire：节点字段（pptr）要转成 PPtr 值，不能把 `"@root"` 当字符串写进去。
    """
    import behavior_codec as BC
    vals = BC.make_defaults(reg, cls)
    root = {"kind": "inline", "fields": reg.ser_fields(cls)}

    def put(cur, wire, t):
        for k, tv in (t or {}).items():
            w = _anim_wire_field(reg, wire, k)
            if w is None:
                cur[k] = tv
                continue
            if w.get("kind") == "pptr":
                cur[k] = _anim_slot_pptr(tv, slots)
            elif w.get("kind") == "inline" and isinstance(tv, dict):
                if not isinstance(cur.get(k), dict):
                    cur[k] = {}
                put(cur[k], w, tv)
            else:
                cur[k] = tv

    put(vals, root, tpl)
    return vals


class BAMOD_OT_ApplyAnimPreset(bpy.types.Operator):
    bl_idname = "ba_mod.apply_anim_preset"
    bl_label = "应用动画模板"
    bl_description = (
        "从现成模板生成一组动画行为（16 组：旋翼 / 滞空 / 履带 / 炮塔 / 抛壳 / "
        "加力 / 拉烟 / 雷达 / 雨刷 / 天线 / 显隐 / 地形…）。\n"
        "生成出来的是**普通行为**，之后可以在下面逐条随便改 —— 模板只是起点，不是锁死的")
    bl_options = {"REGISTER", "UNDO"}

    preset: bpy.props.EnumProperty(name="模板", items=_anim_preset_items)
    mode: bpy.props.EnumProperty(name="方式", items=[
        ("append", "追加", "保留现有行为，把模板加在后面"),
        ("replace", "替换全部", "先清空现有行为再加（危险：原动画会没）"),
    ], default="append")
    # 节点槽位（模板里用 @名字 引用）
    # ⛔ v1.8.70：主旋翼/尾桨原来默认**空**，而 root/炮塔/炮口都有默认值 ⇒ 用户选
    #    「直升机·主旋翼+尾桨自转」模板时，两个关键槽位是空的，生成出来两条指向
    #    空引用的行为（还要自己去删）✗。`behavior_dict.NODE_SLOTS` 里本来就写着
    #    `Rotorangle_0` / `Rotorangle_1`（35 / 22 个 prefab 的实际命名），照它预填 ✓。
    s_root: bpy.props.StringProperty(name="根节点", default="root")
    s_main: bpy.props.StringProperty(name="主旋翼", default="Rotorangle_0")
    s_tail: bpy.props.StringProperty(name="尾桨", default="Rotorangle_1")
    s_target: bpy.props.StringProperty(name="目标节点", default="")
    s_turret: bpy.props.StringProperty(name="炮塔", default="turret_0")
    s_muzzle: bpy.props.StringProperty(name="炮口/抛壳口", default="shell_spawn_0")
    s_antenna: bpy.props.StringProperty(name="天线/雷达", default="")
    s_source: bpy.props.StringProperty(name="起点节点", default="")
    s_wingL: bpy.props.StringProperty(name="左舵面", default="")
    s_wingR: bpy.props.StringProperty(name="右舵面", default="")

    def invoke(self, context, event):
        # 用对话框：模板名 + 节点名槽位 + 追加/替换，一次填完再生成
        return context.window_manager.invoke_props_dialog(self, width=560)

    def _slots(self):
        return {k: getattr(self, "s_" + k) for k in
                ("root", "main", "tail", "target", "turret", "muzzle",
                 "antenna", "source", "wingL", "wingR")}

    def draw(self, context):
        layout = self.layout
        bd = _anim_bd()
        p = bd.PRESET_BY_KEY.get(self.preset)
        col = layout.column()
        col.prop(self, "preset", text="模板")
        if p:
            box = col.box()
            box.label(text=p["desc"], icon="INFO")
        col.prop(self, "mode")
        col.separator()
        col.label(text="节点名（填模型里的挂载点名；留空 = 该节点引用为空）", icon="EMPTY_ARROWS")
        grid = col.grid_flow(row_major=True, columns=2, even_columns=True, align=True)
        for k in ("root", "main", "tail", "target", "turret", "muzzle",
                  "antenna", "source", "wingL", "wingR"):
            grid.prop(self, "s_" + k)
        # 只画出该模板真正用到的槽位提示
        if p:
            used = set()
            for it in p["items"]:
                for _nm, v in _iter_ppt_vals(it.get("vals") or {}):
                    if isinstance(v, str) and v.startswith("@"):
                        used.add(v[1:])
            if used:
                col.label(text="本模板用到：%s" % ", ".join(sorted(used)), icon="CHECKMARK")

    def execute(self, context):
        import json as _json
        bd = _anim_bd()
        p = bd.PRESET_BY_KEY.get(self.preset)
        if p is None:
            self.report({"ERROR"}, "模板不存在：%s" % self.preset)
            return {"CANCELLED"}
        try:
            reg = _anim_reg()
        except Exception as e:  # noqa: BLE001
            self.report({"ERROR"}, "行为元数据加载失败：%s" % e)
            return {"CANCELLED"}
        anims = context.scene.bamod_anims
        if self.mode == "replace":
            anims.clear()
        slots = self._slots()
        made, missing = 0, set()
        for it in p["items"]:
            cls = it["type"]
            try:
                vals = _anim_preset_values(reg, cls, it.get("vals") or {}, slots)
            except Exception as e:  # noqa: BLE001
                self.report({"ERROR"}, "模板项 %s 生成失败：%s" % (cls, e))
                return {"CANCELLED"}
            # 目标节点用的是空槽位 ⇒ 记下来提醒（写出来会指向空引用）
            for nm, v in _iter_ppt_vals(vals):
                if not v.get("pathID") and not v.get("__name__"):
                    missing.add("%s.%s" % (cls, nm))
            b = anims.add()
            # ⛔ 顺序：先 raw_type 再 type（items 回调扫描集合里的 raw_type）
            b.raw_type = cls
            b.type = cls
            b.array = it.get("array") or (bd.arrays_for(cls) or ["game"])[0]
            b.rid = ""
            b.src_json = _json.dumps(
                {"type": cls, "_ns": "BrokenArrow.Client.Ecs.AnimationBehaviors",
                 "_asm": "BrokenArrow"}, ensure_ascii=False)
            b.values_json = _json.dumps(vals, ensure_ascii=False)
            # 便捷属性也跟着填，方便用老按钮继续调
            _anim_panel_from_vals(b, cls, vals)
            _anim_rebuild_fields(context, len(anims) - 1)
            made += 1
        _anim_touch(context)
        msg = "已应用模板「%s」：%d 条行为" % (p["name"], made)
        if missing:
            msg += "；⚠ 还有节点没填：%s" % ", ".join(sorted(missing))
            self.report({"WARNING"}, msg)
        else:
            self.report({"INFO"}, msg)
        return {"FINISHED"}


def _iter_ppt_vals(o, prefix=""):
    """遍历值树里的 PPtr（给模板做"槽位没填"检查用）。产出 (路径, pptr值)。"""
    if isinstance(o, dict):
        if o.get("__pptr__"):
            yield prefix, o
            return
        for k, v in o.items():
            yield from _iter_ppt_vals(v, ("%s.%s" % (prefix, k)) if prefix else k)
    elif isinstance(o, list):
        for i, v in enumerate(o):
            yield from _iter_ppt_vals(v, "%s[%d]" % (prefix, i))


def _anim_panel_from_vals(b, cls, vals):
    """把字段值回填到 5 个"便捷属性"上（面板顶部那套输入框仍可用）。"""
    def _nm(v):
        return (v or {}).get("__name__", "") if isinstance(v, dict) else ""

    def _num(v, d=0.0):
        try:
            return float(v)
        except (TypeError, ValueError):
            return d

    if cls == "Torque":
        b.target = _nm(vals.get("_target"))
        d = list(vals.get("_direction") or [0, -1, 0]) + [0, 0, 0]
        b.dir_x, b.dir_y, b.dir_z = _num(d[0]), _num(d[1]), _num(d[2])
        b.speed = _num(vals.get("_speed"), 1080.0)
        b.lod = int(vals.get("_lodGroup", 0) or 0)
    elif cls in ("FloatEffect", "WaterFloatEffect"):
        b.target = _nm(vals.get("_rootBone"))
        names = [n for n in ("_verticalSpeed", "_verticalAmplitude", "_rotationSpeed",
                             "_rotationAmplitude", "_horizontalSpeed",
                             "_horizontalAmplitude", "_forwardBackwardSpeed",
                             "_forwardBackwardAmplitude") if n in vals]
        b.values_str = ", ".join("%g" % _num(vals[n]) for n in names)
        b.lod = int(vals.get("_lodGroup", 0) or 0)
    elif cls == "AxisRandom":
        b.speed = _num(vals.get("_speed"), 20.0)
        b.min_time = _num(vals.get("_minimalTime"), 2.0)
        b.max_time = _num(vals.get("_maximalTime"), 10.0)
        for ax in ("x", "y", "z"):
            a = vals.get("_" + ax) or {}
            setattr(b, ax + "_source", _nm(a.get("Source")))
            setattr(b, ax + "_min", _num(a.get("MinimalAngle")))
            setattr(b, ax + "_max", _num(a.get("MaximalAngle")))
    elif cls == "MathConnect":
        b.root = _nm(vals.get("_root"))
        b.target = _nm(vals.get("_target"))
        s = vals.get("_settings") or {}
        b.freq = _num(s.get("_frequency"))
        b.damper = _num(s.get("_damper"))
        b.reaction = _num(s.get("_reaction"))
        b.freeze_x = bool(vals.get("_freezeXPos"))
        b.freeze_y = bool(vals.get("_freezeYPos"))
        b.freeze_z = bool(vals.get("_freezeZPos"))


# ---------------------------------------------------------------------------
# 行为集导出 / 导入（跨模型复用：做一个模型上的动画，搬到别的模型）
# ---------------------------------------------------------------------------
ANIM_SET_FORMAT = "ba_anim_set"
ANIM_SET_VERSION = 1


def _anim_export_items(context):
    """面板集合 → **与模型无关**的行为集（pid 换成挂载点名）。

    ⛔ 导出必须是"可搬到别的模型"的：pid 是单个 prefab 内部的编号，换个模型就废 ✗。
       所以值树里的 PPtr 统一剥成 `__name__`（挂载点名），导入时再按新模型解析。
    """
    import json as _json
    out = []
    for b in context.scene.bamod_anims:
        vals = _anims_to_dict_values(b)
        _anim_annotate_names(context, vals)

        def _strip(o):
            if isinstance(o, dict):
                if o.get("__pptr__"):
                    return {"__pptr__": True, "fileID": 0, "pathID": 0,
                            **({"__name__": o["__name__"]} if o.get("__name__") else {})}
                return {k: _strip(v) for k, v in o.items()}
            if isinstance(o, list):
                return [_strip(v) for v in o]
            return o

        item = {"array": b.array, "type": b.raw_type or b.type, "raw_only": bool(b.raw_only),
                "vals": _strip(vals) if not b.raw_only else None}
        try:
            base = _json.loads(b.src_json or "{}")
            if isinstance(base, dict):
                item["ns"] = base.get("_ns", "")
                item["asm"] = base.get("_asm", "")
        except Exception:  # noqa: BLE001
            pass
        raw = _anim_behavior_bytes(b)
        if b.raw_only and raw:
            import base64 as _b64
            item["rawB64"] = _b64.b64encode(raw).decode("ascii")
        out.append(item)
    return {"format": ANIM_SET_FORMAT, "version": ANIM_SET_VERSION,
            "behaviors": out}


def _anim_import_items(context, data, mode="append"):
    """行为集 → 面板集合。返回 (成功条数, 跳过条数, 缺节点名集合)。

    节点引用按**名字**在当前模型的节点表里查 pid；查不到就写 0 并保留 `__name__`
    （面板会显示名字，用户可以用吸管重新指）。
    """
    import json as _json
    if not isinstance(data, dict) or data.get("format") != ANIM_SET_FORMAT:
        raise ValueError("不是 ba_anim_set 文件（format=%r）" % (data or {}).get("format"))
    items = data.get("behaviors") or []
    if mode == "replace":
        context.scene.bamod_anims.clear()
    try:
        import behavior_codec as BC
        reg = _anim_reg()
    except Exception as e:  # noqa: BLE001
        raise ValueError("行为元数据加载失败：%s" % e)
    ok, skipped, missing = 0, 0, set()
    for it in items:
        cls = it.get("type") or "AxisRandom"
        b = context.scene.bamod_anims.add()
        b.raw_type = cls          # ⛔ 先 raw_type 再 type
        b.type = cls
        b.array = it.get("array") or "game"
        b.rid = ""
        b.src_json = _json.dumps(
            {"type": cls, "_ns": it.get("ns") or "BrokenArrow.Client.Ecs.AnimationBehaviors",
             "_asm": it.get("asm") or "BrokenArrow"}, ensure_ascii=False)
        if it.get("rawB64"):
            import base64 as _b64
            try:
                vals = BC.parse(reg, cls, _b64.b64decode(it["rawB64"]))
                b.values_json = _json.dumps(vals, ensure_ascii=False)
            except Exception:  # noqa: BLE001
                skipped += 1
                b.values_json = "{}"
                b.raw_only = True
        else:
            vals = it.get("vals") or {}
            for nm, v in _iter_ppt_vals(vals):
                name = v.get("__name__")
                if not name:
                    continue
                pid = _anim_name_pid(context, name)
                if pid is None:
                    missing.add(name)
                    v["pathID"] = 0
                else:
                    v["pathID"] = int(pid)
            b.values_json = _json.dumps(vals, ensure_ascii=False)
            _anim_panel_from_vals(b, cls, vals)
        _anim_rebuild_fields(context, len(context.scene.bamod_anims) - 1)
        ok += 1
    return ok, skipped, missing


class BAMOD_OT_ExportAnim(bpy.types.Operator):
    bl_idname = "ba_mod.export_anim"
    bl_label = "导出行为集（.baanim）"
    bl_description = (
        "把当前全部动画行为存成一个 JSON 文件，可以：\n"
        "  · 备份（改坏了随时读回来）\n"
        "  · 搬到别的模型上复用（节点按**名字**匹配，不依赖 pid）\n"
        "  · 手工编辑后再读回（字段名就是游戏里的字段名）")
    bl_options = {"REGISTER", "UNDO"}

    filepath: bpy.props.StringProperty(subtype="FILE_PATH", default="")
    filter_glob: bpy.props.StringProperty(default="*.baanim;*.json", options={"HIDDEN"})

    def invoke(self, context, event):
        import os
        if not self.filepath:
            self.filepath = os.path.join(_default_export_dir(), "anim_set.baanim")
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        import json as _json
        import os
        p = bpy.path.abspath(self.filepath)
        if not p.lower().endswith((".baanim", ".json")):
            p += ".baanim"
        data = _anim_export_items(context)
        try:
            with open(p, "w", encoding="utf-8") as f:
                _json.dump(data, f, ensure_ascii=False, indent=1)
        except Exception as e:  # noqa: BLE001
            self.report({"ERROR"}, "写出失败：%s" % e)
            return {"CANCELLED"}
        self.report({"INFO"}, "已导出 %d 条行为 → %s"
                    % (len(data["behaviors"]), os.path.basename(p)))
        return {"FINISHED"}


class BAMOD_OT_ImportAnim(bpy.types.Operator):
    bl_idname = "ba_mod.import_anim"
    bl_label = "读入行为集（.baanim）"
    bl_description = "从 .baanim 文件读回动画行为；节点引用按**名字**匹配当前模型"

    bl_options = {"REGISTER", "UNDO"}
    filepath: bpy.props.StringProperty(subtype="FILE_PATH", default="")
    filter_glob: bpy.props.StringProperty(default="*.baanim;*.json", options={"HIDDEN"})
    mode: bpy.props.EnumProperty(name="方式", items=[
        ("append", "追加", "保留现有行为"), ("replace", "替换全部", "清空后读入")],
        default="append")

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        import json as _json
        p = bpy.path.abspath(self.filepath)
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = _json.load(f)
            ok, skipped, missing = _anim_import_items(context, data, self.mode)
        except Exception as e:  # noqa: BLE001
            self.report({"ERROR"}, "读入失败：%s" % e)
            return {"CANCELLED"}
        _anim_touch(context)
        msg = "已读入 %d 条行为" % ok
        if skipped:
            msg += "；%d 条无法解析（已保留原始字节）" % skipped
        if missing:
            msg += "；⚠ 当前模型没有这些节点：%s" % ", ".join(sorted(missing))
            self.report({"WARNING"}, msg)
        else:
            self.report({"INFO"}, msg)
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# 从**任意 prefab** 抄行为（不用把它设成复制源）
# ---------------------------------------------------------------------------
def _anim_prefab_hub(prefs, pid, prefab_path=None):
    """读某个 prefab 的 AnimationHub：`(hub_raw, name_of, pid2name)`；没有则 (None,None,None)。"""
    unitypy_bridge.add_paths(prefs.rev_dir, prefs.unitypy_dir)
    import importlib, base64, struct
    import UnityPy
    import copy_full, hub_edit
    importlib.reload(copy_full)
    importlib.reload(hub_edit)
    env = UnityPy.load(prefs.bundle)
    objs = list(list(env.objects)[0].assets_file.objects.values())
    from analyze_common import Hierarchy
    h = Hierarchy(objs, {o.path_id: o for o in objs})
    src_objects, _, _ = copy_full.collect_prefab_objects(prefs.bundle, int(pid),
                                                        want_path=prefab_path)
    hub_raw = None
    for o in src_objects:
        if o["type_name"] == "MonoBehaviour":
            raw = base64.b64decode(o["raw"])
            if len(raw) >= 28 and struct.unpack_from("<q", raw, 20)[0] == hub_edit.HUB_SCRIPT:
                hub_raw = raw
                break
    if hub_raw is None:
        return None, None, None
    pid2name = {}
    for trpid, gpid in h.tr_go.items():
        nm = h.name(gpid)
        if nm:
            pid2name.setdefault(int(trpid), nm)
    return (hub_raw,
            (lambda p: pid2name.get(int(p), "") if p else ""),
            pid2name)


class BAMOD_OT_SetCopySource(bpy.types.Operator):
    bl_idname = "ba_mod.set_copy_source"
    bl_label = "设为当前模型"
    bl_description = (
        "把 ① 面板里选中的那个 prefab 设为**当前模型（复制源）**。\n"
        "之后「读取当前模型的动画」和「构建写回」都以它为目标。\n\n"
        "什么时候用：为了搬旋翼先导入了捐赠机，结果**复制源**变成了捐赠机 ——\n"
        "用它把目标切回来，**不必重做 Blender 场景**。\n\n"
        "⛔ 场景里的挂载点树必须属于这个模型：若场景里还留着别的模型的挂载点树，\n"
        "   请先清掉（构建会把所有 ba_mount 空物体都当成挂载点树、可能挑错根）。\n"
        "⛔ 已加好的动画行为**保留不动**；节点引用按名字在新模型里重新解析。")
    bl_options = {"REGISTER", "UNDO"}

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=560)

    def draw(self, context):
        col = self.layout.column()
        col.label(text="现在的当前模型：%s" % (_copy_source_name(context) or "（未导入）"))
        col.label(text="要改成：%s" % (_selected_prefab_name(context) or "（① 里没选中）"),
                  icon="FORWARD")
        col.separator()
        col.label(text="改完之后：")
        col.label(text="  · 「读取当前模型的动画」读的就是它")
        col.label(text="  · 「构建写回」也写进它")
        col.label(text="  · 动画行为保留；节点引用按名字重新解析")
        col.label(text="⚠ 场景里的挂载点树要属于这个模型（别的模型的树先清掉）",
                  icon="ERROR")

    def execute(self, context):
        lst = context.scene.bamod_prefabs
        i = context.scene.bamod_prefab_index
        if not (0 <= i < len(lst)):
            self.report({"ERROR"}, "先在 ① 面板里选中一个模型")
            return {"CANCELLED"}
        it = lst[i]
        context.scene["ba_copy_source_pid"] = str(it.pid)
        context.scene["ba_copy_source_path"] = it.path
        # 节点表 / 顺序表都是跟着**旧模型**建的 ⇒ 清掉，让「读取当前模型的动画」重建
        for k in ("bamod_anim_node_pids", "bamod_anim_node_names",
                  "bamod_anim_local_names", "bamod_anim_types", "bamod_anim_order"):
            if k in context.scene:
                del context.scene[k]
        _txt = bpy.data.texts.get("bamod_anim")
        if _txt is not None:
            _txt.clear()
        _anim_touch(context)
        self.report({"INFO"}, "当前模型已设为 %s —— 接着点「读取当前模型的动画」重建节点表"
                    % (it.name or _short_name(it.path)))
        return {"FINISHED"}


class BAMOD_OT_ImportAnimFromPrefab(bpy.types.Operator):
    bl_idname = "ba_mod.import_anim_from_prefab"
    bl_label = "读取所选模型的动画"
    bl_description = (
        "**① 面板选中哪个模型，就读哪个模型的动画** —— 不需要它是『复制源』。\n"
        "节点引用按**名字**转换到当前模型（名字对不上的会在提示里列出来）。\n"
        "用法：① 面板刷新 prefab 列表 → 选中一个模型 → 回这里点一下；\n"
        "      想拿它当基底就选「替换全部」，只是想补几条就选「追加」。\n"
        "⛔ 点这里**不会**去①里点「导入所选模型」，所以不会清空场景与面板")
    bl_options = {"REGISTER", "UNDO"}

    mode: bpy.props.EnumProperty(name="方式", items=[
        ("append", "追加", "保留现有行为，把抄来的加在后面"),
        ("replace", "替换全部", "先清空再加"),
    ], default="append")
    types_filter: bpy.props.StringProperty(
        name="只要这些类", default="",
        description="逗号分隔的行为类名，留空 = 全部抄。例：Torque,FloatEffect")

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=520)

    def execute(self, context):
        prefs = _prefs(context)
        lst = context.scene.bamod_prefabs
        idx = context.scene.bamod_prefab_index
        if idx < 0 or idx >= len(lst):
            self.report({"ERROR"}, "先在 ① 面板刷新并选一个 prefab")
            return {"CANCELLED"}
        it = lst[idx]
        try:
            hub_raw, name_of, pid2name = _anim_prefab_hub(prefs, int(it.pid), it.path)
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            self.report({"ERROR"}, "读取失败：%s" % e)
            return {"CANCELLED"}
        if hub_raw is None:
            self.report({"ERROR"}, "%s 没有 AnimationHub（这个 prefab 本来就没有动画）"
                        % it.name)
            return {"CANCELLED"}
        import hub_edit
        d = hub_edit.hub_to_dict(hub_raw, name_of)
        want = {t.strip() for t in (self.types_filter or "").replace("，", ",").split(",")
                if t.strip()}
        if want:
            for k in list(d):
                if k.startswith("_"):
                    continue
                d[k] = [x for x in d[k] if x.get("type") in want]
            d["_order"] = [r for r in d.get("_order", [])
                           if any(x.get("_rid") == r for k, v in d.items()
                                  if not k.startswith("_") for x in v)]
        if self.mode == "replace":
            context.scene.bamod_anims.clear()
        before = len(context.scene.bamod_anims)
        # ⛔ 跨 prefab 抄行为时**必须重映射节点引用**：`hub_to_dict` 里的
        #    `target` / `root` 已经是**名字**（它自己用了源 prefab 的 name_of），
        #    但 `values_json` 里的 PPtr 仍是**源 prefab 的 pid** —— 在目标 prefab 里
        #    那个 pid 要么不存在、要么是别的节点 ⇒ 行为指错 ✗。
        #    `name_map` 就是"源 pid → 节点名"，由 `_fill_anims_from_dict` 用它换成
        #    当前模型的 pid（按名字匹配）。
        try:
            _fill_anims_from_dict(context, d, append=True, name_map=pid2name)
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            self.report({"ERROR"}, "填表失败：%s" % e)
            return {"CANCELLED"}
        n = len(context.scene.bamod_anims) - before
        _anim_touch(context)
        missing = set()
        for b in context.scene.bamod_anims:
            missing |= _anim_missing_nodes(context, _anims_to_dict_values(b))
        msg = "已从 %s 抄入 %d 条行为" % (it.name, n)
        if missing:
            msg += "；⚠ 当前模型没有这些节点：%s" % ", ".join(sorted(missing))
            self.report({"WARNING"}, msg)
        else:
            self.report({"INFO"}, msg)
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# 校验 / 文本块双向同步 / 清空
# ---------------------------------------------------------------------------
class BAMOD_OT_ValidateAnim(bpy.types.Operator):
    bl_idname = "ba_mod.validate_anim"
    bl_label = "校验动画（面板 → 字节 → 回读）"
    bl_description = (
        "把当前面板的每个行为编码成字节、再解析回字段值，逐条比对：\n"
        "  · 结构能不能写出来（写不出 = 构建时一定会坏）\n"
        "  · 写回再读回是否一致（不一致 = 有个字段会被静默丢掉）\n"
        "  · 节点引用能不能解析（解析不出 = 进游戏是空引用，行为不生效）\n"
        "不影响任何数据，可以随时点")

    def execute(self, context):
        try:
            import behavior_codec as BC
            reg = _anim_reg()
        except Exception as e:  # noqa: BLE001
            self.report({"ERROR"}, "元数据加载失败：%s" % e)
            return {"CANCELLED"}
        n2p, _ = _anim_node_maps(context)
        local_names = _anim_local_names(context)
        anims = context.scene.bamod_anims
        bad, warn, node_warn = [], [], []
        for i, b in enumerate(anims):
            cls = b.raw_type or b.type
            tag = "#%d [%s] %s" % (i + 1, b.array, cls)
            if b.raw_only:
                raw = _anim_behavior_bytes(b)
                if not raw:
                    bad.append("%s：标了【仅原始字节】却拿不到原始字节" % tag)
                continue
            vals = _anims_to_dict_values(b)
            if not vals:
                bad.append("%s：字段值为空（构建时写不出内容）" % tag)
                continue
            try:
                raw = BC.emit(reg, cls, vals)
            except Exception as e:  # noqa: BLE001
                bad.append("%s：写不出字节（%s）" % (tag, e))
                continue
            if not raw:
                bad.append("%s：写出来是 0 字节" % tag)
                continue
            try:
                back = BC.parse(reg, cls, raw)
            except Exception as e:  # noqa: BLE001
                warn.append("%s：写出的字节解析不回来（%d 字节，%s）" % (tag, len(raw), e))
                continue
            # ⛔ 判据用**再编码后的字节**，不能用 JSON 字符串比对：
            #    默认值里 Vector3 是 `[0, 0, 0]`（整数），解析回来是 `[0.0, 0.0, 0.0]`
            #    ⇒ json.dumps 文本不同，会误报"字段会丢"（实测踩到）。
            #    真正的判据是「写出的字节 → 读回 → 再写出的字节」必须一模一样。
            try:
                if BC.emit(reg, cls, back) != raw:
                    warn.append("%s：写出的字节解析后再写回**不一致**（可能丢字段）" % tag)
            except Exception as e:  # noqa: BLE001
                warn.append("%s：解析结果写不回去（%s）" % (tag, e))
            for nm, v in _iter_ppt_vals(vals):
                name = v.get("__name__")
                if not name:
                    if not v.get("pathID"):
                        node_warn.append("%s：%s 指向**空引用**" % (tag, nm))
                    continue
                if name not in local_names:
                    node_warn.append("%s：节点 %r 不在**本模型**里（引用会失效）"
                                     % (tag, name))
        print("=" * 60)
        print("[动画校验] %d 条行为：错误 %d / 警告 %d / 节点问题 %d"
              % (len(anims), len(bad), len(warn), len(node_warn)))
        for x in bad:
            print("  ✗", x)
        for x in warn:
            print("  ⚠", x)
        for x in node_warn:
            print("  ⚠", x)
        print("=" * 60)
        if bad:
            self.report({"ERROR"}, "校验失败 %d 项（详见系统控制台）" % len(bad))
        elif warn or node_warn:
            self.report({"WARNING"}, "校验通过但有 %d 条提醒（详见控制台）" % (len(warn) + len(node_warn)))
        else:
            self.report({"INFO"}, "校验通过：%d 条行为全部正常" % len(anims))
        return {"FINISHED"}


class BAMOD_OT_SyncAnimText(bpy.types.Operator):
    bl_idname = "ba_mod.sync_anim_text"
    bl_label = "文本块 JSON ↔ 面板"
    bl_description = (
        "把动画 JSON 写进/读出文本块 bamod_anim。\n"
        "**文本块 → 面板**：想批量改（查找替换、改坐标、复制粘贴一堆行为）时用 —— "
        "在文本编辑器里改完 JSON，点这里读回面板，再构建。")
    bl_options = {"REGISTER", "UNDO"}

    direction: bpy.props.EnumProperty(name="方向", items=[
        ("to_text", "面板 → 文本块", "把面板现状写成 JSON（只读快照）"),
        ("from_text", "文本块 → 面板", "按文本块里的 JSON 重建面板（会覆盖面板）"),
    ], default="to_text")

    def execute(self, context):
        import json as _json
        txt = bpy.data.texts.get("bamod_anim")
        if self.direction == "to_text":
            if txt is None:
                txt = bpy.data.texts.new("bamod_anim")
            txt.clear()
            txt.write(_json.dumps(_anim_export_items(context), ensure_ascii=False, indent=1))
            self.report({"INFO"}, "已写出文本块 bamod_anim（%d 条行为）"
                        % len(context.scene.bamod_anims))
            return {"FINISHED"}
        if txt is None:
            self.report({"ERROR"}, "没有文本块 bamod_anim（先点「面板 → 文本块」或用读取动画生成）")
            return {"CANCELLED"}
        s = txt.as_string()
        try:
            data = _json.loads(s)
        except Exception as e:  # noqa: BLE001
            self.report({"ERROR"}, "文本块里的 JSON 解析失败：%s" % e)
            return {"CANCELLED"}
        # 兼容两种形态：行为集（本插件导出）与 hub 原样 dict（{universal:[...],…}）
        try:
            if isinstance(data, dict) and data.get("format") == ANIM_SET_FORMAT:
                ok, skipped, missing = _anim_import_items(context, data, "replace")
            elif isinstance(data, dict):
                context.scene.bamod_anims.clear()
                _fill_anims_from_dict(context, data)
                ok, skipped, missing = len(context.scene.bamod_anims), 0, set()
            else:
                raise ValueError("顶层不是对象")
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            self.report({"ERROR"}, "读入失败：%s" % e)
            return {"CANCELLED"}
        _anim_touch(context)
        msg = "文本块 → 面板：%d 条行为" % ok
        if skipped:
            msg += "；%d 条无法解析（保留原始字节）" % skipped
        if missing:
            msg += "；⚠ 缺少节点：%s" % ", ".join(sorted(missing))
        self.report({"INFO"}, msg)
        return {"FINISHED"}


class BAMOD_OT_ClearAnims(bpy.types.Operator):
    bl_idname = "ba_mod.clear_anims"
    bl_label = "清空全部行为"
    bl_description = "清空 ④ 面板里的全部动画行为（**构建时会把 AnimationHub 写空** —— 先导出备份）"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        n = len(context.scene.bamod_anims)
        context.scene.bamod_anims.clear()
        _anim_drop_panels()                    # v1.8.87：真子面板一并撤掉
        self.report({"INFO"}, "已清空 %d 条行为" % n)
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# ④ 动画：**每条行为一个真正的子面板**（v1.8.87）
# ---------------------------------------------------------------------------
# ⛔⛔ 以前是**假折叠栏**：在同一面板里用 `b.open` 布尔 + `TRIA_DOWN/TRIA_RIGHT` 图标
#     自己切换显示 ✗ —— 用户实测反馈"每个动画下面的折叠栏都是假的"。
#     现在改成 Blender 的**真子面板**：动态生成面板类（`bl_parent_id = "BAMOD_PT_Animation"`、
#     `bl_options = {"DEFAULT_CLOSED"}`）⇒ 有原生三角箭头、能记住展开状态、默认折叠 ✓
# ⛔ 动态面板的 `bl_parent_id` 必须等于父面板的 **bl_idname**（不是类名）✓
# ⛔ 注册/注销**绝不能在 draw 里做**（绘制期间改面板注册会崩）⇒ 只在“行为列表变了”的
#    算子执行时重建一次（见 `_anim_rebuild_panels`）✓
_DYN_ANIM_CLASSES = []
_DYN_ANIM_SIG = None


def _anim_item_summary(b):
    """行为标题里那句摘要（Torque 转速 / MathConnect 两端 / 其他类取词典说明）。"""
    if b.type == "AxisRandom":
        axes = [ax for ax in ("x", "y", "z") if getattr(b, ax + "_source")]
        return " / ".join("%s:%s %s~%s" % (ax.upper(), getattr(b, ax + "_source"),
                                           getattr(b, ax + "_min"), getattr(b, ax + "_max"))
                          for ax in axes) or "（无轴向源）"
    if b.type == "Torque":
        return "%s ← (%g,%g,%g) %g°/s" % (b.target or "?", b.dir_x, b.dir_y, b.dir_z, b.speed)
    if b.type in ("FloatEffect", "WaterFloatEffect"):
        return b.target or "（自身）"
    if b.type == "MathConnect":
        return "%s → %s" % (b.root or "?", b.target or "?")
    try:
        _i = _anim_bd().info(b.type)
    except Exception:  # noqa: BLE001
        _i = {}
    s = _i.get("desc") or "（字节原样保留）"
    return ("⚠ " + s) if _i.get("danger") else s


def _anim_item_label(i, b):
    """子面板标题：序号 · [数组] 类型（中文名） · 摘要。**要短**（面板头很窄）。"""
    cn = ""
    try:
        _c = _anim_bd().cn_name(b.type)
        cn = "" if _c == b.type else " %s" % _c
    except Exception:  # noqa: BLE001
        pass
    s = _anim_item_summary(b)
    if len(s) > 34:
        s = s[:33] + "…"
    return "%d · [%s]%s (%s) · %s" % (i + 1, b.array, cn, b.type, s)


def _draw_anim_item_body(layout, context, i, b):
    """一个行为的**便捷属性**部分（通用字段表在它自己的子栏里）。"""
    prefs = _prefs(context)
    col = layout.column(align=True)
    r = col.row()
    r.prop(b, "array", text="数组")
    r.prop(b, "type", text="类型")
    if b.type in ("Torque", "FloatEffect", "WaterFloatEffect", "AxisRandom", "MathConnect"):
        r = col.row()
        r.prop(b, "conv", text="顶部输入框接管")
        if not b.conv:
            r.label(text="（下方通用字段为准）", icon="INFO")
    if b.rid:
        col.prop(b, "rid")
    mount_names = set()
    for o in bpy.data.objects:
        if o.type == "EMPTY" and o.get("ba_mount"):
            mount_names.add(_normalize_name(o.name))
    ce = col.column(align=True)
    ce.enabled = bool(b.conv)
    if b.type == "AxisRandom":
        ce.prop(b, "speed")
        r = ce.row(align=True)
        r.prop(b, "min_time")
        r.prop(b, "max_time")
        ce.prop(b, "lod")
        for ax in ("x", "y", "z"):
            r = ce.row(align=True)
            r.prop(b, ax + "_source", text=ax.upper())
            r.prop(b, ax + "_min", text="最小")
            r.prop(b, ax + "_max", text="最大")
    elif b.type == "Torque":
        r = ce.row(align=True)
        r.prop(b, "target")
        op = r.operator("ba_mod.pick_anim_node", text="", icon="EYEDROPPER")
        op.index = i
        if b.target and b.target not in mount_names:
            col.label(text="⚠ 场景里没有节点 %s（挂接会失败）" % b.target, icon="ERROR")
        # ★⑬ 这里才是真正"选自转轴 / 选挂点"的地方（本次旋翼踩坑就在这儿）⇒ 显示游戏坐标 ✓
        tgt_obj = next((o for o in bpy.data.objects if _normalize_name(o.name) == b.target), None)
        if tgt_obj is not None:
            _draw_game_coord(col, context, box=col.box())
        r = ce.row(align=True)
        r.prop(b, "dir_x")
        r.prop(b, "dir_y")
        r.prop(b, "dir_z")
        r = ce.row(align=True)
        r.prop(b, "speed")
        r.prop(b, "lod")
        ce.label(text="美系主旋翼 (0,-1,0)、俄系 (0,1,0)；尾桨 (-1,0,0)", icon="INFO")
    elif b.type in ("FloatEffect", "WaterFloatEffect"):
        r = ce.row(align=True)
        r.prop(b, "target")
        op = r.operator("ba_mod.pick_anim_node", text="", icon="EYEDROPPER")
        op.index = i
        ce.prop(b, "lod")
        ce.prop(b, "values_str")
    elif b.type == "MathConnect":
        r = ce.row()
        r.prop(b, "root")
        r.prop(b, "target")
        r = ce.row()
        r.prop(b, "freq")
        r.prop(b, "damper")
        r.prop(b, "reaction")
        r = ce.row()
        r.prop(b, "freeze_x")
        r.prop(b, "freeze_y")
        r.prop(b, "freeze_z")
        if b.shots_b64:
            ce.label(text="shots: %d 字节（raw b64 透传）" % (len(b.shots_b64) * 3 // 4))
    else:
        col.label(text="（展开下面的「通用字段」子栏可编辑全部字段）", icon="INFO")


def _draw_anim_item_fields(layout, context, i, b, src="anim"):
    """一个对象（行为 / 组件）的**通用字段表**（元数据驱动，任何类任何字段都在这里）。"""
    if b.raw_only:
        layout.label(text="⚠ 该实例布局与类定义不一致：只按原始字节保留", icon="ERROR")
        return
    col = layout.column(align=True)
    if not len(b.fields):
        col.label(text="（没有字段：这条行为只有原始字节）", icon="INFO")
    for fi, f in enumerate(b.fields):
        row = col.row(align=True)
        ind = row.row(align=True)
        ind.separator(factor=min(f.depth, 6) * 0.30)
        lbl = f.label.split("  ·  ")[0] if f.label else f.name
        # ⛔ 数组/字典元素的增删复制按钮必须画在**所有 continue 之前**：
        #    元素是内联类（group）时以前会先 continue ⇒ 结构体数组**只能加不能删** ✗
        del_ops = None
        if f.list_index >= 0:
            del_ops = row.row(align=True)
        if f.kind == "group":
            ind.label(text=("▸ " + lbl) if del_ops is None else ("· %s" % lbl),
                      icon="OUTLINER_DATA_EMPTY")
        elif f.kind in ("list", "dict"):
            ind.label(text="▾ %s  [%d]" % (lbl, f.fcount),
                      icon="PRESET" if f.kind == "dict" else "LINENUMBERS_ON")
            w = row.row(align=True)
            op = w.operator("ba_mod.anim_list_edit", text="", icon="ADD")
            op.beh, op.field, op.mode = i, fi, "add"
            op.src = src
        elif f.kind == "unsupported":
            ind.label(text="%s（不支持）" % lbl, icon="LOCKED")
        elif f.kind == "keep":
            # 有内容、但面板**不重写它**（例如 AnimationCurve 的关键帧）：
            # 字节会原样写回去 ⇒ 既不是"不支持"、也不是"能改"，照实说 ✓
            # （v1.8.91：draw 冒烟测试发现曲线行落到 unsupported 分支后显示成"不支持"，误导）
            ind.label(text="%s（%d 项，原样保留）" % (lbl, f.fcount), icon="LOCKED")
        else:
            ind.label(text=lbl)
            w = row.row(align=True)
            if f.kind == "bool":
                w.prop(f, "b", text="")
            elif f.kind == "int":
                w.prop(f, "i", text="")
            elif f.kind == "enum":
                w.prop(f, "e", text="")
            elif f.kind == "float":
                w.prop(f, "f", text="")
            elif f.kind == "string":
                w.prop(f, "s", text="")
            elif f.kind == "node":
                w.prop(f, "s", text="")
                op = w.operator("ba_mod.anim_pick_node", text="", icon="EYEDROPPER")
                op.beh, op.field = i, fi
                op.src = src
            elif f.kind == "vec":
                if f.felem == "int":
                    w.prop(f, "i", text="x")
                    w.prop(f, "i2", text="y")
                    if f.fcount > 2:
                        w.prop(f, "i3", text="z")
                else:
                    w.prop(f, "f", text="x")
                    w.prop(f, "f2", text="y")
                    if f.fcount > 2:
                        w.prop(f, "f3", text="z")
        if del_ops is not None:
            op = del_ops.operator("ba_mod.anim_list_edit", text="", icon="DUPLICATE")
            op.beh, op.field = i, fi
            op.mode, op.index = "dup", f.list_index
            op.src = src
            op = del_ops.operator("ba_mod.anim_list_edit", text="", icon="X")
            op.beh, op.field = i, fi
            op.mode, op.index = "del", f.list_index
            op.src = src


def _make_anim_item_panel(i, label):
    """造一条行为的真子面板类（含一个嵌套的「通用字段」子栏）。"""
    idn = "BAMOD_PT_ANIMITEM_%d" % i

    def draw(self, context):
        items = context.scene.bamod_anims
        if i >= len(items):
            self.layout.label(text="（这条行为已被删除）", icon="INFO")
            return
        _draw_anim_item_body(self.layout, context, i, items[i])

    def draw_header(self, context):
        # 复制 / 换数组 / 删除 摆到面板头上（和以前一样是图标按钮，但现在是真面板头 ✓）
        row = self.layout.row(align=True)
        op = row.operator("ba_mod.dup_animation", text="", icon="DUPLICATE")
        op.index = i
        op = row.operator("ba_mod.move_animation", text="", icon="ARROW_LEFTRIGHT")
        op.index = i
        op = row.operator("ba_mod.remove_animation", text="", icon="X")
        op.index = i

    fields_idn = idn + "_FIELDS"

    def draw_fields(self, context):
        items = context.scene.bamod_anims
        if i >= len(items):
            return
        _draw_anim_item_fields(self.layout, context, i, items[i])

    item_cls = type(idn, (bpy.types.Panel,), {
        "bl_idname": idn,
        "bl_label": label,
        "bl_space_type": "VIEW_3D",
        "bl_region_type": "UI",
        "bl_category": "BA Mod",
        "bl_parent_id": "BAMOD_PT_Animation",
        "bl_options": {"DEFAULT_CLOSED"},
        "bl_order": i,
        "bamod_index": i,
        "draw": draw,
        "draw_header": draw_header,
    })
    fields_cls = type(idn + "_FIELDS", (bpy.types.Panel,), {
        "bl_idname": fields_idn,
        "bl_label": "通用字段（全部字段都在这里）",
        "bl_space_type": "VIEW_3D",
        "bl_region_type": "UI",
        "bl_category": "BA Mod",
        "bl_parent_id": idn,
        "bl_options": {"DEFAULT_CLOSED"},
        "bl_order": 0,
        "bamod_index": i,
        "draw": draw_fields,
    })
    return item_cls, fields_cls


def _anim_rebuild_panels(context):
    """按当前行为列表重建动态子面板（**只能在算子执行时调用，绝不可以在 draw 里** ✗）。

    ⛔ 带**签名去重**：同一个列表被连续调用多次（读动画里 `_fill_anims_from_dict` +
       `_anim_touch` 都会调）时，标签没变就直接返回，避免反复注册/注销同一批类 ✓
    """
    global _DYN_ANIM_CLASSES, _DYN_ANIM_SIG
    try:
        n = len(context.scene.bamod_anims)
        labels = [_anim_item_label(i, context.scene.bamod_anims[i]) for i in range(n)]
    except Exception:  # noqa: BLE001
        return 0
    sig = tuple(labels)
    if sig == _DYN_ANIM_SIG and len(_DYN_ANIM_CLASSES) == 2 * n:
        return n
    for cls in reversed(_DYN_ANIM_CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:  # noqa: BLE001
            pass
    _DYN_ANIM_CLASSES = []
    for i, label in enumerate(labels):
        try:
            item_cls, fields_cls = _make_anim_item_panel(i, label)
        except Exception as e:  # noqa: BLE001
            print("[动画] 造第 %d 条行为的子面板失败：%s" % (i + 1, e))
            continue
        # ⛔ 注册顺序：**父先、子后**（子栏的 bl_parent_id 必须已注册，否则
        #    "parent 'BAMOD_PT_ANIMITEM_0' not found" 直接注册失败 ✗）
        #    注销时反过来（`reversed` 遍历）⇒ 子先撤、父后撤 ✓
        for cls in (item_cls, fields_cls):
            try:
                bpy.utils.register_class(cls)
                _DYN_ANIM_CLASSES.append(cls)
            except Exception as e:  # noqa: BLE001
                print("[动画] 注册子面板失败：%s" % e)
    _DYN_ANIM_SIG = sig
    return len(_DYN_ANIM_CLASSES) // 2


def _anim_drop_panels():
    """注销全部动态子面板（unregister / 清空列表时用）。"""
    global _DYN_ANIM_CLASSES, _DYN_ANIM_SIG
    for cls in reversed(_DYN_ANIM_CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:  # noqa: BLE001
            pass
    _DYN_ANIM_CLASSES = []
    _DYN_ANIM_SIG = None


def _anim_load_handler(_dummy=None):
    """打开/新建 .blend 之后按里面的行为列表重建子面板（动态面板不随文件走 ✓）。"""
    try:
        _anim_rebuild_panels(bpy.context)
    except Exception as e:  # noqa: BLE001
        print("[动画] 载入后重建行为子面板失败：%s" % e)


class BAMOD_PT_Animation(bpy.types.Panel):
    bl_label = "④ 动画"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "BA Mod"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        r"""④ 动画面板**主栏**：读取 + 状态摘要。

        ⛔ v1.8.87：每条行为现在是**真正的子面板**（在父栏下面，各自带原生折叠箭头、
           默认折叠）。以前是"假折叠栏"（同面板内 `b.open` + 三角图标自己切换）✗
           ⇒ 主栏只留：当前模型 / 读动画 / 开关 / 条数提示。
        """
        layout = self.layout
        prefs = _prefs(context)
        # v1.8.73：一直显示"当前模型（复制源）"—— 它同时决定读谁、写谁。
        # 用户实测踩过坑：先导目标模型、再指导入捐赠机 ⇒ 读到的是捐赠机的行为 ✗
        _src = _copy_source_name(context)
        if _src:
            layout.label(text="当前模型（读取/构建都用它）：%s" % _src, icon="FILE_TEXT")
        else:
            layout.label(text="⚠ 还没导入模型 —— 先做 ① 模型导入", icon="ERROR")
        # v1.8.74：① 里选中的和"当前模型"不一致时，直接把救援按钮摆出来。
        _sel = _selected_prefab_name(context)
        if _sel and _src and _sel != _src:
            box = layout.box()
            box.label(text="① 里选中了 %s，和当前模型不一致" % _sel, icon="ERROR")
            box.operator("ba_mod.set_copy_source",
                         text="把①所选模型设为当前模型", icon="FILE_TICK")
        row = layout.row()
        row.operator("ba_mod.read_animation", text="读取当前模型的动画", icon="ANIM")
        layout.prop(prefs, "apply_anim")
        n = len(context.scene.bamod_anims)
        if n == 0:
            layout.label(text="（还没有动画行为：展开「新建」子栏加）", icon="INFO")
        else:
            layout.label(text="当前 %d 条行为 —— 每条是一个折叠栏（默认折叠）" % n,
                         icon="CHECKMARK")
        txt = bpy.data.texts.get("bamod_anim")
        if txt is not None:
            layout.label(text="（JSON 已同步到文本块 bamod_anim，可手改；面板有内容时以面板为准）")


# ---------------------------------------------------------------------------
# ④ 动画 —— 折叠子栏（v1.8.69，全部**默认折叠**）
# ---------------------------------------------------------------------------
# ⛔ Blender 的子面板（`bl_parent_id`）**永远排在父面板内容之后**，不能插在中间，
#    所以父栏只放"读动画 + 行为列表"（最常用），其余全部下沉到这几个折叠栏 ✓
# ⛔ `bl_options = {"DEFAULT_CLOSED"}` 才是"默认折叠"；少了它就是默认展开 ✗
# ⛔ 子栏标签**不带「④·」前缀**：父栏已经叫「④ 动画」，子栏再重复一个④很违和
#    （用户反馈"每个前面都有个④"）。保留普通序号「1/2/3/4」，层级由 Blender 缩进体现 ✓
# ---------------------------------------------------------------------------
class BAMOD_PT_Anim_Create(bpy.types.Panel):
    bl_label = "1 从零做（模板 / 读取动画）"
    bl_parent_id = "BAMOD_PT_Animation"
    bl_order = 1000        # v1.8.87：让「每条行为」的真子面板排在前面
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "BA Mod"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        box = layout.box()
        box.label(text="模板：16 组现成起点，生成后就是普通行为，随便改", icon="PRESET_NEW")
        box.operator("ba_mod.apply_anim_preset", text="选择模板并应用…", icon="PRESET_NEW")
        layout.separator()
        # v1.8.73：把"读哪个模型的动画"讲清楚 —— 用户一直分不清"复制源"和"① 里选中的"
        box = layout.box()
        box.label(text="读任意模型的动画：① 选中哪个就读哪个", icon="COPYDOWN")
        box.operator("ba_mod.import_anim_from_prefab", text="读取①所选模型的动画…",
                     icon="COPYDOWN")
        _sel = _selected_prefab_name(context)
        if _sel:
            box.label(text="① 当前选中：%s" % _sel, icon="CHECKMARK")
        else:
            box.label(text="① 面板里还没选中模型", icon="INFO")
        box.label(text="当前模型（复制源）：%s" % (_copy_source_name(context) or "（未导入）"))
        box.label(text="想拿它当整个基底 → 对话框里选「替换全部」")
        box.label(text="只需补几条 → 选「追加」（默认）")


class BAMOD_PT_Anim_Add(bpy.types.Panel):
    bl_label = "2 新增一条行为"
    bl_parent_id = "BAMOD_PT_Animation"
    bl_order = 1001
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "BA Mod"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        layout.operator("ba_mod.add_animation", text="＋ 新增一条行为（选类型）…",
                        icon="ADD")
        box = layout.box()
        box.label(text="24 个行为类全在对话框的下拉里（带中文名）", icon="INFO")
        box.label(text="例：恒速自转 / 空中浮动 / 弹簧跟随 / 抛壳 / 加力尾焰…")
        box.label(text="新增后每条都能复制(⧉) / 换数组(⇄) / 删除(✕) / 展开改参数")


class BAMOD_PT_Anim_Set(bpy.types.Panel):
    bl_label = "3 行为集 / 校验"
    bl_parent_id = "BAMOD_PT_Animation"
    bl_order = 1002
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "BA Mod"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        box = layout.box()
        box.label(text="行为集（备份 / 跨模型复用）", icon="FILEBROWSER")
        row = box.row(align=True)
        row.operator("ba_mod.export_anim", text="导出 .baanim", icon="EXPORT")
        op = row.operator("ba_mod.import_anim", text="读入", icon="IMPORT")
        op.mode = "append"
        row = box.row(align=True)
        op = row.operator("ba_mod.sync_anim_text", text="面板 → 文本块", icon="TEXT")
        op.direction = "to_text"
        op = row.operator("ba_mod.sync_anim_text", text="文本块 → 面板", icon="TEXT")
        op.direction = "from_text"
        row = box.row(align=True)
        row.operator("ba_mod.validate_anim", text="校验", icon="CHECKMARK")
        row.operator("ba_mod.clear_anims", text="清空全部", icon="TRASH")
        layout.label(text="文本块 JSON 可在 Blender 文本编辑器里批量查找替换",
                     icon="INFO")


class BAMOD_PT_Anim_Help(bpy.types.Panel):
    bl_label = "4 速查（数组 / 节点 / 危险）"
    bl_parent_id = "BAMOD_PT_Animation"
    bl_order = 1003
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "BA Mod"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        layout.label(text="数组 = 什么时候播（改错数组是最高频的坑）", icon="TIME")
        box = layout.box()
        box.label(text="universal  战场 + 军械库都生效")
        box.label(text="demo       只在军械库演示播（战场不播）")
        box.label(text="game       只在战场")
        box.label(text="preDeath   被打后（残血 / 受损）")
        box.label(text="death      死亡时")
        box.label(text="⛔ 旋翼自转放 demo ⇒ 预览里转、实战不转", icon="ERROR")
        layout.separator()
        layout.label(text="节点引用用**名字**，不用 pid", icon="EMPTY_ARROWS")
        box = layout.box()
        box.label(text="面板会自动带上挂载点名，构建期解析成真实 pid")
        box.label(text="自己新建的挂载点也能被正确解析（v1.8.67 起）")
        box.label(text="名字对不上时会提示「本模型没有这个节点」")
        layout.separator()
        layout.label(text="⚠ 会改动模型部件的行为", icon="ERROR")
        box = layout.box()
        box.label(text="DestroyMesh 销毁部件 / ModelReplace 换模型 / TurretFly 炮塔飞脱")
        box.label(text="先在备份上试")


# ---------------------------------------------------------------------------
# ⑤ 皮肤（载具涂装：显示 / 修改 / 新增 / 打包导入游戏）
# ---------------------------------------------------------------------------

_SKIN_INDEX_CACHE = {}     # bundle 路径 -> skin_data.build_skin_index() 结果
_SKIN_SCAN_CACHE = {}      # bundle 路径 -> {"skins": {id: {...}}, "ren_map": {ren_pid: mat_pid}}
_IMG_CACHE = {}            # (bundle, tex_pid) -> Blender Image
_IMG_AVG = {}              # (bundle, tex_pid) -> (r,g,b) 贴图平均色（实体模式视口色）


def _skin_import_mods():
    import os
    import sys
    import skin_data  # noqa: F401  （_rev_tools 目录已在 sys.path）
    return skin_data


def _skins_env(prefs):
    import skin_data as sd
    return sd._load_env(prefs.bundle)


def _scan_skins(context, prefs):
    """扫描当前导入模型可用的皮肤（按 renderer pid + 材质 pid 交集）。返回 (skins, ren_map)。"""
    import skin_data as sd
    bundle = prefs.bundle
    objs = [o for o in context.scene.objects if "ba_renderer_pid" in o]
    if not objs:
        return None, "请先导入模型（① 模型导入）"
    ren_pids = set(int(o["ba_renderer_pid"]) for o in objs)
    mat_pids = set()
    for o in objs:
        if "ba_materials" in o:
            mat_pids.update(int(x) for x in o["ba_materials"])
    if bundle in _SKIN_INDEX_CACHE:
        index = _SKIN_INDEX_CACHE[bundle]
    else:
        index = sd.build_skin_index(bundle)
        _SKIN_INDEX_CACHE[bundle] = index
    if not index:
        return None, "bundle 里没有皮肤数据（SkinStorageBridge）"
    # 匹配：renderer 交集 + 材质交集，按覆盖度排序
    scored = {}
    for entry in index:
        for it in entry["items"]:
            cover = 0
            for m in it["mats"]:
                if any(r["pid"] in ren_pids for r in m["renderers"]) or m["pid"] in mat_pids:
                    cover += 1
            if cover <= 0:
                continue
            bag = scored.setdefault(it["id"], {"mats": {}, "cover": 0, "bridges": []})
            bag["cover"] = max(bag["cover"], cover)
            for m in it["mats"]:
                bag["mats"].setdefault(m["pid"], m)
            bag["bridges"].append(entry["prefab"])
    skins = {}
    ren_map = {}
    for sid in sorted(scored, key=lambda s: -scored[s]["cover"]):
        skins[sid] = scored[sid]
        for m in scored[sid]["mats"].values():
            for r in m["renderers"]:
                ren_map.setdefault(r["pid"], m["pid"])
    _SKIN_SCAN_CACHE[bundle] = {"skins": skins, "ren_map": ren_map}
    return skins, None


class SKIN_Item(bpy.types.PropertyGroup):
    id: bpy.props.IntProperty(name="皮肤 Id")
    label: bpy.props.StringProperty(name="说明")


class STREAMTEX_Item(bpy.types.PropertyGroup):
    r"""★⑮ 候选贴图（**流式引用**那条路的贴图池）的一行。"""
    name: bpy.props.StringProperty(name="贴图名")
    detail: bpy.props.StringProperty(name="规格")
    refs: bpy.props.IntProperty(name="材质引用数", default=0)


class STREAMTEX_UL(bpy.types.UIList):
    bl_idname = "BA_MOD_UL_stream_texts"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname):
        if self.layout_type in {"DEFAULT", "COMPACT"}:
            # ⛔ 0 引用的那张**可能**是死数据 —— KB 卡坑 3 纠正过："解码失败 ≠ 死数据"，
            #   判死活要看**有没有材质引用**（`US_AH-1Z_3_BaseMap` 就被误判过：它其实是冬季迷彩）
            #   ⇒ 行上直接把引用数摆出来，别让用户猜 ✓
            layout.label(text=item.name, icon="IMAGE_DATA" if item.refs else "ERROR")
            layout.label(text=item.detail)


class SKIN_UL(bpy.types.UIList):
    bl_idname = "BA_MOD_UL_skins"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname):
        if self.layout_type in {"DEFAULT", "COMPACT"}:
            layout.label(text=item.label, icon="COLOR")


class BAMOD_OT_ScanSkins(bpy.types.Operator):
    bl_idname = "ba_mod.scan_skins"
    bl_label = "扫描皮肤"
    bl_description = "从 bundle 读取该模型可用的皮肤（按渲染器/材质匹配）"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        prefs = _prefs(context)
        if not prefs.bundle or not os.path.isfile(prefs.bundle):
            self.report({"ERROR"}, "请先在偏好里检测游戏目录")
            return {"CANCELLED"}
        skins, err = _scan_skins(context, prefs)
        if err:
            self.report({"ERROR"}, err)
            return {"CANCELLED"}
        scene = context.scene
        scene.bamod_skins.clear()
        scene["ba_skin_hint"] = ""
        for sid in sorted(skins):
            item = scene.bamod_skins.add()
            item.id = sid
            mats = skins[sid]["mats"]
            names = sorted(set(m["name"] for m in mats.values()))
            short = "、".join(names[:3]) + ("…" if len(names) > 3 else "")
            item.label = "皮肤 %d · %d 材质（%s）" % (sid, len(mats), short)
        if not skins:
            # 指引：皮肤桥都在炮塔/士兵 prefab 上——建议导入同名的炮塔变体模型
            src_path = scene.get("ba_copy_source_path", "")
            base = _normalize_name(os.path.basename(src_path).replace(".prefab", ""))
            sug = []
            index = _SKIN_INDEX_CACHE.get(prefs.bundle) or []
            seen = set()
            for entry in index:
                p = entry.get("prefab") or ""
                if base and base in p and p != src_path:
                    k = os.path.basename(p).replace(".prefab", "")
                    if k not in seen:
                        seen.add(k)
                        sug.append(k)
            hint = ("当前模型没有皮肤数据。皮肤桥都在炮塔/士兵 prefab 上，"
                    "请在 ① 面板导入同名炮塔变体：%s" % ("、".join(sug[:4]) if sug else "Turrets/… 下同名前缀的模型"))
            scene["ba_skin_hint"] = hint
            print("[皮肤] " + hint)
            self.report({"WARNING"}, "没找到皮肤：%s" % ("试试 " + sug[0] if sug else "请导入炮塔变体模型"))
        else:
            self.report({"INFO"}, "找到 %d 个皮肤（列表选中后点「应用皮肤」即可在视口看到效果）" % len(skins))
        return {"FINISHED"}


def _ensure_tex_image(bundle, tex, prefs):
    """tex: {"pid", "name"} -> Blender Image（缓存 + 临时 PNG）。同时缓存贴图平均色。"""
    key = (bundle, tex["pid"])
    if key in _IMG_CACHE:
        return _IMG_CACHE[key]
    import skin_data as sd
    _, _objs, by_pid = sd._load_env(bundle)
    png = sd.texture_png_bytes(by_pid, tex["pid"])
    avg = None
    if not png:
        img = bpy.data.images.new(tex["name"] or "tex", 4, 4)
    else:
        import tempfile
        tmp = os.path.join(bpy.app.tempdir, "bamod_skins")
        os.makedirs(tmp, exist_ok=True)
        fp = os.path.join(tmp, "%d.png" % tex["pid"])
        with open(fp, "wb") as f:
            f.write(png)
        img = bpy.data.images.load(fp)
        img.name = tex["name"] or ("tex_%d" % tex["pid"])
        img.pack()
        try:
            os.remove(fp)
        except OSError:
            pass
        # 平均色（供实体模式视口显示色用；Pillow 随 UnityPy 已装）
        try:
            from PIL import Image as _PILImage
            import io as _io
            im = _PILImage.open(_io.BytesIO(png)).convert("RGB").resize((16, 16))
            px = list(im.getdata())
            if px:
                avg = tuple(sum(c[i] for c in px) / len(px) / 255.0 for i in range(3))
        except Exception:
            avg = None
    _IMG_CACHE[key] = img
    _IMG_AVG[key] = avg
    return img


def _fixup_mat_nodes(bm):
    """按类型修理材质节点（节点名会被 UI 本地化：原理化 BSDF/材质输出，不能按名找）。

    - 保证存在 Principled BSDF 且连到 Material Output 的 Surface；
    - 删除多余的 Principled（旧版 bug 留下的孤儿节点）。返回 (bsdf, out)。
    """
    nt = bm.node_tree
    if nt is None:
        return None, None
    bsdfs = [n for n in nt.nodes if n.type == "BSDF_PRINCIPLED"]
    out = next((n for n in nt.nodes if n.type == "OUTPUT_MATERIAL"), None)
    if out is None:
        out = nt.nodes.new("ShaderNodeOutputMaterial")
    # 选接到输出的那个 BSDF（没有则用第一个/新建）
    bsdf = next((n for n in bsdfs
                 if any(l.to_node == out for l in nt.links if l.from_node == n)), None)
    if bsdf is None:
        bsdf = bsdfs[0] if bsdfs else nt.nodes.new("ShaderNodeBsdfPrincipled")
    for n in [n for n in bsdfs if n != bsdf]:
        nt.nodes.remove(n)
    if not any(l.to_node == out for l in nt.links if l.from_node == bsdf):
        nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    return bsdf, out


def _ensure_blender_mat(skin_id, mat, bundle, prefs):
    """mat: {"pid","name","tex"} -> Blender Material（Principled + BaseMap）。

    - 节点按类型定位（兼容本地化界面，如中文「原理化 BSDF」）；
    - 已存在的同名材质也重新修理（修复旧版孤儿节点问题）；
    - 视口显示色 = 贴图平均色（实体模式不灰）。
    """
    nm = "SKIN%d_%s" % (skin_id, (mat["name"] or "mat")[:48])
    bm = bpy.data.materials.get(nm)
    if bm is None:
        bm = bpy.data.materials.new(nm)
    if not bm.node_tree:
        bm.use_nodes = True
    bsdf, out = _fixup_mat_nodes(bm)
    if bsdf is not None and mat.get("tex"):
        img = _ensure_tex_image(bundle, mat["tex"][0], prefs)
        nt = bm.node_tree
        tex_node = next((n for n in nt.nodes if n.type == "TEX_IMAGE"), None)
        if tex_node is None:
            tex_node = nt.nodes.new("ShaderNodeTexImage")
            tex_node.location = (-300, 100)
        tex_node.image = img
        bc = bsdf.inputs.get("Base Color")
        if bc is not None and not any(l.to_node == bsdf and l.to_socket == bc for l in nt.links):
            nt.links.new(tex_node.outputs["Color"], bc)
        avg = _IMG_AVG.get((bundle, mat["tex"][0]["pid"]))
        if avg:
            bm.diffuse_color = (avg[0], avg[1], avg[2], 1.0)
    return bm


def _apply_mats(context, mat_of_renderer):
    """按 renderer -> mat pid 映射给场景网格赋材质。返回应用的物体数。"""
    import skin_data as sd
    prefs = _prefs(context)
    _, _objs, by_pid = sd._load_env(prefs.bundle)
    n = 0
    for o in context.scene.objects:
        if "ba_renderer_pid" not in o:
            continue
        rpid = int(o["ba_renderer_pid"])
        mpid = mat_of_renderer(rpid, o)
        if not mpid:
            continue
        mo = by_pid.get(mpid)
        if not mo or mo.type.name != "Material":
            continue
        try:
            info = {"pid": mpid, "name": mo.read().m_Name or "?", "tex": []}
            for te in (mo.read().m_SavedProperties.m_TexEnvs or []):
                tp = te[1].m_Texture.m_PathID if te[1].m_Texture else 0
                if tp:
                    to = by_pid.get(tp)
                    tn = "?"
                    if to:
                        try:
                            tn = to.read().m_Name
                        except Exception:
                            pass
                    info["tex"].append({"pid": tp, "name": tn})
        except Exception:
            info = {"pid": mpid, "name": "?", "tex": []}
        bm = _ensure_blender_mat(context.scene.get("ba_applied_skin_id", 0), info, prefs.bundle, prefs)
        o.data.materials.clear()
        o.data.materials.append(bm)
        o["ba_active_mat_pid"] = str(mpid)
        n += 1
    return n


class BAMOD_OT_ApplyDefaultMats(bpy.types.Operator):
    bl_idname = "ba_mod.apply_default_mats"
    bl_label = "显示默认材质"
    bl_description = "给所有网格赋该模型的默认材质（无皮肤）"

    def execute(self, context):
        # ⛔ 补 bundle 校验：偏好里没填路径（新装插件 / 自动检测失败）时以前会直接
        #    `sd._load_env("")` 抛异常冒泡到面板 ✗（ApplySkin 一直有这个守卫，这里漏了）
        prefs = _prefs(context)
        if not prefs.bundle or not os.path.isfile(prefs.bundle):
            self.report({"ERROR"}, "请先在偏好里填对 bundle 路径")
            return {"CANCELLED"}

        def mat_of(rpid, obj):
            mats = obj.get("ba_materials")
            if mats and len(mats):
                return int(mats[0])
            return 0
        n = _apply_mats(context, mat_of)
        context.scene["ba_applied_skin_id"] = 0
        self.report({"INFO"}, "默认材质：%d 个物体" % n)
        return {"FINISHED"}


class BAMOD_OT_ApplySkin(bpy.types.Operator):
    bl_idname = "ba_mod.apply_skin"
    bl_label = "应用选中皮肤"
    bl_description = "把选中皮肤应用/显示到场景网格（材质 + 贴图）"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        prefs = _prefs(context)
        if not prefs.bundle or not os.path.isfile(prefs.bundle):
            self.report({"ERROR"}, "请先在偏好里检测游戏目录")
            return {"CANCELLED"}
        scene = context.scene
        idx = scene.bamod_skin_index
        if idx < 0 or idx >= len(scene.bamod_skins):
            self.report({"ERROR"}, "请先扫描并选中一个皮肤")
            return {"CANCELLED"}
        sid = scene.bamod_skins[idx].id
        bundle = prefs.bundle
        if bundle not in _SKIN_SCAN_CACHE:
            _scan_skins(context, prefs)
        data = _SKIN_SCAN_CACHE.get(bundle, {})
        skins = data.get("skins", {})
        ren_map = data.get("ren_map", {})
        skin = skins.get(sid)
        if not skin:
            self.report({"ERROR"}, "皮肤数据不在（请重新扫描）")
            return {"CANCELLED"}
        mat_pids = set(skin["mats"].keys())
        scene["ba_applied_skin_id"] = sid

        def mat_of(rpid, obj):
            if rpid in ren_map and ren_map[rpid] in mat_pids:
                return ren_map[rpid]
            mats = obj.get("ba_materials")
            for x in (mats or []):
                if int(x) in mat_pids:
                    return int(x)
            # 皮肤不覆盖的部分保持默认材质（否则变灰色）
            if mats and len(mats):
                return int(mats[0])
            return 0
        n = _apply_mats(context, mat_of)
        self.report({"INFO"}, "皮肤 %d：%d 个物体" % (sid, n))
        return {"FINISHED"}


class BAMOD_OT_ExportSkinTextures(bpy.types.Operator):
    bl_idname = "ba_mod.export_skin_textures"
    bl_label = "导出贴图（皮肤/模型）"
    bl_description = "把当前皮肤（或默认材质）的贴图导出为 PNG（可外部编辑后打包回游戏）"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        import skin_data as sd
        prefs = _prefs(context)
        scene = context.scene
        sid = scene.get("ba_applied_skin_id", 0)
        bundle = prefs.bundle
        data = _SKIN_SCAN_CACHE.get(bundle, {})
        skin = data.get("skins", {}).get(sid) if sid else None
        mode = "皮肤 %d" % sid if skin else "模型默认材质"
        out_dir = scene.bamod_skin_tex_dir
        if not out_dir:
            out_dir = os.path.join(prefs.output_dir or
                                   os.path.dirname(prefs.bundle), "skin_textures")
        # ⛔ v1.8.83：导出逻辑抽成 _export_model_textures()，与 ⑥ 涂装共用一份
        #    （避免同功能两份实现以后分叉 ✗）
        try:
            _mats, out = _export_model_textures(context, prefs, out_dir)
        except Exception as e:  # noqa: BLE001
            self.report({"ERROR"}, "导出失败：%s" % e)
            return {"CANCELLED"}
        self.report({"INFO"}, "导出 %s：%d 张贴图 -> %s" % (mode, len(out), out_dir))
        return {"FINISHED"}


class BAMOD_OT_PackSkin(bpy.types.Operator):
    bl_idname = "ba_mod.pack_skin"
    bl_label = "打包皮肤 .bamod"
    bl_description = "用编辑后的贴图打包皮肤改动（在 BA_Mod_Maker 里导入游戏）"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        import pack_model as pm
        prefs = _prefs(context)
        scene = context.scene
        bundle = prefs.bundle
        sid = scene.get("ba_applied_skin_id", 0)
        data = _SKIN_SCAN_CACHE.get(bundle, {})
        skin = data.get("skins", {}).get(sid)
        if not skin:
            self.report({"ERROR"}, "请先应用一个皮肤")
            return {"CANCELLED"}
        tex_dir = scene.bamod_skin_tex_dir
        if not tex_dir or not os.path.isdir(tex_dir):
            self.report({"ERROR"}, "请先填写/导出皮肤贴图目录")
            return {"CANCELLED"}
        out_path = scene.bamod_skin_pack_path
        if not out_path:
            out_path = os.path.join(prefs.output_dir or tex_dir, "SKIN_%d.bamod" % sid)
        replace_mats = []
        textures = []
        tex_seen = set()
        for mpid, m in skin["mats"].items():
            slots = []
            for tex in (m.get("tex") or []):
                fp = os.path.join(tex_dir, tex["name"] + ".png")
                if not os.path.isfile(fp):
                    continue
                if os.path.abspath(fp) not in tex_seen:
                    tex_seen.add(os.path.abspath(fp))
                    textures.append({"name": tex["name"], "png": fp})
                slots.append({"slot": tex["slot"], "png": fp})
            if slots:
                # ★[★㉖-续·D P16] 皮肤包也要带**材质名**：消费侧才能在"自建包 pid 全变"时按名定位
                #   （与 matswap 路 L5706 同口径；⛔ 既有键 `orig`/`texs` 语义不动 ⇒ 旧消费侧照样读）
                replace_mats.append({"orig": mpid, "orig_name": m.get("name"),
                                     "texs": slots})
        if not replace_mats:
            self.report({"ERROR"}, "贴图目录里没有与该皮肤材质匹配的 PNG（先导出皮肤贴图再编辑）")
            return {"CANCELLED"}
        new_id = scene.bamod_skin_new_id
        try:
            pm.create_skin_pack(out_path, replace_mats, textures, sid,
                                context.scene.get("ba_copy_source_path", ""),
                                new_id=new_id)
        except Exception as e:  # noqa: BLE001
            self.report({"ERROR"}, "打包失败：%s" % e)
            return {"CANCELLED"}
        msg = "已打包 %s（%d 材质 / %d 贴图）" % (out_path, len(replace_mats), len(textures))
        if new_id > 0:
            msg += "；新增皮肤槽 %d（菜单显示需另改 UserItemsConfig）" % new_id
        self.report({"INFO"}, msg)
        return {"FINISHED"}


def _model_default_mat_bindings(scene, by_pid, objects=None):
    """当前导入模型（枪械等无皮肤桥）的默认材质绑定：{mat_pid: {pid,name,tex}}。

    ★ v1.9.1（★⑮）：`objects` 给定时只统计**这些物体**的材质 —— 勾了「只对选中对象生效」
      就必须连材质一起收窄，否则车身材质也会被写进 manifest（虽然渲染器过滤保证了车身不会被改，
      但包里会多出无用的材质克隆）✓
    """
    src = objects if objects is not None else scene.objects
    mat_pids = set()
    for o in src:
        if "ba_renderer_pid" not in o:
            continue
        for x in (o.get("ba_materials") or []):
            mat_pids.add(int(x))
    out = {}
    for mpid in mat_pids:
        mo = by_pid.get(mpid)
        if not mo or mo.type.name != "Material":
            continue
        try:
            mread = mo.read()
            info = {"pid": mpid, "name": mread.m_Name or "?", "tex": []}
            for te in (mread.m_SavedProperties.m_TexEnvs or []):
                tp = te[1].m_Texture.m_PathID if te[1].m_Texture else 0
                if tp:
                    to = by_pid.get(tp)
                    tn = "?"
                    if to:
                        try:
                            tn = to.read().m_Name
                        except Exception:
                            pass
                    info["tex"].append({"slot": te[0], "pid": tp, "name": tn})
            out[mpid] = info
        except Exception:
            continue
    return out


class BAMOD_OT_ScanStreamTextures(bpy.types.Operator):
    r"""★⑮：列出游戏包里可当「零件原机贴图」的**流式** `Texture2D`（不搬像素那条路）。"""
    bl_idname = "ba_mod.scan_stream_textures"
    bl_label = "列出候选贴图"
    bl_description = ("从游戏包里列出**流式**贴图（★⑮：把它指给选中网格 ⇒ 包只大几十 KB，不搬像素）")
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        import tex_stream as ts
        prefs = _prefs(context)
        scene = context.scene
        bundle = prefs.bundle
        if not bundle or not os.path.isfile(bundle):
            self.report({"ERROR"}, "请先在偏好里检测游戏目录（.bundle）")
            return {"CANCELLED"}
        q = (scene.bamod_stream_query or "").strip()
        if not q:
            self.report({"ERROR"}, "先填「搜索」—— 贴图名的一部分（例如 AH-1Z_viper）")
            return {"CANCELLED"}
        try:
            cands = ts.list_textures(bundle, grep=q, streamed_only=True, limit=200)
        except Exception as e:  # noqa: BLE001
            self.report({"ERROR"}, "列候选失败：%s" % e)
            return {"CANCELLED"}
        scene.bamod_stream_texts.clear()
        for c in cands:
            it = scene.bamod_stream_texts.add()
            it.name = c["name"]
            it.refs = int(c.get("refs", 0))
            it.detail = "%dx%d fmt=%d %.0fKB%s" % (
                c["w"], c["h"], c["fmt"], c["size"] / 1024.0,
                ("·引用%d" % it.refs) if it.refs else "·**无引用**")
        if not cands:
            self.report({"WARNING"}, "没找到名字带 %r 的**流式**贴图 —— 换个词，"
                                     "或者那张图是内嵌的（那就用「PNG」那条路）" % q)
            return {"CANCELLED"}
        scene.bamod_stream_tex_index = 0
        n_dead = sum(1 for c in cands if not c.get("refs"))
        self.report({"INFO"}, "候选 %d 张%s —— 点一条，打包时把选中网格的基准色槽指向它"
                    % (len(cands),
                       ("（其中 %d 张**没有任何材质引用**，可能是死数据，慎重）" % n_dead)
                       if n_dead else ""))
        return {"FINISHED"}


class BAMOD_OT_PackMatSwap(bpy.types.Operator):
    bl_idname = "ba_mod.pack_matswap"
    bl_label = "打包贴图替换包"
    bl_description = "用编辑后的贴图打包模型贴图替换（枪械等无皮肤模型；导入后该模型直接换新贴图）"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        import pack_model as pm
        import skin_data as sd
        import tex_stream as ts
        prefs = _prefs(context)
        scene = context.scene
        bundle = prefs.bundle
        out_path = scene.bamod_matswap_pack_path
        sel_only = bool(scene.bamod_matswap_selected_only)
        source = scene.bamod_matswap_tex_source or "png"
        # ---- ① 目标渲染器：★⑮ 勾了「只对选中对象生效」就只取选中网格；不勾 = 整车 ----
        host = [o for o in scene.objects if "ba_renderer_pid" in o]
        if sel_only:
            host = [o for o in host if o.select_get()]
            if not host:
                self.report({"ERROR"},
                            "勾了「只对选中对象生效」，但视口里没选中任何已导入的网格 —— "
                            "先在视口里选中要换材质的网格（例如旋翼），或者取消勾选（=整车一起换）")
                return {"CANCELLED"}
        renderer_pids = [int(o["ba_renderer_pid"]) for o in host]
        if not renderer_pids:
            self.report({"ERROR"}, "场景里没有导入的模型（① 先导入枪械/模型）")
            return {"CANCELLED"}
        _, _objs, by_pid = sd._load_env(bundle)
        mats = _model_default_mat_bindings(scene, by_pid, objects=host)
        if not mats:
            self.report({"ERROR"}, "这些网格没有可换的材质（① 先导入模型并「默认材质」）")
            return {"CANCELLED"}
        # ★★ v1.11.0（★⑯）：`ba_renderer_pid`/`ba_materials` 是**源包**（units）的 pid，而导入目标
        #   常常是**自建包**（`copy_full` 导入时**重分配过 pid**）⇒ 目标包里按 pid 一个都查不到 ✗
        #   ⇒ 打包时把**网格名 + 材质名**一起写进 manifest，导入端才能按名字定位（而且抗重建）✓
        renderer_info = []
        for o in host:
            mnames = []
            for x in (o.get("ba_materials") or []):
                info = mats.get(int(x))
                if info and info.get("name") and info["name"] != "?":
                    mnames.append(info["name"])
            renderer_info.append({"pid": int(o["ba_renderer_pid"]), "go": o.name, "mats": mnames})
        # ★★ ★⑰ 整车覆盖面：不勾「只对选中对象生效」时，**不只**取场景里那几个网格，
        #   还要把 prefab 子树里**构建期生成**的渲染器（各级 LOD 克隆等）一起认进来 ——
        #   它们不在 `.blend` 里，以前整车也换不到 ⇒ 用户**拉远/切低 LOD 时还是旧贴图** ✗
        #   ⛔ 归属判不出来的一律**不动**并逐条打印原因（猜错=把 A 部件的图贴到 B 部件，更难发现）
        #   ⛔⛔ **不许静默降级**（插件线 2026-09-16 只读核对指出的口子）：以前展开失败只 print 一行
        #      "不影响建包" ⇒ 用户拿到的是"看着成功、覆盖面却退回旧行为"的包 ✗
        #      ⇒ 现在三条一起做：① 明确 **WARNING 报告**（不是埋在日志里的 print）
        #                      ② 失败/未换的原因**写进 manifest 的 `matswap.scope`**（随包留证，
        #                         导入端与自检都读得到）③ 逐条列出"没换哪些 + 为什么" ✓
        scope_used = {"mode": "scene_only", "reason": "未跑子树展开（勾了「只对选中对象生效」）"}
        scope_warn = ""
        if not sel_only:
            try:
                import matswap_scope as mss
                res = mss.expand_owners(prefs.bundle, scene.get("ba_copy_source_path", ""),
                                        [{"name": o.name} for o in host], log=print)
                if res["assigned"]:
                    have = set(renderer_pids)
                    for a in res["assigned"]:
                        if a["pid"] in have:
                            continue
                        renderer_pids.append(a["pid"])
                        have.add(a["pid"])
                        own = next((o for o in host if o.name == a["owner"]), None)
                        om = []
                        for x in ((own.get("ba_materials") if own else None) or []):
                            info = mats.get(int(x))
                            if info and info.get("name") and info["name"] != "?":
                                om.append(info["name"])
                        renderer_info.append({"pid": a["pid"], "go": a["go"], "mats": om})
                    scope_used = {"mode": "subtree", "assigned": len(res["assigned"]),
                                  "total": len(res["renderers"]),
                                  "skipped": [{"go": s["go"], "mesh": s.get("mesh", ""),
                                               "reason": s["reason"]} for s in res["skipped"]]}
                    print("[★⑰] 整车覆盖面：prefab 子树 %d 个渲染器 ⇒ 归属 %d、按保守规则不动 %d"
                          % (len(res["renderers"]), len(res["assigned"]), len(res["skipped"])))
                    for s in res["skipped"]:
                        print("[★⑰]   未换：%-24s 原因：%s" % (s["go"], s["reason"]))
                    if res["skipped"]:
                        scope_warn = ("★⑰ 整车覆盖面：这 %d 个渲染器**没换**（逐条原因见日志与包的 "
                                      "matswap.scope.skipped）：%s"
                                      % (len(res["skipped"]),
                                         "；".join("%s（%s）" % (s["go"], s["reason"])
                                                   for s in res["skipped"][:6])))
                else:
                    why = res.get("error") or "子树里没有可归属的渲染器"
                    scope_used = {"mode": "scene_only", "reason": why,
                                  "total": len(res.get("renderers") or [])}
                    scope_warn = ("⛔ ★⑰ 本次**未能**展开构建期渲染器 ⇒ 覆盖面**退回旧行为**"
                                  "（只改场景里的网格）：%s；**这台车 prefab 子树里不在 .blend 的那些"
                                  "渲染器仍保持原材质**（远看 / 切低 LOD 可能还是旧贴图）" % why)
                    print("[★⑰] " + scope_warn)
            except Exception as e:  # noqa: BLE001
                why = "%s: %s" % (type(e).__name__, e)
                scope_used = {"mode": "scene_only", "reason": why}
                scope_warn = ("⛔ ★⑰ 子树展开**出错** ⇒ 覆盖面**退回旧行为**（只改场景里的网格）：%s；"
                              "**这台车 prefab 子树里不在 .blend 的渲染器仍保持原材质**"
                              "（远看 / 切低 LOD 可能还是旧贴图）" % why)
                print("[★⑰] " + scope_warn)
        tex_dir = scene.bamod_skin_tex_dir
        replace_mats = []
        textures = []
        tex_seen = set()
        if source == "stream":
            # ---- ② 贴图来源 = 流式引用：把材质**基准色槽**指向游戏里已有的那张贴图 ----
            idx = scene.bamod_stream_tex_index
            if not (0 <= idx < len(scene.bamod_stream_texts)):
                self.report({"ERROR"}, "先在「候选贴图」列表里点一条"
                                       "（没有就先填搜索词 → 列出候选贴图）")
                return {"CANCELLED"}
            tex_name = scene.bamod_stream_texts[idx].name
            cand_refs = int(scene.bamod_stream_texts[idx].refs)
            if not cand_refs:
                # ⛔ 不拦，但**必须说清楚**：这张在当前包里没有任何材质引用 —— 可能是死数据，
                #   也可能只是"还没被谁用上"（我们本来就是要把它指给一个原本不引用它的零件）
                #   KB 卡坑 3 的规矩：判死活看引用，不看解码成不成功 ✓
                print("[★⑮] ⚠ 候选 %s 在当前包里**没有材质引用**（可能是死数据）—— "
                      "若打进包后进游戏看不到图，就换一张有引用的" % tex_name)
                self.report({"WARNING"}, "⚠ %s 在当前包里没有任何材质引用（可能是死数据）；"
                                         "进游戏看不到图就换一张" % tex_name)
            try:
                item = ts.make_stream_item(bundle, tex_name)
            except Exception as e:  # noqa: BLE001
                self.report({"ERROR"}, "取流式引用失败：%s" % e)
                return {"CANCELLED"}
            textures.append({"name": tex_name, "stream": item["stream"]})
            manual = (scene.bamod_matswap_stream_slot or "").strip()
            skipped = []
            for mpid, m in mats.items():
                envs = [(t["slot"], t["name"]) for t in (m.get("tex") or [])]
                slots = [manual] if manual else ts.pick_base_slots(envs)
                slots = [s for s in slots if any(s == e[0] for e in envs)]
                if not slots:
                    skipped.append("%s(%s)" % (mpid, m.get("name")))
                    continue
                replace_mats.append({"orig": mpid, "orig_name": m.get("name"),
                                     "texs": [{"slot": s, "name": tex_name, "stream": True}
                                              for s in slots]})
            if not replace_mats:
                self.report({"ERROR"}, "这些材质里挑不出基准色槽（%s）—— 在「槽名」里手工填一个"
                                       "（槽名见 ⑤→导出贴图 出来的文件名，或材质面板）"
                             % "、".join(skipped[:4]))
                return {"CANCELLED"}
            if skipped:
                print("[★⑮] 这些材质没挑出基准色槽、已跳过：%s" % "、".join(skipped))
        else:
            # ---- ② 贴图来源 = PNG 内嵌：贴图目录里文件名 == 槽的贴图名 ----
            if not tex_dir or not os.path.isdir(tex_dir):
                self.report({"ERROR"}, "请先填写贴图目录（先导出贴图再编辑）")
                return {"CANCELLED"}
            for mpid, m in mats.items():
                slots = []
                for tex in m.get("tex") or []:
                    fp = os.path.join(tex_dir, tex["name"] + ".png")
                    if not os.path.isfile(fp):
                        continue
                    if os.path.abspath(fp) not in tex_seen:
                        tex_seen.add(os.path.abspath(fp))
                        textures.append({"name": tex["name"], "png": fp})
                    slots.append({"slot": tex["slot"], "png": fp})
                if slots:
                    replace_mats.append({"orig": mpid, "orig_name": m.get("name"), "texs": slots})
            if not replace_mats:
                self.report({"ERROR"}, "贴图目录里没有与这些材质匹配的 PNG（先「导出贴图」，"
                                       "把图按**槽的贴图名**命名后放进该目录）")
                return {"CANCELLED"}
        if not out_path:
            base = prefs.output_dir or tex_dir or os.path.dirname(bundle)
            out_path = os.path.join(base, "MATSWAP.bamod")
        try:
            pm.create_matswap_pack(out_path, replace_mats, textures, renderer_pids,
                                   scene.get("ba_copy_source_path", ""),
                                   tex_source=source, renderer_info=renderer_info,
                                   scope=scope_used)          # ★⑰：整车按子树展开的标记
        except Exception as e:  # noqa: BLE001
            self.report({"ERROR"}, "打包失败：%s" % e)
            return {"CANCELLED"}
        self.report({"INFO"}, "已打包 %s（%d 材质 / %d 贴图 / %d 渲染器%s%s）%s"
                    % (out_path, len(replace_mats), len(textures), len(renderer_pids),
                       "，只改选中的网格" if sel_only else "，整车",
                       ("（含 prefab 子树里构建期生成的 %d 个）" % scope_used["assigned"])
                       if scope_used.get("mode") == "subtree" else "",
                       "；流式引用（不搬像素）" if source == "stream" else ""))
        # ★★ ★⑰ 覆盖面**必须可见**（不许静默降级）：要么报"没换哪几个 + 为什么"，
        #   要么报"这次没展开成 ⇒ 覆盖面退回旧行为"——都用 WARNING 报告（不是埋在日志的一行）✓
        #   面板也留一份（`ba_matswap_last_scope_*`）：用户不看日志、只看面板也能看到 ✓
        try:
            if scope_used.get("mode") == "subtree":
                _lines = ["★⑰ 整车覆盖面：prefab 子树 %s 个 ⇒ 归属 %s 个"
                          % (scope_used.get("total"), scope_used.get("assigned"))]
                for _s in (scope_used.get("skipped") or []):
                    _lines.append("★⑰ 未换：%s —— %s" % (_s.get("go"), _s.get("reason")))
                if len(_lines) == 1:
                    _lines[0] += "（子树里的都归属上了）"
                scene["ba_matswap_last_scope_text"] = "\n".join(_lines)[:900]
                scene["ba_matswap_last_scope_bad"] = False
            elif not sel_only:
                scene["ba_matswap_last_scope_text"] = (
                    "⛔ ★⑰ 本次**没展开** prefab 子树 ⇒ 覆盖面**退回旧行为**（只改场景里 %d 个网格）；"
                    "原因：%s" % (len(renderer_pids), scope_used.get("reason", "?")))[:900]
                scene["ba_matswap_last_scope_bad"] = True
            else:
                scene["ba_matswap_last_scope_text"] = ""
                scene["ba_matswap_last_scope_bad"] = False
        except Exception:  # noqa: BLE001
            pass
        if scope_warn:
            self.report({"WARNING"}, scope_warn)
        if scope_used.get("mode") != "subtree" and not sel_only:
            self.report({"WARNING"},
                        "本次**没有**按 prefab 子树展开（原因：%s）⇒ 只换了场景里那 %d 个网格；"
                        "构建期生成的渲染器（LOD 各级克隆等）仍是原材质"
                        % (scope_used.get("reason", "?"), len(renderer_pids)))
        print("[★⑯] 包里已带网格名/材质名 ⇒ 导入到**自建包**（pid 被重分配）也能按名字定位：%s"
              % "、".join(e["go"] for e in renderer_info[:6]))
        return {"FINISHED"}


class BAMOD_PT_Skin(bpy.types.Panel):
    bl_label = "⑤ 皮肤"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "BA Mod"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        layout.operator("ba_mod.scan_skins", icon="COLOR")
        if scene.get("ba_skin_hint"):
            box = layout.box()
            for line in scene["ba_skin_hint"].split("\n"):
                box.label(text=line, icon="INFO")
        if scene.get("ba_applied_skin_id", 0):
            layout.label(text="当前皮肤：%d" % scene["ba_applied_skin_id"])


class BAMOD_PT_Skin_List(bpy.types.Panel):
    bl_label = "皮肤列表与应用"
    bl_parent_id = "BAMOD_PT_Skin"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "BA Mod"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        layout.template_list("BA_MOD_UL_skins", "", scene, "bamod_skins",
                             scene, "bamod_skin_index", rows=5)
        row = layout.row(align=True)
        row.operator("ba_mod.apply_default_mats", text="默认材质", icon="MATERIAL")
        row.operator("ba_mod.apply_skin", text="应用皮肤", icon="RESTRICT_COLOR_ON")


class BAMOD_PT_Skin_Tex(bpy.types.Panel):
    bl_label = "贴图导出"
    bl_parent_id = "BAMOD_PT_Skin"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "BA Mod"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        layout.label(text="贴图目录（编辑后的 PNG）：")
        layout.prop(scene, "bamod_skin_tex_dir", text="")
        layout.operator("ba_mod.export_skin_textures", icon="EXPORT")


class BAMOD_PT_Skin_Pack(bpy.types.Panel):
    bl_label = "打包"
    bl_parent_id = "BAMOD_PT_Skin"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "BA Mod"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        col = layout.column(align=True)
        col.prop(scene, "bamod_skin_new_id", text="新增皮肤 Id（0=重涂原槽）")
        col.prop(scene, "bamod_skin_pack_path", text="输出 .bamod")
        col.operator("ba_mod.pack_skin", icon="FILE_ARCHIVE")
        layout.separator()
        box = layout.box()
        box.label(text="枪械/无皮肤模型：默认材质 → 导出贴图 → 改图 → 打包替换包", icon="INFO")
        box.prop(scene, "bamod_matswap_pack_path", text="替换包 .bamod")
        # ★★ v1.9.1（★⑮）：把「只对选中对象生效」做成显式开关 —— 移植过来的零件
        #   （例：ACV 上的 AH-1Z 旋翼）用它原机贴图，车身一个渲染器都不动 ✓
        box.prop(scene, "bamod_matswap_selected_only", text="只对选中对象生效（不勾=整车）")
        n_all = 0
        n_sel = 0
        for o in scene.objects:
            if "ba_renderer_pid" not in o:
                continue
            n_all += 1
            if o.select_get():
                n_sel += 1
        box.label(text="已导入网格 %d 个 / 选中 %d 个 ⇒ %s"
                       % (n_all, n_sel,
                          ("只换选中的那 %d 个" % n_sel) if scene.bamod_matswap_selected_only
                          else "整车 %d 个一起换" % n_all),
                  icon="RESTRICT_SELECT_OFF" if scene.bamod_matswap_selected_only else "INFO")
        # ★★ ★⑰（插件线要求：**降级必须在面板上也看得见**，不能只躺在日志里）：
        #   上次打包的覆盖面结果留在这两个 scene 属性里，面板直接显示 —— 用户不看日志也能看到
        #   "整车到底换到几个 / 哪几个没换 / 是不是没展开成（退回旧行为）" ✓
        _sc_txt = scene.get("ba_matswap_last_scope_text", "")
        if _sc_txt:
            _bad = scene.get("ba_matswap_last_scope_bad", False)
            for _ln in _sc_txt.split("\n"):
                box.label(text=_ln, icon="ERROR" if _bad else "CHECKMARK")
        box.prop(scene, "bamod_matswap_tex_source", text="贴图来源")
        if scene.bamod_matswap_tex_source == "stream":
            box.prop(scene, "bamod_stream_query", text="搜索")
            box.operator("ba_mod.scan_stream_textures", icon="VIEWZOOM")
            box.template_list("BA_MOD_UL_stream_texts", "", scene, "bamod_stream_texts",
                              scene, "bamod_stream_tex_index", rows=4)
            box.prop(scene, "bamod_matswap_stream_slot", text="槽名（留空=自动挑基准色槽）")
            box.label(text="流式引用：不搬像素，包只大几十 KB", icon="INFO")
        else:
            box.label(text="PNG：导出的图按**槽的贴图名**命名放进上面「贴图目录」", icon="INFO")
        box.operator("ba_mod.pack_matswap", icon="FILE_ARCHIVE")


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# ⑥ 涂装（在 Blender 里给车涂涂画画 / 贴图片 → 打包成皮肤，进游戏就能看到）
# ---------------------------------------------------------------------------
# 这条链就是面板里的 ①→⑤：
#   ① 开始涂装：把模型材质的贴图导出成 PNG，并给每个导入过的网格建一个「绘制材质」
#      （图像纹理节点指向那张 PNG，且设为**活动节点** = 绘制目标）
#   ② 打开画板：把这张 PNG 设成「单张图像」模式的**画布**并在图像编辑器里打开
#      ⇒ 用 Blender 自带画笔（任意笔刷）直接在皮肤上涂涂画画 ✓
#   ③ 贴图片到车上：把一张图片**合成进画布**（位置/大小/旋转/不透明度/羽化、或整张铺满）✓
#   ④ 重新载入：用外部软件（Photoshop / Krita / 画图）画完再读回来 ✓
#   ⑤ 保存涂装：把画好的图像写回贴图目录（打包读的就是这个目录）✓
#      打包：直接复用已有的「打包贴图替换包 (.bamod)」⇒ 在 BA_Mod_Maker 里导入 ✓
#
# ⛔ 为什么不新写一套打包：贴图替换包的整条导入链（克隆材质/贴图 + 改渲染器材质槽）
#    在 BA_Mod_Maker 里已经验证可用 ✓ 这里只补「画」这一半，避免两份实现分叉 ✗
#
# ⛔⛔ v1.8.83：印章**不走画笔纹理**（实测两次踩死，别回头再试）：
#    1) 「模板(stencil)」= `brush.texture_slot.map_mode='STENCIL'` + `slot.texture`，
#       而 Blender 5.x 里 `image_paint.brush` 是**只读**的（`readonly=True`，改不了当前画笔），
#       官方笔刷又是从 `essentials_brushes-*.blend` **链接**进来的 ⇒ 往它的指针属性赋值
#       `slot.texture = tex` 会**静默失败**（读回 None，不报错 ✗✗）。新建的本地画笔能写，
#       但换不上当前画笔 ⇒ 这条路在 5.x 上根本走不通 ✗
#       （`brush.texture` 同病；登录期先查 `is_readonly` 也没用——它只对**新画笔**是 False ✓）
#    2) `image_paint.mode` 只有 MATERIAL / IMAGE 两档，写 'STENCIL' **静默回落**成 MATERIAL ✗；
#       `image_paint.stencil_image` + `use_stencil_layer` 是**遮罩**（Stencil Mask 面板），
#       不是拿图片往皮肤上盖 ✗
#    ⇒ 改成**像素级合成**：直接用 numpy 算画布像素，确定、可测、headless 也能验收 ✓✓

_PAINT_MAT_PREFIX = "BA_Paint_"


def _paint_target_objs(context):
    """导入过的网格对象（① 导入模型时会给它们打 ba_renderer_pid / ba_materials）。"""
    return [o for o in context.scene.objects
            if o.type == "MESH" and "ba_renderer_pid" in o]


def _export_model_textures(context, prefs, out_dir):
    """导出「当前模型（或已应用皮肤）」的贴图到 out_dir。

    返回 (材质表, [(贴图名, png 路径)])。
    ⑤ 的「导出贴图（皮肤/模型）」与 ⑥ 的「开始涂装」**共用这一个函数** ✓
    """
    import skin_data as sd
    scene = context.scene
    sid = scene.get("ba_applied_skin_id", 0)
    bundle = prefs.bundle
    data = _SKIN_SCAN_CACHE.get(bundle, {})
    skin = data.get("skins", {}).get(sid) if sid else None
    os.makedirs(out_dir, exist_ok=True)
    _, _objs, by_pid = sd._load_env(bundle)
    if skin:
        mats = skin["mats"]
    else:
        mat_pids = set()
        for o in scene.objects:
            if "ba_renderer_pid" not in o:
                continue
            for x in (o.get("ba_materials") or []):
                mat_pids.add(int(x))
        mats = {}
        for mpid in mat_pids:
            mo = by_pid.get(mpid)
            if not mo or mo.type.name != "Material":
                continue
            try:
                mread = mo.read()
                info = {"pid": mpid, "name": mread.m_Name or "?", "tex": []}
                for te in (mread.m_SavedProperties.m_TexEnvs or []):
                    tp = te[1].m_Texture.m_PathID if te[1].m_Texture else 0
                    if not tp:
                        continue
                    to = by_pid.get(tp)
                    tn = "?"
                    if to:
                        try:
                            tn = to.read().m_Name
                        except Exception:
                            pass
                    info["tex"].append({"slot": te[0], "pid": tp, "name": tn})
                mats[mpid] = info
            except Exception:
                continue
    out = []
    for mpid, m in mats.items():
        for tex in (m.get("tex") or []):
            png = sd.texture_png_bytes(by_pid, tex["pid"])
            if not png:
                print("[贴图] 纹理解码失败: %s (%d)" % (tex["name"], tex["pid"]))
                continue
            fp = os.path.join(out_dir, tex["name"] + ".png")
            with open(fp, "wb") as f:
                f.write(png)
            out.append((tex["name"], fp))
    return mats, out


def _paint_image_for(png_path):
    """按路径取/载入图像（同一路径只会有一份 ✓ 绘制时写回的就是它）。"""
    try:
        return bpy.data.images.load(png_path, check_existing=True)
    except Exception as e:  # noqa: BLE001
        print("[涂装] 载入图像失败 %s：%s" % (png_path, e))
        return None


def _paint_pixels_get(img):
    """取图像像素成 numpy 数组 (高, 宽, 4) float32。

    ⛔ 两个实测要点：
      · `img.pixels` 是**自下往上**的行序（第 0 行 = 图像最下面一行）✓
        画布与图片都是这个序 ⇒ 直接算就行，不要翻来翻去 ✗
      · 4096² 用 `list(img.pixels)` 是 6700 万个 float ⇒ 又慢又吃内存 ✗
        必须 `foreach_get` ✓
    """
    import numpy as np
    w, h = img.size
    if not w or not h:
        return None
    buf = np.empty(w * h * 4, dtype=np.float32)
    img.pixels.foreach_get(buf)
    return buf.reshape(h, w, 4)


def _paint_pixels_set(img, arr):
    """把 (高, 宽, 4) 写回图像（写视图/子块都行，因为 arr 是同一个大数组的切片 ✓）。"""
    img.pixels.foreach_set(arr.reshape(-1))
    img.update()


def _paint_composite(canvas, stamp, cx, cy, scale, rot_deg, alpha, fill, feather):
    """把 stamp 合成到 canvas 上（**原地**改 canvas 像素）。返回改到的像素数。

    cx / cy   ：印章中心在画布上的比例位置（0..1，自左下角算）
    scale     ：印章宽度占画布宽的比例
    rot_deg   ：逆时针旋转角度（度）
    alpha     ：整体不透明度 0..1
    fill=True ：忽略上面几项，等比把图片铺满整张画布 ✓
    feather   ：边缘羽化宽度占印章短边的比例（0 = 硬边）

    做法是**反向采样**（对画布每个像素反查它在印章上的位置），
    这样旋转/缩放不会出现空洞，也不用担心正向投影的裂缝 ✓
    """
    import numpy as np
    import math
    cav = _paint_pixels_get(canvas)
    spx = _paint_pixels_get(stamp)
    if cav is None or spx is None:
        return 0
    ch, cw = cav.shape[0], cav.shape[1]
    sh, sw = spx.shape[0], spx.shape[1]

    if fill:
        k = max(cw / float(sw), ch / float(sh))
        W, H = sw * k, sh * k
        pcx, pcy = cw * 0.5, ch * 0.5
    else:
        W = max(1.0, float(scale) * cw)
        H = W * (sh / float(sw))
        pcx, pcy = float(cx) * cw, float(cy) * ch

    # 只算印章可能覆盖的那一块（旋转后按外接圆算）——
    # 4096² 整张算一遍是 1600 万像素，白算 90% ✗
    reach = 0.5 * math.hypot(W, H)
    x0 = int(max(0, math.floor(pcx - reach)))
    x1 = int(min(cw, math.ceil(pcx + reach)))
    y0 = int(max(0, math.floor(pcy - reach)))
    y1 = int(min(ch, math.ceil(pcy + reach)))
    if x1 <= x0 or y1 <= y0:
        return 0

    gx = (np.arange(x0, x1, dtype=np.float32) + 0.5) - pcx
    gy = (np.arange(y0, y1, dtype=np.float32) + 0.5) - pcy
    GX, GY = np.meshgrid(gx, gy)
    t = math.radians(float(rot_deg))
    ct, st = math.cos(t), math.sin(t)
    RX = GX * ct + GY * st                  # 反向旋转到印章自己的坐标
    RY = -GX * st + GY * ct
    U = RX / W + 0.5
    V = RY / H + 0.5
    inside = (U >= 0.0) & (U < 1.0) & (V >= 0.0) & (V < 1.0)
    if not inside.any():
        return 0
    si = np.clip((U * sw).astype(np.int32), 0, sw - 1)
    sj = np.clip((V * sh).astype(np.int32), 0, sh - 1)
    s = spx[sj, si]                          # 与画布子块同形状的 (h, w, 4)
    a = s[..., 3] * float(alpha)
    if feather > 0.0:
        fw = max(1.0, float(feather) * 0.5 * min(W, H))
        edge = np.minimum(np.minimum(U, 1.0 - U), np.minimum(V, 1.0 - V)) * min(W, H)
        a = a * np.clip(edge / fw, 0.0, 1.0)
    a = np.where(inside, a, 0.0)[..., None]
    region = cav[y0:y1, x0:x1]               # ← 视图（不是拷贝）⇒ 下面能就地写 ✓
    region[..., :3] = s[..., :3] * a + region[..., :3] * (1.0 - a)
    region[..., 3] = a[..., 0] + region[..., 3] * (1.0 - a[..., 0])
    _paint_pixels_set(canvas, cav)
    n = int(inside.sum())
    return n


def _paint_canvas_image(context):
    """当前要画的画布：优先 image_paint 里设过的 canvas，否则第一个绘制材质的图像。"""
    try:
        ts = context.scene.tool_settings.image_paint
        if ts.canvas is not None:
            return ts.canvas
    except Exception:  # noqa: BLE001
        pass
    for mat in _paint_mats():
        img = _paint_mat_image(mat)
        if img is not None:
            return img
    return None


def _paint_editor_for(context, img):
    """把图像编辑器打开到这张图上（没有图像编辑器就想办法分一个出来）。

    界面操作只对真有窗口时才有意义 ⇒ 全程 try/except，headless 下静默跳过 ✓
    """
    wm = context.window_manager
    try:
        for w in wm.windows:
            for a in w.screen.areas:
                if a.type != "IMAGE_EDITOR":
                    continue
                for sp in a.spaces:
                    if sp.type == "IMAGE_EDITOR":
                        sp.image = img
                        return "已在图像编辑器里打开"
    except Exception as e:  # noqa: BLE001
        print("[涂装] 找图像编辑器失败：%s" % e)
    # 没找到 ⇒ 从最大的 3D 视图劈一块出来当图像编辑器
    try:
        win = next((w for w in wm.windows if w.screen), None)
        if win is None:
            return "（没有窗口：headless 下打不开编辑器，正常）"
        area = None
        for a in win.screen.areas:
            if a.type == "VIEW_3D" and (area is None or
                                        a.width * a.height > area.width * area.height):
                area = a
        if area is None:
            return "（没找到 3D 视图可劈分：手动把某个编辑器切成「图像编辑器」即可）"
        before = {a.as_pointer() for a in win.screen.areas}
        over = {"window": win, "screen": win.screen, "area": area,
                "region": area.regions[-1] if area.regions else None}
        with context.temp_override(**over):
            bpy.ops.screen.area_split(direction="HORIZONTAL", factor=0.72)
        for a in win.screen.areas:
            if a.as_pointer() not in before:
                if a.type == "VIEW_3D":      # 新出来的那个改了类型
                    a.type = "IMAGE_EDITOR"
                    for sp in a.spaces:
                        if sp.type == "IMAGE_EDITOR":
                            sp.image = img
                return "已新建一个图像编辑器并打开贴图"
    except Exception as e:  # noqa: BLE001
        return "（自动开编辑器失败，请手动把某个编辑器切成「图像编辑器」：%s）" % e
    return ""


def _paint_material_for(obj, png_path):
    """建/复用「绘制材质」：图像纹理 → Principled → 输出；图像节点设为**活动**（绘制目标）。"""
    name = _PAINT_MAT_PREFIX + (obj.name or "mesh")
    mat = bpy.data.materials.get(name)
    if mat is None:
        mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    img_node = nt.nodes.new("ShaderNodeTexImage")
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    outp = nt.nodes.new("ShaderNodeOutputMaterial")
    img_node.location = (-400, 0)
    bsdf.location = (-80, 0)
    outp.location = (220, 0)
    nt.links.new(img_node.outputs["Color"], bsdf.inputs["Base Color"])
    nt.links.new(bsdf.outputs["BSDF"], outp.inputs["Surface"])
    img = _paint_image_for(png_path)
    img_node.image = img
    nt.nodes.active = img_node          # ⛔ 不设活动节点 ⇒ 纹理绘制会画到别处/报没有图像 ✗
    mat["ba_paint_png"] = png_path
    return mat


def _paint_mats():
    """所有「绘制材质」（带 ba_paint_png 标记的）。"""
    out = []
    for mat in bpy.data.materials:
        try:
            if mat.get("ba_paint_png"):
                out.append(mat)
        except Exception:  # noqa: BLE001
            continue
    return out


def _paint_mat_image(mat):
    """取绘制材质里当前那张图像（活动节点上的）。"""
    try:
        nt = mat.node_tree
        node = nt.nodes.active if nt else None
        img = getattr(node, "image", None)
        if img is not None:
            return img
        for n in (nt.nodes if nt else []):
            if n.type == "TEX_IMAGE" and n.image:
                return n.image
    except Exception:  # noqa: BLE001
        pass
    return None


class BAMOD_OT_PaintSetup(bpy.types.Operator):
    bl_idname = "ba_mod.paint_setup"
    bl_label = "① 开始涂装（导出贴图 + 建绘制材质）"
    bl_description = ("把模型贴图导出成 PNG，并给每个导入过的网格建一个绘制材质\n"
                      "之后切到「纹理绘制」模式就能直接在车上画")
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        prefs = _prefs(context)
        scene = context.scene
        objs = _paint_target_objs(context)
        if not objs:
            self.report({"ERROR"}, "先 ① 导入模型（涂装是在导入的网格上画的）")
            return {"CANCELLED"}
        if not prefs.bundle:
            self.report({"ERROR"}, "先在偏好里填游戏 bundle 路径")
            return {"CANCELLED"}
        out_dir = scene.bamod_skin_tex_dir
        if not out_dir:
            out_dir = os.path.join(prefs.output_dir or os.path.dirname(prefs.bundle),
                                   "skin_textures")
            scene.bamod_skin_tex_dir = out_dir
        try:
            mats, out = _export_model_textures(context, prefs, out_dir)
        except Exception as e:  # noqa: BLE001
            self.report({"ERROR"}, "导出贴图失败：%s" % e)
            return {"CANCELLED"}
        if not out:
            self.report({"ERROR"}, "没导出到任何贴图（这个模型没有贴图可画？）")
            return {"CANCELLED"}
        by_name = {n: p for n, p in out}

        def pick(mpid):
            info = mats.get(int(mpid)) or {}
            names = [t["name"] for t in (info.get("tex") or []) if t["name"] in by_name]
            if not names:
                return None
            for t in names:                     # 优先主贴图（BaseMap / Base / Albedo / Diffuse）
                low = t.lower()
                if "base" in low or "albedo" in low or "diffuse" in low:
                    return t
            return names[0]

        n = 0
        used_names = []
        for o in objs:
            tex_name = None
            for x in (o.get("ba_materials") or []):
                tex_name = pick(x)
                if tex_name:
                    break
            if not tex_name:
                continue
            try:
                mat = _paint_material_for(o, by_name[tex_name])
            except Exception as e:  # noqa: BLE001
                self.report({"WARNING"}, "%s 建材质失败：%s" % (o.name, e))
                continue
            o.data.materials.clear()
            o.data.materials.append(mat)
            n += 1
            used_names.append(tex_name)
        if not n:
            self.report({"ERROR"}, "没能把贴图配到网格上（这些网格没有可用的材质贴图）")
            return {"CANCELLED"}
        # 切到纹理绘制模式（只有界面里才有意义，headless 下失败不影响）
        try:
            bpy.context.view_layer.objects.active = objs[0]
            for o in objs:
                o.select_set(True)
            bpy.ops.object.mode_set(mode="TEXTURE_PAINT")
        except Exception as e:  # noqa: BLE001
            print("[涂装] 切纹理绘制模式失败（手动切一次即可）：%s" % e)
        self.report({"INFO"}, "已准备 %d 个网格 / %d 张贴图：%s —— 接着点「② 打开画板」开始画"
                    % (n, len(out), ", ".join(sorted(set(used_names)))))
        return {"FINISHED"}


class BAMOD_OT_PaintBoard(bpy.types.Operator):
    bl_idname = "ba_mod.paint_board"
    bl_label = "② 打开画板（在图像编辑器里涂）"
    bl_description = ("把导出的贴图设成「单张图像」模式的画布并在图像编辑器里打开：\n"
                      "用任意画笔/颜色直接往皮肤上画，画的就是游戏里那张贴图\n"
                      "（画完 ⑤ 保存 → 打包 → 导入游戏）")
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        img = None
        for mat in _paint_mats():
            img = _paint_mat_image(mat)
            if img is not None:
                break
        if img is None:
            self.report({"ERROR"}, "还没有绘制贴图：先点「① 开始涂装」")
            return {"CANCELLED"}
        note = ""
        try:
            ts = context.scene.tool_settings.image_paint
            ts.mode = "IMAGE"                  # 单张图像（只有 MATERIAL / IMAGE 两档）
            ts.canvas = img                    # 画就是往这张图上画 ✓
            ts.use_occlude = False             # 投影绘制别把背面挡掉（车壳是闭合的）
            note = _paint_editor_for(context, img)
        except Exception as e:  # noqa: BLE001
            self.report({"WARNING"}, "设置画布失败：%s" % e)
        # 顺手把活动对象切到有绘制材质的那个网格上，否则「纹理绘制」模式点不进去
        try:
            for o in context.scene.objects:
                if o.type != "MESH" or not o.data.materials:
                    continue
                if any(m and m.get("ba_paint_png") for m in o.data.materials):
                    context.view_layer.objects.active = o
                    break
            bpy.ops.object.mode_set(mode="TEXTURE_PAINT")
        except Exception as e:  # noqa: BLE001
            print("[涂装] 切纹理绘制模式失败（手动切一次即可）：%s" % e)
        self.report({"INFO"}, "画板已打开：%s（%d×%d）%s —— 直接画，画完点「⑤ 保存涂装」"
                    % (img.name, img.size[0], img.size[1], note))
        return {"FINISHED"}


class BAMOD_OT_PaintStamp(bpy.types.Operator):
    bl_idname = "ba_mod.paint_stamp"
    bl_label = "③ 贴图片到车上（盖印）"
    bl_description = ("把选中的图片合成进画布：默认盖在中间（可调位置/大小/旋转/不透明度）\n"
                      "勾「整张铺满」= 用这张图铺满整张皮肤（直接换皮）\n"
                      "改完参数再点一次就是重新盖一次；不满意用「还原原始贴图」或者撤销")
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        path = scene.bamod_paint_stamp
        if not path or not os.path.isfile(path):
            self.report({"ERROR"}, "先选一张图片（要贴到车上的图）")
            return {"CANCELLED"}
        try:
            stamp = bpy.data.images.load(path, check_existing=True)
        except Exception as e:  # noqa: BLE001
            self.report({"ERROR"}, "载入图片失败：%s" % e)
            return {"CANCELLED"}
        canvases = []
        for mat in _paint_mats():
            img = _paint_mat_image(mat)
            if img is not None:
                canvases.append((mat, img))
        if not canvases:
            self.report({"ERROR"}, "还没有画布：先点「① 开始涂装」")
            return {"CANCELLED"}
        pt = (scene.bamod_paint_stamp_x, scene.bamod_paint_stamp_y)
        n_ok, n_px, bad = 0, 0, []
        for mat, img in canvases:
            try:
                got = _paint_composite(
                    img, stamp, pt[0], pt[1], scene.bamod_paint_stamp_scale,
                    scene.bamod_paint_stamp_rot, scene.bamod_paint_stamp_alpha,
                    bool(scene.bamod_paint_fill), scene.bamod_paint_stamp_feather)
            except Exception as e:  # noqa: BLE001
                bad.append("%s:%s" % (img.name, e))
                continue
            if got:
                n_ok += 1
                n_px += got
        if not n_ok:
            self.report({"ERROR"}, "没盖上去（图片可能在画布外/大小是 0）%s"
                        % ("；失败：%s" % "; ".join(bad[:2]) if bad else ""))
            return {"CANCELLED"}
        how = ("整张铺满" if scene.bamod_paint_fill else
               "盖在 (%.2f, %.2f)、宽 %.0f%%、转 %.0f°" %
               (pt[0], pt[1], scene.bamod_paint_stamp_scale * 100.0,
                scene.bamod_paint_stamp_rot))
        self.report({"INFO"}, "已把「%s」%s 到 %d 张画布（共改 %s 像素）—— 记得「⑤ 保存涂装」"
                    % (os.path.basename(path), how, n_ok, format(n_px, ",")))
        return {"FINISHED"}


class BAMOD_OT_PaintReload(bpy.types.Operator):
    bl_idname = "ba_mod.paint_reload"
    bl_label = "③ 从文件重新载入（外部软件画完用这个）"
    bl_description = "重新从贴图目录读取 PNG（用 Photoshop/Krita/画图 改过之后点它）"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        n = 0
        for mat in _paint_mats():
            png = mat.get("ba_paint_png")
            img = _paint_mat_image(mat)
            if img is None or not png or not os.path.isfile(png):
                continue
            try:
                if os.path.normcase(os.path.abspath(img.filepath or "")) != \
                        os.path.normcase(os.path.abspath(png)):
                    img.filepath = png
                img.reload()
                n += 1
            except Exception as e:  # noqa: BLE001
                self.report({"WARNING"}, "重载失败 %s：%s" % (os.path.basename(png), e))
        if not n:
            self.report({"ERROR"}, "没有可重载的绘制贴图（先点「① 开始涂装」）")
            return {"CANCELLED"}
        self.report({"INFO"}, "已重新载入 %d 张贴图" % n)
        return {"FINISHED"}


class BAMOD_OT_PaintSave(bpy.types.Operator):
    bl_idname = "ba_mod.paint_save"
    bl_label = "④ 保存涂装到贴图目录"
    bl_description = "把画好的图像写回贴图目录的 PNG（打包读的就是这个目录）"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        n = 0
        bad = []
        for mat in _paint_mats():
            png = mat.get("ba_paint_png")
            img = _paint_mat_image(mat)
            if img is None or not png:
                continue
            try:
                d = os.path.dirname(png)
                if d:
                    os.makedirs(d, exist_ok=True)
                img.filepath_raw = png
                img.file_format = "PNG"
                img.save()
                n += 1
            except Exception as e:  # noqa: BLE001
                bad.append("%s:%s" % (os.path.basename(png), e))
        if not n:
            self.report({"ERROR"}, "没有可保存的涂装（先点「① 开始涂装」）%s"
                        % ("；失败：%s" % "; ".join(bad[:3]) if bad else ""))
            return {"CANCELLED"}
        self.report({"INFO"}, "已保存 %d 张贴图%s"
                    % (n, ("；%d 张失败：%s" % (len(bad), "; ".join(bad[:3]))) if bad else ""))
        return {"FINISHED"}


class BAMOD_OT_PaintReset(bpy.types.Operator):
    bl_idname = "ba_mod.paint_reset"
    bl_label = "还原原始贴图（会先备份现在的）"
    bl_description = ("先把当前 PNG 备份成 <名字>.painted.png，再从游戏 bundle 重新导出原始贴图")
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        import shutil
        prefs = _prefs(context)
        scene = context.scene
        out_dir = scene.bamod_skin_tex_dir
        if not out_dir:
            self.report({"ERROR"}, "还没设贴图目录（先点「① 开始涂装」）")
            return {"CANCELLED"}
        backed = 0
        for mat in _paint_mats():
            png = mat.get("ba_paint_png")
            if png and os.path.isfile(png):
                try:
                    shutil.copy2(png, png + ".painted.png")
                    backed += 1
                except OSError as e:
                    self.report({"WARNING"}, "备份失败 %s：%s" % (os.path.basename(png), e))
        try:
            _mats, out = _export_model_textures(context, prefs, out_dir)
        except Exception as e:  # noqa: BLE001
            self.report({"ERROR"}, "重新导出失败：%s" % e)
            return {"CANCELLED"}
        n = 0
        for mat in _paint_mats():
            img = _paint_mat_image(mat)
            if img is not None:
                try:
                    img.reload()
                    n += 1
                except Exception:  # noqa: BLE001
                    pass
        self.report({"INFO"}, "已还原 %d 张贴图（备份了 %d 张 -> *.painted.png）"
                    % (len(out), backed))
        return {"FINISHED"}


class BAMOD_PT_Paint(bpy.types.Panel):
    bl_label = "⑥ 涂装（在车上画 / 贴图片）"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "BA Mod"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        r"""⑥ 涂装**主栏**：只放"从哪开始"（贴图目录 + 准备 + 开画板）。

        ⛔ v1.8.87（按用户要求"多用折叠栏、默认折叠"）：贴图参数、外部软件、保存打包、
           还原 全部下沉到默认折叠的子栏 ⇒ 主栏短、子栏按需展开 ✓
        """
        layout = self.layout
        scene = context.scene
        layout.label(text="① 开始涂装 → ② 画 / ③ 贴图 → ⑤ 保存 → 打包", icon="BRUSH_DATA")
        layout.prop(scene, "bamod_skin_tex_dir", text="贴图目录")
        layout.operator("ba_mod.paint_setup", icon="IMAGE_DATA")
        layout.operator("ba_mod.paint_board", icon="IMAGE_EDITOR")
        layout.label(text="画笔：左侧「纹理绘制」+ 任意笔刷", icon="INFO")


class BAMOD_PT_Paint_Stamp(bpy.types.Panel):
    bl_label = "③ 贴图片到车上（盖印 / 整张铺满）"
    bl_parent_id = "BAMOD_PT_Paint"
    bl_order = 10
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "BA Mod"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        box = layout.box()
        box.prop(scene, "bamod_paint_stamp", text="图片")
        box.prop(scene, "bamod_paint_fill", text="整张铺满（换皮）", toggle=True)
        col = box.column(align=True)
        col.active = not scene.bamod_paint_fill
        col.prop(scene, "bamod_paint_stamp_x", text="左右", slider=True)
        col.prop(scene, "bamod_paint_stamp_y", text="上下", slider=True)
        col.prop(scene, "bamod_paint_stamp_scale", text="宽度", slider=True)
        col.prop(scene, "bamod_paint_stamp_rot", text="旋转")
        box.prop(scene, "bamod_paint_stamp_alpha", text="不透明度", slider=True)
        box.prop(scene, "bamod_paint_stamp_feather", text="羽化", slider=True)
        box.operator("ba_mod.paint_stamp", icon="BRUSH_DATA")
        box.label(text="改参数再点=重新盖一次（可撤销）", icon="BLANK1")


class BAMOD_PT_Paint_File(bpy.types.Panel):
    bl_label = "④ 外部软件 / 保存 / 打包"
    bl_parent_id = "BAMOD_PT_Paint"
    bl_order = 11
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "BA Mod"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        col = layout.column(align=True)
        col.label(text="外部软件画完（Photoshop/Krita）：", icon="INFO")
        col.operator("ba_mod.paint_reload", icon="FILE_REFRESH")
        layout.separator()
        col = layout.column(align=True)
        col.operator("ba_mod.paint_save", icon="FILE_TICK")
        col.operator("ba_mod.pack_matswap", icon="FILE_ARCHIVE")
        col.prop(scene, "bamod_matswap_pack_path", text="输出")


class BAMOD_PT_Paint_Reset(bpy.types.Panel):
    bl_label = "⑤ 还原原始贴图（会先备份）"
    bl_parent_id = "BAMOD_PT_Paint"
    bl_order = 12
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "BA Mod"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        layout.operator("ba_mod.paint_reset", icon="LOOP_BACK")
        layout.label(text="当前涂装会先备份成 *.painted.png", icon="INFO")


# ---------------------------------------------------------------------------
# ⑦ 模型自带动画（防浪板 / 舱门 / 折叠 / 装卸载…）
# ---------------------------------------------------------------------------
# 这些动作是 prefab 自带的 Unity **AnimationClip**，由 ④ 里的 `AnimatorConnect`
# 发 Trigger 让 Animator 播（ACV 的防浪板 = `WaterShield`，后舱门 = `Embark`）。
# 这一段把片段解出来**打成 Blender 关键帧**，于是可以直接拖时间轴看到它怎么动 ✓
#
# ⛔ 关键帧**时间戳**在 streamed 数据里解不出来（块头那个 float 实测恒为 0）⇒
#    按**等间隔**铺满片段时长（该时长是真的：实测 0.25/0.5/1/3.5/8.5 秒都出现过）✓
def _clip_mod():
    """取片段模块（sys.path 由 register() 里加过；这里再兜一次，防老版本装法）。"""
    import os as _os
    import sys as _sys
    here = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "_rev_tools")
    if _os.path.isdir(here) and here not in _sys.path:
        _sys.path.insert(0, here)
    import clip_anim
    return clip_anim


def _clip_names(context):
    return [x for x in (context.scene.get("bamod_clip_list", "") or "").split("\x1f") if x]


def _clip_enum_items(self, context):
    """片段下拉：名字 + 时长（从 scene 里存的列表来）。"""
    return [(str(i), n, "片段 %s" % n) for i, n in enumerate(_clip_names(context))] \
        or [("0", "（先点①列出片段）", "")]


def _clip_prefab(context):
    """① 面板当前选中的 prefab：返回 (bundle, prefab_path, 短名)。"""
    prefs = _prefs(context)
    lst = context.scene.bamod_prefabs
    i = context.scene.bamod_prefab_index
    if not (0 <= i < len(lst)):
        return prefs.bundle, "", ""
    it = lst[i]
    return prefs.bundle, it.path, (it.name or _short_name(it.path))


class BAMOD_OT_ClipRefresh(bpy.types.Operator):
    bl_idname = "ba_mod.clip_refresh"
    bl_label = "① 列出模型自带的动画片段"
    bl_description = ("读取 ① 面板所选模型自带的 Unity AnimationClip（防浪板/舱门/折叠/装卸载…）\n"
                      "第一次要读 bundle，稍等；之后有缓存")
    bl_options = {"REGISTER"}

    def execute(self, context):
        bundle, path, short = _clip_prefab(context)
        if not bundle or not path:
            self.report({"ERROR"}, "先在 ① 面板选中一个模型")
            return {"CANCELLED"}
        try:
            clips = _clip_mod().list_clips(bundle, path)
        except Exception as e:  # noqa: BLE001
            self.report({"ERROR"}, "读片段失败：%s" % e)
            return {"CANCELLED"}
        if not clips:
            self.report({"ERROR"}, "%s 没有自带动画片段" % (short or path))
            return {"CANCELLED"}
        context.scene["bamod_clip_list"] = "\x1f".join(n for n, _d in clips)
        context.scene.bamod_clip_name = "0"
        self.report({"INFO"}, "%s：%d 个片段 —— %s"
                    % (short or path, len(clips),
                       "、".join("%s(%.2fs)" % (n, d) for n, d in clips[:6])))
        return {"FINISHED"}


def _clip_pick(context):
    """当前选中的片段名（从下拉的枚举键换回名字）。"""
    names = _clip_names(context)
    if not names:
        return ""
    try:
        i = int(context.scene.bamod_clip_name)
    except (TypeError, ValueError):
        i = 0
    return names[i] if 0 <= i < len(names) else names[0]


class BAMOD_OT_ClipImport(bpy.types.Operator):
    bl_idname = "ba_mod.clip_import"
    bl_label = "② 导入成关键帧（拖时间轴看它动）"
    bl_description = ("把选中的片段解出来，给模型上的对应节点打关键帧\n"
                      "（旋转/位移都按片段里的真实数值；时间轴按等间隔铺满片段时长）")
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        name = _clip_pick(context)
        if not name:
            self.report({"ERROR"}, "先点「① 列出模型自带的动画片段」")
            return {"CANCELLED"}
        bundle, path, short = _clip_prefab(context)
        ca = _clip_mod()
        try:
            ci = ca.read_clip(bundle, path, name)
        except Exception as e:  # noqa: BLE001
            self.report({"ERROR"}, "解码片段失败：%s" % e)
            return {"CANCELLED"}
        if ci is None or not ci.bindings:
            self.report({"ERROR"}, "片段 %s 没有可用的曲线" % name)
            return {"CANCELLED"}
        fps = float(context.scene.bamod_clip_fps or 24.0)
        mode = context.scene.bamod_clip_mode or "all"
        skip = [x.strip() for x in (context.scene.bamod_clip_skip or "").split(",")
                if x.strip()]
        n_obj, n_key, missing, n_bone, skipped = ca.apply_to_scene(
            context.scene, ci, fps=fps, mode=mode, skip_names=skip)
        if not n_obj and not n_bone:
            self.report({"ERROR"},
                        "没找到对应节点（先在 ① 面板把这个模型导入进来）：%s"
                        % ", ".join(missing[:4]))
            return {"CANCELLED"}
        tip = ("；%d 个节点没找到：%s" % (len(missing), ", ".join(missing[:3]))
               if missing else "")
        if skipped:
            tip += "；子件刚性跟随（跳过 %d 个）：%s" % (len(skipped), ", ".join(skipped[:4]))
        bone_tip = ("，驱动骨骼 %d 根" % n_bone) if n_bone else \
            "（⚠ 没找到骨架：只有空物体在动、网格不会跟着动 —— 先用 ① 导入模型）"
        self.report({"INFO"}, "已导入片段「%s」：%d 个节点%s / %d 个关键帧 / %.2f 秒%s"
                    % (name, n_obj, bone_tip, n_key, ci.duration, tip))
        return {"FINISHED"}


class BAMOD_OT_ClipNodePos(bpy.types.Operator):
    bl_idname = "ba_mod.clip_node_pos"
    bl_label = "③ 查这个零件在车的哪儿（世界坐标）"
    bl_description = "把片段里动的节点连世界坐标一起打印出来（判断装车头/车尾/车顶）"
    bl_options = {"REGISTER"}

    def execute(self, context):
        name = _clip_pick(context)
        bundle, path, short = _clip_prefab(context)
        if not name:
            self.report({"ERROR"}, "先「① 列出模型自带的动画片段」再选一个")
            return {"CANCELLED"}
        try:
            ci = _clip_mod().read_clip(bundle, path, name)
        except Exception as e:  # noqa: BLE001
            self.report({"ERROR"}, "解码失败：%s" % e)
            return {"CANCELLED"}
        if ci is None:
            self.report({"ERROR"}, "读不到片段")
            return {"CANCELLED"}
        lines = []
        for b in ci.bindings:
            obj = _clip_mod().find_object(context.scene, b["path"])
            w = "（模型里找不到这个节点）"
            if obj is not None:
                p = obj.matrix_world.translation
                w = "(%.3f, %.3f, %.3f)" % (p.x, p.y, p.z)
            lines.append("%s [%s] %s" % (b["path"].split("/")[-1],
                                         "旋转" if b["kind"] == "rot" else
                                         "位置" if b["kind"] == "pos" else "缩放", w))
        self.report({"INFO"}, "%s 动了 %d 个节点：%s"
                    % (name, len(ci.bindings), "；".join(lines[:5])))
        for ln in lines:
            print("[片段节点] %s" % ln)
        return {"FINISHED"}


class BAMOD_PT_Clip(bpy.types.Panel):
    bl_label = "⑦ 模型自带动画（防浪板 / 舱门 / 折叠）"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "BA Mod"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        r"""⑦ **主栏**：只放主流程 3 步（列片段 → 选片段 → 打成关键帧）。
        其余（查节点位置 / 帧率 / 说明）在默认折叠的子栏里 ✓ v1.8.87"""
        layout = self.layout
        scene = context.scene
        layout.label(text="模型自带的 Unity 片段（游戏里由 Trigger 播）", icon="ARMATURE_DATA")
        layout.operator("ba_mod.clip_refresh", icon="FILE_REFRESH")
        names = _clip_names(context)
        if names:
            layout.prop(scene, "bamod_clip_name", text="片段")
            layout.operator("ba_mod.clip_import", icon="TIME")
            layout.label(text="② 导入后拖时间轴即可看到它怎么动", icon="INFO")
        else:
            layout.label(text="先在 ① 面板选中模型，再点上面那个", icon="INFO")


class BAMOD_PT_Clip_More(bpy.types.Panel):
    """⑦ 的次要功能单独成栏（v1.8.87：多用折叠栏、默认折叠 ✓）。"""
    bl_label = "节点位置 / 时间轴选项 / 说明"
    bl_parent_id = "BAMOD_PT_Clip"
    bl_order = 10
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "BA Mod"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        layout.operator("ba_mod.clip_node_pos", icon="VIEWZOOM")
        layout.prop(scene, "bamod_clip_fps", text="时间轴帧率")
        layout.separator()
        col = layout.column(align=True)
        col.label(text="驱动范围（接缝被拉开时改这里）:", icon="MODIFIER")
        col.prop(scene, "bamod_clip_mode", text="")
        if scene.bamod_clip_mode == "skip":
            col.prop(scene, "bamod_clip_skip", text="跳过")
        layout.label(text="关键帧顺序/数值是解出来的真实值", icon="INFO")
        layout.label(text="时间戳解不出 ⇒ 关键帧按等间隔铺满真实时长", icon="BLANK1")
        layout.label(text="子件被单独驱动会把接缝拉开 ⇒ 选「只驱动主件」", icon="BLANK1")


# ===========================================================================
# ⑧ 组件字段 —— prefab 上**其它** MonoBehaviour 的字段也能改（v1.8.90）
# ---------------------------------------------------------------------------
# 为什么要有这一节：④ 只管 `AnimationHub` 里的"动画行为"。可自建模型真正缺入口的
# 是**其它组件**上的参数 —— 实测 `US_ACV` 上就有：
#   · `AnimationManager._container._settings` → `WheelSpeed` / `SuspensionSpeed` /
#     `BodyShake`（车身抖动）/ `BodyShakeAggressive` / `BodyMass`（车速与抖动的真实来源）
#   · `UnitPrefabTurretInfo` / `FmodTurretsTurn` / `SkinStorageBridge` /
#     `EffectsSpawnPoint` / `ContainerSeatInitializer` / `DecalProjector` …
# 这些东西在 dump.cs 里**有完整定义**、在字节里**能原样解析**（往返一致），
# 只是从来没人把它们接出来 ⇒ 这一节统一接上同一套元数据驱动的字段编辑器。
#
# 判定标准（同 ④）：**写回后与原字节完全一致**才算"可编辑"。不一致的只列出来、
# 明确标红说明为什么，绝不假装能改（那是造静默错误）。
# ===========================================================================

def _comp_prefab(context, prefs):
    """取「复制源 prefab」的对象集 + 真正的根 pid。

    ⛔ 必须走 `collect_prefab_objects(..., want_path=...)`：导入 `.bamod` 会把容器条目的
       asset pid 改指**新分配的**根 pid，光用场景里记的旧 pid 会找不到（④ 已踩过这个坑）✓
    """
    import copy_full
    src = context.scene.get("ba_copy_source_pid")
    if not src:
        raise RuntimeError("请先 ① 导入一个 prefab（它会记录「复制源」）")
    want = context.scene.get("ba_copy_source_path", "")
    objects, _pre, path = copy_full.collect_prefab_objects(prefs.bundle, int(src),
                                                          want_path=want)
    if not objects:
        raise RuntimeError("复制源的组件清单是空的 —— 偏好里的 bundle 指对了吗？")
    root = copy_full.container_root_pid(prefs.bundle, path) or int(src)
    return objects, root


def _comp_raw_of(context, prefs, pid):
    """按 pid 取该组件的原始字节（读取 / 写回都要用）。"""
    import base64
    objects, _root = _comp_prefab(context, prefs)
    for o in objects:
        if int(o["pid"]) == int(pid) and o.get("type_name") == "MonoBehaviour":
            return base64.b64decode(o["raw"]), objects
    raise RuntimeError("复制源里已经没有 pid=%s 这个组件了（模型被重新导入过？）"
                       "—— 请点「扫描组件」重新扫一遍" % pid)


def _comp_edits(context):
    """已写回待构建的组件改动 {pid字符串: {cls, node, values}}。"""
    import json as _json
    try:
        d = _json.loads(context.scene.get("bamod_comp_edits", "{}") or "{}")
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _comp_edits_set(context, d):
    import json as _json
    context.scene["bamod_comp_edits"] = _json.dumps(d, ensure_ascii=False)


def _comp_selected(context):
    """当前选中的 COMP_Item（没有就返回 None）。"""
    try:
        i = int(context.scene.bamod_comp_index)
        if 0 <= i < len(context.scene.bamod_comps):
            return context.scene.bamod_comps[i]
    except Exception:  # noqa: BLE001
        pass
    return None


def _comp_pick_items(self, context):
    """组件下拉：`[节点路径] 类名 · N 字段`（动态枚举，必须走缓存，见 `_anim_cached_items`）。"""
    scene = context.scene
    rows = []
    try:
        for i, c in enumerate(scene.bamod_comps):
            if not c.ok and c.nfields:
                tag = "✗%d 字段" % c.nfields
            elif not c.nfields:
                tag = "无字段"
            else:
                tag = "%d 字段" % c.nfields
            rows.append((i, c.node, c.cls, tag))
    except Exception:  # noqa: BLE001
        rows = []

    def build():
        out = []
        for i, node, cls, tag in rows:
            # 节点路径太长会把下拉撑爆 ⇒ 留尾部
            out.append((str(i), "[%s] %s · %s" % (node[-40:], cls, tag), ""))
        if not out:
            out = [("0", "（还没有扫描 —— 点上面的「扫描组件」）", "")]
        return out

    return _anim_cached_items(("comp_pick", tuple(rows), str(scene.bamod_comp_index)), build)


def _comp_pick_changed(self, context):
    r"""下拉一改就写回 `bamod_comp_index` —— 下标始终是**唯一权威**。

    ⛔ 与 ④ 的 `pick`/`type` 同一个坑：Blender 的枚举属性**总是有值**（默认第一项），
       所以 `pick or index` 那种写法会让脚本/测试传进来的 `bamod_comp_index` 被静默忽略 ✗。
       这里反过来：下拉只是选择器，值写进 `bamod_comp_index`；其余代码只读 `bamod_comp_index` ✓
    """
    try:
        self.bamod_comp_index = int(self.bamod_comp_pick)
    except Exception:  # noqa: BLE001
        pass


class COMP_Item(bpy.types.PropertyGroup):
    """prefab 上**一个具体组件**（一个 MB 实例）的编辑状态。

    ⛔ `pid` / `gpid` / `script_pid` 必须是**字符串**：Unity 的 pathID 是 int64，
       实测 `US_ACV` 的根 pid = 6302881677221457888 > 2^31，塞进 Blender 的
       IntProperty（32 位）会**溢出成负数**，之后按 pid 找对象就永远找不到 ✗
    """
    node: bpy.props.StringProperty(name="节点路径", default="")
    cls: bpy.props.StringProperty(name="类名", default="")
    # 与 ANIM_Behavior 同名：⑧ 直接复用 ④ 那套字段代码时按 `raw_type or type` 取类名
    raw_type: bpy.props.StringProperty(name="类名(权威)", default="")
    pid: bpy.props.StringProperty(default="0")
    gpid: bpy.props.StringProperty(default="0")
    script_pid: bpy.props.StringProperty(default="0")
    size: bpy.props.IntProperty(default=0)
    nfields: bpy.props.IntProperty(default=0)
    tail: bpy.props.IntProperty(default=0)
    ok: bpy.props.BoolProperty(default=False)
    err: bpy.props.StringProperty(default="")
    values_json: bpy.props.StringProperty(default="")
    orig_json: bpy.props.StringProperty(default="")
    raw_only: bpy.props.BoolProperty(default=False)
    applied: bpy.props.BoolProperty(name="已写回", default=False)
    # fields 在 register() 里挂（ANIM_Field 必须先注册，见那里的说明）


class BAMOD_OT_CompScan(bpy.types.Operator):
    bl_idname = "ba_mod.comp_scan"
    bl_label = "扫描组件"
    bl_description = ("扫描「复制源 prefab」上的全部 MonoBehaviour，列出哪些字段能改"
                      "（判据：写回后与原字节完全一致）")
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        import json as _json
        prefs = _prefs(context)
        scene = context.scene
        unitypy_bridge.add_paths(prefs.rev_dir, prefs.unitypy_dir)
        try:
            import importlib
            import component_edit as CE
            importlib.reload(CE)
            objects, root = _comp_prefab(context, prefs)
            reg = _anim_reg()
            # 类名表要从**整个 bundle** 的 MonoScript 取（prefab 自己的对象集里没有 MonoScript）
            import copy_full
            _env, _sf, objs, by_pid = copy_full._load_env(prefs.bundle)
            sn = CE.script_names(objs)
            rows = CE.scan_components_raw(reg, objects, sn, root)
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            self.report({"ERROR"}, "扫描组件失败：%s" % e)
            return {"CANCELLED"}

        # 节点名 ↔ pid 两张表（⑧ **自己**一份，绝不覆盖 ④ 的全 bundle 表 —— 见 _node_key）
        try:
            _ro, tr_go, _go_tr, _fa, name, _cp = CE.maps_from_raw(objects)
            n2p, p2n = {}, {}
            for tpid, gpid in tr_go.items():
                nm = name.get(gpid, "")
                if not nm:
                    continue
                p2n[str(tpid)] = nm
                n2p.setdefault(nm, tpid)
            scene[_node_key("comp", "p")] = _json.dumps(n2p, ensure_ascii=False)
            scene[_node_key("comp", "n")] = _json.dumps(p2n, ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            print("[组件] 节点名表建立失败（节点引用字段会显示成数字）：%s" % e)

        flt = (scene.bamod_comp_filter or "").strip().lower()
        keep = [r for r in rows
                if (not flt) or (flt in (r["cls"] or "").lower())
                or (flt in (r["cls_full"] or "").lower())]
        scene.bamod_comps.clear()
        for r in keep:
            c = scene.bamod_comps.add()
            c.node = r["node"]
            c.cls = r["cls"]
            c.raw_type = r["cls"]
            # ⛔ 全用字符串存 pid（int64 会溢出，见 COMP_Item 的说明）
            c.pid = str(r["pid"])
            c.gpid = str(r["gpid"])
            c.script_pid = str(r["script_pid"])
            c.size = int(r["size"])
            c.nfields = int(r["nfields"])
            c.tail = int(r["tail"])
            c.ok = bool(r["ok"])
            c.err = r["err"]
        scene.bamod_comp_index = 0
        scene["bamod_comp_info"] = "%d/%d 个组件（可编辑 %d）" % (
            len(keep), len(rows), len([r for r in keep if r["ok"] and r["nfields"]]))
        if not keep:
            self.report({"WARNING"}, "没扫到组件（过滤词是 %r？）" % flt)
        else:
            self.report({"INFO"}, "扫描完成：%s —— %s"
                        % (_copy_source_name(context) or "?",
                           scene["bamod_comp_info"]))
        return {"FINISHED"}


class BAMOD_OT_CompRead(bpy.types.Operator):
    bl_idname = "ba_mod.comp_read"
    bl_label = "读取字段"
    bl_description = "把这个组件的当前字节解析成字段，填进下面的「通用字段」表"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        import json as _json
        c = _comp_selected(context)
        if c is None:
            self.report({"ERROR"}, "先扫描并选中一个组件")
            return {"CANCELLED"}
        if not c.cls:
            self.report({"ERROR"}, "这个组件的脚本不在 bundle 里（没有类名，无法解析）")
            return {"CANCELLED"}
        prefs = _prefs(context)
        unitypy_bridge.add_paths(prefs.rev_dir, prefs.unitypy_dir)
        try:
            import component_edit as CE
            reg = _anim_reg()
            raw, _objs = _comp_raw_of(context, prefs, c.pid)
            vals, _st = CE.parse_component(reg, c.cls, raw)
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            self.report({"ERROR"}, "读取失败：%s" % e)
            return {"CANCELLED"}
        n = len([k for k in vals if not k.startswith("__")])
        if n == 0:
            self.report({"WARNING"},
                        "%s 没有可编辑的序列化字段（%d 字节只能整段保留）"
                        % (c.cls, c.size))
        txt = _json.dumps(vals, ensure_ascii=False)
        c.values_json = txt
        c.orig_json = txt
        c.raw_only = False
        c.applied = str(c.pid) in _comp_edits(context)
        _anim_rebuild_fields(context, int(context.scene.bamod_comp_index), "comp")
        self.report({"INFO"}, "已读取 %s（%d 个字段）" % (c.cls, n))
        return {"FINISHED"}


class BAMOD_OT_CompApply(bpy.types.Operator):
    bl_idname = "ba_mod.comp_apply"
    bl_label = "写回模型"
    bl_description = ("把面板上的字段写成字节并**当场校验**（面板→字节→回读），"
                      "存入待构建的改动；构建时写进模型")
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        import json as _json
        c = _comp_selected(context)
        if c is None:
            self.report({"ERROR"}, "先扫描并选中一个组件")
            return {"CANCELLED"}
        try:
            vals = _json.loads(c.values_json or "{}")
        except Exception as e:  # noqa: BLE001
            self.report({"ERROR"}, "字段数据坏了（先点「读取字段」重置）：%s" % e)
            return {"CANCELLED"}
        if not vals:
            self.report({"ERROR"}, "还没有读取字段 —— 先点「读取字段」")
            return {"CANCELLED"}
        prefs = _prefs(context)
        unitypy_bridge.add_paths(prefs.rev_dir, prefs.unitypy_dir)
        try:
            import component_edit as CE
            reg = _anim_reg()
            raw, _objs = _comp_raw_of(context, prefs, c.pid)
            new, ok, diff = CE.verify_component(reg, c.cls, raw, vals)
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            self.report({"ERROR"}, "写回失败：%s" % e)
            return {"CANCELLED"}
        if not ok:
            # ⛔ 校验不过就**不许**入库：宁可报错，也不能往包里写一份自己都读不回来的字节
            self.report({"ERROR"},
                        "校验没通过（回读与面板不一致：%s）—— 已放弃，模型未被改动"
                        % "、".join(diff[:5]))
            return {"CANCELLED"}
        d = _comp_edits(context)
        d[str(c.pid)] = {"cls": c.cls, "node": c.node, "values": vals, "size": len(new)}
        _comp_edits_set(context, d)
        c.applied = True
        self.report({"INFO"}, "%s 已写回（%d → %d 字节，回读一致 ✓）构建模型时生效"
                    % (c.cls, len(raw), len(new)))
        return {"FINISHED"}


class BAMOD_OT_CompRevert(bpy.types.Operator):
    bl_idname = "ba_mod.comp_revert"
    bl_label = "撤销写回"
    bl_description = "把这个组件的改动从待构建清单里去掉（模型恢复成原样）"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        c = _comp_selected(context)
        if c is None:
            self.report({"ERROR"}, "先选中一个组件")
            return {"CANCELLED"}
        d = _comp_edits(context)
        if str(c.pid) in d:
            d.pop(str(c.pid))
            _comp_edits_set(context, d)
            c.applied = False
            self.report({"INFO"}, "已撤销该组件的写回")
        else:
            self.report({"INFO"}, "这个组件本来就没有写回改动")
        return {"FINISHED"}


class BAMOD_OT_CompClear(bpy.types.Operator):
    bl_idname = "ba_mod.comp_clear"
    bl_label = "清空全部组件改动"
    bl_description = "把所有组件的待构建改动全部丢掉"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        n = len(_comp_edits(context))
        _comp_edits_set(context, {})
        try:
            for c in context.scene.bamod_comps:
                c.applied = False
        except Exception:  # noqa: BLE001
            pass
        self.report({"INFO"}, "已清空 %d 个组件的改动" % n)
        return {"FINISHED"}


# ---- ⑧ 的面板 -------------------------------------------------------------
class BAMOD_PT_Components(bpy.types.Panel):
    bl_label = "⑧ 组件字段（其它组件）"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "BA Mod"
    bl_order = 11
    # ★ 2026-10 第 74 轮（用户反馈）：**所有折叠栏默认都该是折叠的** ⇒ ⑧ 之前漏了这一行、
    #   全插件只有它默认展开 ✗（用 `技术资料\scripts\audit_addon_panels.py` 在真 Blender 里扫出来的）
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        src = _copy_source_name(context)
        box = layout.box()
        box.label(text="复制源：%s" % (src or "（还没 ① 导入）"),
                  icon="FILE_TILES" if src else "ERROR")
        row = box.row(align=True)
        row.prop(scene, "bamod_comp_filter", text="", icon="VIEWZOOM")
        row.operator("ba_mod.comp_scan", text="扫描组件", icon="FILE_REFRESH")
        info = scene.get("bamod_comp_info", "")
        if info:
            box.label(text=info, icon="INFO")
        if not len(scene.bamod_comps):
            box.label(text="扫描后这里会按节点列出所有能改的组件", icon="BLANK1")
            layout.label(text="这里改的是**其它组件**；动画行为在 ④", icon="INFO")
            return

        c = _comp_selected(context)
        layout.prop(scene, "bamod_comp_pick", text="组件")
        if c is None:
            return
        col = layout.column(align=True)
        col.label(text="节点：%s" % c.node[-48:], icon="OUTLINER_OB_EMPTY")
        col.label(text="类：%s   字节：%d   字段：%d" % (c.cls or "?", c.size, c.nfields))
        if not c.ok and c.nfields:
            col.label(text="⚠ %s —— 只列不改" % c.err, icon="ERROR")
        elif not c.nfields:
            col.label(text="该组件没有可编辑字段（整段字节原样保留）", icon="LOCKED")
        elif c.tail:
            # ⛔ 有"无对应字段的尾部字节"时必须**说出来**：这些字节会原样写回去
            #    （实测 `WeaponPrefabInfo` 45 个实例、`AnimationManagerBridge` 98 个实例
            #    都恒定多出 4 个 `00 00 00 00`，dump.cs 里没有对应字段）✓
            col.label(text="（末尾 %d 字节无对应字段，会原样保留）" % c.tail, icon="INFO")
        # 改动状态
        dirty = bool(c.values_json) and c.values_json != c.orig_json
        if c.applied:
            col.label(text="已写回 ✓（构建时生效）", icon="CHECKMARK")
        elif dirty:
            col.label(text="面板上有未写回的改动", icon="GREASEPENCIL")
        row = layout.row(align=True)
        row.operator("ba_mod.comp_read", icon="IMPORT")
        sub = row.row(align=True)
        sub.enabled = bool(c.nfields)
        sub.operator("ba_mod.comp_apply", icon="EXPORT")
        sub.operator("ba_mod.comp_revert", text="", icon="LOOP_BACK")
        if c.cls == "AnimationHub":
            layout.label(text="AnimationHub 的动画行为请用 ④ 面板（这里只显示字段数）",
                         icon="INFO")


class BAMOD_PT_Comp_Fields(bpy.types.Panel):
    """⑧ 的通用字段表（和 ④ 用的是**同一套**控件，DEFAULT_CLOSED）。"""
    bl_label = "通用字段（全部字段都在这里）"
    bl_parent_id = "BAMOD_PT_Components"
    bl_order = 0
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "BA Mod"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        c = _comp_selected(context)
        if c is None:
            self.layout.label(text="（没选中组件）", icon="INFO")
            return
        if not c.values_json:
            self.layout.label(text="先点「读取字段」", icon="INFO")
            return
        _draw_anim_item_fields(self.layout, context,
                               int(context.scene.bamod_comp_index), c, "comp")


class BAMOD_PT_Comp_Done(bpy.types.Panel):
    """已写回的组件清单（构建时会被写进模型）。"""
    bl_label = "已写回的组件"
    bl_parent_id = "BAMOD_PT_Components"
    bl_order = 1
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "BA Mod"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        d = _comp_edits(context)
        if not d:
            layout.label(text="（还没有写回任何组件）", icon="INFO")
            return
        col = layout.column(align=True)
        for _pid, e in sorted(d.items(), key=lambda kv: kv[1].get("node", "")):
            n = len([k for k in (e.get("values") or {}) if not k.startswith("__")])
            col.label(text="[%s] %s · %d 字段"
                      % ((e.get("node") or "")[-28:], e.get("cls") or "?", n),
                      icon="CHECKMARK")
        layout.operator("ba_mod.comp_clear", icon="TRASH")


CLASSES = [
    BAModPreferences,
    PREFAB_Item, MOUNT_Item, SKIN_Item,
    PREFAB_UL, MOUNT_UL, SKIN_UL,
    BAMOD_OT_AutoDetect,
    BAMOD_OT_RefreshPrefabs, BAMOD_OT_ImportPrefab,
    BAMOD_OT_RefreshMounts, BAMOD_OT_SelectMount,
    BAMOD_OT_AddMount, BAMOD_OT_RemoveMount,
    # ★⑫/★⑬ v2.7.118：游戏坐标（显示 / 输入反算 loc / 对齐网格中心 / 摊平父级逆变换）
    BAMOD_OT_SetGameCoord, BAMOD_OT_AlignToMeshCenter, BAMOD_OT_FlattenParentInverse,
    BAMOD_OT_SelfcheckXform,        # ★25 v1.12.4 候选：一键一致性自检（与摊平按钮同一面板并排）
    BAMOD_OT_ComputeCRC, BAMOD_OT_TransferWeights,
    BAMOD_OT_BuildModel, BAMOD_OT_ReadAnimation,
    BAMOD_OT_AddAnimation, BAMOD_OT_RemoveAnimation,
    BAMOD_OT_PickAnimNode, BAMOD_OT_AddRotorTorque, BAMOD_OT_AddFloatEffect,
    BAMOD_OT_AnimPickNode, BAMOD_OT_AnimListEdit,
    # v1.8.67 —— 高灵活度动画编辑（复制 / 换数组 / 模板 / 导入导出 / 跨 prefab 抄 / 校验）
    BAMOD_OT_DupAnimation, BAMOD_OT_MoveAnimation,
    BAMOD_OT_ApplyAnimPreset, BAMOD_OT_ClearAnims,
    BAMOD_OT_ExportAnim, BAMOD_OT_ImportAnim, BAMOD_OT_ImportAnimFromPrefab,
    BAMOD_OT_SetCopySource,
    BAMOD_OT_SyncAnimText, BAMOD_OT_ValidateAnim,
    BAMOD_OT_ScanSkins, BAMOD_OT_ApplyDefaultMats, BAMOD_OT_ApplySkin,
    BAMOD_OT_ExportSkinTextures, BAMOD_OT_PackSkin, BAMOD_OT_PackMatSwap,
    # ★ v1.9.1（★⑮）：候选贴图（流式引用那条路）
    STREAMTEX_Item, STREAMTEX_UL, BAMOD_OT_ScanStreamTextures,
    # ⛔ v1.8.52：`BAMOD_OT_RefreshPoseBones` 已随 ⑥ 步兵姿势整段删除 ✓（此处引用一并移除 ✓）
    # ⛔ v1.8.51 已注销（步兵动画导入下线 ✓）：
    #   BAMOD_OT_ImportAnimation（导入动画到时间轴 ✗）
    #   BAMOD_OT_RefreshPoseClips（刷新动画列表 ✗）
    #   ⇒ 面板入口已移除 ✓，算子也不再注册 ✓（F3 搜索里也搜不到 ✓）
    ANIM_Field, ANIM_Behavior, COMP_Item,
    BAMOD_PT_Import, BAMOD_PT_Mounts, BAMOD_PT_Tools,
    # ⑥步兵姿势 面板（BAMOD_PT_Pose*）已按需求下线：姿势本质是动画，改用 ④ 里的「导入动画到时间轴」。
    # 相关算子仍保留（F3 搜索里可调），只是不再有面板入口。
    BAMOD_OT_PaintSetup, BAMOD_OT_PaintBoard, BAMOD_OT_PaintStamp, BAMOD_OT_PaintReload,
    BAMOD_OT_PaintSave, BAMOD_OT_PaintReset,
    BAMOD_PT_Animation, BAMOD_PT_Skin, BAMOD_PT_Skin_List, BAMOD_PT_Skin_Tex, BAMOD_PT_Skin_Pack,
    BAMOD_PT_Paint,
    BAMOD_PT_Paint_Stamp, BAMOD_PT_Paint_File, BAMOD_PT_Paint_Reset,
    # ⑦ 模型自带动画片段（v1.8.85）：列片段 / 打成关键帧 / 查节点位置
    BAMOD_OT_ClipRefresh, BAMOD_OT_ClipImport, BAMOD_OT_ClipNodePos, BAMOD_PT_Clip,
    BAMOD_PT_Clip_More,
    # v1.8.68：④ 的折叠子栏（全部 DEFAULT_CLOSED）。子栏必须在父栏**之后**注册。
    BAMOD_PT_Anim_Create, BAMOD_PT_Anim_Add, BAMOD_PT_Anim_Set, BAMOD_PT_Anim_Help,
    # ⑧ 组件字段（v1.8.90）：其它 MonoBehaviour 的字段也能改。
    # ⛔ 子栏（Comp_Fields / Comp_Done）必须排在父栏 BAMOD_PT_Components **之后** ✓
    BAMOD_OT_CompScan, BAMOD_OT_CompRead, BAMOD_OT_CompApply,
    BAMOD_OT_CompRevert, BAMOD_OT_CompClear,
    BAMOD_PT_Components, BAMOD_PT_Comp_Fields, BAMOD_PT_Comp_Done,
]


def register():
    bpy.utils.register_class(BAMOD_OT_CopyPath)
    import importlib
    import sys
    for mod in ("blender_addon.unitypy_bridge", "blender_addon.mount_dict"):
        if mod in sys.modules:
            try:
                importlib.reload(sys.modules[mod])
            except Exception:
                pass
    for cls in CLASSES:
        try:
            bpy.utils.register_class(cls)
        except ValueError:
            pass  # 已注册（重复加载场景）
        except Exception as e:  # noqa: BLE001
            # ⛔ 以前只捕 ValueError：中途某个类注册失败会留下**半注册**状态，
            #    而且异常冒泡出去让整个插件"启用失败"却看不出是哪个类 ✗
            print("[BA Mod] 注册 %s 失败（已跳过）：%s: %s"
                  % (getattr(cls, "__name__", cls), type(e).__name__, e))
    # v1.8.87：④ 的「每条行为一个真子面板」是**动态注册**的 ⇒ 开图/启用插件后要按
    #   当前场景的行为列表重建一次，否则会把已有行为"看不见"（面板子栏缺失）✗
    try:
        bpy.app.handlers.load_post.append(_anim_load_handler)
    except Exception as e:  # noqa: BLE001
        print("[BA Mod] 挂 load_post 处理器失败（忽略）：%s" % e)
    try:
        _anim_rebuild_panels(bpy.context)
    except Exception as e:  # noqa: BLE001
        print("[BA Mod] 启动时重建行为子面板失败（忽略）：%s" % e)
    # ★[路径-02] v2.7.118：把**落在 C 盘 / 插件目录内**的**输出**路径迁到 D 盘
    #   （v2.7.113 只改了默认值，**已存在的老值不会被自动修** ⇒ 用户 C 盘上会攒 GB 级产物）
    try:
        _moved = _migrate_paths_off_c()
        if _moved:
            print("[BA Mod] 已自动迁移输出路径（%s）—— 原来在 C 盘/插件目录里" % ", ".join(_moved))
    except Exception as e:  # noqa: BLE001
        print("[BA Mod] 路径迁移失败（忽略，不影响使用）：%s" % e)
    if not hasattr(bpy.types.Scene, "bamod_prefabs"):
        bpy.types.Scene.bamod_prefabs = bpy.props.CollectionProperty(type=PREFAB_Item)
    bpy.types.Scene.bamod_prefab_index = bpy.props.IntProperty()
    if not hasattr(bpy.types.Scene, "bamod_mounts"):
        bpy.types.Scene.bamod_mounts = bpy.props.CollectionProperty(type=MOUNT_Item)
    bpy.types.Scene.bamod_mount_index = bpy.props.IntProperty()
    if not hasattr(bpy.types.Scene, "bamod_anims"):
        bpy.types.Scene.bamod_anims = bpy.props.CollectionProperty(type=ANIM_Behavior)
    # 通用字段集合：必须在 ANIM_Field **注册之后**才能挂（见 ANIM_Behavior 处的说明）
    if not hasattr(ANIM_Behavior, "fields"):
        ANIM_Behavior.fields = bpy.props.CollectionProperty(type=ANIM_Field)
    bpy.types.Scene.bamod_anim_index = bpy.props.IntProperty()
    if not hasattr(bpy.types.Scene, "bamod_skins"):
        bpy.types.Scene.bamod_skins = bpy.props.CollectionProperty(type=SKIN_Item)
    bpy.types.Scene.bamod_skin_index = bpy.props.IntProperty()
    bpy.types.Scene.bamod_skin_tex_dir = bpy.props.StringProperty(
        name="贴图目录", subtype="DIR_PATH",
        description="皮肤贴图 PNG 所在目录（导出到这里编辑，打包时读这里）")
    bpy.types.Scene.bamod_skin_new_id = bpy.props.IntProperty(
        name="新增皮肤 Id", default=0, min=0,
        description="0=重涂原皮肤槽；>0=复制为新皮肤槽（菜单显示需另改 UserItemsConfig）")
    bpy.types.Scene.bamod_skin_pack_path = bpy.props.StringProperty(
        name="皮肤包路径", subtype="FILE_PATH",
        description="皮肤 .bamod 输出路径（在 BA_Mod_Maker 里导入）")
    bpy.types.Scene.bamod_matswap_pack_path = bpy.props.StringProperty(
        name="贴图替换包路径", subtype="FILE_PATH",
        description="枪械/模型贴图替换 .bamod 输出路径（在 BA_Mod_Maker 里导入）")
    # ★★ v1.9.1（★⑮）：只对选中对象生效 + 贴图来源两条路
    bpy.types.Scene.bamod_matswap_selected_only = bpy.props.BoolProperty(
        name="只对选中对象生效", default=False,
        description="勾上 = 只改**视口里选中**的那些网格的材质（移植零件的场景）；"
                    "**不勾 = 整车**一起换（默认，与旧版一致）")
    bpy.types.Scene.bamod_matswap_tex_source = bpy.props.EnumProperty(
        name="贴图来源", default="png",
        items=[("png", "PNG 内嵌（编辑过的贴图）", "把导出/编辑好的 PNG 打进包（RGBA32，占体积）"),
               ("stream", "流式引用（原机贴图，包最小）",
                "只建一个 Texture2D，m_StreamData 指向游戏 .resS 里的那一段 —— 不搬像素，包只大几十 KB")],
        description="★⑮ 的两条贴图来源")
    if not hasattr(bpy.types.Scene, "bamod_stream_texts"):
        bpy.types.Scene.bamod_stream_texts = bpy.props.CollectionProperty(type=STREAMTEX_Item)
    bpy.types.Scene.bamod_stream_tex_index = bpy.props.IntProperty()
    bpy.types.Scene.bamod_stream_query = bpy.props.StringProperty(
        name="搜索", description="按名字的一部分搜游戏包里的**流式**贴图（例如 AH-1Z_viper）")
    bpy.types.Scene.bamod_matswap_stream_slot = bpy.props.StringProperty(
        name="槽名", description="要换的纹理槽名；留空 = 自动挑基准色槽（贴图名带 BaseMap 的那个）")
    bpy.types.Scene.bamod_paint_stamp = bpy.props.StringProperty(
        name="印章图片", subtype="FILE_PATH",
        description="要贴到车上的图片（⑥ 涂装 → ③ 贴图片到车上）")
    # v1.8.83：盖印参数（都带默认值 ⇒ 老 .blend 打开也能用 ✓）
    bpy.types.Scene.bamod_paint_fill = bpy.props.BoolProperty(
        name="整张铺满", default=False,
        description="用这张图铺满整张皮肤（直接换皮）；关掉就是按下面的位置/大小盖印")
    bpy.types.Scene.bamod_paint_stamp_x = bpy.props.FloatProperty(
        name="左右", default=0.5, min=-1.0, max=2.0, subtype="FACTOR",
        description="印章中心在贴图上的横向位置（0=最左，1=最右）")
    bpy.types.Scene.bamod_paint_stamp_y = bpy.props.FloatProperty(
        name="上下", default=0.5, min=-1.0, max=2.0, subtype="FACTOR",
        description="印章中心在贴图上的纵向位置（0=最下，1=最上）")
    bpy.types.Scene.bamod_paint_stamp_scale = bpy.props.FloatProperty(
        name="宽度", default=0.35, min=0.005, max=4.0, subtype="FACTOR",
        description="印章宽度占贴图宽度的比例（0.35=贴图宽度的 35%）")
    bpy.types.Scene.bamod_paint_stamp_rot = bpy.props.FloatProperty(
        name="旋转", default=0.0, min=-360.0, max=360.0,
        description="印章旋转角度（度，逆时针）")
    bpy.types.Scene.bamod_paint_stamp_alpha = bpy.props.FloatProperty(
        name="不透明度", default=1.0, min=0.0, max=1.0, subtype="FACTOR",
        description="盖印的不透明度（1=完全盖住原皮肤；0.2=淡淡的贴一层）")
    bpy.types.Scene.bamod_paint_stamp_feather = bpy.props.FloatProperty(
        name="羽化", default=0.0, min=0.0, max=1.0, subtype="FACTOR",
        description="边缘羽化宽度（占印章短边的比例，0=硬边；0.1 左右比较自然）")
    # ⑦ 模型自带动画片段（v1.8.85）
    bpy.types.Scene.bamod_clip_name = bpy.props.EnumProperty(
        name="片段", items=_clip_enum_items,
        description="这个 prefab 自带的 Unity AnimationClip（防浪板/舱门/折叠/装卸载…）")
    bpy.types.Scene.bamod_clip_fps = bpy.props.FloatProperty(
        name="时间轴帧率", default=24.0, min=1.0, max=120.0,
        description="打成关键帧时用的帧率（片段时长 × 帧率 = 帧数）")
    # v1.8.89：驱动范围 —— 处理"连接杆看起来断开"（子件被单独驱动、接缝被拉开）
    bpy.types.Scene.bamod_clip_mode = bpy.props.EnumProperty(
        name="驱动范围", default="all",
        items=[("all", "全部按片段（和游戏一致）",
                "每个被片段绑定的节点都按片段的数值驱动 —— 最忠实，但片段里子件与父件转动不一致时，"
                "接缝会被拉开（看起来像「连接杆断开」）"),
               ("leaf", "只驱动主件（子件刚性跟随）",
                "父件被驱动时，它的子件不再单独动 ⇒ 接缝保持贴合（修「连接杆断开」）；"
                "代价是子件的过场动作与游戏不同"),
               ("skip", "跳过指定节点（下面填名字）",
                "只跳过下面列出的节点，其余照片段驱动")],
        description="决定哪些节点按片段驱动。默认「全部按片段」= 和游戏一致 ✓")
    bpy.types.Scene.bamod_clip_skip = bpy.props.StringProperty(
        name="跳过的节点", default="",
        description="逗号分隔的节点名（只在下拉选「跳过指定节点」时生效），例如：Shield_01,Shield_help")
    # ---- ⑧ 组件字段（v1.8.90）------------------------------------------------
    # ⛔ `bamod_comps` 里的 `fields` 同样要在 ANIM_Field 注册**之后**才挂（理由见
    #    ANIM_Behavior 处的说明：类体是在 import 时求值的，那时谁都没注册 ✗）
    if not hasattr(bpy.types.Scene, "bamod_comps"):
        bpy.types.Scene.bamod_comps = bpy.props.CollectionProperty(type=COMP_Item)
    if not hasattr(COMP_Item, "fields"):
        COMP_Item.fields = bpy.props.CollectionProperty(type=ANIM_Field)
    bpy.types.Scene.bamod_comp_index = bpy.props.IntProperty()
    bpy.types.Scene.bamod_comp_filter = bpy.props.StringProperty(
        name="类名过滤", default="",
        description="只列出类名里含这几个字的组件（留空=全部）。"
                    "扫描时生效，所以改完要再点一次「扫描组件」")
    bpy.types.Scene.bamod_comp_pick = bpy.props.EnumProperty(
        name="组件", items=_comp_pick_items, update=_comp_pick_changed,
        description="当前编辑的组件（显示为 [节点路径] 类名 · 字段数）")
    # ⛔ v1.8.67：注册时就把 `_rev_tools` / `_unitypy` 加进 sys.path。
    #    以前只有**算子执行时**才加（`unitypy_bridge.add_paths`）⇒ 刚启用插件、还没点过
    #    任何按钮就打开 ④ 面板时，`import behavior_dict / behavior_meta` 会失败：
    #    类型下拉只剩 5 个内置项、字段中文标签出不来（**看起来像功能没做**）✗。
    #    放在这里之后，面板一打开就是完整的 24 类 + 中文标签 ✓。
    try:
        _own = os.path.dirname(os.path.abspath(__file__))
        unitypy_bridge.add_paths(os.path.join(_own, "_rev_tools"),
                                 os.path.join(_own, "_unitypy"))
    except Exception as e:  # noqa: BLE001
        print("[BA Mod] 注册期加 sys.path 失败（不影响，算子运行时会再补）：%s" % e)
    # v1.8.75：自动备份开关与 BA_Mod_Maker 同步（同一份 settings.json）——
    #   在另一边改过之后，这里显示的才是**实际生效**的状态。
    try:
        _b = _bp()
        if _b is not None:
            _stored = bool(_b.backup_enabled())
            _pf = _prefs(bpy.context)
            # 只在真的不一致时才赋值：赋值会触发 update 回调去写共享配置，
            # 而"每次注册都回写一遍"是没必要的副作用 ✗
            if bool(_pf.auto_backup) != _stored:
                _pf.auto_backup = _stored
    except Exception:  # noqa: BLE001
        pass
    # ⛔⛔ v1.8.52：⑥ 步兵姿势 / 步兵动画的**全部场景属性**已随代码段删除 ✓
    #   原内容：`bamod_pose_bones/lib/clips`（引用 `POSE_Bone/POSE_Item/POSE_Clip` ✗）、
    #          `bamod_pose_frame/relative/new_name/rot/loc/prefab_path/pack_path`、
    #          `bamod_anim_all/anim_step`（④ 步兵动画导入选项 ✓）
    #   ⇒ 一并移除 ✓（其引用的数据类与 update 回调 `_on_pose_rot/_on_pose_loc` 都已随段删除 ✗，
    #      留着会 `NameError: POSE_Bone is not defined` ✗ —— 真机加载测试抓到的 ✓✓）


def unregister():
    # ⛔ v1.8.87：动态生成的「每条行为」子面板必须在**父面板之前**注销
    #    （先撤子栏、再撤父栏；反了 Blender 会拒绝注销父面板 ✗）
    _anim_drop_panels()
    try:
        if _anim_load_handler in bpy.app.handlers.load_post:
            bpy.app.handlers.load_post.remove(_anim_load_handler)
    except Exception:  # noqa: BLE001
        pass
    for prop in ("bamod_prefabs", "bamod_prefab_index", "bamod_mounts", "bamod_mount_index",
                 "bamod_anims", "bamod_anim_index", "bamod_skins", "bamod_skin_index",
                 "bamod_skin_tex_dir", "bamod_skin_new_id", "bamod_skin_pack_path",
                 "bamod_matswap_pack_path", "bamod_paint_stamp", "bamod_paint_fill",
                 # ★ v1.9.1（★⑮）：只对选中对象生效 / 贴图来源 / 候选贴图池
                 "bamod_matswap_selected_only", "bamod_matswap_tex_source",
                 "bamod_stream_texts", "bamod_stream_tex_index",
                 "bamod_stream_query", "bamod_matswap_stream_slot",
                 "bamod_paint_stamp_x", "bamod_paint_stamp_y", "bamod_paint_stamp_scale",
                 "bamod_paint_stamp_rot", "bamod_paint_stamp_alpha",
                 "bamod_paint_stamp_feather",
                 "bamod_clip_name", "bamod_clip_fps", "bamod_clip_mode", "bamod_clip_skip",
                 "bamod_comps", "bamod_comp_index", "bamod_comp_filter",
                 "bamod_comp_pick"):
        # ⛔ v1.8.52：此处原有的 `bamod_pose_*` / `bamod_anim_*` 属性名已随
        #   ⑥ 步兵姿势 / 步兵动画整段删除 ✓（属性本身也已不再注册 ✓）
        if hasattr(bpy.types.Scene, prop):
            delattr(bpy.types.Scene, prop)
    # ⛔ 动态挂在 ANIM_Behavior 上的 `fields` 以前没删：同进程再次 register() 时
    #    `hasattr` 为真就跳过重建，而它可能还指向**上一次的旧 RNA 类型** ✗
    try:
        if hasattr(ANIM_Behavior, "fields"):
            delattr(ANIM_Behavior, "fields")
    except Exception as e:  # noqa: BLE001
        print("[BA Mod] 清理 ANIM_Behavior.fields 失败（忽略）：%s" % e)
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()




