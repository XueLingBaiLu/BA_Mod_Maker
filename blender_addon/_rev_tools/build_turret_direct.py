# -*- coding: utf-8 -*-
r"""直接构造炮塔 prefab（无 deep-copy、无模板）。

从零构造一个最小合法炮塔模块 prefab：
  root -> body(根骨) -> turret_0(旋转骨+UnitPrefabTurretInfo)
                      +- weapon_0(俯仰挂点)
                      +- shell_spawn_0(开火点)
       -> mesh GO(SMR + 立方体蒙皮网格)

每个对象都是「读一个同类型对象当 typetree 参考 -> 覆写全部数据 -> 存为新对象」，
不做 pathID 重映射、不搬模板层级，结构从一开始就正确。

用法:
    from build_turret_direct import build_turret
    log, new_root, new_mesh_pid = build_turret(
        bundle, out_bundle, new_prefab, new_name,
        positions, triangles, uv, bones, weights)
"""
import sys, os, struct, math

HERE = os.path.dirname(os.path.abspath(__file__))
try:
    import UnityPy  # noqa: F401  （Blender 里用 pip 装的；本地测试用工作区副本）
except ImportError:
    # `_unitypy` 有两份（cp314 / cp313）：统一按解释器 tag 挑（说明见 unitypy_path.py）
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    from unitypy_path import ensure as _ensure_unitypy
    _ensure_unitypy(HERE)
    import UnityPy
from UnityPy.files.ObjectReader import ObjectReader
from UnityPy.classes.PPtr import PPtr
from UnityPy.classes.generated import AssetInfo, ComponentPair
from UnityPy.classes.math import Vector3f, Quaternionf, Matrix4x4f

BONE_NAME_HASHES = {
    "body": 0x65A7524A,
    "turret_0": 0x2A839EFE,
    "weapon_0_1_2": 0xBF21869B,
    "recoil_0_0": 0x5A6309D7,
}

UNITPREFABTURRETINFO_SCRIPT = 6426374804064612000
ANIMATIONHUB_SCRIPT = 4665939560152279323
ANIMATIONMANAGERBRIDGE_SCRIPT = 8775279424834731323

# 军械库演示动画源（RU_BMP2M 炮塔，units_assets_all 内）：
#   AnimationHub pid=4361187508110648207（764 字节，demo 状态 = 1 个 AxisRandom 扫掠 turret_0）
#   AnimationManagerBridge pid=-8929406280605604977（448 字节，后坐力/开火联动）
SRC_HUB_PID = 4361187508110648207
SRC_BRIDGE_PID = -8929406280605604977
# RU_BMP2M 根/骨骼 Transform pathID（字节替换用）
SRC_RU_BMP2M_ROOT_GO = 2938015386852811663
SRC_RU_BMP2M_SHELL_0 = 8824504462917985167
SRC_RU_BMP2M_SHELL_1 = 3194859834443260815

# 原版炮塔 prefab 的骨骼结构（所有炮塔通用，从 AssetRipper 导出对比得出）：
# body 骨在 y=0.863，turret_0 骨在 body 上方 0.811（世界 y=1.674）。
# 游戏把炮塔 prefab 的 root 挂在车辆原点，炮塔几何在 y≈1.7 才是正确高度。
BODY_LOCAL_Y = 0.863
TURRET_LOCAL_Y = 0.811
TURRET_WORLD_Y = BODY_LOCAL_Y + TURRET_LOCAL_Y  # 1.674

_IDENTITY4 = Matrix4x4f(1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0)


def _trans(x, y, z):
    return Matrix4x4f(1.0, 0.0, 0.0, x, 0.0, 1.0, 0.0, y, 0.0, 0.0, 1.0, z, 0.0, 0.0, 0.0, 1.0)


def _make_alloc(existing, base):
    i = [0]
    def alloc():
        while True:
            cand = base + i[0]; i[0] += 1
            if cand not in existing:
                existing.add(cand)
                return cand
    return alloc


def _new_reader(src_obj, dst_sf, new_pid):
    return ObjectReader(assets_file=dst_sf, reader=dst_sf.reader, path_id=new_pid,
                        type_id=src_obj.type_id, serialized_type=src_obj.serialized_type,
                        class_id=src_obj.class_id, type=src_obj.type,
                        byte_start=src_obj.byte_start, byte_size=0,
                        is_destroyed=src_obj.is_destroyed, is_stripped=src_obj.is_stripped)


def _save(dst_sf, src_obj, new_pid, obj):
    r = _new_reader(src_obj, dst_sf, new_pid)
    r.save_typetree(obj)
    dst_sf.objects[new_pid] = r
    return r


def _read_from_data(obj_reader):
    from UnityPy.streams import EndianBinaryReader
    from UnityPy.helpers import TypeTreeHelper
    node = obj_reader._get_typetree_node()
    data = obj_reader.data
    r = EndianBinaryReader(data, endian=obj_reader.reader.endian)
    obj = TypeTreeHelper.read_typetree(node, r, as_dict=False, assetsfile=obj_reader.assets_file,
                                       byte_size=len(data), check_read=False)
    obj.set_object_reader(obj_reader)
    return obj


def _mb_script_pid(o):
    raw = o.get_raw_data()
    return struct.unpack_from("<q", raw, 20)[0] if len(raw) >= 28 else None


def _find_sources(objs, by_pid):
    tr_go = {}; go_tr = {}; go_comps = {}
    for o in objs:
        if o.type.name == "Transform":
            tr = o.read(); g = tr.m_GameObject.m_PathID if tr.m_GameObject else 0
            tr_go[o.path_id] = g; go_tr[g] = o.path_id
        elif o.type.name == "GameObject":
            go_comps[o.path_id] = [c.m_PathID for c in (o.read().m_Components or []) if c and c.m_PathID]
    empty_go = None
    for gpid in go_comps:
        if gpid in go_tr:
            empty_go = gpid
            break
    if empty_go is None:
        raise RuntimeError("找不到 GameObject")

    def _matching(m):
        # 必须匹配 build_mesh_data 写的布局：stream1 有 2 个 UV 通道 + stream2 有 weights+bones
        chans = m.m_VertexData.m_Channels
        n_uv = sum(1 for c in chans if c.stream == 1 and (c.dimension & 0xF) == 2)
        s2 = [c for c in chans if c.stream == 2]
        has_w = any(c.format == 0 and (c.dimension & 0xF) == 2 for c in s2)
        has_b = any(c.format == 10 and (c.dimension & 0xF) == 2 for c in s2)
        return n_uv == 2 and has_w and has_b

    smr_src = None
    mesh_src = None
    for o in objs:
        if o.type.name == "SkinnedMeshRenderer" and smr_src is None:
            r = o.read()
            if r.m_Mesh and r.m_Mesh.m_PathID in by_pid and r.m_Bones:
                m = by_pid[r.m_Mesh.m_PathID].read()
                if _matching(m):
                    smr_src = o
                    mesh_src = by_pid[r.m_Mesh.m_PathID]
                    break
    if smr_src is None:
        raise RuntimeError("找不到匹配布局(2UV+双骨)的 SkinnedMeshRenderer")

    ti_src = None
    for o in objs:
        if o.type.name == "MonoBehaviour":
            if _mb_script_pid(o) == UNITPREFABTURRETINFO_SCRIPT:
                ti_src = o
                break
    if ti_src is None:
        raise RuntimeError("找不到 UnitPrefabTurretInfo MonoBehaviour")

    lod_src = None
    for o in objs:
        if o.type.name == "LODGroup":
            lod_src = o
            break
    if lod_src is None:
        raise RuntimeError("找不到 LODGroup")

    # 演示动画组件源（军械库演示用）。优先用已知的 RU_BMP2M 源；
    # bundle 更新后回退为任意 AnimationHub / AnimationManagerBridge。
    hub_src = by_pid.get(SRC_HUB_PID)
    bridge_src = by_pid.get(SRC_BRIDGE_PID)
    for o in objs:
        if o.type.name != "MonoBehaviour":
            continue
        sp = _mb_script_pid(o)
        if hub_src is None and sp == ANIMATIONHUB_SCRIPT:
            hub_src = o
        if bridge_src is None and sp == ANIMATIONMANAGERBRIDGE_SCRIPT:
            bridge_src = o
    if hub_src is None:
        raise RuntimeError("找不到 AnimationHub 源")
    if bridge_src is None:
        raise RuntimeError("找不到 AnimationManagerBridge 源")

    hash_lookup = dict(BONE_NAME_HASHES)
    try:
        m = mesh_src.read()
        smr = smr_src.read()
        hashes = list(m.m_BoneNameHashes)
        for i, b in enumerate(smr.m_Bones):
            t = by_pid.get(b.m_PathID)
            if not t:
                continue
            tr = t.read(); gid = tr.m_GameObject.m_PathID if tr.m_GameObject else 0
            go = by_pid.get(gid)
            if go and i < len(hashes):
                nm = go.read().m_Name
                if nm and nm not in hash_lookup:
                    hash_lookup[nm] = hashes[i]
    except Exception:
        pass
    return empty_go, go_tr[empty_go], smr_src, mesh_src, ti_src, lod_src, hub_src, bridge_src, hash_lookup


def _make_mesh(sf, alloc, mesh_src, positions, triangles, uv, bones, weights, bone_hashes):
    from turret_swap import build_mesh_data
    faces = []
    ui = 0
    for t in range(0, len(triangles), 3):
        a, b, c = triangles[t], triangles[t + 1], triangles[t + 2]
        faces.append(((a, ui), (b, ui + 1), (c, ui + 2)))
        ui += 3
    skin = [(int(bones[i][0]), int(bones[i][1]), float(weights[i][0]), float(weights[i][1]))
            for i in range(len(positions))]
    # 几何上移到炮塔高度（root 在原点，几何应在 y≈1.674，和原版炮塔一致）
    positions = [(x, y + TURRET_WORLD_Y, z) for (x, y, z) in positions]
    vbytes, ibytes, vcount = build_mesh_data(positions, uv, faces, skin, skin_mode=2)

    m = mesh_src.read()
    m.m_Name = "cube"
    m.m_VertexData.m_VertexCount = vcount
    m.m_VertexData.m_DataSize = vbytes
    m.m_IndexBuffer = list(ibytes)
    m.m_IndexFormat = 1 if vcount > 65535 else 0
    sub = m.m_SubMeshes[0]
    sub.indexCount = vcount
    sub.firstByte = 0
    sub.firstVertex = 0
    sub.vertexCount = vcount
    sub.topology = 0
    m.m_BoneNameHashes = [bone_hashes["body"], bone_hashes["turret_0"]]
    m.m_RootBoneNameHash = bone_hashes["body"]
    # 绑定姿势 = 骨骼绑定世界矩阵的逆（body 在 y=0.863，turret_0 世界 y=1.674）
    m.m_BindPose = [_trans(0.0, -BODY_LOCAL_Y, 0.0), _trans(0.0, -TURRET_WORLD_Y, 0.0)]
    return _save(sf, mesh_src, alloc(), m)


def _make_go(sf, alloc, go_src, tr_src, name, parent_tr, local_pos=(0.0, 0.0, 0.0)):
    gpid = alloc(); trpid = alloc()
    tr = tr_src.read()
    tr.m_GameObject = PPtr(m_FileID=0, m_PathID=gpid, assetsfile=sf)
    tr.m_Father = PPtr(m_FileID=0, m_PathID=parent_tr, assetsfile=sf) if parent_tr else PPtr(m_FileID=0, m_PathID=0, assetsfile=sf)
    tr.m_Children = []
    tr.m_LocalPosition = Vector3f(*local_pos)
    tr.m_LocalRotation = Quaternionf(0.0, 0.0, 0.0, 1.0)
    tr.m_LocalScale = Vector3f(1.0, 1.0, 1.0)
    _save(sf, tr_src, trpid, tr)

    go = go_src.read()
    go.m_Name = name
    go.m_Layer = 0
    go.m_IsActive = 1
    # 最小 GO 也要含 Transform（游戏里所有 GO 的 m_Component 首项都是 Transform）
    go.m_Component = [ComponentPair(component=PPtr(m_FileID=0, m_PathID=trpid, assetsfile=sf))]
    _save(sf, go_src, gpid, go)
    return gpid, trpid


def _attach_child(sf, parent_tr_obj, child_trpid):
    tr = _read_from_data(parent_tr_obj)
    kids = [c.m_PathID for c in (tr.m_Children or []) if c and c.m_PathID]
    if child_trpid not in kids:
        kids.append(child_trpid)
    tr.m_Children = [PPtr(m_FileID=0, m_PathID=k, assetsfile=sf) for k in kids]
    parent_tr_obj.save_typetree(tr)


def _synth_turret_info(sf, alloc, ti_src, turret_gpid, weapon_trpids, shell_trpids, turret_index):
    n = len(weapon_trpids)
    buf = bytearray()
    buf += struct.pack("<iq", 0, turret_gpid)
    buf += struct.pack("<B", 1)
    buf += bytes(3)
    buf += struct.pack("<iq", 0, UNITPREFABTURRETINFO_SCRIPT)
    buf += struct.pack("<i", 0)
    buf += struct.pack("<i", turret_index)
    buf += struct.pack("<i", n)
    for i in range(n):
        buf += struct.pack("<iq", 0, weapon_trpids[i])
        buf += struct.pack("<iq", 0, shell_trpids[i])
    pid = alloc()
    r = ObjectReader(assets_file=sf, reader=sf.reader, path_id=pid,
                     type_id=ti_src.type_id, serialized_type=ti_src.serialized_type,
                     class_id=ti_src.class_id, type=ti_src.type,
                     byte_start=ti_src.byte_start, byte_size=len(buf),
                     is_destroyed=ti_src.is_destroyed, is_stripped=ti_src.is_stripped,
                     data=bytes(buf))
    sf.objects[pid] = r
    return pid


def _clean_stale_build(sf):
    r"""幂等清理：移除旧构建残留的 MY_TURRET 对象与 preload 条目。

    早期版本从"已部署 bundle"累积构建，每次都会留下上一代的对象
    （0x4355424500000000 起的 pid 段专属本工具），旧 TI 等对象连同
    旧 preload 条目永远留在 bundle 里，战场加载时导致
    "Read 32 bytes but expected 64 bytes" 崩溃（军械库不受影响）。
    清理后 preload 表会重映射所有容器的 preloadIndex。
    """
    BASE = 0x4355424500000000
    BASE_RANGE = 0x10000
    ab_obj = next(o for o in sf.objects.values() if o.type.name == "AssetBundle")
    ab = ab_obj.read()
    old_pl = list(ab.m_PreloadTable)
    keep = [not (BASE <= (p.m_PathID or 0) < BASE + BASE_RANGE) for p in old_pl]
    if all(keep):
        return
    remap = {}
    ni = 0
    for i in range(len(old_pl)):
        remap[i] = ni
        if keep[i]:
            ni += 1
    ab.m_PreloadTable = [p for i, p in enumerate(old_pl) if keep[i]]
    for n, a in ab.m_Container:
        if a.preloadIndex in remap:
            a.preloadIndex = remap[a.preloadIndex]
    ab_obj.save_typetree(ab)
    removed = 0
    for pid in list(sf.objects.keys()):
        if BASE <= pid < BASE + BASE_RANGE:
            sf.objects.pop(pid, None)
            removed += 1
    print("[清理] 移除旧构建对象 %d 个、preload 条目 %d 条" % (removed, len(old_pl) - ni))


def _synth_animation_hub(sf, alloc, hub_src, root_gpid, turret_trpid):
    r"""从零合成最小 AnimationHub：仅 demo 状态含 1 个 AxisRandom。

    AxisRandom 数据布局（从 RU_BMP2M 原版 Hub 逆向解析，80 字节）：
      lod(int32) + _inspectorName(string) + _speed/_minimalTime/_maximalTime(float×3)
      + _x/_y/_z 三个 AxisRandContainer(PPtr<Transform> 12 + Min/MaxAngle float×2)
    演示行为：每 2~10 秒随机一次，turret_0 绕 Y 轴在 -45°~+45° 间随机旋转（speed=20）。
    SerializeReference 结构：数组元素 = rid(int64)，尾部 ManagedReferencesRegistry
    (version=2 + 条目数 + rid + class/ns/asm 字符串 + 数据)。
    """
    rid = 1
    buf = bytearray()
    buf += struct.pack("<iq", 0, root_gpid)             # m_GameObject PPtr
    buf += struct.pack("<B", 1)                          # m_Enabled
    buf += bytes(3)
    buf += struct.pack("<iq", 0, ANIMATIONHUB_SCRIPT)    # m_Script PPtr
    buf += struct.pack("<i", 0)                          # m_Name
    buf += struct.pack("<i", 0)                          # _universalState: 空
    buf += struct.pack("<i", 1)                          # _demoState: 1 个元素
    buf += struct.pack("<q", rid)
    buf += struct.pack("<i", 0)                          # _gameState: 空
    buf += struct.pack("<i", 0)                          # _preDeathState: 空
    buf += struct.pack("<i", 0)                          # _deathState: 空
    buf += struct.pack("<i", 2)                          # references.version
    buf += struct.pack("<i", 1)                          # RefIds 条目数
    buf += struct.pack("<q", rid)
    buf += struct.pack("<i", 10); buf += b"AxisRandom"; buf += bytes(2)
    buf += struct.pack("<i", 41); buf += b"BrokenArrow.Client.Ecs.AnimationBehaviors"; buf += bytes(3)
    buf += struct.pack("<i", 11); buf += b"BrokenArrow"; buf += bytes(1)
    # AxisRandom 数据
    buf += struct.pack("<i", 1)                          # _lodGroup
    buf += struct.pack("<i", 0)                          # _inspectorName 空串
    buf += struct.pack("<fff", 20.0, 2.0, 10.0)          # _speed/_minimalTime/_maximalTime
    buf += struct.pack("<iqff", 0, 0, 0.0, 0.0)          # _x 空
    buf += struct.pack("<iqff", 0, turret_trpid, -45.0, 45.0)  # _y = turret_0 扫掠
    buf += struct.pack("<iqff", 0, 0, 0.0, 0.0)          # _z 空
    pid = alloc()
    r = ObjectReader(assets_file=sf, reader=sf.reader, path_id=pid,
                     type_id=hub_src.type_id, serialized_type=hub_src.serialized_type,
                     class_id=hub_src.class_id, type=hub_src.type,
                     byte_start=hub_src.byte_start, byte_size=len(buf),
                     is_destroyed=hub_src.is_destroyed, is_stripped=hub_src.is_stripped,
                     data=bytes(buf))
    sf.objects[pid] = r
    return pid


def _copy_animation_bridge(sf, alloc, bridge_src, root_gpid, shell_trpid):
    r"""字节复制原版 AnimationManagerBridge，把引用替换到我的对象上。

    桥接 recoil/VFX 联动（开火时炮口后坐等），原版引用 shell_spawn_0_0/shell_spawn_1_0，
    全部替换到我的 shell_spawn_0。

    注意：当前 build_turret 已禁用该组件 —— 其 _recoils 字典的键必须是蒙皮骨骼，
    立方体炮塔的 shell_spawn 是空物体，战场 BoneContainer.Bake 解析时会崩溃。
    若将来炮塔带真实骨骼，可重新启用。
    """
    raw = bytearray(bridge_src.get_raw_data())
    raw = raw.replace(struct.pack("<q", SRC_RU_BMP2M_ROOT_GO), struct.pack("<q", root_gpid))
    raw = raw.replace(struct.pack("<q", SRC_RU_BMP2M_SHELL_0), struct.pack("<q", shell_trpid))
    raw = raw.replace(struct.pack("<q", SRC_RU_BMP2M_SHELL_1), struct.pack("<q", shell_trpid))
    pid = alloc()
    r = ObjectReader(assets_file=sf, reader=sf.reader, path_id=pid,
                     type_id=bridge_src.type_id, serialized_type=bridge_src.serialized_type,
                     class_id=bridge_src.class_id, type=bridge_src.type,
                     byte_start=bridge_src.byte_start, byte_size=len(raw),
                     is_destroyed=bridge_src.is_destroyed, is_stripped=bridge_src.is_stripped,
                     data=bytes(raw))
    sf.objects[pid] = r
    return pid


def build_turret(bundle, out_bundle, new_prefab, new_name,
                 positions, triangles, uv, bones, weights,
                 turret_index=0, weapon_names=None, shell_names=None,
                 bone_hashes=None, mount_points=None, material_pid=None, env=None,
                 include_hub=True):
    weapon_names = weapon_names or ["weapon_0"]
    shell_names = shell_names or ["shell_spawn_0"]
    assert len(weapon_names) == len(shell_names)
    mount_points = mount_points or {}

    if env is None:
        env = UnityPy.load(bundle)
    sf = list(env.objects)[0].assets_file
    _clean_stale_build(sf)
    objs = list(sf.objects.values())
    by_pid = {o.path_id: o for o in objs}

    go_src_gpid, go_src_trpid, smr_src, mesh_src, ti_src, lod_src, hub_src, bridge_src, hash_lookup = _find_sources(objs, by_pid)
    go_src = by_pid[go_src_gpid]
    tr_src = by_pid[go_src_trpid]
    if bone_hashes:
        hash_lookup.update(bone_hashes)

    existing = set(by_pid.keys())
    alloc = _make_alloc(existing, 0x4355424500000000)

    mesh_pid = _make_mesh(sf, alloc, mesh_src, positions, triangles, uv, bones, weights, hash_lookup).path_id

    root_gpid, root_trpid = _make_go(sf, alloc, go_src, tr_src, new_name, 0)
    body_gpid, body_trpid = _make_go(sf, alloc, go_src, tr_src, "body", root_trpid, local_pos=(0.0, BODY_LOCAL_Y, 0.0))
    turret_gpid, turret_trpid = _make_go(sf, alloc, go_src, tr_src, "turret_0", body_trpid, local_pos=(0.0, TURRET_LOCAL_Y, 0.0))
    _attach_child(sf, sf.objects[root_trpid], body_trpid)
    _attach_child(sf, sf.objects[body_trpid], turret_trpid)

    weapon_trpids = []; shell_trpids = []
    mount_pids = []
    for wn, sn in zip(weapon_names, shell_names):
        wp = mount_points.get(wn, (0.0, 0.0, 0.0))
        sp = mount_points.get(sn, (0.0, 0.0, 0.0))
        wg, wtr = _make_go(sf, alloc, go_src, tr_src, wn, turret_trpid, local_pos=wp)
        sg, str_ = _make_go(sf, alloc, go_src, tr_src, sn, turret_trpid, local_pos=sp)
        _attach_child(sf, sf.objects[turret_trpid], wtr)
        _attach_child(sf, sf.objects[turret_trpid], str_)
        weapon_trpids.append(wtr); shell_trpids.append(str_)
        mount_pids.extend([wg, wtr, sg, str_])

    mesh_go_gpid, mesh_go_trpid = _make_go(sf, alloc, go_src, tr_src, "cube", root_trpid)
    _attach_child(sf, sf.objects[root_trpid], mesh_go_trpid)

    smr = smr_src.read()
    smr.m_Mesh = PPtr(m_FileID=0, m_PathID=mesh_pid, assetsfile=sf)
    smr.m_GameObject = PPtr(m_FileID=0, m_PathID=mesh_go_gpid, assetsfile=sf)
    smr.m_Bones = [PPtr(m_FileID=0, m_PathID=body_trpid, assetsfile=sf),
                   PPtr(m_FileID=0, m_PathID=turret_trpid, assetsfile=sf)]
    smr.m_RootBone = PPtr(m_FileID=0, m_PathID=body_trpid, assetsfile=sf)
    xs = [p[0] for p in positions]; ys = [p[1] for p in positions]; zs = [p[2] for p in positions]
    if xs:
        cx = (min(xs) + max(xs)) / 2; cy = (min(ys) + max(ys)) / 2; cz = (min(zs) + max(zs)) / 2
        ex = (max(xs) - min(xs)) / 2 or 0.5; ey = (max(ys) - min(ys)) / 2 or 0.5; ez = (max(zs) - min(zs)) / 2 or 0.5
        smr.m_AABB.m_Center = Vector3f(cx, cy, cz)
        smr.m_AABB.m_Extent = Vector3f(ex, ey, ez)
    smr_pid = alloc()
    _save(sf, smr_src, smr_pid, smr)
    mesh_go = sf.objects[mesh_go_gpid]
    mg = _read_from_data(mesh_go)
    mg.m_Component.append(ComponentPair(component=PPtr(m_FileID=0, m_PathID=smr_pid, assetsfile=sf)))
    mesh_go.save_typetree(mg)

    # LODGroup（原版炮塔 root 上都有；游戏 LOD 系统靠它管理渲染器可见性，没有它场景里不显示）
    # 用字节级复制原版 LODGroup（typetree 写会漏可选字段，序列化畸形导致游戏原生崩溃）
    lod_read = lod_src.read()
    raw_lod = bytearray(lod_src.get_raw_data())
    raw_lod = raw_lod.replace(struct.pack("<q", lod_read.m_GameObject.m_PathID), struct.pack("<q", root_gpid))
    for lod in lod_read.m_LODs:
        for lr in (lod.renderers or []):
            raw_lod = raw_lod.replace(struct.pack("<q", lr.renderer.m_PathID), struct.pack("<q", smr_pid))
    lod_pid = alloc()
    lod_obj = ObjectReader(assets_file=sf, reader=sf.reader, path_id=lod_pid,
                           type_id=lod_src.type_id, serialized_type=lod_src.serialized_type,
                           class_id=lod_src.class_id, type=lod_src.type,
                           byte_start=lod_src.byte_start, byte_size=len(raw_lod),
                           is_destroyed=lod_src.is_destroyed, is_stripped=lod_src.is_stripped,
                           data=bytes(raw_lod))
    sf.objects[lod_pid] = lod_obj
    root_go = sf.objects[root_gpid]
    rg = _read_from_data(root_go)
    rg.m_Component.append(ComponentPair(component=PPtr(m_FileID=0, m_PathID=lod_pid, assetsfile=sf)))
    root_go.save_typetree(rg)

    ti_pid = _synth_turret_info(sf, alloc, ti_src, turret_gpid, weapon_trpids, shell_trpids, turret_index)
    turret_go = sf.objects[turret_gpid]
    tg = _read_from_data(turret_go)
    tg.m_Component.append(ComponentPair(component=PPtr(m_FileID=0, m_PathID=ti_pid, assetsfile=sf)))
    turret_go.save_typetree(tg)

    # 军械库演示动画：AnimationHub(demo AxisRandom 扫掠 turret_0)
    # 逆向解析自原版 AnimationHub 的 SerializeReference 格式（见 _synth_animation_hub 注释）
    # 注意：不复制 AnimationManagerBridge —— 其 _recoils 字典的键必须是蒙皮骨骼，
    # 立方体的 shell_spawn 是空物体，战场 BoneContainer.Bake 解析时会崩溃（军械库不运行该逻辑）。
    hub_pid = 0
    if include_hub:
        hub_pid = _synth_animation_hub(sf, alloc, hub_src, root_gpid, turret_trpid)
        root_go2 = sf.objects[root_gpid]
        rg2 = _read_from_data(root_go2)
        rg2.m_Component.append(ComponentPair(component=PPtr(m_FileID=0, m_PathID=hub_pid, assetsfile=sf)))
        root_go2.save_typetree(rg2)

    ab_obj = next(o for o in objs if o.type.name == "AssetBundle")
    # ⛔ 必须用 `_read_from_data`（读 obj_reader.data = 内存当前状态），**不能** `.read()`：
    #    后者会 reset 后从流里重读，把上面 `_clean_stale_build()` 刚写进内存的清理结果
    #    丢掉 ⇒ 旧 preload/container 条目被写回、而它们指向的对象已从 sf.objects 删除
    #    ⇒ **悬挂引用 ⇒ 战场加载崩溃**，工具却仍报成功 ✗（import_pack 一直用正确写法）。
    ab = _read_from_data(ab_obj)
    # preload 顺序关键：MonoScript 必须排在 MB 之前。
    # 战场加载按容器 preload 表顺序反序列化，MB 的 m_Script 指向的 MonoScript
    # 尚未加载时会报 "script unknown or not yet loaded"（Read 32 but expected 64）并崩溃；
    # 军械库/编辑器走 Addressables 按需加载所以不受影响。原版炮塔的 preload 范围
    # 开头就是 5 个 MonoScript（实测 RU_BMPT2 炮塔 [74649..74653]）。
    preload_scripts = [UNITPREFABTURRETINFO_SCRIPT]
    if hub_pid:
        preload_scripts.append(ANIMATIONHUB_SCRIPT)
    all_pids = preload_scripts + [root_gpid, root_trpid, body_gpid, body_trpid,
                                  turret_gpid, turret_trpid, mesh_go_gpid, mesh_go_trpid,
                                  mesh_pid, smr_pid, ti_pid, lod_pid] + mount_pids
    if hub_pid:
        all_pids.append(hub_pid)
    pl = [PPtr(m_FileID=0, m_PathID=p, assetsfile=sf) for p in all_pids]
    new_start = len(ab.m_PreloadTable)
    ab.m_PreloadTable.extend(pl)
    ab.m_Container = [(n, a) for n, a in ab.m_Container if n != new_prefab]
    ab.m_Container.append((new_prefab, AssetInfo(asset=PPtr(m_FileID=0, m_PathID=root_gpid, assetsfile=sf),
                                                 preloadIndex=new_start, preloadSize=len(pl))))
    ab_obj.save_typetree(ab)

    from stream_save import ensure_stream_save
    ensure_stream_save()
    env.file.save_stream(out_bundle, "lz4")
    return ("DONE -> %s | root=%d mesh=%d turretInfo=%d hub=%d" % (
        out_bundle, root_gpid, mesh_pid, ti_pid, hub_pid), root_gpid, mesh_pid)
