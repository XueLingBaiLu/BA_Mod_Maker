# -*- coding: utf-8 -*-
r"""完整复制模式（copy-full）：整段字节复制原 prefab，只改网格 + 挂点。

与 build_model 的"从零重建"不同，这里把原 prefab 的 preload 范围内所有对象
字节复制进 .bamod，只对网格（重建顶点数据）和挂点 Transform（改位置/旋转/缩放）
做 patch，其余对象（材质/AnimationHub/AnimationManagerBridge/Fmod/SkinStorage/LOD...）
原样保留。

用法：
    from copy_full import collect_prefab_objects, build_copy
    objs, preload, src_path = collect_prefab_objects(bundle, root_gpid)
    build_copy(bundle, out_pack, new_prefab, new_name, root_gpid, bone_tree, meshes)
"""
import os, sys, struct, base64, json, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
# ⛔ `_unitypy` 有两份（工具根=cp314 / 插件内=cp313），指错会崩在 `lz4._version`。
#    以前这里写死 `HERE/../..` 并 `insert(0)` ⇒ 在 Blender(3.13) 里必崩，而且因为
#    "谁先 import 谁说了算"而表现为**偶发**。统一走 `unitypy_path.ensure`（按解释器 tag 挑）✓
from unitypy_path import ensure as _ensure_unitypy  # noqa: E402
_ensure_unitypy(HERE)

import UnityPy
from UnityPy.files.ObjectReader import ObjectReader
from UnityPy.streams import EndianBinaryReader
from UnityPy.helpers import TypeTreeHelper
from UnityPy.classes.PPtr import PPtr
from UnityPy.classes.generated import ComponentPair
from UnityPy.classes.math import Vector3f, Quaternionf

from pack_model import _type_identity
from build_model import _world_matrices, _invert, _mul
from build_turret_direct import _find_sources, _save, _make_go, _make_alloc, _read_from_data
from bone_hashes import KNOWN_HASHES, bone_hash
from hub_edit import HUB_SCRIPT, json_to_hub

FORMAT = "bamod-assets"
VERSION = 2
MOD_PID_BASE = 0x4355424500000000


def _load_env(bundle):
    # 复用 extract_model 的缓存，避免同一 bundle 反复解压（3.4GB）导致 lz4 内存不足/缓冲区不够
    try:
        from extract_model import _load as _cached_load
        env, objs, by_pid = _cached_load(bundle)
        sf = list(env.objects)[0].assets_file
        return env, sf, objs, by_pid
    except Exception:
        env = UnityPy.load(bundle)
        sf = list(env.objects)[0].assets_file
        objs = list(sf.objects.values())
        by_pid = {o.path_id: o for o in objs}
        return env, sf, objs, by_pid
    return env, sf, objs, by_pid


def container_root_pid(bundle, prefab_path):
    """容器条目里记的 `asset` pid = 这个 prefab **真正的根 GameObject** pid。

    ⛔ v1.8.79：必须用它、不能用调用方传进来的"复制源 pid"。导入 `.bamod` 会把容器
       asset 指向新分配的根 pid ⇒ 传入的旧 pid 会过期；这时我们是**按路径回退**找到条目的，
       若 manifest 仍写旧 pid，导入端一查"root_pid 不在包对象里"就判**包坏了** ✗（实测报错：
       `manifest.root_pid=6302881677221457888 不在包对象里`）。
    """
    env, sf, objs, by_pid = _load_env(bundle)
    ab = next(o for o in objs if o.type.name == "AssetBundle").read()
    for n, a in ab.m_Container:
        if n == prefab_path and a is not None and a.asset:
            return a.asset.m_PathID
    return None


def collect_prefab_objects(bundle, root_gpid, want_path=None):
    """收集原 prefab 的完整对象集（preload 范围内全部对象，MonoScript 除外）。

    返回 (objects, preload_pids, prefab_path)：
      objects: [{pid, class_id, type_name, script_id, tree_hash, raw(base64)}]
      preload_pids: 原 prefab 的 preload 顺序（含 MonoScript 常量 pid）
      prefab_path: 容器条目路径

    want_path: 可选，场景里记的 prefab 内部路径。pid 查不到时按路径回退（见下）。
    """
    env, sf, objs, by_pid = _load_env(bundle)
    ab = next(o for o in objs if o.type.name == "AssetBundle").read()
    prefab_path = None
    preload_index = preload_size = None
    for n, a in ab.m_Container:
        if a.asset and a.asset.m_PathID == root_gpid:
            prefab_path = n
            preload_index = a.preloadIndex
            preload_size = a.preloadSize
            break
    if preload_index is None and want_path:
        # ⛔ v1.8.78：**pid 会过期**。导入 .bamod 时工具会把容器的 asset 指向
        #    **新分配**的根 pid ⇒ 之后再拿"导入前记下的 pid"来找就必然找不到 ✗
        #    （实测用户点「读取动画」直接抛 `找不到 root=... 的容器条目`，完全看不懂）。
        #    这里退化成按**路径**找（场景里存了 `ba_copy_source_path`），能自愈 ✓
        base = want_path.split("/")[-1].lower()
        for n, a in ab.m_Container:
            if n == want_path or n.split("/")[-1].lower() == base:
                prefab_path = n
                preload_index = a.preloadIndex
                preload_size = a.preloadSize
                print("[copy-full] ⚠ 复制源 pid %d 在当前 bundle 里已不存在（多半是这个 "
                      "prefab 被导入过、容器 asset 已改指新 pid）⇒ 按路径回退命中 %s"
                      % (root_gpid, n))
                break
    if preload_index is None:
        raise ValueError(
            "找不到 prefab 的容器条目：root pid=%d%s\n"
            "  ⛔ 常见原因：这个 bundle 里的 prefab 容器**已被上一次导入改成新分配的根 pid**\n"
            "     ⇒ 场景里记的『复制源』pid 过期了。三种解法（任选其一）：\n"
            "     1) 偏好设置 →『游戏 bundle 文件』指回**没被导入过**的那份"
            "（如 D:\\断箭模组制作\\备份\\units_assets_all_…bundle），再重新 ① 导入；\n"
            "     2) 直接用当前 bundle 重新 ① 导入所选模型（刷新复制源）；\n"
            "     3) 或点 ④ 面板里的『把①所选模型设为当前模型』。"
            % (root_gpid, ("，路径=%r" % want_path) if want_path else ""))
    seg = [p.m_PathID for p in ab.m_PreloadTable[preload_index:preload_index + preload_size]]
    objects = []
    for pid in seg:
        o = by_pid.get(pid)
        if o is None or o.type.name == "MonoScript":
            continue
        raw = o.data if getattr(o, "data", None) is not None else o.get_raw_data()
        if not raw:
            continue
        obj = _type_identity(o)
        obj["pid"] = pid
        obj["raw"] = base64.b64encode(raw).decode("ascii")
        objects.append(obj)
    return objects, seg, prefab_path


def _read_mesh(raw, mesh_src):
    r = ObjectReader(assets_file=mesh_src.assets_file, reader=mesh_src.reader, path_id=1,
                     type_id=mesh_src.type_id, serialized_type=mesh_src.serialized_type,
                     class_id=mesh_src.class_id, type=mesh_src.type,
                     byte_start=0, byte_size=len(raw), is_destroyed=False, is_stripped=False,
                     data=raw)
    node = r._get_typetree_node()
    er = EndianBinaryReader(raw, endian=mesh_src.reader.endian)
    m = TypeTreeHelper.read_typetree(node, er, as_dict=False, assetsfile=mesh_src.assets_file,
                                     byte_size=len(raw), check_read=False)
    m.set_object_reader(r)
    return m, r


def _read_go(raw, go_src):
    r = ObjectReader(assets_file=go_src.assets_file, reader=go_src.reader, path_id=1,
                     type_id=go_src.type_id, serialized_type=go_src.serialized_type,
                     class_id=go_src.class_id, type=go_src.type,
                     byte_start=0, byte_size=len(raw), is_destroyed=False, is_stripped=False,
                     data=raw)
    node = r._get_typetree_node()
    er = EndianBinaryReader(raw, endian=go_src.reader.endian)
    g = TypeTreeHelper.read_typetree(node, er, as_dict=False, assetsfile=go_src.assets_file,
                                     byte_size=len(raw), check_read=False)
    g.set_object_reader(r)
    return g, r


def _read_smr(raw, smr_src):
    """用 SMR 的类型树读一段 raw（新网格要借它当模板：材质/AABB/骨骼都从这里来）。"""
    r = ObjectReader(assets_file=smr_src.assets_file, reader=smr_src.reader, path_id=1,
                     type_id=smr_src.type_id, serialized_type=smr_src.serialized_type,
                     class_id=smr_src.class_id, type=smr_src.type,
                     byte_start=0, byte_size=len(raw), is_destroyed=False, is_stripped=False,
                     data=raw)
    node = r._get_typetree_node()
    er = EndianBinaryReader(raw, endian=smr_src.reader.endian)
    s = TypeTreeHelper.read_typetree(node, er, as_dict=False, assetsfile=smr_src.assets_file,
                                     byte_size=len(raw), check_read=False)
    s.set_object_reader(r)
    return s


def _read_tr(raw, tr_src):
    """用 Transform 的类型树读一段 raw（本地 TRS + 父级）。"""
    r = ObjectReader(assets_file=tr_src.assets_file, reader=tr_src.reader, path_id=1,
                     type_id=tr_src.type_id, serialized_type=tr_src.serialized_type,
                     class_id=tr_src.class_id, type=tr_src.type,
                     byte_start=0, byte_size=len(raw), is_destroyed=False, is_stripped=False,
                     data=raw)
    node = r._get_typetree_node()
    er = EndianBinaryReader(raw, endian=tr_src.reader.endian)
    return TypeTreeHelper.read_typetree(node, er, as_dict=False, assetsfile=tr_src.assets_file,
                                        byte_size=len(raw), check_read=False)


def _src_channel_map(mesh):
    """源网格的「(位置, UV0) -> (法线, 切线, UV1)」表 —— 用于"能对上就照抄"。

    ⛔ v1.8.82：工具原来**重算法线**（同位置面法线求平均）并把 **UV1 覆盖成 UV0**，
       这两件事都会改变外观（硬边被抹平 / 依赖 UV1 的采样从常量变成变化）✗
       见 `turret_swap.pack_by_layout` 的说明。返回 {} 表示没有可用数据。
    """
    try:
        import extract_model as _em
        vd = mesh.m_VertexData
        data = bytes(vd.m_DataSize or b"")
        if not data:
            return {}
        N, data, chans = _em._read_mesh_data(mesh, lambda f: _em._FMT_SIZE.get(f, 4), data,
                                             padded=True)
        by = {i: (d, o, s, c) for (_a, _b), (_k, d, o, s, c, i) in chans.items()}
        if 0 not in by or 4 not in by:
            return {}

        def chan(idx):
            if idx not in by:
                return None
            d, o, s, c = by[idx]
            return _em._read_chan(data, N, o, s, c, d)

        pos, uv0 = chan(0), chan(4)
        nrm, tan, uv1 = chan(1), chan(2), chan(5)
        if not pos or not uv0:
            return {}
        out = {}
        for i in range(min(N, len(pos), len(uv0))):
            k = (round(pos[i][0], 3), round(pos[i][1], 3), round(pos[i][2], 3),
                 round(uv0[i][0], 4), round(uv0[i][1], 4))
            out.setdefault(k, (nrm[i] if nrm else None,
                               tan[i] if tan else None,
                               uv1[i] if uv1 else None))
        return out
    except Exception as e:  # noqa: BLE001
        print("[copy-full] ⚠ 源网格通道表读不出（法线/UV1 将按重算写）：%s: %s"
              % (type(e).__name__, e))
        return {}


def _find_hub_template(objs):
    """在 bundle 里找一个**现成的** AnimationHub MonoBehaviour 当类型模板。

    为什么要它：MonoBehaviour 的类型身份（`script_id` / tree_hash）在 bundle 的
    SerializedFile 类型表里，新建一个 MonoBehaviour 必须沿用**同一个**类型条目，
    否则导入端 `_match_type` 只能退化成"第一个 class_id=114 的类型" ⇒ 挂错脚本 ✗。
    返回 ObjectReader 或 None。
    """
    for o in objs:
        if o.type.name != "MonoBehaviour":
            continue
        try:
            d = o.data if getattr(o, "data", None) is not None else o.get_raw_data()
        except Exception:  # noqa: BLE001
            continue
        if d and len(d) >= 28 and struct.unpack_from("<q", d, 20)[0] == HUB_SCRIPT:
            return o
    return None


def build_copy(bundle, out_pack, new_prefab, new_name, source_root_gpid, bone_tree, meshes,
               source_path=None, hub_json=None, comp_edits=None):
    r"""patch 模式导出 .bamod：字节复制原 prefab 全部对象，只 patch 网格 + 挂点。

    bone_tree: [(name, parent_name, pos_unity, rot_unity(xyzw), scale_unity), ...] 父在前
    meshes:    [{name, positions, triangles, uv, bones[(b0,b1)每顶点], weights[(w0,w1)每顶点],
                bone_names[骨骼名列表]}]
    source_path: 可选，复制源的 prefab 内部路径（pid 失效时按它回退定位，见
                `collect_prefab_objects`）。
    comp_edits: 可选，{pid字符串: {cls, node, values}} —— ⑧ 面板改过的**其它组件**。
                按 MB 的 pid 精确命中，用 `component_edit.emit_component` 重序列化 ✓
    """
    from turret_swap import build_mesh_data
    env, sf, objs, by_pid = _load_env(bundle)
    # 源对象集
    src_objects, src_preload, src_path = collect_prefab_objects(bundle, source_root_gpid,
                                                               want_path=source_path)
    # ⛔ 真正的根 pid 以**容器条目**为准（见 container_root_pid 的说明）：
    #    复制源 pid 过期时我们是按路径回退找到条目的，若继续沿用旧 pid，
    #    manifest.root_pid 会指向包里不存在的对象 ⇒ 导入端直接判"包坏了"✗
    root_pid = container_root_pid(bundle, src_path) or source_root_gpid
    if root_pid != source_root_gpid:
        print("[copy-full] ⚠ 复制源 pid %d 已过期（这个 prefab 被导入过，容器 asset 已改指"
              "新 pid）⇒ 按容器条目改用真实根 pid %d"
              % (source_root_gpid, root_pid))
    _, _, smr_src, mesh_src, _, _, _, _, hash_lookup = _find_sources(objs, by_pid)
    hash_lookup.update(KNOWN_HASHES)
    go_src = next(o for o in objs if o.type.name == "GameObject")
    tr_src = next(o for o in objs if o.type.name == "Transform")

    world = _world_matrices(bone_tree)

    # 骨骼路径链（root 起）→ 用与 build_model 相同的算法直接算哈希。
    # ⛔ 必须这样算，不能用 KNOWN_HASHES 查表：自定义挂点名（如 Rotorangle_0 / 自建
    #    桨叶骨架）不在表里 ⇒ 旧代码 `hash_lookup[b]` 直接 KeyError ✗（v1.8.57 修）。
    _parent_of = {n: p for n, p, _, _, _ in bone_tree}
    _tree_names = [n for n, _, _, _, _ in bone_tree]
    _node = {n: (p, pos, rot, sc) for n, p, pos, rot, sc in bone_tree}

    def _path_of(name):
        parts = []
        cur = name
        for _ in range(64):
            if cur is None:
                break
            parts.append(cur)
            cur = _parent_of.get(cur)
        return list(reversed(parts))

    bone_hashes = {n: bone_hash(_path_of(n)) for n in _tree_names}

    # ---- 建名字索引：GO 名 -> pid；mesh 名 -> pid ----
    go_by_name = {}   # name -> go pid
    tr_by_go = {}     # go pid -> tr pid
    mesh_by_name = {} # name -> mesh pid
    for o in src_objects:
        raw = base64.b64decode(o["raw"])
        if o["type_name"] == "GameObject":
            try:
                g, _ = _read_go(raw, go_src)
                go_by_name[g.m_Name] = o["pid"]
            except Exception:
                pass
        elif o["type_name"] == "Transform":
            try:
                r = ObjectReader(assets_file=sf, reader=sf.reader, path_id=1,
                                 type_id=tr_src.type_id, serialized_type=tr_src.serialized_type,
                                 class_id=tr_src.class_id, type=tr_src.type,
                                 byte_start=0, byte_size=len(raw), is_destroyed=False,
                                 is_stripped=False, data=raw)
                node = r._get_typetree_node()
                er = EndianBinaryReader(raw, endian=sf.reader.endian)
                t = TypeTreeHelper.read_typetree(node, er, as_dict=False, assetsfile=sf,
                                                 byte_size=len(raw), check_read=False)
                gid = t.m_GameObject.m_PathID if t.m_GameObject else 0
                tr_by_go[gid] = o["pid"]
            except Exception:
                pass
        elif o["type_name"] == "Mesh":
            try:
                m, _ = _read_mesh(raw, mesh_src)
                mesh_by_name[m.m_Name] = o["pid"]
            except Exception:
                pass

    # ---- 新增节点：骨骼树里有、源 prefab 里没有的（如挂载点 Rotorangle_0）----
    # ⛔ v1.8.57 之前 copy-full **只会 patch 已存在的 Transform** ⇒ 用户新加的挂载点被
    #    静默丢掉（④ 面板里能看到、构建立刻消失）✗。这里补上：新建 GameObject +
    #    Transform、挂到父节点、写回父的 m_Children，并把新对象并入 manifest。
    tr_pid_of = {}          # 节点名 -> Transform pid（原有 + 新建）
    go_pid_of = {}          # 节点名 -> GameObject pid
    for nm, gpid in go_by_name.items():
        t = tr_by_go.get(gpid)
        if t is not None:
            tr_pid_of[nm] = t
            go_pid_of[nm] = gpid
    added = [n for n in _tree_names if n not in go_by_name]
    new_objs_extra = []     # [{pid, class_id, type_name, script_id, tree_hash, raw(b64)}]
    patch_children = {}     # 原有 Transform pid -> [新子 Transform pid]

    def _dump_new(pid):
        """把刚写到 sf 里的新对象导成 manifest 条目（**幂等**：同 pid 覆盖，不重复）。

        ⛔ 必须用 `.data`：`get_raw_data()` 对**刚 _save 出**的对象是按 byte_size 从流里
        重读的，而新建 reader 的 byte_size=0 ⇒ 只会拿到空字节 ✗（实测踩坑）。
        ⛔ 同 pid 不能出现两条：导入端 `pid_map` 只会记住最后一次映射，
        前面那条会变成没人引用的孤儿对象。
        """
        o = sf.objects[pid]
        raw_new = o.data if getattr(o, "data", None) is not None else o.get_raw_data()
        for e in [x for x in new_objs_extra if x["pid"] == pid]:
            new_objs_extra.remove(e)
        new_objs_extra.append({
            "pid": pid, "class_id": int(o.class_id), "type_name": o.type.name,
            "script_id": None, "tree_hash": None,
            "raw": base64.b64encode(raw_new).decode("ascii")})

    # ⛔⛔ v1.8.80：原版各网格的**绑定姿势 / 骨骼名哈希**（按骨骼名索引）
    #    网格被替换时**必须照抄**这些值，绝不能重算。
    #    原版烘焙出的 `m_BindPose` **不一定等于** `inverse(节点世界矩阵)` ——
    #    实测 ACV：`Shield`/`Shield_01`/`apparel*` 这些节点**带旋转**（±74°/80°/96°），
    #    但原版 BindPose 的旋转部分是**单位阵**。按"重算"写回去 ⇒ 静止时
    #    `v' = W_node · BindPose_new · v ≠ v` ⇒ 绑在这些骨骼上的几何**脱离模型** ✗
    #    （现场症状：正面护甲偏 7.68、后门/裙板偏 5.53；轮子那 16 根旋转≈0 的骨骼正常）
    #    重算只作兜底：原版没有该骨骼名时（如新加的 `rot_blade_1`）✓
    src_bind, src_hash = {}, {}
    _mesh_by_pid = {o["pid"]: o for o in src_objects if o["type_name"] == "Mesh"}
    for o in src_objects:
        if o["type_name"] != "SkinnedMeshRenderer":
            continue
        try:
            s0 = _read_smr(base64.b64decode(o["raw"]), smr_src)
            mo = _mesh_by_pid.get(s0.m_Mesh.m_PathID) if s0.m_Mesh else None
            if mo is None:
                continue
            m0, _r0 = _read_mesh(base64.b64decode(mo["raw"]), mesh_src)
            bps = list(getattr(m0, "m_BindPose", None) or [])
            hs = list(getattr(m0, "m_BoneNameHashes", None) or [])
            for i, b in enumerate(s0.m_Bones or []):
                tpid = b.m_PathID if b else 0
                nm = next((k for k, v in tr_pid_of.items() if v == tpid), None)
                if not nm or i >= len(bps):
                    continue
                src_bind.setdefault(nm, bps[i])
                if i < len(hs):
                    src_hash.setdefault(nm, hs[i])
        except Exception as e:  # noqa: BLE001
            # ⛔ 不要静默！这里 swallow 过一次真 bug（块里用了不存在的 `pid_of`
            #    ⇒ src_bind 永远为空、照抄逻辑整个失效，却只表现为"照抄 0 根"）✗
            print("[copy-full] ⚠ 读原版绑定数据失败（这条 SMR 跳过）：%s: %s"
                  % (type(e).__name__, e))
            continue
    if src_bind:
        print("[copy-full] 原版绑定数据：%d 根骨骼的 BindPose、%d 根的名字哈希（替换网格时照抄）"
              % (len(src_bind), len(src_hash)))

    # ⛔ v1.8.81：源 prefab 里各**骨骼节点**的世界矩阵 —— 用于"保持外观"的绑定姿势换算。
    #    只照抄 BindPose 是不够的：用户**移动过**挂载点时（该挂载点已在上一次构建里存在，
    #    于是它这次就出现在"原版"里），照抄旧值 ⇒ **节点动了、绑定没动** ⇒ 那块几何整体
    #    偏移（实测：把 `rot_blade_1` 抬高 2.567 后，旋翼在游戏里**悬空 2.567** ✗）。
    #    正确做法 = 保持外观：`BindPose_new = inverse(W_new) · W_old · BindPose_old` ✓
    #      · 节点没动（W_new == W_old）⇒ 结果就是照抄 ✓
    #      · 节点动了、且原绑定自洽 ⇒ inverse(W_new) ⇒ **几何留在原位、只有轴心跟着移动** ✓
    src_world = {}
    try:
        _loc, _nm2, _tr_go2 = {}, {}, {}
        for o in src_objects:
            if o["type_name"] == "GameObject":
                g, _ = _read_go(base64.b64decode(o["raw"]), go_src)
                _nm2[o["pid"]] = g.m_Name
        for o in src_objects:
            if o["type_name"] != "Transform":
                continue
            t = _read_tr(base64.b64decode(o["raw"]), tr_src)
            gid = t.m_GameObject.m_PathID if t.m_GameObject else 0
            fa = t.m_Father.m_PathID if (t.m_Father and t.m_Father.m_PathID) else 0
            _tr_go2[o["pid"]] = gid
            _loc[o["pid"]] = (gid, fa,
                              (t.m_LocalPosition.x, t.m_LocalPosition.y, t.m_LocalPosition.z),
                              (t.m_LocalRotation.x, t.m_LocalRotation.y,
                               t.m_LocalRotation.z, t.m_LocalRotation.w),
                              (t.m_LocalScale.x, t.m_LocalScale.y, t.m_LocalScale.z))
        ordered, done = [], set()
        for _ in range(len(_loc) + 2):
            for pid, (gid, fa, p, q, s) in _loc.items():
                if pid in done:
                    continue
                if fa and fa in _loc and fa not in done:
                    continue
                # ⛔ 父级名必须由 **Transform pid → GO pid → 名字** 两步查：
                #    直接把 Transform pid 丢给 GO 名字表会得到 None ⇒ 父级不乘 ✗
                #    （实测：Lantenna/Rantenna 少了 body 的 1.2174，绑定姿势就错了）
                pname = _nm2.get(_tr_go2.get(fa, -1)) if fa else None
                ordered.append((_nm2.get(gid, ""), pname, p, q, s))
                done.add(pid)
            if len(done) == len(_loc):
                break
        src_world = _world_matrices(ordered)
    except Exception as e:  # noqa: BLE001
        print("[copy-full] ⚠ 源节点世界矩阵算不出（移动过的骨骼会退化为直接重算）：%s: %s"
              % (type(e).__name__, e))

    # 源 prefab 里没有同名的用户网格 = **全新网格**（例如 ACV 的旋翼桨叶）
    src_mesh_names = set(mesh_by_name.keys())
    new_meshes = [m for m in meshes
                  if m.get("name") and m.get("name") not in src_mesh_names]

    if added or new_meshes:
        # ⛔ 用 `sf.objects.keys()`（**当前**对象表）而不是缓存的 `by_pid` 快照：
        #    同一会话里第二次构建时，`by_pid` 还是加载时的旧快照，不含上一次新建的对象
        #    ⇒ 每次都从 BASE+0 重新分配、把 `sf.objects[BASE+0]` 覆盖掉 ✗
        alloc = _make_alloc(set(sf.objects.keys()), MOD_PID_BASE)
        go_src2 = next(o for o in objs if o.type.name == "GameObject")
        tr_src2 = next(o for o in objs if o.type.name == "Transform")

    if added:
        for nm in _tree_names:          # bone_tree 保证父在前
            if nm not in added:
                continue
            par = _parent_of.get(nm)
            ptr = tr_pid_of.get(par, 0) if par else 0
            if par is None:
                # ⛔ v1.8.77：无父级的新节点会变成 prefab 里的独立根节点（与车体平级）
                #    ⇒ 游戏里它不会跟着车动（用户看到的"旋翼飘在原地"就是这么来的）。
                print("[copy-full] ⚠ 新节点 %s **没有父级** ⇒ 会写成独立根节点、不跟着车体 ✗"
                      "（② 面板添加挂载点时要先在大纲视图选中父挂载点，如 body）" % nm)
            elif not ptr:
                # 父不在树里（最常见：② 面板添加挂载点时选中的是**网格**而不是挂载点 Empty，
                # 于是父子关系没进树）⇒ 明确报出来，别静默丢节点 ✗
                print("[copy-full] ⚠ 新节点 %s 的父 %r 不在挂载点树里，已跳过"
                      "（添加挂载点时要选中**挂载点 Empty** 作父级）" % (nm, par))
                continue
            gpid, trpid = _make_go(sf, alloc, go_src2, tr_src2, nm, ptr)
            # _make_go 只给单位 TRS；这里按 Blender 侧的值写回
            node = _node.get(nm)
            if node:
                _, pos, rot, sc = node
                t = _read_from_data(sf.objects[trpid])
                t.m_LocalPosition = Vector3f(pos[0], pos[1], pos[2])
                t.m_LocalRotation = Quaternionf(rot[0], rot[1], rot[2], rot[3])
                t.m_LocalScale = Vector3f(sc[0], sc[1], sc[2])
                sf.objects[trpid].save_typetree(t)
            go_pid_of[nm] = gpid
            tr_pid_of[nm] = trpid
            if par and ptr:
                # 父可能是**原有节点**（主循环里补 m_Children）也可能是**同为新建的节点**
                # （下面的后处理里直接写）⇒ 两种都登记，由后处理按 tr pid 分流。
                # ⛔ 旧写法只登记「父在 go_by_name 里」的情形 ⇒ 新节点套新节点时父的
                #    m_Children 为空 ✗（Rotorangle_1 挂在 Rotorangle_0 下就踩这个坑）。
                patch_children.setdefault(ptr, []).append(trpid)
            _dump_new(gpid)
            _dump_new(trpid)

    # ---- 新增网格：Mesh + GameObject + SkinnedMeshRenderer（挂到 root）----
    # ⛔ 同样是 v1.8.57 才有的：以前 copy-full 只替换**源 prefab 里同名**的网格，
    #    用户自己新建的桨叶网格会被静默丢掉 ✗ —— 而「给 ACV 加旋翼」恰恰需要新网格。
    #    蒙皮网格的变形只取决于骨骼，所以挂点选 root 不影响观感。
    new_smr_pids = []
    if new_meshes:
        from turret_swap import build_mesh_data
        attach_parent = "root" if "root" in tr_pid_of else _tree_names[0]
        # ⛔ v1.8.76：新网格的 SMR 模板优先取**目标 prefab 自己**的 SMR。
        #    以前用 `_find_sources` 随便挑的"bundle 里第一个 SMR"当模板 ⇒ 新网格继承到
        #    **别人的材质**：实测给 ACV 加的旋翼拿到了 `RU_1BTR80_82 1`（BTR-80 的贴图）✗
        #    现在优先用「本次要替换的那个网格」的 SMR ⇒ 新网格继承本车材质 ✓
        _user_names = {mm.get("name") for mm in meshes if mm.get("name")}
        pref_raw = None
        for o in src_objects:
            if o["type_name"] != "SkinnedMeshRenderer":
                continue
            try:
                s_ = _read_smr(base64.b64decode(o["raw"]), smr_src)
                mp = s_.m_Mesh.m_PathID if s_.m_Mesh else 0
                nm_ = next((k for k, v in mesh_by_name.items() if v == mp), None)
                if nm_ in _user_names:
                    pref_raw = base64.b64decode(o["raw"])
                    break
                if pref_raw is None:
                    pref_raw = base64.b64decode(o["raw"])
            except Exception:  # noqa: BLE001
                continue
        for mesh in new_meshes:
            bn = list(mesh.get("bone_names") or [])
            if not bn:
                continue
            if not all(b in tr_pid_of for b in bn):
                print("[copy-full] 新网格 %s 的骨骼名有缺失，跳过" % mesh.get("name"))
                continue
            skin = [(int(mesh["bones"][i][0]), int(mesh["bones"][i][1]),
                     float(mesh["weights"][i][0]), float(mesh["weights"][i][1]))
                    for i in range(len(mesh["positions"]))]
            faces = []
            ui = 0
            for tt in range(0, len(mesh["triangles"]), 3):
                a, b, c = (mesh["triangles"][tt], mesh["triangles"][tt + 1],
                           mesh["triangles"][tt + 2])
                faces.append(((a, ui), (b, ui + 1), (c, ui + 2)))
                ui += 3
            # ⛔ v1.8.76：新网格也照模板网格自己的通道表写（同上）
            #    `m = mesh_src.read()` 必须先执行 —— 布局要从它的 m_Channels 读
            m = mesh_src.read()
            vbytes, ibytes, vcount = build_mesh_data(
                mesh["positions"], mesh["uv"], faces, skin, skin_mode=2,
                layout_from=m)
            m.m_Name = mesh["name"]
            m.m_VertexData.m_VertexCount = vcount
            m.m_VertexData.m_DataSize = vbytes
            m.m_IndexBuffer = list(ibytes)
            m.m_IndexFormat = 1 if vcount > 65535 else 0
            if m.m_SubMeshes:
                sub = m.m_SubMeshes[0]
                sub.indexCount = vcount
                sub.firstByte = 0
                sub.firstVertex = 0
                sub.vertexCount = vcount
                sub.topology = 0
            # ⛔ v1.8.80：新网格若绑到**原版已有**的骨骼上，绑定姿势/哈希也要照抄原版
            #    （重算 != 原版，会让这块几何相对车体错位 —— 见 src_bind 处的说明）
            m.m_BoneNameHashes = [(src_hash[b] if b in src_hash else bone_hashes[b]) & 0xFFFFFFFF
                                  for b in bn]
            m.m_RootBoneNameHash = ((bone_hashes.get("body", m.m_BoneNameHashes[0])
                                     if "body" in bn else m.m_BoneNameHashes[0])
                                    & 0xFFFFFFFF)
            m.m_BindPose = [
                (_mul(_invert(world[b]), _mul(src_world[b], src_bind[b]))
                 if (b in src_bind and b in src_world and b in world)
                 else _invert(world[b]))
                for b in bn]
            mesh_pid = _save(sf, mesh_src, alloc(), m).path_id
            _dump_new(mesh_pid)

            mgpid, mgtrpid = _make_go(sf, alloc, go_src2, tr_src2,
                                      mesh["name"], tr_pid_of[attach_parent])
            patch_children.setdefault(tr_pid_of[attach_parent], []).append(mgtrpid)
            _dump_new(mgpid)
            _dump_new(mgtrpid)

            s = _read_smr(pref_raw, smr_src) if pref_raw else smr_src.read()
            s.m_Mesh = PPtr(m_FileID=0, m_PathID=mesh_pid, assetsfile=sf)
            s.m_GameObject = PPtr(m_FileID=0, m_PathID=mgpid, assetsfile=sf)
            s.m_Bones = [PPtr(m_FileID=0, m_PathID=tr_pid_of[b], assetsfile=sf) for b in bn]
            s.m_RootBone = PPtr(m_FileID=0, m_PathID=tr_pid_of.get("body", tr_pid_of[bn[0]]),
                                assetsfile=sf)
            xs = [p[0] for p in mesh["positions"]]
            ys = [p[1] for p in mesh["positions"]]
            zs = [p[2] for p in mesh["positions"]]
            if xs:
                s.m_AABB.m_Center = Vector3f((min(xs) + max(xs)) / 2,
                                             (min(ys) + max(ys)) / 2,
                                             (min(zs) + max(zs)) / 2)
                s.m_AABB.m_Extent = Vector3f((max(xs) - min(xs)) / 2 or 0.5,
                                             (max(ys) - min(ys)) / 2 or 0.5,
                                             (max(zs) - min(zs)) / 2 or 0.5)
            smr_pid = _save(sf, smr_src, alloc(), s).path_id
            new_smr_pids.append(smr_pid)
            _dump_new(smr_pid)
            mg = _read_from_data(sf.objects[mgpid])
            mg.m_Component.append(ComponentPair(
                component=PPtr(m_FileID=0, m_PathID=smr_pid, assetsfile=sf)))
            sf.objects[mgpid].save_typetree(mg)
            _dump_new(mgpid)          # 组件列表改了 ⇒ 覆盖式重导（幂等）

    # 网格替换清单：mesh pid -> 用户网格的骨骼名顺序（用于同步 SMR.m_Bones）
    mesh_bone_names = {}
    for nm, mpid in mesh_by_name.items():
        user = next((m for m in meshes if m.get("name") == nm), None)
        if user is not None and user.get("bone_names"):
            mesh_bone_names[mpid] = list(user["bone_names"])

    # 父节点**也是新建的**那些 patch_children：主循环只遍历源对象 ⇒ patch 不到，
    # 这里直接写进新 Transform（例：桨叶挂在 Rotorangle_0 下、而 Rotorangle_0 是新建节点）。
    _orig_tr_pids = set(tr_by_go.values())
    for ptrl, kids in list(patch_children.items()):
        if ptrl in _orig_tr_pids:
            continue                      # 原节点，留给主循环
        try:
            t = _read_from_data(sf.objects[ptrl])
            cur = [c.m_PathID for c in (t.m_Children or []) if c and c.m_PathID]
            for k in kids:
                if k not in cur:
                    cur.append(k)
            t.m_Children = [PPtr(m_FileID=0, m_PathID=k, assetsfile=sf) for k in cur]
            sf.objects[ptrl].save_typetree(t)
            _dump_new(ptrl)           # 子列表改了 ⇒ 覆盖式重导（幂等）
        except Exception as e:  # noqa: BLE001
            print("[copy-full] 新节点子列表写入失败 pid=%s：%s" % (ptrl, e))
        del patch_children[ptrl]

    # ---- 名字 → Transform pid 解析器（构建期才定得下 pid，见下）----
    # ⛔ v1.8.67：行为里的节点引用现在**按名字**留给构建期解析（`behavior_codec.emit`
    #    的 `pid_of` 参数）。原因：用户自己新建的挂载点（② 面板加的 Rotorangle_0）
    #    pid 是构建期才分配的，面板编码时只能写 0 ⇒ 进游戏指向空引用（旋翼不转）✗。
    #    优先级：本 prefab 内的名字 → **本次新建的**节点 → 全 bundle 的名字。
    name_to_tr = {}
    for gname, gpid in go_by_name.items():
        trpid = tr_by_go.get(gpid)
        if trpid is not None:
            name_to_tr[gname] = trpid
    full_go_tr = {}
    full_go_name = {}
    for oo in objs:
        if oo.type.name == "Transform":
            try:
                tr = oo.read()
                gid = tr.m_GameObject.m_PathID if tr.m_GameObject else 0
                full_go_tr[gid] = oo.path_id
            except Exception:
                pass
        elif oo.type.name == "GameObject":
            try:
                full_go_name[oo.path_id] = oo.read().m_Name
            except Exception:
                pass
    full_name_to_tr = {}
    for gpid, trpid in full_go_tr.items():
        nm = full_go_name.get(gpid, "")
        if nm and nm not in full_name_to_tr:
            full_name_to_tr[nm] = trpid

    def _pid_of(name):
        if not name:
            return 0
        # hub JSON 往返时 source/root/target 常是 pid 数字字符串
        # （如 "-1521521995993491084"）：直接用该 pid（导入端会重映射）
        if isinstance(name, str):
            try:
                return int(name)
            except ValueError:
                pass
        p = name_to_tr.get(name)
        if p:
            return p
        p = tr_pid_of.get(name)      # 含本次新建的挂载点（Rotorangle_0 等）
        if p:
            return p
        return full_name_to_tr.get(name, 0)

    hub_created = [False]            # 是否已经处理过（改过或新建过）AnimationHub

    def _emit_comp(ed, raw):
        """⑧ 面板改过的组件：用编辑后的字段重序列化整段 MB 字节。

        `pid_of=_pid_of` 是必须的：字段里可能有**节点引用**（PPtr），面板存的是
        节点**名字**（`__name__`），构建期才拿得到真实 pid ✓
        """
        import component_edit as _CE
        return _CE.emit_component(_CE.registry(), ed["cls"], raw, ed["values"],
                                  pid_of=_pid_of)

    # ---- 重写对象：默认原样，patch 网格 + 挂点 ----
    # v1.8.90：⑧ 组件改动的 pid 一律按字符串比（Unity 的 pathID 是 int64，
    #   Blender 的 IntProperty 存不下，面板那边就是字符串 —— 两边统一 ✓）
    comp_edits = {int(k): v for k, v in (comp_edits or {}).items()}
    comp_done = [0]
    comp_hit = set()
    new_objects = []
    for o in src_objects:
        raw = base64.b64decode(o["raw"])
        if o["type_name"] == "Mesh" and o["pid"] in mesh_by_name.values():
            # 找到对应网格名 -> 用户网格
            nm = next(k for k, v in mesh_by_name.items() if v == o["pid"])
            user = next((m for m in meshes if m.get("name") == nm), None)
            if user is not None:
                m, r = _read_mesh(raw, mesh_src)
                # ⛔ 先把源网格的「法线/切线/UV1」按 (位置,UV0) 收好 —— 覆盖 m 的顶点数据之前
                src_attrs = _src_channel_map(m)
                if src_attrs:
                    print("[copy-full] %s：源网格法线/UV1 照抄表 %d 条" % (m.m_Name, len(src_attrs)))
                skin = [(int(user["bones"][i][0]), int(user["bones"][i][1]),
                         float(user["weights"][i][0]), float(user["weights"][i][1]))
                        for i in range(len(user["positions"]))]
                faces = []
                ui = 0
                for tt in range(0, len(user["triangles"]), 3):
                    a, b, c = user["triangles"][tt], user["triangles"][tt + 1], user["triangles"][tt + 2]
                    faces.append(((a, ui), (b, ui + 1), (c, ui + 2)))
                    ui += 3
                # ⛔ v1.8.76：把目标网格本体传下去，让顶点数据照**它自己的通道表**写
                #    （bundle 里有 29 种顶点布局，写死 68 字节的那种只占 4.8%）
                vbytes, ibytes, vcount = build_mesh_data(
                    user["positions"], user["uv"], faces, skin, skin_mode=2,
                    layout_from=m, src_attrs=src_attrs)
                m.m_VertexData.m_VertexCount = vcount
                m.m_VertexData.m_DataSize = vbytes
                m.m_IndexBuffer = list(ibytes)
                m.m_IndexFormat = 1 if vcount > 65535 else 0
                if m.m_SubMeshes:
                    sub = m.m_SubMeshes[0]
                    sub.indexCount = vcount
                    sub.firstByte = 0
                    sub.firstVertex = 0
                    sub.vertexCount = vcount
                    sub.topology = 0
                # 蒙皮哈希/绑定姿势：仅当用户网格带了骨骼名才更新（否则保留原样，保贴图动画）
                bn = user.get("bone_names")
                if bn:
                    hashes = []
                    bp = []
                    n_copy_bp = n_copy_h = 0
                    for b in bn:
                        # ① 哈希：原版有就照抄（原版才是权威），否则查表
                        h = src_hash.get(b)
                        if h is not None:
                            n_copy_h += 1
                        else:
                            h = bone_hashes.get(b)
                            if h is None:
                                h = hash_lookup.get(b)
                            if h is None:
                                raise ValueError("骨骼 %s 既不在挂载点树里、也没有已知哈希" % b)
                        hashes.append(h & 0xFFFFFFFF)
                        # ② 绑定姿势：保持外观（见 src_world 处的推导）
                        mb = src_bind.get(b)
                        if b not in world:
                            raise ValueError("骨骼 %s 不在骨骼树里，无法算绑定姿势" % b)
                        wo = src_world.get(b)
                        if mb is not None and wo is not None:
                            bp.append(_mul(_invert(world[b]), _mul(wo, mb)))
                            n_copy_bp += 1
                        else:
                            bp.append(_invert(world[b]))
                    m.m_BoneNameHashes = hashes
                    root_h = bone_hashes.get("body")
                    if root_h is None:
                        root_h = hash_lookup.get("body")
                    m.m_RootBoneNameHash = (root_h if ("body" in bn and root_h is not None)
                                            else hashes[0]) & 0xFFFFFFFF
                    m.m_BindPose = bp
                    print("[copy-full] %s：绑定姿势照抄原版 %d 根 / 重算 %d 根；哈希照抄 %d 根"
                          % (m.m_Name, n_copy_bp, len(bn) - n_copy_bp, n_copy_h))
                r.save_typetree(m)
                raw = r.data
        elif o["type_name"] == "SkinnedMeshRenderer":
            # 网格被替换 ⇒ SMR 的 m_Bones 必须与 Mesh 的 m_BindPose / m_BoneNameHashes
            # **同序同长**，否则顶点骨索引指向错的骨（新挂载点最明显：桨叶不跟着旋翼转）。
            # 全部名字都能解析时才改；任一缺失就原样保留（宁可不改，也不制造错位）。
            try:
                r = ObjectReader(assets_file=sf, reader=sf.reader, path_id=1,
                                 type_id=smr_src.type_id, serialized_type=smr_src.serialized_type,
                                 class_id=smr_src.class_id, type=smr_src.type,
                                 byte_start=0, byte_size=len(raw), is_destroyed=False,
                                 is_stripped=False, data=raw)
                node = r._get_typetree_node()
                er = EndianBinaryReader(raw, endian=sf.reader.endian)
                s = TypeTreeHelper.read_typetree(node, er, as_dict=False, assetsfile=sf,
                                                 byte_size=len(raw), check_read=False)
                mp = s.m_Mesh.m_PathID if s.m_Mesh else 0
                bn = mesh_bone_names.get(mp)
                if bn and all(b in tr_pid_of for b in bn):
                    s.m_Bones = [PPtr(m_FileID=0, m_PathID=tr_pid_of[b], assetsfile=sf)
                                 for b in bn]
                    if "body" in tr_pid_of:
                        s.m_RootBone = PPtr(m_FileID=0, m_PathID=tr_pid_of["body"], assetsfile=sf)
                    r.save_typetree(s)
                    raw = r.data
            except Exception as e:  # noqa: BLE001 - 同步失败不阻断构建
                print("[copy-full] SMR 骨骼同步跳过：%s" % e)
        elif o["type_name"] == "Transform":
            # 找该 Transform 对应的 GO 名，若在用户挂点树里则 patch 位置
            gid = None
            # tr_by_go 是 go_pid -> tr_pid，反查
            gid = next((g for g, t in tr_by_go.items() if t == o["pid"]), None)
            if gid is not None:
                gname = next((n for n, g in go_by_name.items() if g == gid), None)
                if gname is not None:
                    user_node = next((n for n in bone_tree if n[0] == gname), None)
                    extra_kids = patch_children.get(o["pid"])
                    if user_node is not None or extra_kids:
                        r = ObjectReader(assets_file=sf, reader=sf.reader, path_id=1,
                                         type_id=tr_src.type_id, serialized_type=tr_src.serialized_type,
                                         class_id=tr_src.class_id, type=tr_src.type,
                                         byte_start=0, byte_size=len(raw), is_destroyed=False,
                                         is_stripped=False, data=raw)
                        node = r._get_typetree_node()
                        er = EndianBinaryReader(raw, endian=sf.reader.endian)
                        t = TypeTreeHelper.read_typetree(node, er, as_dict=False, assetsfile=sf,
                                                         byte_size=len(raw), check_read=False)
                        if user_node is not None:
                            nm, parent, pos, rot, scale = user_node
                            t.m_LocalPosition = Vector3f(pos[0], pos[1], pos[2])
                            t.m_LocalRotation = Quaternionf(rot[0], rot[1], rot[2], rot[3])
                            t.m_LocalScale = Vector3f(scale[0], scale[1], scale[2])
                        if extra_kids:
                            kids = [c.m_PathID for c in (t.m_Children or []) if c and c.m_PathID]
                            for k in extra_kids:
                                if k not in kids:
                                    kids.append(k)
                            t.m_Children = [PPtr(m_FileID=0, m_PathID=k, assetsfile=sf)
                                            for k in kids]
                        r.save_typetree(t)
                        raw = r.data
        elif o["type_name"] == "MonoBehaviour":
            # 应用编辑后的动画：找到 AnimationHub，用 JSON 重序列化替换其字节
            if hub_json and len(raw) >= 28 and struct.unpack_from("<q", raw, 20)[0] == HUB_SCRIPT:
                hub_root = struct.unpack_from("<q", raw, 4)[0]
                raw = json_to_hub(hub_json, hub_root, _pid_of)
                hub_created[0] = True
            # v1.8.90：⑧ 面板改过的**其它组件**（AnimationManager / UnitPrefabTurretInfo /
            # FmodTurretsTurn / SkinStorageBridge …）。按 pid 精确命中，逐个重序列化。
            # ⛔ 单个组件失败**不阻断**构建（只跳过并打印）—— 但它绝不会静默：
            #    面板上「写回模型」那一步已经做过"面板→字节→回读"校验 ✓
            ed = comp_edits.get(int(o["pid"])) if comp_edits else None
            if ed:
                try:
                    raw = _emit_comp(ed, raw)
                    comp_done[0] += 1
                    comp_hit.add(int(o["pid"]))
                except Exception as e:  # noqa: BLE001
                    print("[copy-full] ⚠ 组件 %s (pid=%s) 写回失败：%s —— 该组件保持原样"
                          % (ed.get("cls"), o["pid"], e))
        new_objects.append({"pid": o["pid"], "class_id": o["class_id"],
                            "type_name": o["type_name"], "script_id": o.get("script_id"),
                            "tree_hash": o.get("tree_hash"),
                            "raw": base64.b64encode(raw).decode("ascii")})

    # ---- 该 prefab 原本**没有** AnimationHub ⇒ 新建一个（从零自制动画）----
    # ⛔ 以前这里**静默什么都不做**：用户在 ④ 面板辛苦加的动画，构建时被无声丢掉，
    #    包导进游戏一看——毫无反应，也没有任何报错 ✗（最恶劣的一种失败）。
    #    现在：把 AnimationHub 作为**新 MonoBehaviour** 挂到 prefab 根 GameObject 上，
    #    并入 manifest（导入端会分配 pid + 重映射里面的全部 pid 引用）。
    if hub_json and not hub_created[0]:
        hub_tpl = _find_hub_template(objs)
        if hub_tpl is None:
            print("[copy-full] ⚠ 本 bundle 里找不到任何 AnimationHub 作类型模板 ⇒ "
                  "无法新建 AnimationHub；这个模型会**没有动画**")
        else:
            try:
                alloc2 = _make_alloc(set(sf.objects.keys()), MOD_PID_BASE)
                # 根 GameObject 就是容器条目指向的那个（= 上面解析出的真实根 pid）
                root_go = int(root_pid)
                hub_bytes = json_to_hub(hub_json, root_go, _pid_of)
                hub_pid = alloc2()
                ident = _type_identity(hub_tpl)
                sf.objects[hub_pid] = ObjectReader(
                    assets_file=sf, reader=sf.reader, path_id=hub_pid,
                    type_id=hub_tpl.type_id, serialized_type=hub_tpl.serialized_type,
                    class_id=hub_tpl.class_id, type=hub_tpl.type,
                    byte_start=0, byte_size=len(hub_bytes),
                    is_destroyed=False, is_stripped=False, data=hub_bytes)
                # 挂进根 GameObject 的组件表。
                # ⛔ 在**manifest 里那条已收集的字节**上改（而不是改 sf.objects）：
                #    根 GO 原样字节就在 `new_objects` 里，改它不会产生同 pid 的第二条；
                #    也不能用 `_read_from_data(sf.objects[root_go])` —— 那是**懒加载**
                #    对象，`.data` 是 None ⇒ EndianBinaryReader 直接 TypeError ✗（实测）。
                go_entry = next((e for e in new_objects if e["pid"] == root_go), None)
                if go_entry is None:
                    print("[copy-full] ⚠ 根 GameObject(pid=%s) 不在 manifest 对象集里 ⇒ "
                          "新建的 AnimationHub 挂不上去" % root_go)
                else:
                    raw_go = base64.b64decode(go_entry["raw"])
                    rg = ObjectReader(assets_file=sf, reader=sf.reader, path_id=1,
                                      type_id=go_src.type_id,
                                      serialized_type=go_src.serialized_type,
                                      class_id=go_src.class_id, type=go_src.type,
                                      byte_start=0, byte_size=len(raw_go),
                                      is_destroyed=False, is_stripped=False, data=raw_go)
                    gnode = rg._get_typetree_node()
                    ger = EndianBinaryReader(raw_go, endian=sf.reader.endian)
                    g = TypeTreeHelper.read_typetree(gnode, ger, as_dict=False,
                                                     assetsfile=sf, byte_size=len(raw_go),
                                                     check_read=False)
                    cur = [c.component.m_PathID for c in (g.m_Component or [])
                           if c and c.component]
                    if hub_pid in cur:
                        print("[copy-full] 根节点上已经有 AnimationHub 组件，跳过")
                    else:
                        comps = list(g.m_Component or [])
                        comps.append(ComponentPair(component=PPtr(
                            m_FileID=0, m_PathID=hub_pid, assetsfile=sf)))
                        g.m_Component = comps
                        rg.save_typetree(g)
                        go_entry["raw"] = base64.b64encode(rg.data).decode("ascii")
                        # ⛔ `script_id` / `tree_hash` 必须带上：导入端 `_match_type` 靠它
                        #    精确匹配到 **AnimationHub** 这个脚本类型；留空会退化成
                        #    "第一个 class_id=114 的类型" ⇒ 挂错脚本 ✗
                        _dump_new(hub_pid)
                        for e in new_objs_extra:
                            if e["pid"] == hub_pid:
                                e["script_id"] = ident.get("script_id")
                                e["tree_hash"] = ident.get("tree_hash")
                        hub_created[0] = True
                        print("[copy-full] 该 prefab 原本没有 AnimationHub ⇒ 已新建 "
                              "(pid=%s, script_id=%s, 挂到根 GO %s)"
                              % (hub_pid, ident.get("script_id"), root_go))
            except Exception as e:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                print("[copy-full] ⚠ 新建 AnimationHub 失败：%s" % e)

    # 新增节点并入 manifest（对象 + preload），导入端会给它们分配 pid 并重映射引用
    new_objects.extend(new_objs_extra)
    # ⛔ v1.8.72：`new_prefab` **留空 = 原地替换"源 prefab"**（界面上的说明就是这么写的）。
    #    以前这里直接写 `new_prefab` ⇒ 空串 ⇒ 导入端 `if prefab_path and has_objects:`
    #    不成立 ⇒ **既不写容器条目、也不清理旧对象** ✗ ⇒
    #    游戏仍然按**原来的**容器条目加载**原来那个 prefab** ⇒
    #    新对象全成了没人引用的孤儿 ⇒ **改了半个小时的模型进游戏一点变化都没有** ✗✗
    #    （实测确认：UI 走「新 prefab 路径留空」时产出的 manifest.prefab_path 就是 ''）
    #    `src_path` 是 `collect_prefab_objects()` 返回的源 prefab 容器路径，
    #    用它 = 删掉同名条目 + 追加同名新条目 ⇒ 真正的"原地替换" ✓
    #    填了别的路径 = 注册成**新 prefab**（原 prefab 不动，即"克隆成一个新单位"）✓
    manifest = {
        "format": FORMAT, "version": VERSION, "mode": "copy-full",
        "bundle": "units_assets_all", "prefab_path": (new_prefab or src_path),
        "root_pid": root_pid,
        "preload": list(src_preload) + [e["pid"] for e in new_objs_extra],
        "objects": new_objects,
        "added_nodes": [n for n in _tree_names if n in go_pid_of and n in set(added)],
    }
    with zipfile.ZipFile(out_pack, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
    # v1.8.90：组件改动必须**报出来**。⑧ 面板上写的那些字段（车身抖动、车速、炮塔转速…）
    #   如果因为 pid 变了没命中，用户从日志里必须看得见 —— 否则就是"调了半天没反应"✗
    comp_note = ""
    if comp_edits:
        comp_note = "| 组件改动 %d/%d" % (comp_done[0], len(comp_edits))
        if comp_done[0] != len(comp_edits):
            miss = [k for k in comp_edits if k not in comp_hit]
            print("[copy-full] ⚠ 有 %d 个组件改动**没命中**（pid 已变）：%s\n"
                  "           ⇒ 请回 ⑧ 面板重新「扫描组件」再写回一次"
                  % (len(miss), ", ".join(str(m) for m in miss[:8])))
    return ("DONE copy-full -> %s | %d 对象（新增节点 %d%s）| src=%s%s"
            % (out_pack, len(new_objects), len(new_objs_extra),
               ("：" + ", ".join(added) if added else ""), src_path, comp_note),
            manifest["root_pid"])
