# -*- coding: utf-8 -*-
"""UnityPy 桥接层：供 Blender 插件调用。

职责：
  - read_mesh_full()：把游戏 bundle 里的 mesh（含骨骼蒙皮）读成纯 Python 数据。
  - write_bundle()：把 Blender 里改好的网格数据写回（deep-copy + 换 mesh + 写权重 + 保存）。

依赖：_unitypy（UnityPy）+ _rev_tools（turret_swap.py）。路径由插件偏好设置传入。
"""
import os
import struct
import sys


def add_paths(rev_dir=None, unitypy_dir=None):
    """把 _rev_tools 加到 sys.path；UnityPy 靠 Blender 的 Python 里 pip 装。

    - 优先用 addon 自带的 _rev_tools（自包含，玩家不用单独下载 _rev_tools）；
    - 若偏好里填了外部「工具脚本目录」，外部目录优先（开发用，改脚本即时生效）；
    - UnityPy 不打包（.pyd 按 Python 版本编译，和 Blender 对不上），请用
      pip install UnityPy 装进 Blender 的 Python。"""
    bundled = os.path.join(os.path.dirname(os.path.realpath(__file__)), "_rev_tools")
    if os.path.isdir(bundled) and bundled not in sys.path:
        sys.path.insert(0, bundled)
    if rev_dir and os.path.isdir(rev_dir) and rev_dir not in sys.path:
        sys.path.insert(0, rev_dir)  # 外部目录优先级更高（开发用）
    try:
        import UnityPy  # noqa: F401
        return  # Blender 的 Python 里已有 UnityPy
    except ImportError:
        pass
    if unitypy_dir and os.path.isdir(unitypy_dir) and unitypy_dir not in sys.path:
        sys.path.insert(0, unitypy_dir)


def _comp_size(fmt):
    return {0: 4, 1: 2, 2: 1, 3: 1, 4: 2, 5: 2, 6: 1, 7: 1, 8: 2, 9: 2, 10: 4, 11: 4}.get(fmt, 4)


def read_mesh_full(bundle, mesh_pid):
    """读一个 mesh 的完整数据，返回 dict：
    positions, triangles(展平索引), uv(每顶点), bones(每顶点 2 int), weights(每顶点 2 float), bone_count。"""
    import UnityPy
    env = UnityPy.load(bundle)
    by_pid = {o.path_id: o for o in env.objects}
    m = by_pid[int(mesh_pid)].read()
    vd = m.m_VertexData
    N = vd.m_VertexCount
    data = vd.m_DataSize

    strides = {}
    for ch in vd.m_Channels:
        if ch.stream not in strides:
            strides[ch.stream] = 0
        if ch.dimension > 0:
            strides[ch.stream] += (ch.dimension & 0xF) * _comp_size(ch.format)
    offs = {}
    total = 0
    for s in sorted(strides):
        offs[s] = total
        block = strides[s] * N
        total += block
        if block % 16:
            total += 16 - (block % 16)
    s0off = offs.get(0, 0); s0s = strides.get(0, 0)
    s1off = offs.get(1, 0); s1s = strides.get(1, 0)
    s2off = offs.get(2, 0); s2s = strides.get(2, 0)

    # ⛔ 位置通道也要取它自己的 offset：旧实现直接 `s0off + i*s0s` **漏了 ch.offset**，
    #    对 offset≠0 的网格会**静默读出错误顶点**（比报错更危险）✗
    #    UV 那边一直是对的（用了 `+ uvoff`），位置这边忘了。
    poff = 0
    for ch in vd.m_Channels:
        if ch.stream == 0 and (ch.dimension & 0xF) >= 3:
            poff = ch.offset
            break

    uvoff = 0; uvf = 1
    for ch in vd.m_Channels:
        if ch.stream == 1 and (ch.dimension & 0xF) == 2:
            uvoff = ch.offset; uvf = ch.format
            break
    woff = 0; boff = 8
    for ch in vd.m_Channels:
        if ch.stream == 2:
            if ch.format == 0 and (ch.dimension & 0xF) == 2:
                woff = ch.offset
            elif ch.format == 10 and (ch.dimension & 0xF) == 2:
                boff = ch.offset

    positions = []; uv = []; bones = []; weights = []
    for i in range(N):
        positions.append(struct.unpack_from("<fff", data, s0off + i * s0s + poff))
        if uvf == 1:
            u, v = struct.unpack_from("<ee", data, s1off + i * s1s + uvoff)
            uv.append((float(u), float(v)))
        else:
            u, v = struct.unpack_from("<ff", data, s1off + i * s1s + uvoff)
            uv.append((u, v))
        weights.append(struct.unpack_from("<ff", data, s2off + i * s2s + woff))
        bones.append(struct.unpack_from("<II", data, s2off + i * s2s + boff))

    ib = m.m_IndexBuffer
    if isinstance(ib, list):
        ib = bytes(ib)
    total = sum(s.indexCount for s in m.m_SubMeshes)
    # ⛔ 用引擎字段 `m_IndexFormat`（0=16 位 / 1=32 位），别按缓冲区长度猜：
    #    长度巧合会用错宽度解析（struct.error 或垃圾三角面）✗
    fmt = getattr(m, "m_IndexFormat", None)
    if fmt in (0, 1):
        is16 = (fmt == 0)
    else:
        is16 = len(ib) == total * 2
    need = total * (2 if is16 else 4)
    if need > len(ib):
        raise ValueError("索引缓冲区太小：需要 %d 字节，实际 %d" % (need, len(ib)))
    triangles = list(struct.unpack_from("<%dH" % total, ib, 0) if is16
                     else struct.unpack_from("<%dI" % total, ib, 0))

    return {
        "positions": positions,
        "triangles": triangles,
        "uv": uv,
        "bones": bones,
        "weights": weights,
        "bone_count": len(m.m_BoneNameHashes) if hasattr(m, "m_BoneNameHashes") else 0,
    }


_ENV_CACHE = {}


def _load_env(bundle):
    """加载（并缓存）bundle 的 UnityPy 环境，避免重复加载 3.4GB bundle 导致内存爆掉。"""
    import UnityPy
    if bundle not in _ENV_CACHE:
        print("[写回] 加载 bundle: %s ..." % bundle, flush=True)
        _ENV_CACHE[bundle] = UnityPy.load(bundle)
    return _ENV_CACHE[bundle]


def _drop_env(bundle=None):
    """释放缓存的 bundle 环境。保存后 env 已失效；不释放会和下一次加载重复占用约 5GB 内存。

    bundle 传 None 时清空全部缓存。"""
    import gc
    if bundle is None:
        _ENV_CACHE.clear()
    else:
        _ENV_CACHE.pop(bundle, None)
    gc.collect()


def read_bone_names(bundle, root_pid, mesh_pid):
    """读「目标 prefab（root_pid 子树内）」引用该 mesh 的 SkinnedMeshRenderer 的骨骼名字列表。

    只在本 prefab 子树里找 SMR，避免误读到原版游戏里同名 mesh 的其他 SMR
    （模板 US_AMPV_MCV_Empty 的原版也在同一 bundle 里引用这个占位 mesh，会串名）。"""
    env = _load_env(bundle)
    objs = list(env.objects)
    by_pid = {o.path_id: o for o in objs}
    mp = int(mesh_pid)
    rp = int(root_pid)
    # 建层级索引
    tr_go = {}; go_tr = {}; children = {}; go_comps = {}
    for o in objs:
        tn = o.type.name
        if tn == "Transform":
            tr = o.read()
            g = tr.m_GameObject.m_PathID if tr.m_GameObject else 0
            tr_go[o.path_id] = g; go_tr[g] = o.path_id
            children[o.path_id] = [c.m_PathID for c in (tr.m_Children or []) if c and c.m_PathID]
        elif tn == "GameObject":
            go_comps[o.path_id] = [c.m_PathID for c in (o.read().m_Components or []) if c and c.m_PathID]
    # 在 root_pid 子树里找指向 mesh_pid 的 SMR
    target = None
    seen = set(); stack = [rp]
    while stack:
        gpid = stack.pop()
        if gpid in seen:
            continue
        seen.add(gpid)
        for cpid in go_comps.get(gpid, []):
            o = by_pid.get(cpid)
            if o and o.type.name == "SkinnedMeshRenderer":
                r = o.read()
                if r.m_Mesh and r.m_Mesh.m_PathID == mp:
                    target = r
                    break
        if target is not None:
            break
        trpid = go_tr.get(gpid)
        if trpid:
            for c in children.get(trpid, []):
                cg = tr_go.get(c)
                if cg:
                    stack.append(cg)
    if target is None:
        return []
    names = []
    for bptr in (target.m_Bones or []):
        tr = by_pid.get(bptr.m_PathID)
        nm = "?"
        if tr:
            t = tr.read()
            gid = t.m_GameObject.m_PathID if t.m_GameObject else 0
            go = by_pid.get(gid)
            if go:
                nm = go.read().m_Name
        names.append(nm)
    return names


def write_bundle(bundle, root_pid, mesh_pid, positions, triangles, uv, bones, weights, out_bundle, new_prefab):
    """把 Blender 里改好的网格数据写回（deep-copy + 换 mesh + 写权重 + 保存）。

    复用 read_bone_names 已加载的 env，避免重复加载 3.4GB bundle（内存不够会卡死）。"""
    import sys
    import importlib
    # 每次强制重载，确保 _rev_tools/turret_swap.py 的最新改动即时生效（不用重启 Blender）
    if "turret_swap" in sys.modules:
        importlib.reload(sys.modules["turret_swap"])
    import turret_swap
    env = _load_env(bundle)
    try:
        result = turret_swap.run_from_data(
            bundle, root_pid, mesh_pid, positions, triangles, uv, bones, weights, out_bundle, new_prefab, env=env)
        # run_from_data 成功返回 (log, new_root, new_mesh_pid)，出错返回字符串
        if isinstance(result, tuple):
            return result
        return result, None, None
    finally:
        # 保存后 env 已失效，立刻释放，避免 ⑦⑨ 再加载时两份大 bundle 同存导致内存爆掉卡死
        _drop_env(bundle)


def build_turret_direct(bundle, out_bundle, new_prefab, new_name,
                        positions, triangles, uv, bones, weights,
                        turret_index=0, weapon_names=None, shell_names=None,
                        mount_points=None):
    """直接构造炮塔 prefab（无 deep-copy、无模板）。

    bones/weights 里的骨骼索引约定：0=body，1=turret_0。返回 (日志, 新root, 新mesh_pid)。"""
    import sys
    import importlib
    if "build_turret_direct" in sys.modules:
        importlib.reload(sys.modules["build_turret_direct"])
    import build_turret_direct
    env = _load_env(bundle)
    try:
        result = build_turret_direct.build_turret(
            bundle, out_bundle, new_prefab, new_name,
            positions, triangles, uv, bones, weights,
            turret_index=turret_index, weapon_names=weapon_names,
            shell_names=shell_names, mount_points=mount_points, env=env)
        if isinstance(result, tuple):
            return result
        return result, None, None
    finally:
        _drop_env(bundle)


# ---------------------------------------------------------------------------
