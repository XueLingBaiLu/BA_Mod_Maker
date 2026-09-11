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
sys.path.insert(0, os.path.join(HERE, "..", "..", "_unitypy"))

import UnityPy
from UnityPy.files.ObjectReader import ObjectReader
from UnityPy.streams import EndianBinaryReader
from UnityPy.helpers import TypeTreeHelper
from UnityPy.classes.math import Vector3f, Quaternionf

from pack_model import _type_identity
from build_model import _world_matrices, _invert
from build_turret_direct import _find_sources, _save
from bone_hashes import KNOWN_HASHES
from hub_edit import HUB_SCRIPT, json_to_hub

FORMAT = "bamod-assets"
VERSION = 1


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


def collect_prefab_objects(bundle, root_gpid):
    """收集原 prefab 的完整对象集（preload 范围内全部对象，MonoScript 除外）。

    返回 (objects, preload_pids, prefab_path)：
      objects: [{pid, class_id, type_name, script_id, tree_hash, raw(base64)}]
      preload_pids: 原 prefab 的 preload 顺序（含 MonoScript 常量 pid）
      prefab_path: 容器条目路径
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
    if preload_index is None:
        raise ValueError("找不到 root=%d 的容器条目" % root_gpid)
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


def build_copy(bundle, out_pack, new_prefab, new_name, source_root_gpid, bone_tree, meshes, hub_json=None):
    r"""patch 模式导出 .bamod：字节复制原 prefab 全部对象，只 patch 网格 + 挂点。

    bone_tree: [(name, parent_name, pos_unity, rot_unity(xyzw), scale_unity), ...] 父在前
    meshes:    [{name, positions, triangles, uv, bones[(b0,b1)每顶点], weights[(w0,w1)每顶点],
                bone_names[骨骼名列表]}]
    """
    from turret_swap import build_mesh_data
    env, sf, objs, by_pid = _load_env(bundle)
    # 源对象集
    src_objects, src_preload, src_path = collect_prefab_objects(bundle, source_root_gpid)
    _, _, smr_src, mesh_src, _, _, _, _, hash_lookup = _find_sources(objs, by_pid)
    hash_lookup.update(KNOWN_HASHES)
    go_src = next(o for o in objs if o.type.name == "GameObject")
    tr_src = next(o for o in objs if o.type.name == "Transform")

    world = _world_matrices(bone_tree)

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

    # ---- 重写对象：默认原样，patch 网格 + 挂点 ----
    new_objects = []
    for o in src_objects:
        raw = base64.b64decode(o["raw"])
        if o["type_name"] == "Mesh" and o["pid"] in mesh_by_name.values():
            # 找到对应网格名 -> 用户网格
            nm = next(k for k, v in mesh_by_name.items() if v == o["pid"])
            user = next((m for m in meshes if m.get("name") == nm), None)
            if user is not None:
                m, r = _read_mesh(raw, mesh_src)
                skin = [(int(user["bones"][i][0]), int(user["bones"][i][1]),
                         float(user["weights"][i][0]), float(user["weights"][i][1]))
                        for i in range(len(user["positions"]))]
                faces = []
                ui = 0
                for tt in range(0, len(user["triangles"]), 3):
                    a, b, c = user["triangles"][tt], user["triangles"][tt + 1], user["triangles"][tt + 2]
                    faces.append(((a, ui), (b, ui + 1), (c, ui + 2)))
                    ui += 3
                vbytes, ibytes, vcount = build_mesh_data(
                    user["positions"], user["uv"], faces, skin, skin_mode=2)
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
                    m.m_BoneNameHashes = [hash_lookup[b] & 0xFFFFFFFF for b in bn]
                    m.m_RootBoneNameHash = (hash_lookup["body"] if "body" in bn
                                            else (hash_lookup[bn[0]] & 0xFFFFFFFF)) & 0xFFFFFFFF
                    m.m_BindPose = [_invert(world[b]) for b in bn]
                r.save_typetree(m)
                raw = r.data
        elif o["type_name"] == "Transform":
            # 找该 Transform 对应的 GO 名，若在用户挂点树里则 patch 位置
            gid = None
            # tr_by_go 是 go_pid -> tr_pid，反查
            gid = next((g for g, t in tr_by_go.items() if t == o["pid"]), None)
            if gid is not None:
                gname = next((n for n, g in go_by_name.items() if g == gid), None)
                if gname is not None:
                    user_node = next((n for n in bone_tree if n[0] == gname), None)
                    if user_node is not None:
                        r = ObjectReader(assets_file=sf, reader=sf.reader, path_id=1,
                                         type_id=tr_src.type_id, serialized_type=tr_src.serialized_type,
                                         class_id=tr_src.class_id, type=tr_src.type,
                                         byte_start=0, byte_size=len(raw), is_destroyed=False,
                                         is_stripped=False, data=raw)
                        node = r._get_typetree_node()
                        er = EndianBinaryReader(raw, endian=sf.reader.endian)
                        t = TypeTreeHelper.read_typetree(node, er, as_dict=False, assetsfile=sf,
                                                         byte_size=len(raw), check_read=False)
                        nm, parent, pos, rot, scale = user_node
                        t.m_LocalPosition = Vector3f(pos[0], pos[1], pos[2])
                        t.m_LocalRotation = Quaternionf(rot[0], rot[1], rot[2], rot[3])
                        t.m_LocalScale = Vector3f(scale[0], scale[1], scale[2])
                        r.save_typetree(t)
                        raw = r.data
        elif o["type_name"] == "MonoBehaviour" and hub_json:
            # 应用编辑后的动画：找到 AnimationHub，用 JSON 重序列化替换其字节
            if len(raw) >= 28 and struct.unpack_from("<q", raw, 20)[0] == HUB_SCRIPT:
                # 炮塔内部 name -> transform pid
                name_to_tr = {}
                for gname, gpid in go_by_name.items():
                    trpid = tr_by_go.get(gpid)
                    if trpid is not None:
                        name_to_tr[gname] = trpid
                # 全 bundle 的 name -> transform pid（车体等外部引用，如 Lspecial/Rspecial/antenna/scope）
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
                    return full_name_to_tr.get(name, 0)
                hub_root = struct.unpack_from("<q", raw, 4)[0]
                raw = json_to_hub(hub_json, hub_root, _pid_of)
        new_objects.append({"pid": o["pid"], "class_id": o["class_id"],
                            "type_name": o["type_name"], "script_id": o.get("script_id"),
                            "tree_hash": o.get("tree_hash"),
                            "raw": base64.b64encode(raw).decode("ascii")})

    manifest = {
        "format": FORMAT, "version": VERSION, "mode": "copy-full",
        "bundle": "units_assets_all", "prefab_path": new_prefab,
        "root_pid": source_root_gpid,
        "preload": list(src_preload), "objects": new_objects,
    }
    with zipfile.ZipFile(out_pack, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
    return ("DONE copy-full -> %s | %d 对象 | src=%s" % (out_pack, len(new_objects), src_path),
            manifest["root_pid"])
