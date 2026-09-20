# -*- coding: utf-8 -*-
r"""游戏模型提取器：Unity prefab → Blender 数据（挂载点树 + 蒙皮网格）。

设计：游戏的 GO 层级就是骨骼/挂载点统一层级（turret_0 既是骨骼又是挂载点）。
提取后 Blender 场景里：
  - 每个 GO = 一个父级嵌套的 Empty（名字=GO 名，位置=局部坐标）→ 即挂载点可视化；
  - 每个 LOD0 渲染器 = 一个 Mesh 物体（顶点组名=骨骼名，权重=蒙皮权重）；
  - Y-up -> Z-up 变换：(x,y,z) -> (x,-z,y)。

全部函数返回纯 Python 数据，由插件的 operator 负责创建 Blender 对象（Blender API 隔离）。
"""
import os, sys, struct

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

# ⛔ 脚本 pathID 从 `hub_edit` 取（**单一来源**）：以前这里也自己写一份 ⇒ 游戏更新后过期
#   不报错、只是"这些组件认不出来"（静默失效）✗（2026-10-16 立此存照）
from hub_edit import UNITPREFABTURRETINFO_SCRIPT as _TI_SCRIPT        # noqa: E402
from hub_edit import HUB_SCRIPT as _HUB_SCRIPT


_ENV_CACHE = {}   # bundle 路径 -> (env, objs, by_pid)
_ENV_MTIME = {}   # bundle 路径 -> 加载时的 mtime（文件被外部改动就重新加载）


def _load(bundle):
    """加载（并缓存）bundle。

    ⛔ 缓存**按 mtime 失效**：旧实现只按路径缓存 ⇒ 用工具导入新包覆盖 bundle 之后，
    同一 Blender 会话里再读还是**旧内容** ✗（表现为"导入成功了但读不出来"）。
    """
    try:
        mtime = os.path.getmtime(bundle)
    except OSError:
        mtime = None
    if bundle in _ENV_CACHE and _ENV_MTIME.get(bundle) == mtime:
        return _ENV_CACHE[bundle]
    if bundle in _ENV_CACHE:
        print("[加载] bundle 已被外部改动，重新加载：%s" % os.path.basename(bundle))
        _ENV_CACHE.pop(bundle, None)
    env = UnityPy.load(bundle)
    objs = list(env.objects)
    by_pid = {o.path_id: o for o in objs}
    _ENV_CACHE[bundle] = (env, objs, by_pid)
    _ENV_MTIME[bundle] = mtime
    return env, objs, by_pid


def clear_cache():
    _ENV_CACHE.clear()
    _ENV_MTIME.clear()


def list_prefabs(bundle):
    """列出 bundle 内全部 prefab：[(内部路径, 根 GO pathID)]，按路径排序。"""
    _, objs, _ = _load(bundle)
    ab = next(o for o in objs if o.type.name == "AssetBundle").read()
    out = [(n, a.asset.m_PathID) for n, a in ab.m_Container if a.asset and a.asset.m_PathID]
    out.sort(key=lambda x: x[0].lower())
    return out


def _comp_map(by_pid):
    tr_go, go_tr = {}, {}
    for o in by_pid.values():
        if o.type.name == "Transform":
            try:
                tr = o.read()
                g = tr.m_GameObject.m_PathID if tr.m_GameObject else 0
                tr_go[o.path_id] = g
                go_tr[g] = o.path_id
            except Exception:
                pass
    return tr_go, go_tr


def _unity_to_blender(pos, rot, scale):
    """Unity Y-up -> Blender Z-up（Vector3f/Quaternionf 用属性访问）。"""
    x, y, z = pos.x, pos.y, pos.z
    qx, qy, qz, qw = rot.x, rot.y, rot.z, rot.w
    sx, sy, sz = scale.x, scale.y, scale.z
    return (-x, -z, y), (qx, qz, -qy, qw), (sx, sz, sy)  # ★㓓：det=−1（翻 X）；旋转按镜像共轭 —— 规则由 _scratch\★㓓-手性修-preview\verify_math.py V4/V7 数值验证


def extract_prefab(bundle, root_gpid):
    """提取一个 prefab 的完整结构。

    返回:
      tree: {gpid: {name, parent_gpid, pos, rot, scale(blender系), comps: {类型: [pid...]}, children: [gpid...]}}
      order: 拓扑序 gpid 列表（父在前）
      meshes: [{go_gpid, renderer_pid, mesh_pid, bones: [gpid...], is_lod0}]
      ti: [ {go_gpid, index, weapons: [(weapon_gpid, shell_gpid)]} ]
    """
    _, objs, by_pid = _load(bundle)
    tr_go, go_tr = _comp_map(by_pid)
    tree = {}
    stack = [root_gpid]
    while stack:
        gpid = stack.pop()
        if gpid in tree:
            continue
        go = by_pid.get(gpid)
        if not go:
            continue
        tpid = go_tr.get(gpid)
        t = by_pid.get(tpid)
        if not t:
            continue
        tr = t.read()
        f = tr.m_Father.m_PathID if tr.m_Father else 0
        pg = tr_go.get(f, 0)
        comps = {}
        g = go.read()
        for c in (g.m_Components or []):
            cp = c.m_PathID if c else 0
            o = by_pid.get(cp)
            if o:
                comps.setdefault(o.type.name, []).append(cp)
        p, r, s = _unity_to_blender(tr.m_LocalPosition, tr.m_LocalRotation, tr.m_LocalScale)
        kids = [tr_go.get(k.m_PathID, 0) for k in (tr.m_Children or []) if k and k.m_PathID]
        # is_mount = 没有渲染器/网格组件（真正的挂载点/骨骼）；网格容器节点不算挂载点
        has_renderer = any(k in comps for k in ("SkinnedMeshRenderer", "MeshRenderer", "MeshFilter"))
        tree[gpid] = {"name": g.m_Name, "parent": pg, "pos": p, "rot": r, "scale": s,
                      "comps": comps, "children": kids, "is_mount": not has_renderer}
        # ParentConstraint：动态武器/附件跟随源挂载点（含偏移），用于修正显示位置。
        for cp in comps.get("ParentConstraint", []):
            o = by_pid.get(cp)
            if not o:
                continue
            try:
                pc = o.read()
            except Exception:
                continue
            if not getattr(pc, "m_Active", True):
                continue
            srcs = getattr(pc, "m_Sources", None) or []
            if not srcs:
                continue
            src = getattr(srcs[0], "sourceTransform", None)
            src_go = tr_go.get(src.m_PathID, 0) if (src and src.m_PathID) else 0
            if not src_go:
                continue
            to = (getattr(pc, "m_TranslationOffsets", None) or [None])[0]
            ro = (getattr(pc, "m_RotationOffsets", None) or [None])[0]
            tree[gpid]["constraint"] = {
                "source": src_go,
                "t_off": (to.x, to.y, to.z) if to else (0.0, 0.0, 0.0),
                "r_off": (ro.x, ro.y, ro.z) if ro else (0.0, 0.0, 0.0),
            }
            break
        stack.extend(kids)

    order = []
    def walk(gpid):
        node = tree.get(gpid)
        if not node:
            return
        order.append(gpid)
        for c in node["children"]:
            walk(c)
    walk(root_gpid)

    # 精修 is_mount：纯网格/LOD 容器（无渲染器但子树内没有任何挂载点）不算挂载点。
    # 挂载点 = 无渲染器 且（叶子 或 子树内存在挂载点）。自底向上（order 反序）传播。
    for gpid in reversed(order):
        node = tree[gpid]
        if not node["is_mount"]:
            continue
        kids = [k for k in node["children"] if k in tree]
        node["is_mount"] = (not kids) or any(tree[k]["is_mount"] for k in kids)

    meshes = []
    for gpid in order:
        node = tree[gpid]
        for rpid in node["comps"].get("SkinnedMeshRenderer", []) + node["comps"].get("MeshRenderer", []):
            r = by_pid.get(rpid)
            if not r:
                continue
            rd = r.read()
            # 只取本 bundle 内的网格（m_FileID != 0 = 外链共享网格，如 pid 10202，跳过）
            mp = 0
            m_ref = getattr(rd, "m_Mesh", None)
            if m_ref and getattr(m_ref, "m_FileID", 0) == 0 and getattr(m_ref, "m_PathID", 0):
                mp = m_ref.m_PathID
            if not mp:
                # MeshRenderer 本身没有 m_Mesh —— 网格在同 GO 的 MeshFilter 组件上
                for mf_pid in node["comps"].get("MeshFilter", []):
                    mf = by_pid.get(mf_pid)
                    if not mf:
                        continue
                    mf_ref = getattr(mf.read(), "m_Mesh", None)
                    if mf_ref and getattr(mf_ref, "m_FileID", 0) == 0 and getattr(mf_ref, "m_PathID", 0):
                        mp = mf_ref.m_PathID
                        break
            if not mp:
                continue
            # m_Bones 是 Transform pathID，映射回 GO pathID（树以 GO 为键）
            # ⛔⛔ **槽位必须原样保留，包括空槽（v1.8.76 修）**：
            #    顶点权重里的 blend index 是**按这个数组的槽位**索引的。旧写法
            #    `if b and b.m_PathID` 把空槽过滤掉 ⇒ 38 槽压成 31/26 槽 ⇒
            #      · 下标 ≥ 第一个空槽的全部**错位到别的骨骼**上；
            #      · 下标 ≥ 压缩后长度的**直接越界**，权重被静默丢弃（顶点落到 body）。
            #    实测（US_ACV，38 槽有 12 个空槽）：下标 23 本该是轮子 LR001，
            #    却被绑到 RR002；下标 26~37 全丢 ⇒ 游戏里轮子乱飞、车门掉到车底 ✗
            #    空槽写成 0，导入端命名为 `bone_N`（不参与骨骼表），槽位对齐 ✓
            bones = []
            if getattr(rd, "m_Bones", None):
                bones = [(tr_go.get(b.m_PathID, 0) if (b and b.m_PathID) else 0)
                         for b in rd.m_Bones]
            # 蒙皮根骨 + 根骨 bind pose：用于把网格放到骨骼"静止"比例上（root_scale 缩放）。
            # 网格顶点是原始绑定空间（scale=1），骨骼树可能带 root_scale(如 0.89)，
            # 静止位姿 = world[root_bone] @ bindpose_root，让网格与骨骼对齐。
            root_bone = 0
            root_bind_pose = None
            if getattr(rd, "m_RootBone", None) and getattr(rd.m_RootBone, "m_PathID", 0):
                root_bone = tr_go.get(rd.m_RootBone.m_PathID, 0)
            mo = by_pid.get(mp)
            if mo and root_bone:
                mdr = mo.read()
                bp = getattr(mdr, "m_BindPose", None) or []
                idx = bones.index(root_bone) if (root_bone and root_bone in bones) else 0
                if idx < len(bp):
                    root_bind_pose = [getattr(bp[idx], "e%d%d" % (i, j), 0.0)
                                      for i in range(4) for j in range(4)]
            # 默认材质（皮肤功能：按材质 pid 匹配皮肤、显示默认外观）
            materials = []
            for mr in (getattr(rd, "m_Materials", None) or []):
                if mr and mr.m_PathID:
                    materials.append(mr.m_PathID)
            meshes.append({"go": gpid, "renderer": rpid, "mesh": mp, "bones": bones,
                           "root_bone": root_bone, "root_bind_pose": root_bind_pose,
                           "materials": materials,
                           "is_lod0": False})

    # LOD0 = LODGroup 第一个 LOD 的 renderers
    lod0 = set()
    for gpid in order:
        node = tree[gpid]
        for rpid in node["comps"].get("LODGroup", []):
            lg = by_pid.get(rpid)
            if not lg:
                continue
            try:
                lods = lg.read().m_LODs
                for lr in (lods[0].renderers or []) if lods else []:
                    lod0.add(lr.renderer.m_PathID if lr.renderer else 0)
            except Exception:
                pass
    for m in meshes:
        m["is_lod0"] = m["renderer"] in lod0
    if not any(m["is_lod0"] for m in meshes):
        for m in meshes:
            m["is_lod0"] = True

    # TI 解析
    tis = []
    for gpid in order:
        node = tree[gpid]
        for tpid in node["comps"].get("MonoBehaviour", []):
            o = by_pid.get(tpid)
            raw = o.get_raw_data()
            if len(raw) >= 28 and struct.unpack_from("<q", raw, 20)[0] == _TI_SCRIPT:
                idx, n = struct.unpack_from("<ii", raw, 32)
                weapons = []
                off = 40
                for _ in range(n):
                    wf, wp = struct.unpack_from("<iq", raw, off); off += 12
                    sf, sp = struct.unpack_from("<iq", raw, off); off += 12
                    weapons.append((wp, sp))
                tis.append({"go": gpid, "index": idx, "weapons": weapons})
    return tree, order, meshes, tis


_FMT_SIZE = {0: 4, 1: 2, 2: 1, 3: 1, 4: 2, 5: 2, 6: 1, 7: 1, 8: 2, 9: 2, 10: 4, 11: 4}
_FMT_CODE = {0: "f", 1: "e", 2: "B", 3: "b", 4: "H", 5: "h", 6: "B", 7: "b", 8: "H", 9: "h", 10: "I", 11: "i"}


def _resolve_stream_path(bundle, path):
    """archive:/CAB-<hash>/CAB-<hash>.resS -> <bundle>_unpacked/CAB-<hash>.resS"""
    if not path:
        return None
    cab = path.rstrip("/").split("/")[-1]
    if not cab.endswith(".resS"):
        return None
    return os.path.join(bundle + "_unpacked", cab)


def _read_stream_data(bundle, sd):
    """按 StreamingInfo 从 .resS 读流式顶点数据块。"""
    res = _resolve_stream_path(bundle, getattr(sd, "path", "") or "")
    if not res or not os.path.isfile(res):
        return None
    try:
        with open(res, "rb") as f:
            f.seek(getattr(sd, "offset", 0))
            return f.read(getattr(sd, "size", 0))
    except Exception:
        return None


def _read_mesh_data(m, fmt_size, data, padded=True):
    """解析 VertexData 通道布局，返回 (顶点数, 数据字节, {通道键: (kind,dim,off,stride,code,index)})。

    padded: 内联数据每个流按 16 字节对齐；流式(.resS)数据紧密排列（无对齐填充）。
    """
    vd = m.m_VertexData
    N = vd.m_VertexCount
    strides = {}
    for ch in vd.m_Channels:
        strides.setdefault(ch.stream, 0)
        if ch.dimension > 0:
            strides[ch.stream] += (ch.dimension & 0xF) * fmt_size(ch.format)
    offs = {}
    total = 0
    for s in sorted(strides):
        offs[s] = total
        block = strides[s] * N
        total += block
        if padded and block % 16:
            total += 16 - (block % 16)
    out = {}
    for idx, ch in enumerate(vd.m_Channels):
        s = ch.stream
        dim = ch.dimension & 0xF
        if not dim:
            continue
        code = _FMT_CODE.get(ch.format, "f")
        off = offs.get(s, 0) + ch.offset
        stride = strides.get(s, 1)
        out[(s, ch.offset)] = ("float" if code in ("e", "f") else "int", dim, off, stride, code, idx)
    return N, data, out


def _read_chan(data, N, off, stride, code, dim):
    """读一个通道的全部 N 个值（支持 float/half/int）。"""
    fmt = {"f": "f", "e": "e", "I": "I", "i": "i", "H": "H", "h": "h", "B": "B", "b": "b"}.get(code, "f")
    try:
        return [struct.unpack_from("<%d%s" % (dim, fmt), data, off + i * stride) for i in range(N)]
    except Exception:
        return None


def extract_mesh_geometry(bundle, mesh_pid):
    """读一个 mesh 的几何：positions/triangles/uv/bones/weights（纯 Python）。

    支持内联顶点数据与流式(.resS)顶点数据；按 Unity 固定通道索引语义定位：
    0=位置 4=UV0 12=蒙皮权重 13=骨骼索引。
    """
    _, objs, by_pid = _load(bundle)
    m = by_pid[mesh_pid].read()
    vd = m.m_VertexData
    data = vd.m_DataSize
    streamed = False
    if not data:
        sd = getattr(m, "m_StreamData", None)
        if sd is not None and getattr(sd, "path", ""):
            data = _read_stream_data(bundle, sd)
            streamed = True
    if not data:
        data = b""
    N, data, chans = _read_mesh_data(m, lambda f: _FMT_SIZE.get(f, 4), data, padded=not streamed)
    by_idx = {idx: (dim, off, stride, code) for (_s, _o), (kind, dim, off, stride, code, idx) in chans.items()}

    def _get(i):
        return by_idx.get(i)

    positions = []
    p = _get(0)
    if p:
        dim, off, stride, code = p
        vals = _read_chan(data, N, off, stride, code, dim)
        if vals:
            positions = [(-x, -z, y) for (x, y, z) in vals]  # Y-up -> Z-up（★㓓：det=−1，翻 X）
    if not positions:
        # ⛔ 顶点位置一个都没读到时**必须出声**：旧行为是静默用全零顶点，用户只看到
        #    "模型塌陷到原点"却不知道原因（多半是 .resS 侧载文件缺失/路径不对）✗
        if N:
            print("[提取] ⚠ 网格 %s：顶点数 %d 但位置数据读不到（检查 bundle 旁的 "
                  "*_unpacked/*.resS 侧载文件是否齐全）—— 将用全零点位导入"
                  % (getattr(m, "m_Name", "?"), N))
        positions = [(0.0, 0.0, 0.0)] * N

    uvs = []
    u = _get(4)
    if u:
        dim, off, stride, code = u
        vals = _read_chan(data, N, off, stride, code, dim)
        if vals:
            uvs = [(v[0], v[1]) for v in vals]

    weights = []
    bones = []
    b = _get(13)
    if b:
        bdim, boff, bstride, bcode = b
        bvals = _read_chan(data, N, boff, bstride, bcode, bdim)
        w = _get(12)
        wvals = None
        if w:
            wdim, woff, wstride, wcode = w
            wvals = _read_chan(data, N, woff, wstride, wcode, wdim)
        if bvals:
            if bdim >= 2:
                bones = [(int(v[0]), int(v[1])) for v in bvals]
                weights = [(v[0], v[1]) for v in wvals] if wvals else [(1.0, 0.0)] * N
            else:
                # 单骨骼影响（dim=1，无权重通道）：权重固定 1.0，双骨索引重复
                bones = [(int(v[0]), int(v[0])) for v in bvals]
                weights = [(1.0, 0.0)] * N

    ib = m.m_IndexBuffer
    if isinstance(ib, list):
        ib = bytes(ib)
    total = sum(s.indexCount for s in m.m_SubMeshes)
    # ⛔ 索引位宽要用**引擎字段** `m_IndexFormat`（0=16 位 / 1=32 位），
    #    旧写法按"缓冲区长度是否等于 indexCount*2"猜 —— 长度巧合或 sub.indexCount
    #    异常时会用错宽度解析（struct.error 或垃圾三角面）✗
    fmt = getattr(m, "m_IndexFormat", None)
    if fmt in (0, 1):
        is16 = (fmt == 0)
        need = total * (2 if is16 else 4)
        if len(ib) < need:
            print("[提取] ⚠ 索引缓冲区 %d 字节 < m_IndexFormat 要求的 %d 字节，"
                  "退回按长度推断" % (len(ib), need))
            is16 = len(ib) == total * 2
    else:
        is16 = len(ib) == total * 2
    if (total * 2 if is16 else total * 4) > len(ib):
        raise ValueError("索引缓冲区太小：需要 %d 字节，实际 %d"
                         % (total * 2 if is16 else total * 4, len(ib)))
    triangles = list(struct.unpack_from("<%dH" % total, ib, 0) if is16
                     else struct.unpack_from("<%dI" % total, ib, 0))
    return {"positions": positions, "triangles": triangles, "uv": uvs,
            "weights": weights, "bones": bones, "name": m.m_Name}
