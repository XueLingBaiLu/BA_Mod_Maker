# -*- coding: utf-8 -*-
r"""通用模型构建器：从骨骼树 + 网格 + 挂载点生成任意类型 prefab。

与 build_turret_direct 的关系：复用其全部底层函数（GO/Transform/Mesh/SMR/TI/Hub/LODGroup/
preload/清理），把"固定 cube 结构"泛化为"任意骨骼树 + 任意网格 + 任意挂载点"。

输入（Blender 侧统一产出，Unity 坐标系 Y-up）：
    bone_tree: [(name, parent_name, pos(xyz), rot(quat xyzw), scale(xyz)), ...]  父在前，
               必须含 root 与 body。
    meshes:    [{name, positions[(x,y,z)], triangles[展开索引], uv(每角), bones[(b0,b1)每顶点],
                weights[(w0,w1)每顶点], bone_names[骨骼名列表]}]
               骨骼名必须是已知哈希表里的（bone_hashes.py），否则蒙皮匹配会失败。
    turret_mounts: [{turret: "turret_0", index: 0, weapons: ["weapon_0_1_2"], shells: ["shell_spawn_0_0"]}]
               缺省时按名字自动配对：turret_N 下的 weapon_* / shell_spawn_*。
"""
import os, sys, struct

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "_unitypy"))

from build_turret_direct import (
    _find_sources, _make_go, _attach_child, _save, _read_from_data, _synth_turret_info,
    _synth_animation_hub, _clean_stale_build, _trans, _make_alloc,
    UNITPREFABTURRETINFO_SCRIPT, ANIMATIONHUB_SCRIPT)
from bone_hashes import KNOWN_HASHES, bone_hash

from UnityPy.files.ObjectReader import ObjectReader
from UnityPy.classes.PPtr import PPtr
from UnityPy.classes.generated import AssetInfo, ComponentPair
from UnityPy.classes.math import Vector3f, Quaternionf, Matrix4x4f

_IDENTITY4 = Matrix4x4f(1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0)


def _qmat(q):
    x, y, z, w = q
    return Matrix4x4f(
        1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w), 0.0,
        2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w), 0.0,
        2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y), 0.0,
        0.0, 0.0, 0.0, 1.0)


def _flat(m):
    """Matrix4x4f(e00..e33) -> 16 元素列表（行主序）。"""
    return [getattr(m, "e%d%d" % (i, j)) for i in range(4) for j in range(4)]


def _mul(a, b):
    a = _flat(a); b = _flat(b)
    r = [0.0] * 16
    for i in range(4):
        for j in range(4):
            r[i * 4 + j] = sum(a[i * 4 + k] * b[k * 4 + j] for k in range(4))
    return Matrix4x4f(*r)


def _world_matrices(bone_tree):
    """每根骨骼的世界矩阵（父链连乘）。返回 {name: Matrix4x4f}。"""
    mats = {}
    for name, parent, pos, rot, scale in bone_tree:
        t = Matrix4x4f(scale[0], 0.0, 0.0, pos[0], 0.0, scale[1], 0.0, pos[1],
                       0.0, 0.0, scale[2], pos[2], 0.0, 0.0, 0.0, 1.0)
        m = _mul(_qmat(rot), t)
        if parent and parent in mats:
            m = _mul(mats[parent], m)
        mats[name] = m
    return mats


def _invert(m):
    # 仅支持 TRS（无切变）矩阵求逆：R^T 与 -R^T t
    m = _flat(m)
    r = [[m[i * 4 + j] for j in range(3)] for i in range(3)]
    rt = [[r[j][i] for j in range(3)] for i in range(3)]
    t = [m[i * 4 + 3] for i in range(3)]
    nt = [-sum(rt[i][k] * t[k] for k in range(3)) for i in range(3)]
    return Matrix4x4f(rt[0][0], rt[0][1], rt[0][2], nt[0],
                      rt[1][0], rt[1][1], rt[1][2], nt[1],
                      rt[2][0], rt[2][1], rt[2][2], nt[2],
                      0.0, 0.0, 0.0, 1.0)


def _rebuild_lod_group(lod_src, lod_read, root_gpid, smr_pids):
    r"""从源 LODGroup 重建字节：m_GameObject 指向 root，每个 LOD 级别都引用全部 SMR。

    源 LODGroup 序列化布局（units_assets_all，140 字节）：
      前缀 36B(m_GameObject12 + m_LocalReferencePoint12 + m_Size4 + m_FadeMode4 + 标志4)
      + m_LODs 计数 4B + 每级(screenRelativeHeight4 + fadeTransitionWidth4
      + renderers 计数 4B + N×PPtr12) + 尾部 4B(m_Enabled+padding)。
    逐字节重建，避免 typetree 写回漏可选字段（会原生崩溃）。
    多网格时每个 LOD 级别都必须引用全部 SMR，否则只进 LODGroup 的第一个网格可见。
    """
    raw = bytearray(lod_src.get_raw_data())
    heights = [lod.screenRelativeHeight for lod in (lod_read.m_LODs or [])]
    n_lod = len(heights)
    if len(raw) != 36 + 4 + n_lod * 24 + 4:
        # 结构假设失效：回退到旧的字面单渲染器替换（单网格场景仍可用）
        raw = raw.replace(struct.pack("<q", lod_read.m_GameObject.m_PathID), struct.pack("<q", root_gpid))
        for lod in lod_read.m_LODs:
            for lr in (lod.renderers or []):
                raw = raw.replace(struct.pack("<q", lr.renderer.m_PathID), struct.pack("<q", smr_pids[0]))
        return bytes(raw)
    prefix = bytearray(raw[0:0x24])
    prefix[4:12] = struct.pack("<q", root_gpid)
    trailing = raw[-4:]
    out = bytearray(prefix)
    out += struct.pack("<i", n_lod)
    for h in heights:
        out += struct.pack("<f", float(h))
        out += struct.pack("<f", 0.0)
        out += struct.pack("<i", len(smr_pids))
        for sp in smr_pids:
            out += struct.pack("<iq", 0, sp)
    out += trailing
    return bytes(out)


def build_model(bundle, out_bundle, prefab_path, new_name,
                bone_tree, meshes, turret_mounts=None,
                include_hub=True, env=None, pack_path=None):
    """pack_path 非空时：只导出 .bamod 小包（仅新对象），跳过 3.4GB 全量保存。"""
    import UnityPy
    if env is None:
        env = UnityPy.load(bundle)
    sf = list(env.objects)[0].assets_file
    _clean_stale_build(sf)
    objs = list(sf.objects.values())
    by_pid = {o.path_id: o for o in objs}
    _, _, smr_src, mesh_src, ti_src, lod_src, hub_src, _, hash_lookup = _find_sources(objs, by_pid)
    hash_lookup.update(KNOWN_HASHES)  # 兼容旧引用（新哈希一律走 bone_hash 算法）

    names = [n for n, _, _, _, _ in bone_tree]
    assert "root" in names and "body" in names, "bone_tree 必须包含 root 与 body"
    # 骨骼路径链（root 起）→ 按破解算法直接算哈希（不再依赖查找表）
    parent_of = {n: p for n, p, _, _, _ in bone_tree}

    def path_of(name):
        parts = []
        cur = name
        for _ in range(64):
            if cur is None:
                break
            parts.append(cur)
            cur = parent_of.get(cur)
        return list(reversed(parts))

    bone_hashes = {n: bone_hash(path_of(n)) for n in names}
    # 新 prefab 名字：树根节点（容器资产）改名。cube 先例证明顶层名字不影响骨骼哈希匹配。
    # 注意：改名后要把子节点的父引用从旧根名重映射为新根名。
    if new_name and names[0] != new_name:
        old_root = names[0]
        remapped = []
        for np in bone_tree:
            nm, parent, pos, rot, scale = np
            if parent == old_root:
                parent = new_name
            remapped.append((nm, parent, pos, rot, scale))
        bone_tree = [(new_name, None) + tuple(bone_tree[0][2:])] + remapped[1:]
        names[0] = new_name
    root_name = names[0]  # 根节点名（可能被 new_name 改名）
    existing = set(by_pid.keys())
    alloc = _make_alloc(existing, 0x4355424500000000)

    # ---- 骨骼树 GOs ----
    go_pids = {}; tr_pids = {}
    go_src_gpid = None
    for o in objs:
        if o.type.name == "GameObject":
            go_src_gpid = o.path_id
            break
    tr_src = next(o for o in objs if o.type.name == "Transform")
    go_src = by_pid[go_src_gpid]
    for name, parent, pos, rot, scale in bone_tree:
        gpid, trpid = _make_go(sf, alloc, go_src, tr_src, name, 0)
        go_pids[name] = gpid; tr_pids[name] = trpid
        tr = sf.objects[trpid]
        t = _read_from_data(tr)
        t.m_LocalPosition = Vector3f(*pos)
        t.m_LocalRotation = Quaternionf(*rot)
        t.m_LocalScale = Vector3f(*scale)
        t.m_Father = PPtr(m_FileID=0, m_PathID=tr_pids[parent], assetsfile=sf) if parent else PPtr(m_FileID=0, m_PathID=0, assetsfile=sf)
        t.m_Children = []
        tr.save_typetree(t)
    for name, parent, _, _, _ in bone_tree:
        if parent:
            _attach_child(sf, sf.objects[tr_pids[parent]], tr_pids[name])

    # ---- 网格 ----
    from turret_swap import build_mesh_data
    world = _world_matrices(bone_tree)
    smr_pids = []
    mesh_extra_pids = []
    for mi, mesh in enumerate(meshes):
        bone_names = mesh["bone_names"]
        for bn in bone_names:
            if bn not in names:
                raise ValueError("骨骼 %s 不在骨骼树里" % bn)
        skin = [(int(mesh["bones"][i][0]), int(mesh["bones"][i][1]),
                 float(mesh["weights"][i][0]), float(mesh["weights"][i][1]))
                for i in range(len(mesh["positions"]))]
        faces = []
        ui = 0
        for t in range(0, len(mesh["triangles"]), 3):
            a, b, c = mesh["triangles"][t], mesh["triangles"][t + 1], mesh["triangles"][t + 2]
            faces.append(((a, ui), (b, ui + 1), (c, ui + 2)))
            ui += 3
        vbytes, ibytes, vcount = build_mesh_data(
            mesh["positions"], mesh["uv"], faces, skin, skin_mode=2)
        m = mesh_src.read()
        m.m_Name = mesh.get("name", "mesh_%d" % mi)
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
        # 骨骼名哈希按破解算法直接计算（uint32；不再依赖查找表）
        m.m_BoneNameHashes = [bone_hashes[bn] for bn in bone_names]
        m.m_RootBoneNameHash = (bone_hashes["body"] if "body" in bone_names else m.m_BoneNameHashes[0])
        m.m_BindPose = [_invert(world[bn]) for bn in bone_names]
        mesh_pid = _save(sf, mesh_src, alloc(), m).path_id
        mesh_extra_pids.append(mesh_pid)

        root_name = names[0]  # 可能已被 new_name 改名
        mgpid, mgtrpid = _make_go(sf, alloc, go_src, tr_src, mesh.get("name", "mesh_%d" % mi),
                                  tr_pids[root_name])
        mesh_extra_pids.extend([mgpid, mgtrpid])
        _attach_child(sf, sf.objects[tr_pids[root_name]], mgtrpid)
        smr = smr_src.read()
        smr.m_Mesh = PPtr(m_FileID=0, m_PathID=mesh_pid, assetsfile=sf)
        smr.m_GameObject = PPtr(m_FileID=0, m_PathID=mgpid, assetsfile=sf)
        smr.m_Bones = [PPtr(m_FileID=0, m_PathID=tr_pids[bn], assetsfile=sf) for bn in bone_names]
        smr.m_RootBone = PPtr(m_FileID=0, m_PathID=tr_pids["body"], assetsfile=sf)
        xs = [p[0] for p in mesh["positions"]]; ys = [p[1] for p in mesh["positions"]]; zs = [p[2] for p in mesh["positions"]]
        if xs:
            smr.m_AABB.m_Center = Vector3f((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, (min(zs) + max(zs)) / 2)
            smr.m_AABB.m_Extent = Vector3f((max(xs) - min(xs)) / 2 or 0.5, (max(ys) - min(ys)) / 2 or 0.5, (max(zs) - min(zs)) / 2 or 0.5)
        smr_pid = _save(sf, smr_src, alloc(), smr).path_id
        smr_pids.append(smr_pid)
        mesh_extra_pids.append(smr_pid)
        mg = sf.objects[mgpid]
        mgg = _read_from_data(mg)
        mgg.m_Component.append(ComponentPair(component=PPtr(m_FileID=0, m_PathID=smr_pid, assetsfile=sf)))
        mg.save_typetree(mgg)

    # ---- TI（炮塔/武器/抛壳 自动配对） ----
    ti_pids = []
    if turret_mounts is None:
        parent_of = {n: p for n, p, _, _, _ in bone_tree}

        def _nearest_turret(child):
            cur = parent_of.get(child)
            while cur:
                if cur.startswith("turret_"):
                    return cur
                cur = parent_of.get(cur)
            return None

        turret_mounts = []
        for name in names:
            if not name.startswith("turret_"):
                continue
            try:
                idx = int(name[len("turret_"):].split("_")[-1])
            except ValueError:
                continue
            # 武器/抛壳归「最近的那层炮塔」，子炮塔(turret_0_0)的武器不并进父炮塔(turret_0)
            weapons = [w for w in names if w.startswith("weapon_") and _nearest_turret(w) == name]
            shells = [s for s in names if s.startswith("shell_spawn_") and _nearest_turret(s) == name]
            if weapons:
                turret_mounts.append({"turret": name, "index": idx,
                                      "weapons": weapons, "shells": shells})
    for tm in turret_mounts:
        tn = tm["turret"]
        if tn not in tr_pids:
            continue
        weapons = tm.get("weapons") or []
        shells = tm.get("shells") or []
        n = max(len(weapons), 1)
        ti_pid = _synth_turret_info(sf, alloc, ti_src, go_pids[tn],
                                    [tr_pids[w] if w in tr_pids else tr_pids[tn] for w in (weapons[:n] if weapons else ["weapon_0"])],
                                    [tr_pids[s] if s in tr_pids else tr_pids[tn] for s in (shells[:n] if shells else ["shell_spawn_0"])],
                                    tm.get("index", 0))
        ti_pids.append(ti_pid)
        tg = sf.objects[go_pids[tn]]
        tgg = _read_from_data(tg)
        tgg.m_Component.append(ComponentPair(component=PPtr(m_FileID=0, m_PathID=ti_pid, assetsfile=sf)))
        tg.save_typetree(tgg)

    # ---- LODGroup ----
    lod_read = lod_src.read()
    raw_lod = _rebuild_lod_group(lod_src, lod_read, go_pids[root_name], smr_pids)
    lod_pid = alloc()
    sf.objects[lod_pid] = ObjectReader(assets_file=sf, reader=sf.reader, path_id=lod_pid,
                                       type_id=lod_src.type_id, serialized_type=lod_src.serialized_type,
                                       class_id=lod_src.class_id, type=lod_src.type,
                                       byte_start=lod_src.byte_start, byte_size=len(raw_lod),
                                       is_destroyed=lod_src.is_destroyed, is_stripped=lod_src.is_stripped,
                                       data=bytes(raw_lod))
    root_go = sf.objects[go_pids[root_name]]
    rg = _read_from_data(root_go)
    rg.m_Component.append(ComponentPair(component=PPtr(m_FileID=0, m_PathID=lod_pid, assetsfile=sf)))
    root_go.save_typetree(rg)

    # ---- Hub（军械库演示动画，可选） ----
    hub_pid = 0
    if include_hub and "turret_0" in tr_pids:
        hub_pid = _synth_animation_hub(sf, alloc, hub_src, go_pids[root_name], tr_pids["turret_0"])
        rg = _read_from_data(root_go)
        rg.m_Component.append(ComponentPair(component=PPtr(m_FileID=0, m_PathID=hub_pid, assetsfile=sf)))
        root_go.save_typetree(rg)

    # ---- AssetBundle 容器 + preload（MonoScript 前置！） ----
    ab_obj = next(o for o in objs if o.type.name == "AssetBundle")
    ab = ab_obj.read()
    preload_scripts = [UNITPREFABTURRETINFO_SCRIPT]
    if hub_pid:
        preload_scripts.append(ANIMATIONHUB_SCRIPT)
    all_pids = (preload_scripts + [go_pids[n] for n in names] + [tr_pids[n] for n in names]
                + mesh_extra_pids + ti_pids + [lod_pid])
    if hub_pid:
        all_pids.append(hub_pid)
    pl = [PPtr(m_FileID=0, m_PathID=p, assetsfile=sf) for p in all_pids]
    new_start = len(ab.m_PreloadTable)
    ab.m_PreloadTable.extend(pl)
    ab.m_Container = [(n, a) for n, a in ab.m_Container if n != prefab_path]
    ab.m_Container.append((prefab_path, AssetInfo(asset=PPtr(m_FileID=0, m_PathID=go_pids[root_name], assetsfile=sf),
                                                 preloadIndex=new_start, preloadSize=len(pl))))
    ab_obj.save_typetree(ab)

    if pack_path:
        from pack_model import export_pack
        pack, n, size = export_pack(sf, prefab_path, go_pids[root_name], all_pids, pack_path)
        return ("DONE -> pack %s | %d 对象 / %d 字节 | root=%d meshes=%d tis=%d hub=%d" % (
            pack, n, size, go_pids[root_name], len(smr_pids), len(ti_pids), hub_pid), go_pids[root_name])

    from stream_save import ensure_stream_save
    ensure_stream_save()
    env.file.save_stream(out_bundle, "lz4")
    return ("DONE -> %s | root=%d meshes=%d tis=%d hub=%d" % (
        out_bundle, go_pids[root_name], len(smr_pids), len(ti_pids), hub_pid), go_pids[root_name])
