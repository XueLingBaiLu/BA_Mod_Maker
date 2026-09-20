# -*- coding: utf-8 -*-
r"""模型自带 AnimationClip：列出 / 解码 / 变成 Blender 关键帧。

背景（v1.8.85）：
  游戏里很多"零件动作"不是 AnimationHub 行为做的，而是 **prefab 自带的 Unity AnimationClip**，
  由 `AnimatorConnect` 行为发 Trigger 让 Animator 播（例：ACV 的防浪板 `WaterShield`、
  后舱门 `Embark`；战机的机翼折叠 `Fold/Open`；直机的 `Left/Right`…）。
  想在 Blender 里**看到**这些动作，就得把片段解出来打成关键帧。

实测要点（都踩过）：
  · 曲线是 **streamed 压缩格式**，UnityPy 1.25.3 **没有**解码器 ⇒ 自写解码（见 `_decode_streamed`）
  · 记录是**变长的**（`[系数…, 曲线号, 尾系数…]`）⇒ 不能按固定步长跳字；
    按"一个关键帧内曲线号 1..N 递增"串链，旋转再用**四元数模长=1**自校验 ✓
  · 曲线号 → 分量：按 `genericBindings` 顺序，**旋转占 4 个**、位置/缩放占 3 个
  · 每个关键帧的**时间戳**在这些片段里解不出来（块头那个 float 实测恒为 0）⇒
    这里按**等间隔**铺满 `m_MuscleClip.m_StopTime`（该长度是**真的**：实测 0.25/0.5/1.0/3.5/8.5 秒…都出现过 ✓）
"""
import math
import os
import struct
import sys

_ENV_CACHE = {}
_CLIP_CACHE = {}


def _ensure_paths():
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (here, os.path.join(os.path.dirname(here), "_unitypy")):
        if os.path.isdir(p) and p not in sys.path:
            sys.path.insert(0, p)


def pid_of(x):
    return x.m_PathID if hasattr(x, "m_PathID") else int(x)


def f32(u):
    return struct.unpack("<f", struct.pack("<I", u & 0xFFFFFFFF))[0]


def _load(bundle):
    _ensure_paths()
    import UnityPy
    env = _ENV_CACHE.get(bundle)
    if env is None:
        env = UnityPy.load(bundle)
        _ENV_CACHE.clear()
        _ENV_CACHE[bundle] = env
    return env


def _short(name):
    return (name or "").replace("\\", "/").split("/")[-1]


class ClipInfo(object):
    __slots__ = ("path", "name", "duration", "sample_rate", "curves", "bindings", "bind")

    def __init__(self, path, name, duration, sample_rate):
        self.path = path
        self.name = name
        self.duration = duration
        self.sample_rate = sample_rate
        self.curves = 0
        self.bindings = []      # [{path, kind, keys}]
        self.bind = {}          # 节点路径 → (局部四元数, 局部位置)（**绑定姿势**，Unity 轴）


def list_clips(bundle, prefab_path):
    """列出某个 prefab 自带的片段：[(名字, 时长秒)]（带缓存）。"""
    key = (bundle, prefab_path)
    if key in _CLIP_CACHE:
        return _CLIP_CACHE[key]
    out = []
    try:
        env = _load(bundle)
        objs = dict(list(env.objects)[0].assets_file.objects)
        ab = next(o.read() for o in objs.values() if o.type.name == "AssetBundle")
        info = dict(ab.m_Container).get(prefab_path)
        if info is not None:
            pl = list(ab.m_PreloadTable)
            for i in range(info.preloadIndex, info.preloadIndex + info.preloadSize):
                if i >= len(pl) or not pl[i]:
                    continue
                o = objs.get(pid_of(pl[i]))
                if o is None or o.type.name != "AnimationClip":
                    continue
                c = o.read()
                try:
                    dur = float(c.m_MuscleClip.m_StopTime)
                except Exception:  # noqa: BLE001
                    dur = 1.0
                out.append((c.m_Name or "?", dur))
    except Exception as e:  # noqa: BLE001
        print("[片段] 列片段失败：%s" % e)
    out.sort()
    _CLIP_CACHE[key] = out
    return out


def _decode_streamed(streamed):
    """streamed 曲线 → [{曲线号: 取值字位置}] + 字数组（自校验见模块说明）。"""
    d = list(streamed.data)
    cc = streamed.curveCount or 0
    if not cc:
        return [], 0, d
    cand = {}
    for i in range(1, len(d) - 2):
        idx = d[i + 1]
        if 1 <= idx <= cc:
            v = f32(d[i])
            if not math.isnan(v):
                cand.setdefault(idx, []).append(i)
    if 1 not in cand:
        return [], cc, d
    frames, cur, prev, k = [], {}, None, 1
    guard = 0
    while guard < 100000:
        guard += 1
        pool = [p for p in cand.get(k, []) if prev is None or p > prev]
        if not pool:
            break
        p = min(pool, key=lambda q: abs(q - (prev + 5)) if prev is not None else q)
        if k == 1 and cur:
            frames.append(cur)
            cur = {}
        cur[k] = p
        prev = p
        k = k + 1
        if k > cc:
            k = 1
    if cur:
        frames.append(cur)
    return frames, cc, d


def _unit_quat(d, positions, window=3):
    """四元数自校验：±window 字窗口里挑「模长最接近 1」的组合（不校验会把 19° 算成 180° ✗）。"""
    import itertools
    best = None
    for offs in itertools.product(range(-window, window + 1), repeat=4):
        q, ok = [], True
        for p, o in zip(positions, offs):
            j = p + o
            if j < 0 or j >= len(d):
                ok = False
                break
            v = f32(d[j])
            if math.isnan(v) or abs(v) > 1.05:
                ok = False
                break
            q.append(v)
        if not ok:
            continue
        n = math.sqrt(sum(x * x for x in q))
        key = (abs(n - 1.0), sum(abs(o) for o in offs))
        if best is None or key < best[0]:
            best = (key, q, n)
    if best is None:
        return None
    return best[1]


def read_clip(bundle, prefab_path, clip_name):
    """解码一个片段 → ClipInfo（`bindings` 里每条含节点路径与逐关键帧的值）。

    值的形式：旋转 = [x, y, z, w]；位置/缩放 = [x, y, z]。
    """
    key = (bundle, prefab_path, clip_name)
    if key in _CLIP_CACHE:
        return _CLIP_CACHE[key]
    _ensure_paths()
    from bone_hashes import bone_hash
    env = _load(bundle)
    objs = dict(list(env.objects)[0].assets_file.objects)
    ab = next(o.read() for o in objs.values() if o.type.name == "AssetBundle")
    info = dict(ab.m_Container).get(prefab_path)
    if info is None:
        return None
    pl = list(ab.m_PreloadTable)
    pre = [pid_of(pl[i]) for i in range(info.preloadIndex,
                                        info.preloadIndex + info.preloadSize)
           if i < len(pl) and pl[i]]
    # 节点路径 ↔ 哈希（同时把**绑定姿势**的局部 TRS 记下来，驱动骨架要用）
    go_name, tr_go, tr_father, tr_children, tr_local = {}, {}, {}, {}, {}
    for p in pre:
        o = objs.get(p)
        if o is None:
            continue
        if o.type.name == "GameObject":
            go_name[p] = o.read().m_Name
        elif o.type.name == "Transform":
            d = o.read()
            tr_go[p] = pid_of(d.m_GameObject)
            tr_father[p] = pid_of(d.m_Father) if (d.m_Father and pid_of(d.m_Father)) else 0
            tr_children[p] = [pid_of(c) for c in (d.m_Children or []) if c]
            q = d.m_LocalRotation
            tr_local[p] = ((q.x, q.y, q.z, q.w),
                           (d.m_LocalPosition.x, d.m_LocalPosition.y, d.m_LocalPosition.z))
    ph = {}
    bind = {}

    def walk(t, chain):
        nm = go_name.get(tr_go.get(t, -1), "?")
        full = chain + [nm]
        for parts in (full, full[1:]):
            if not parts:
                continue
            try:
                h = bone_hash(parts)
                ph.setdefault(h, "/".join(parts))
                ph.setdefault(h & 0xFFFFFFFF, "/".join(parts))
                if t in tr_local:
                    bind.setdefault("/".join(parts), tr_local[t])
            except Exception:  # noqa: BLE001
                pass
        for c in tr_children.get(t, []):
            walk(c, full)
    for r in [t for t in tr_go if not tr_father.get(t)]:
        walk(r, [])

    clip_obj = None
    for p in pre:
        o = objs.get(p)
        if o is not None and o.type.name == "AnimationClip":
            cc = o.read()
            if (cc.m_Name or "") == clip_name:
                clip_obj = cc
                break
    if clip_obj is None:
        return None
    try:
        duration = float(clip_obj.m_MuscleClip.m_StopTime) or 1.0
    except Exception:  # noqa: BLE001
        duration = 1.0
    ci = ClipInfo(prefab_path, clip_name, duration, float(clip_obj.m_SampleRate or 30.0))
    ci.bind = bind
    frames, cc, words = _decode_streamed(clip_obj.m_MuscleClip.m_Clip.data.m_StreamedClip)
    ci.curves = cc
    bindings = list(clip_obj.m_ClipBindingConstant.genericBindings or [])
    for pi, gg in enumerate(bindings):
        kind = {1: "pos", 2: "rot", 3: "scale", 4: "rot"}.get(gg.attribute)
        if kind is None:
            continue
        base = 1 + sum(4 if b.attribute == 2 else 3 for b in bindings[:pi])
        comps = 4 if kind == "rot" else 3
        keys = []
        for fr in frames:
            pos = [fr.get(base + j) for j in range(comps)]
            if any(x is None for x in pos):
                continue
            if kind == "rot":
                q = _unit_quat(words, pos)
                if q is None:
                    continue
                keys.append(q)
            else:
                keys.append([f32(words[p]) for p in pos])
        if not keys:
            continue
        ci.bindings.append({"path": ph.get(gg.path) or ph.get(gg.path & 0xFFFFFFFF)
                            or "哈希%s" % gg.path,
                            "kind": kind, "keys": keys})
    _CLIP_CACHE[key] = ci
    return ci


# ---------------------------------------------------------------------------
# 应用成 Blender 关键帧
# ---------------------------------------------------------------------------
def object_paths(scene):
    """场景里每个物体 → 它的「根→自己」名字链（列表）。导入的模型是 Empty 层级 ✓"""
    out = {}

    def chain(o):
        names = []
        cur = o
        seen = set()
        while cur is not None and cur.name not in seen:
            seen.add(cur.name)
            names.append(cur.name)
            cur = cur.parent
        return list(reversed(names))
    for o in scene.objects:
        out[o.name] = chain(o)
    return out


def find_object(scene, path):
    """按片段里的节点路径找 Blender 物体：先比"名字链尾部完全一致"，再退回按末名匹配。"""
    parts = [p for p in (path or "").split("/") if p]
    if not parts:
        return None
    want = parts
    chains = object_paths(scene)
    best, best_len = None, -1
    for name, ch in chains.items():
        if len(ch) >= len(want) and ch[len(ch) - len(want):] == want:
            if len(want) > best_len:
                best, best_len = name, len(want)
    if best is not None:
        return scene.objects[best]
    last = want[-1]
    if last in scene.objects:
        return scene.objects[last]
    for name, ch in chains.items():
        if ch and ch[-1] == last:
            return scene.objects[name]
    return None


def q_mul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (ax * bw + aw * bx + ay * bz - az * by,
            ay * bw + aw * by + az * bx - ax * bz,
            az * bw + aw * bz + ax * by - ay * bx,
            aw * bw - ax * bx - ay * by - az * bz)


def q_conj(q):
    return (-q[0], -q[1], -q[2], q[3])


def q_rot(q, v):
    return q_mul(q_mul(q, (v[0], v[1], v[2], 0.0)), q_conj(q))[:3]


def conv_q(q):
    r"""Unity 四元数 (x, y, z, w) → 可直接赋给 Blender 的 (w, x, -z, y)。

    ⛔⛔ **两件事一起做，顺序别搞反**（v1.8.88 修的坑，用户实测"模型绞在一起、防浪板转轴不对"）：
      1. **坐标轴换算**：导入器的约定是 `(x,y,z)_unity → (x,-z,y)_blender` ⇒ 旋转也要换，
         即 `(x,y,z)_u → (x,-z,y)_b`；
      2. **分量顺序**：Unity 四元数是 **(x,y,z,w)**，Blender 的 `rotation_quaternion`
         是 **(w,x,y,z)** ✗ —— 直接把 Unity 的 (x,y,z,w) 赋给 Blender，等于把 w 塞进了 x 槽，
         旋转会乱成一团（这就是"绞起来"的真凶 ✓）。
    """
    return (q[3], q[0], q[2], -q[1])  # ★㓓：随手性修同步（verify_math.py V7 数值验证）


def conv_v(v):
    return (-v[0], -v[2], v[1])  # ★㓓：随手性修同步


def _armature_for(scene):
    """导入的蒙皮网格用的骨架（从 Armature 修改器找最可靠）。"""
    for o in scene.objects:
        if o.type != "MESH":
            continue
        for m in o.modifiers:
            if m.type == "ARMATURE" and m.object is not None:
                return m.object
    for o in scene.objects:
        if o.type == "ARMATURE":
            return o
    return None


def _fcurve_bags(act):
    """动作里的曲线容器（Blender 4.4+ 是 layers→strips→channelbags；老的是 action.fcurves）。"""
    bags = []
    try:
        for lay in act.layers:
            for st in lay.strips:
                for cb in st.channelbags:
                    bags.append(cb)
    except AttributeError:
        pass
    if not bags and hasattr(act, "fcurves"):
        bags.append(act)
    return bags


def clear_channels(scene, ci, bones=()):
    """**先清掉上次导入留下的关键帧**（只清本次要碰的通道，不动用户自己的其它动画）。

    ⛔ 为什么必须清：re-import 时旧键还在 ⇒ 改了「驱动范围」也看不出效果
       （实测：切到"只驱动主件"后接缝仍是 0.594 m，因为子件上一轮的键还留着 ✗）
    """
    if ci is None:
        return 0
    obj_names = set()
    bone_names = set(bones or ())
    for b in ci.bindings:
        nm = (b["path"] or "").split("/")[-1]
        obj_names.add(nm)
        bone_names.add(nm)
    chans = ("rotation_quaternion", "location", "scale")
    n = 0
    for o in list(scene.objects):
        if o.type == "ARMATURE":
            continue
        if o.name not in obj_names:
            continue
        ad = o.animation_data
        if not ad or not ad.action:
            continue
        for bag in _fcurve_bags(ad.action):
            for fc in list(bag.fcurves):
                dp = fc.data_path.split(".")[-1]
                if dp in chans and fc.data_path in ("rotation_quaternion", "location", "scale"):
                    bag.fcurves.remove(fc)
                    n += 1
    for o in list(scene.objects):
        if o.type != "ARMATURE":
            continue
        ad = o.animation_data
        if not ad or not ad.action:
            continue
        for bag in _fcurve_bags(ad.action):
            for fc in list(bag.fcurves):
                for nm in bone_names:
                    pre = 'pose.bones["%s"].' % nm
                    if fc.data_path.startswith(pre) and fc.data_path[len(pre):] in chans:
                        bag.fcurves.remove(fc)
                        n += 1
                        break
    return n


def apply_to_scene(scene, ci, fps=24.0, bind_armature=True, mode="all", skip_names=()):
    """把片段打成关键帧。

    ⛔ **必须同时驱动骨架**：导入的模型是**蒙皮网格**（Armature 修改器 + 顶点组），
       只给挂载点空物体打关键帧 ⇒ 骨架不动 ⇒ 网格一动不动（用户实测报过这个 ✗）。
       骨头的静止姿势实测**就等于 prefab 绑定姿势**（同角度同位置 ✓）⇒ 可以直接写
       `pose_bone`：`basis = 绑定局部⁻¹ · 动画局部`（基于是"相对静止姿势的增量"这个语义 ✓）

    `mode`（v1.8.89 新增，用来处理"连接杆看起来断开"）：
      · `"all"`  —— 全部按片段驱动（**默认**，和游戏一致）
      · `"leaf"` —— **只驱动「主件」**：父件被驱动时，它的**子件不再单独动**（刚性跟随父件）。
        为什么需要：实测 ACV 的 `WaterShield` 里 `Shield_01`（`Shield` 的子件）末帧
        **比父件多转 16.4°** ⇒ 铰链在下方、接缝在 2 m 开外 ⇒ 接缝被拉开 **0.594 m**，
        看起来就是"连接杆断开" ✗。切到这个模式，接缝保持静止时的 0.019 m ✓
      · `"skip"` —— 只跳过 `skip_names` 里点名的节点（按末段名字匹配）✓

    返回 (改动的物体数, 关键帧总数, 未找到的节点路径列表, 骨头数, 跳过的节点名列表)。
    """
    if ci is None:
        return 0, 0, [], 0, []
    n_obj, n_key, missing, n_bone = 0, 0, [], 0
    span = max(float(ci.duration or 1.0), 1e-3)
    arm = _armature_for(scene) if bind_armature else None
    skip_set = set(skip_names or ())
    # 先清旧键（改「驱动范围」再点一次时要真的覆盖 ✓）
    clear_channels(scene, ci)

    def short(p):
        return (p or "").split("/")[-1]

    # 先算出"哪些路径被驱动"⇒ 供 leaf 模式判断"是不是别人的子件"
    driven_paths = []
    for b in ci.bindings:
        if b["keys"]:
            driven_paths.append(b["path"])
    skipped = []
    for b in ci.bindings:
        path = b["path"]
        nm = short(path)
        if mode == "skip" and nm in skip_set:
            skipped.append(nm)
            continue
        if mode == "leaf":
            # 该路径的**任意祖先**也在被驱动列表里 ⇒ 它是子件 ⇒ 跳过（刚性跟随父件 ✓）
            anc = [p for p in driven_paths
                   if p != path and (path.startswith(p + "/"))]
            if anc:
                skipped.append(nm)
                continue
        keys = b["keys"]
        n = len(keys)
        if not n:
            continue
        times = [0.0] if n == 1 else [span * i / float(n - 1) for i in range(n)]
        path = b["path"]
        bind = ci.bind.get(path)
        obj = find_object(scene, path)
        bone = None
        if arm is not None:
            bone = arm.data.bones.get(path.split("/")[-1])
        if obj is None and bone is None:
            missing.append(path)
            continue
        kind = b["kind"]
        ok_obj = obj is not None
        ok_bone = bone is not None
        if ok_bone:
            pb = arm.pose.bones[bone.name]
            pb.rotation_mode = "QUATERNION"
            n_bone += 1
        prev_obj = prev_bone = None
        for t, val in zip(times, keys):
            frame = int(round(t * fps))
            if kind == "rot":
                q_anim = tuple(val)
                q_obj = conv_q(q_anim)
                if bind:
                    q_bind = tuple(bind[0])
                    q_basis = conv_q(q_mul(q_conj(q_bind), q_anim))
                else:
                    q_basis = q_obj
                if ok_obj:
                    qo = list(q_obj)
                    if prev_obj is not None and sum(a * c for a, c in zip(prev_obj, qo)) < 0:
                        qo = [-x for x in qo]
                    prev_obj = qo
                    obj.rotation_mode = "QUATERNION"
                    obj.rotation_quaternion = qo
                    obj.keyframe_insert("rotation_quaternion", frame=frame)
                    n_key += 1
                if ok_bone:
                    qb = list(q_basis)
                    if prev_bone is not None and sum(a * c for a, c in zip(prev_bone, qb)) < 0:
                        qb = [-x for x in qb]
                    prev_bone = qb
                    pb.rotation_quaternion = qb
                    pb.keyframe_insert("rotation_quaternion", frame=frame)
                    n_key += 1
            elif kind == "pos":
                t_anim = tuple(val)
                t_obj = conv_v(t_anim)
                if bind:
                    d = tuple(t_anim[i] - bind[1][i] for i in range(3))
                    t_basis = conv_v(q_rot(q_conj(tuple(bind[0])), d))
                else:
                    t_basis = t_obj
                if ok_obj:
                    obj.location = t_obj
                    obj.keyframe_insert("location", frame=frame)
                    n_key += 1
                if ok_bone:
                    pb.location = t_basis
                    pb.keyframe_insert("location", frame=frame)
                    n_key += 1
            else:
                if ok_obj:
                    obj.scale = tuple(val)
                    obj.keyframe_insert("scale", frame=frame)
                    n_key += 1
                if ok_bone:
                    pb.scale = tuple(val)
                    pb.keyframe_insert("scale", frame=frame)
                    n_key += 1
        if ok_obj:
            n_obj += 1
    scene.frame_start = 0
    scene.frame_end = max(1, int(round(span * fps)))
    return n_obj, n_key, missing, n_bone, skipped
