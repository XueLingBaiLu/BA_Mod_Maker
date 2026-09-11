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
bl_info = {
    "name": "BA Mod Maker（断箭模型工具）",
    "author": "BA Mod Maker",
    "version": (2, 7, 54),  # AUTO-SYNC from version.py
    "blender": (4, 0, 0),
    "location": "3D View > 侧边栏 > BA Mod",
    "description": "提取游戏模型、可视化编辑挂载点、构建写回（全类型模型）",
    "category": "Import-Export",
}

try:
    from .version import ADDON_VERSION
    if tuple(ADDON_VERSION) != tuple(bl_info["version"]):
        print("[BA Mod] 警告：bl_info 版本 %s 与 version.py %s 不一致，"
              "请用 _rebuild_addon_zip.py 重新打包" % (bl_info["version"], ADDON_VERSION))
except ImportError:
    ADDON_VERSION = tuple(bl_info["version"])

import bpy
import glob
import os
import time

from mathutils import Matrix, Quaternion, Vector, Euler

# Unity Y-up -> Blender Z-up 的基变换：C @ [x,y,z]^T = [x,-z,y]^T（正交，C^-1 = C^T）
_UNITY_TO_BLENDER_M = Matrix(((1, 0, 0, 0), (0, 0, -1, 0), (0, 1, 0, 0), (0, 0, 0, 1)))

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
class BAModPreferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    unitypy_dir: bpy.props.StringProperty(name="UnityPy 目录 (_unitypy)", subtype="DIR_PATH")
    rev_dir: bpy.props.StringProperty(name="工具脚本目录 (_rev_tools)", subtype="DIR_PATH")
    bundle: bpy.props.StringProperty(name="游戏 bundle 文件", subtype="FILE_PATH")
    out_bundle: bpy.props.StringProperty(
        name="输出（.bamod 素材包 / .bundle）", subtype="FILE_PATH")
    new_prefab: bpy.props.StringProperty(
        name="新 prefab 内部路径",
        default="Assets/Resources_moved/ModelPrefabs/RU/Turrets/MY_TURRET/MY_TURRET.prefab",
    )
    new_name: bpy.props.StringProperty(name="新 prefab 名字", default="MY_TURRET")
    search: bpy.props.StringProperty(name="搜索", default="")
    mount_new_name: bpy.props.StringProperty(name="挂载点名字", default="")
    cat_filter: bpy.props.EnumProperty(
        name="分类",
        items=lambda self, ctx: [("ALL", "全部", "")] + [(c["id"], c["name"], c["desc"]) for c in MOUNT_CATEGORIES],
    )
    only_lod0: bpy.props.BoolProperty(name="只导入 LOD0", default=True)
    copy_full: bpy.props.BoolProperty(
        name="完整复制模式（保留原组件/材质/动画）",
        description="勾选后：① 导入过的 prefab 会整段字节复制，④ 只改网格和挂点，其余组件原样保留",
        default=True)
    apply_anim: bpy.props.BoolProperty(
        name="应用动画（从文本块读取 JSON）",
        description="勾选后构建时读取文本块 bamod_anim 的动画 JSON 并写回 AnimationHub",
        default=False)
    build_pack: bpy.props.BoolProperty(
        name="导出 .bamod 素材包（分发用，不写完整 bundle）",
        description="勾选后构建只输出小包（KB~MB 级），用 BA_Mod_Maker 的素材导入合并进游戏",
        default=True)

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
    except Exception:
        pass
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
            prefs.out_bundle = os.path.join(outdir or os.path.dirname(os.path.abspath(__file__)),
                                            "units_turret_mod" + ext)
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
        for m in meshes:
            if prefs.only_lod0 and not m["is_lod0"]:
                continue
            try:
                geo = extract_model.extract_mesh_geometry(prefs.bundle, m["mesh"])
            except Exception as e:
                print("[导入] 网格 %d 读取失败: %s" % (m["mesh"], e))
                continue
            bone_names = [tree[b]["name"] if b in tree else "bone_%d" % i
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
        if parent and parent.type in ("EMPTY", "MESH"):
            e.parent = parent
        else:
            e.location = context.scene.cursor.location
        context.collection.objects.link(e)
        _refresh_mounts(context)
        cat = next((c for c in MOUNT_CATEGORIES if c["id"] == category_of(nm)), None)
        self.report({"INFO"}, "已添加 %s（%s）" % (nm, cat["name"] if cat else "挂载点"))
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
        kids = [c for c in o.children if c.type == "EMPTY" and c.get("ba_mount")]
        nm = o.name
        bpy.data.objects.remove(o, do_unlink=True)
        for k in kids:
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
                box.label(text="实际路径：", icon="FILE_TEXT")
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


# ---------------------------------------------------------------------------
# ④ 构建写回（场景 -> prefab）
# ---------------------------------------------------------------------------
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
            positions.append((p.x, p.z, -p.y))
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
        return {
            "name": _normalize_name(obj.name),
            "positions": positions, "triangles": triangles, "uv": uv,
            "bones": bones, "weights": weights, "bone_names": bone_names,
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
        if prefs.build_pack:
            out = out if out.endswith(".bamod") else (out.split(".")[0] if "." in os.path.basename(out) else out) + ".bamod"
        else:
            out = out if out.endswith(".bundle") else (out.split(".")[0] if "." in os.path.basename(out) else out) + ".bundle"
        unitypy_bridge.add_paths(prefs.rev_dir, prefs.unitypy_dir)
        try:
            source_pid = context.scene.get("ba_copy_source_pid")
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
                    int(source_pid), tree, meshes, hub_json=hub_json)
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
        if not prefs.build_pack:
            prefs.bundle = out
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
        layout.operator("ba_mod.build_model", icon="EXPORT")
        layout.separator()
        layout.operator("ba_mod.transfer_weights", icon="MOD_VERTEX_WEIGHT")
        layout.separator()
        layout.operator("ba_mod.compute_crc", icon="FILE_TICK")


class ANIM_Behavior(bpy.types.PropertyGroup):
    array: bpy.props.EnumProperty(name="数组", items=[
        ("universal", "universal", "通用（战场+军械库）"),
        ("demo", "demo", "军械库演示"),
        ("game", "game", "战场游戏"),
        ("preDeath", "preDeath", "死亡前"),
        ("death", "death", "死亡"),
    ])
    type: bpy.props.EnumProperty(name="类型", items=[
        ("AxisRandom", "AxisRandom", "随机扫掠"),
        ("MathConnect", "MathConnect", "跟随"),
    ])
    open: bpy.props.BoolProperty(
        name="展开此行为", default=False,
        description="折叠栏：勾选/点击箭头展开这个动画行为的编辑区（默认折叠）")
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


def _anims_to_dict(context):
    d = {"universal": [], "demo": [], "game": [], "preDeath": [], "death": []}
    for b in context.scene.bamod_anims:
        if b.rid:
            _rid = int(b.rid)
        else:
            _rid = None
        if b.type == "AxisRandom":
            item = {"type": "AxisRandom", "lod": b.lod, "name": "",
                    "speed": b.speed, "minTime": b.min_time, "maxTime": b.max_time,
                    "axes": {"x": {"source": b.x_source, "min": b.x_min, "max": b.x_max},
                             "y": {"source": b.y_source, "min": b.y_min, "max": b.y_max},
                             "z": {"source": b.z_source, "min": b.z_min, "max": b.z_max}}}
        else:
            item = {"type": "MathConnect", "lod": b.lod, "freq": b.freq, "damper": b.damper,
                    "reaction": b.reaction, "objBool": True, "root": b.root, "target": b.target,
                    "freeze": {"x": b.freeze_x, "y": b.freeze_y, "z": b.freeze_z}, "shots": []}
            if b.shots_b64:
                item["shotsRawB64"] = b.shots_b64
        if _rid is not None:
            item["_rid"] = _rid
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
            src_objects, _, _ = copy_full.collect_prefab_objects(prefs.bundle, int(source_pid))
            hub_raw = None
            for o in src_objects:
                if o["type_name"] == "MonoBehaviour":
                    raw = base64.b64decode(o["raw"])
                    if len(raw) >= 28 and struct.unpack_from("<q", raw, 20)[0] == hub_edit.HUB_SCRIPT:
                        hub_raw = raw
                        break
            if hub_raw is None:
                self.report({"ERROR"}, "这个 prefab 没有 AnimationHub（无动画）")
                return {"CANCELLED"}
            def name_of(pid):
                return h.name(h.tr_go.get(pid, 0)) if pid else ""
            d = hub_edit.hub_to_dict(hub_raw, name_of)
            context.scene["bamod_anim_order"] = ",".join(str(r) for r in d.get("_order", []))
            # 同步 JSON 到文本块（方便高级用户用 JSON 改）
            txt = bpy.data.texts.get("bamod_anim")
            if txt is None:
                txt = bpy.data.texts.new("bamod_anim")
            txt.clear()
            txt.write(hub_edit.hub_to_json(hub_raw, name_of, indent=1))
            # 填进面板集合
            context.scene.bamod_anims.clear()
            for arr, items in d.items():
                if arr == "_order":
                    continue
                for it in items:
                    b = context.scene.bamod_anims.add()
                    b.array = arr
                    b.rid = str(it.get("_rid", "")) if it.get("_rid") is not None else ""
                    if it.get("type") == "MathConnect":
                        b.type = "MathConnect"
                        b.lod = it.get("lod", 0)
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
                    else:
                        b.type = "AxisRandom"
                        b.lod = it.get("lod", 1)
                        b.speed = it.get("speed", 20)
                        b.min_time = it.get("minTime", 2)
                        b.max_time = it.get("maxTime", 10)
                        for ax in ("x", "y", "z"):
                            a = it.get("axes", {}).get(ax, {})
                            setattr(b, ax + "_source", a.get("source", ""))
                            setattr(b, ax + "_min", a.get("min", 0))
                            setattr(b, ax + "_max", a.get("max", 0))
            self.report({"INFO"}, "动画已读取：%d 个行为" % len(context.scene.bamod_anims))
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.report({"ERROR"}, "读取动画失败：%s" % e)
        return {"FINISHED"}


class BAMOD_OT_AddAnimation(bpy.types.Operator):
    bl_idname = "ba_mod.add_animation"
    bl_label = "添加动画行为"
    bl_options = {"REGISTER", "UNDO"}
    type: bpy.props.StringProperty(default="AxisRandom")

    def execute(self, context):
        b = context.scene.bamod_anims.add()
        b.type = self.type
        b.array = "demo"
        return {"FINISHED"}


class BAMOD_OT_RemoveAnimation(bpy.types.Operator):
    bl_idname = "ba_mod.remove_animation"
    bl_label = "删除动画行为"
    bl_options = {"REGISTER", "UNDO"}
    index: bpy.props.IntProperty(default=0)

    def execute(self, context):
        if 0 <= self.index < len(context.scene.bamod_anims):
            context.scene.bamod_anims.remove(self.index)
        return {"FINISHED"}


class BAMOD_PT_Animation(bpy.types.Panel):
    bl_label = "④ 动画"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "BA Mod"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        prefs = _prefs(context)
        scene = context.scene
        # ⛔⛔ 已按需求**下线**「从游戏读取动画（肌肉动画 → 关键帧）」整块 ✓（v1.8.51）
        #   原因：步兵肌肉动画的「槽位 ↔ 骨骼」「值 → 角度」映射投入过大、收益不足，
        #   用户决定放弃该功能 ✓。相关算子（`ba_mod.import_animation` / `ba_mod.refresh_pose_clips`）
        #   已从 CLASSES 里**注销** ✓，不再出现在面板与 F3 搜索里 ✓。
        #   本面板**保留**的部分是 **AnimationHub 动画行为编辑** ✓（军械库/载具演示动画 ✓ 另一个功能 ✓）。
        layout.label(text="步兵动画导入已下线（v1.8.51）", icon="INFO")
        layout.separator()
        row = layout.row()
        row.operator("ba_mod.read_animation", icon="ANIM")
        layout.prop(prefs, "apply_anim")
        layout.separator()
        row = layout.row(align=True)
        op = row.operator("ba_mod.add_animation", text="+ 随机扫掠", icon="ADD")
        op.type = "AxisRandom"
        op = row.operator("ba_mod.add_animation", text="+ 跟随", icon="ADD")
        op.type = "MathConnect"
        if len(context.scene.bamod_anims) == 0:
            layout.label(text="（还没读取动画）")
        for i, b in enumerate(context.scene.bamod_anims):
            # 每个动画行为一个折叠栏（默认折叠）
            box = layout.box()
            head = box.row()
            head.prop(b, "open", text="", icon="TRIA_DOWN" if b.open else "TRIA_RIGHT", emboss=False)
            if b.type == "AxisRandom":
                axes = [ax for ax in ("x", "y", "z") if getattr(b, ax + "_source")]
                summary = " / ".join("%s:%s %s~%s" % (ax.upper(), getattr(b, ax + "_source"),
                                                      getattr(b, ax + "_min"), getattr(b, ax + "_max"))
                                     for ax in axes) or "（无轴向源）"
            else:
                summary = "%s → %s" % (b.root or "?", b.target or "?")
            head.label(text="%d · [%s] %s · %s" % (i + 1, b.array, b.type, summary))
            op = head.operator("ba_mod.remove_animation", text="", icon="X")
            op.index = i
            if not b.open:
                continue
            col = box.column(align=True)
            r = col.row(); r.prop(b, "array", text="数组"); r.prop(b, "type", text="类型")
            if b.rid:
                col.prop(b, "rid")
            if b.type == "AxisRandom":
                col.prop(b, "speed")
                r = col.row(align=True); r.prop(b, "min_time"); r.prop(b, "max_time")
                col.prop(b, "lod")
                for ax in ("x", "y", "z"):
                    r = col.row(align=True)
                    r.prop(b, ax + "_source", text=ax.upper())
                    r.prop(b, ax + "_min", text="最小")
                    r.prop(b, ax + "_max", text="最大")
            else:
                r = col.row(); r.prop(b, "root"); r.prop(b, "target")
                r = col.row(); r.prop(b, "freq"); r.prop(b, "damper"); r.prop(b, "reaction")
                r = col.row(); r.prop(b, "freeze_x"); r.prop(b, "freeze_y"); r.prop(b, "freeze_z")
                if b.shots_b64:
                    col.label(text="shots: %d 字节（raw b64 透传）" % (len(b.shots_b64) * 3 // 4))
        txt = bpy.data.texts.get("bamod_anim")
        if txt is not None:
            layout.label(text="（JSON 已同步到文本块 bamod_anim，可手改；面板有内容时以面板为准）")


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
            out_dir = os.path.join(_prefs(context).output_dir or
                                   os.path.dirname(prefs.bundle), "skin_textures")
        os.makedirs(out_dir, exist_ok=True)
        _, _objs, by_pid = sd._load_env(bundle)
        n = 0
        if skin:
            mats = skin["mats"]
        else:
            # 无皮肤（枪械等）：导出当前导入模型的默认材质贴图
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
                        if tp:
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
        for mpid, m in mats.items():
            for tex in m.get("tex") or []:
                png = sd.texture_png_bytes(by_pid, tex["pid"])
                if not png:
                    print("[贴图] 纹理解码失败: %s (%d)" % (tex["name"], tex["pid"]))
                    continue
                fp = os.path.join(out_dir, tex["name"] + ".png")
                with open(fp, "wb") as f:
                    f.write(png)
                n += 1
        self.report({"INFO"}, "导出 %s：%d 张贴图 -> %s" % (mode, n, out_dir))
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
                replace_mats.append({"orig": mpid, "texs": slots})
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


def _model_default_mat_bindings(scene, by_pid):
    """当前导入模型（枪械等无皮肤桥）的默认材质绑定：{mat_pid: {pid,name,tex}}。"""
    mat_pids = set()
    for o in scene.objects:
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


class BAMOD_OT_PackMatSwap(bpy.types.Operator):
    bl_idname = "ba_mod.pack_matswap"
    bl_label = "打包贴图替换包"
    bl_description = "用编辑后的贴图打包模型贴图替换（枪械等无皮肤模型；导入后该模型直接换新贴图）"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        import pack_model as pm
        import skin_data as sd
        prefs = _prefs(context)
        scene = context.scene
        bundle = prefs.bundle
        tex_dir = scene.bamod_skin_tex_dir
        if not tex_dir or not os.path.isdir(tex_dir):
            self.report({"ERROR"}, "请先填写贴图目录（先导出贴图再编辑）")
            return {"CANCELLED"}
        out_path = scene.bamod_matswap_pack_path
        if not out_path:
            out_path = os.path.join(prefs.output_dir or tex_dir, "MATSWAP.bamod")
        _, _objs, by_pid = sd._load_env(bundle)
        mats = _model_default_mat_bindings(scene, by_pid)
        if not mats:
            self.report({"ERROR"}, "场景里没有导入的模型（① 先导入枪械/模型）")
            return {"CANCELLED"}
        renderer_pids = [int(o["ba_renderer_pid"]) for o in scene.objects
                         if "ba_renderer_pid" in o]
        replace_mats = []
        textures = []
        tex_seen = set()
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
                replace_mats.append({"orig": mpid, "texs": slots})
        if not replace_mats:
            self.report({"ERROR"}, "贴图目录里没有与模型材质匹配的 PNG（先导出贴图再编辑）")
            return {"CANCELLED"}
        try:
            pm.create_matswap_pack(out_path, replace_mats, textures, renderer_pids,
                                   scene.get("ba_copy_source_path", ""))
        except Exception as e:  # noqa: BLE001
            self.report({"ERROR"}, "打包失败：%s" % e)
            return {"CANCELLED"}
        self.report({"INFO"}, "已打包 %s（%d 材质 / %d 贴图 / %d 渲染器）" % (
            out_path, len(replace_mats), len(textures), len(renderer_pids)))
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
        box.operator("ba_mod.pack_matswap", icon="FILE_ARCHIVE")


# ---------------------------------------------------------------------------
CLASSES = [
    BAModPreferences,
    PREFAB_Item, MOUNT_Item, SKIN_Item,
    PREFAB_UL, MOUNT_UL, SKIN_UL,
    BAMOD_OT_AutoDetect,
    BAMOD_OT_RefreshPrefabs, BAMOD_OT_ImportPrefab,
    BAMOD_OT_RefreshMounts, BAMOD_OT_SelectMount,
    BAMOD_OT_AddMount, BAMOD_OT_RemoveMount,
    BAMOD_OT_ComputeCRC, BAMOD_OT_TransferWeights,
    BAMOD_OT_BuildModel, BAMOD_OT_ReadAnimation,
    BAMOD_OT_AddAnimation, BAMOD_OT_RemoveAnimation,
    BAMOD_OT_ScanSkins, BAMOD_OT_ApplyDefaultMats, BAMOD_OT_ApplySkin,
    BAMOD_OT_ExportSkinTextures, BAMOD_OT_PackSkin, BAMOD_OT_PackMatSwap,
    # ⛔ v1.8.52：`BAMOD_OT_RefreshPoseBones` 已随 ⑥ 步兵姿势整段删除 ✓（此处引用一并移除 ✓）
    # ⛔ v1.8.51 已注销（步兵动画导入下线 ✓）：
    #   BAMOD_OT_ImportAnimation（导入动画到时间轴 ✗）
    #   BAMOD_OT_RefreshPoseClips（刷新动画列表 ✗）
    #   ⇒ 面板入口已移除 ✓，算子也不再注册 ✓（F3 搜索里也搜不到 ✓）
    ANIM_Behavior,
    BAMOD_PT_Import, BAMOD_PT_Mounts, BAMOD_PT_Tools,
    # ⑥步兵姿势 面板（BAMOD_PT_Pose*）已按需求下线：姿势本质是动画，改用 ④ 里的「导入动画到时间轴」。
    # 相关算子仍保留（F3 搜索里可调），只是不再有面板入口。
    BAMOD_PT_Animation, BAMOD_PT_Skin, BAMOD_PT_Skin_List, BAMOD_PT_Skin_Tex, BAMOD_PT_Skin_Pack,
]


def register():
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
    if not hasattr(bpy.types.Scene, "bamod_prefabs"):
        bpy.types.Scene.bamod_prefabs = bpy.props.CollectionProperty(type=PREFAB_Item)
    bpy.types.Scene.bamod_prefab_index = bpy.props.IntProperty()
    if not hasattr(bpy.types.Scene, "bamod_mounts"):
        bpy.types.Scene.bamod_mounts = bpy.props.CollectionProperty(type=MOUNT_Item)
    bpy.types.Scene.bamod_mount_index = bpy.props.IntProperty()
    if not hasattr(bpy.types.Scene, "bamod_anims"):
        bpy.types.Scene.bamod_anims = bpy.props.CollectionProperty(type=ANIM_Behavior)
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
    # ⛔⛔ v1.8.52：⑥ 步兵姿势 / 步兵动画的**全部场景属性**已随代码段删除 ✓
    #   原内容：`bamod_pose_bones/lib/clips`（引用 `POSE_Bone/POSE_Item/POSE_Clip` ✗）、
    #          `bamod_pose_frame/relative/new_name/rot/loc/prefab_path/pack_path`、
    #          `bamod_anim_all/anim_step`（④ 步兵动画导入选项 ✓）
    #   ⇒ 一并移除 ✓（其引用的数据类与 update 回调 `_on_pose_rot/_on_pose_loc` 都已随段删除 ✗，
    #      留着会 `NameError: POSE_Bone is not defined` ✗ —— 真机加载测试抓到的 ✓✓）


def unregister():
    for prop in ("bamod_prefabs", "bamod_prefab_index", "bamod_mounts", "bamod_mount_index",
                 "bamod_anims", "bamod_anim_index", "bamod_skins", "bamod_skin_index",
                 "bamod_skin_tex_dir", "bamod_skin_new_id", "bamod_skin_pack_path",
                 "bamod_matswap_pack_path"):
        # ⛔ v1.8.52：此处原有的 `bamod_pose_*` / `bamod_anim_*` 属性名已随
        #   ⑥ 步兵姿势 / 步兵动画整段删除 ✓（属性本身也已不再注册 ✓）
        if hasattr(bpy.types.Scene, prop):
            delattr(bpy.types.Scene, prop)
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()



