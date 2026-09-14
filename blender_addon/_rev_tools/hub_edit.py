# -*- coding: utf-8 -*-
r"""AnimationHub 编辑：动画行为 <-> JSON 双向转换（查看 + 修改 + 新增）。

JSON 结构（用户可读/可编辑）：
{
  "universal": [ 行为... ],
  "demo":      [ 行为... ],
  "game":      [ ... ],
  "preDeath":  [ ... ],
  "death":     [ ... ]
}

行为（已破译的五种）：
  AxisRandom:
    {"type":"AxisRandom","lod":0,"name":"","speed":20.0,"minTime":2.0,"maxTime":10.0,
     "axes":{"x":{"source":"","min":0,"max":0},
             "y":{"source":"turret_0","min":-45,"max":45},
             "z":{"source":"","min":0,"max":0}}}
  MathConnect:
    {"type":"MathConnect","lod":0,"freq":0,"damper":0,"reaction":0,"objBool":true,
     "root":"","target":"","freeze":{"x":false,"y":false,"z":false},
     "shots":[{"source":"shell_spawn_0","value":1.0}]}
  Torque（旋翼自转 —— 直升机主桨/尾桨靠它转）:
    {"type":"Torque","lod":2,"target":"Rotorangle_0",
     "direction":{"x":0.0,"y":-1.0,"z":0.0},"speed":1080.0}
    ⛔ 字节布局（32 字节，5 架直升机 16 个实例全部实测一致，v1.8.57）：
       lod(i32) + target PPtr(m_FileID i32 + m_PathID i64) + direction(3×f32) + speed(f32)
    ⛔ 约定（实测）：主旋翼美系 (0,-1,0)、俄系 (0,+1,0)；尾桨一律 (-1,0,0)；
       转速 game=1080 °/s、preDeath=540 °/s。
  FloatEffect / WaterFloatEffect（载具空中/水面浮动）:
    {"type":"FloatEffect","lod":1,"target":"","values":[8 个 f32]}
    ⛔ 布局：lod(i32) + target PPtr(12) + N×f32（FloatEffect N=8，WaterFloatEffect N=4）

未破译的行为（如 AnimatorConnect / HelicopterDustVFX / SpawnVFX / ModelReplace /
HideMesh / PositionRandom / ShowVFX / AudioPlayer / SpawnCorrector / DustVFX /
ProceduralDirt）保留原始字节 rawB64，序列化时不丢失。
"""
import os, sys, struct, json, base64

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

HUB_SCRIPT = 4665939560152279323
NS_ANIM = "BrokenArrow.Client.Ecs.AnimationBehaviors"
ASM_ANIM = "BrokenArrow"
ARRAY_NAMES = ("universal", "demo", "game", "preDeath", "death")


def _read_str(buf, off):
    n = struct.unpack_from("<i", buf, off)[0]
    off += 4
    s = buf[off:off + n].decode("utf-8", "replace")
    off += n
    return s, (off + 3) & ~3


def _write_str(buf, s):
    b = (s or "").encode("utf-8")
    buf += struct.pack("<i", len(b))
    buf += b
    while len(buf) % 4:
        buf += b"\0"


def _axisrandom_data_len(raw, off):
    o = 4
    n = struct.unpack_from("<i", raw, off + o)[0]
    o += 4 + n
    return (o + 12 + 60 + 3) & ~3


def _mathconnect_data_len(raw, off):
    o = 4 + 16 + 12 + 12 + 4
    n = struct.unpack_from("<i", raw, off + o)[0]
    o += 4 + n * 12
    n2 = struct.unpack_from("<i", raw, off + o)[0]
    o += 4 + n2 * 4
    return (o + 3) & ~3


def _parse_axisrandom(d, name_of):
    lod = struct.unpack_from("<i", d, 0)[0]
    name, o = _read_str(d, 4)
    speed, mint, maxt = struct.unpack_from("<fff", d, o)
    o += 12
    axes = {}
    for ax in ("x", "y", "z"):
        _, pid = struct.unpack_from("<iq", d, o); o += 12
        amin, amax = struct.unpack_from("<ff", d, o); o += 8
        axes[ax] = {"source": name_of(pid), "min": amin, "max": amax,
                    "sourcePID": int(pid)}
    return {"type": "AxisRandom", "lod": lod, "name": name,
            "speed": speed, "minTime": mint, "maxTime": maxt, "axes": axes}


# MathConnect 固定头长度：lod(4) + ObjectFollower(16: freq/damper/reaction + defaultSerialized bool)
# + root PPtr(12) + target PPtr(12) + freeze 3 bool(3+1 pad) = 48 字节。
# 之后是 WeaponShotForces（UnitySerializedDictionary<Transform, float>），
# ⛔⛔ v1.8.84 修正：字典**从第 56 字节开始**，不是 48 ✗
#    真布局（实测，与 behavior_codec 的通用解析逐字节一致）：
#      lod(4) + ObjectFollower{freq,damper,reaction}(12) + _defaultSerialized(**4 字节 bool**)
#      + root PPtr(12) + target PPtr(12) + 3×bool(**每个 4 字节** = 12) + 字典
#      ⇒ 字典起点 = 4+12+4+12+12+12 = **56**
#    老代码按"1 字节 bool"算 ⇒ 48，于是把 `01 00 00 00`（第二个 bool=_freezeYPos）
#    当成了字典的头部 ⇒ 面板上「冻结 Y」显示为假、shotForces 也解析成 {'a':1,'b':0,shots:[]} ✗
MATHCONNECT_HEADER = 56


def _parse_shotforces(data, name_of):
    """解析 WeaponShotForces 字典字节（布局见 behavior_codec：**[键数][键…][值数][值…]**）。"""
    import struct
    if len(data) < 8:
        return None
    n = struct.unpack_from("<i", data, 0)[0]
    off = 4
    if n < 0 or n > 4096:
        return None
    shots = []
    for _ in range(n):
        if off + 12 > len(data):
            return None
        fid, pid = struct.unpack_from("<iq", data, off)
        off += 12
        shots.append({"fileID": fid, "pathID": pid, "source": name_of(pid) if pid else ""})
    if off + 4 > len(data):
        return None
    vc = struct.unpack_from("<i", data, off)[0]
    off += 4
    if vc < 0 or off + vc * 4 > len(data):
        return None
    vals = list(struct.unpack_from("<%df" % vc, data, off)) if vc else []
    for i, s in enumerate(shots):
        s["value"] = vals[i] if i < len(vals) else 1.0
    return {"shots": shots, "valueCount": vc}


def _emit_shotforces(d, pid_of):
    """重建 WeaponShotForces 字典字节（**[键数][键…][值数][值…]**，键值数各自独立）。"""
    import struct
    sf = d or {}
    shots = sf.get("shots") or []
    buf = struct.pack("<i", len(shots))
    for s in shots:
        pid = pid_of(s.get("source", "")) if isinstance(s, dict) and s.get("source") else 0
        if isinstance(s, dict) and s.get("pathID"):
            pid = s["pathID"]
        buf += struct.pack("<iq", int(s.get("fileID", 0) if isinstance(s, dict) else 0), int(pid))
    buf += struct.pack("<i", int(sf.get("valueCount", len(shots))))
    for s in shots:
        buf += struct.pack("<f", float(s.get("value", 1.0)) if isinstance(s, dict) else 1.0)
    return bytes(buf)


def _parse_mathconnect(d, name_of):
    lod = struct.unpack_from("<i", d, 0)[0]
    freq, damp, reac = struct.unpack_from("<fff", d, 4)
    # ⛔ bool 在这些资源里是 **4 字节**（v1.8.84 实测；见 behavior_meta.PRIM_SIZE）
    obj_bool = struct.unpack_from("<i", d, 16)[0]
    _, root = struct.unpack_from("<iq", d, 20)
    _, tgt = struct.unpack_from("<iq", d, 32)
    fx, fy, fz = struct.unpack_from("<iii", d, 44)
    shots = d[MATHCONNECT_HEADER:] if len(d) > MATHCONNECT_HEADER else b""
    out = {"type": "MathConnect", "lod": lod, "freq": freq, "damper": damp,
           "reaction": reac, "objBool": bool(obj_bool), "root": name_of(root),
           "target": name_of(tgt), "freeze": {"x": bool(fx), "y": bool(fy), "z": bool(fz)},
           "shotsRawB64": base64.b64encode(shots).decode("ascii") if shots else "",
           "rawB64": base64.b64encode(d).decode("ascii")}
    sf = _parse_shotforces(shots, name_of) if shots else None
    if sf is not None:
        out["shotForces"] = sf
    return out


# ---------------------------------------------------------------------------
# Torque（旋翼自转）—— 32 字节固定布局，已实测（v1.8.57）
#   lod(i32) + target PPtr(m_FileID i32 + m_PathID i64) + direction(3×f32) + speed(f32)
# ---------------------------------------------------------------------------
TORQUE_LEN = 32


def _parse_torque(d, name_of):
    lod = struct.unpack_from("<i", d, 0)[0]
    fid, tpid = struct.unpack_from("<iq", d, 4)
    dx, dy, dz = struct.unpack_from("<fff", d, 16)
    sp = struct.unpack_from("<f", d, 28)[0]
    return {"type": "Torque", "lod": lod, "target": name_of(tpid),
            "fileID": fid, "direction": {"x": dx, "y": dy, "z": dz},
            "speed": sp, "targetPID": int(tpid),
            "rawB64": base64.b64encode(d).decode("ascii")}


def _torque_bytes(d, pid_of):
    """按字段重建（不优先 rawB64 —— 用户改转速/方向必须生效）。"""
    lod = int(d.get("lod", 2))
    fid = int(d.get("fileID", 0) or 0)
    tgt = d.get("target", "")
    tpid = pid_of(tgt) if tgt else 0
    if not tpid and d.get("targetPID"):
        tpid = int(d["targetPID"])
    dr = d.get("direction") or {}
    buf = bytearray()
    buf += struct.pack("<i", lod)
    buf += struct.pack("<iq", fid, int(tpid))
    buf += struct.pack("<fff", float(dr.get("x", 0.0)), float(dr.get("y", -1.0)),
                       float(dr.get("z", 0.0)))
    buf += struct.pack("<f", float(d.get("speed", 1080.0)))
    return bytes(buf)


# ---------------------------------------------------------------------------
# FloatEffect（空中浮动）/ WaterFloatEffect（水面浮动）—— 同布局，f32 个数不同
#   lod(i32) + target PPtr(12) + N×f32      FloatEffect N=8 / WaterFloatEffect N=4
# 实测来源：US_MH60X/US_UH60M/RU_MI24V_VP/US_AH64D 的 FloatEffect 全是 48 字节；
#          US_ACV 的 WaterFloatEffect 是 32 字节。
# ---------------------------------------------------------------------------
EFFECT_FLOATS = {"FloatEffect": 8, "WaterFloatEffect": 4}


def _parse_effect(d, name_of, cls):
    n = EFFECT_FLOATS[cls]
    lod = struct.unpack_from("<i", d, 0)[0]
    fid, tpid = struct.unpack_from("<iq", d, 4)
    vals = list(struct.unpack_from("<%df" % n, d, 16)) if len(d) >= 16 + 4 * n else []
    return {"type": cls, "lod": lod, "target": name_of(tpid), "fileID": fid,
            "values": vals, "targetPID": int(tpid),
            "rawB64": base64.b64encode(d).decode("ascii")}


def _effect_bytes(d, pid_of, cls):
    n = EFFECT_FLOATS[cls]
    vals = list(d.get("values") or [])
    while len(vals) < n:
        vals.append(0.0)
    tgt = d.get("target", "")
    tpid = pid_of(tgt) if tgt else 0
    if not tpid and d.get("targetPID"):
        tpid = int(d["targetPID"])
    buf = bytearray()
    buf += struct.pack("<i", int(d.get("lod", 1)))
    buf += struct.pack("<iq", int(d.get("fileID", 0) or 0), int(tpid))
    buf += struct.pack("<%df" % n, *[float(v) for v in vals[:n]])
    return bytes(buf)


def _entry_header_at(raw, off, remaining):
    """off 处若是一个合法条目头 ⇒ 返回 (rid, 类名字符串起始偏移)；否则 None。

    判据是**双条件**（v1.8.57 修）：rid 必须还在剩余集合里，**且**紧跟其后的类名字符串
    必须合法（长度 1..64 且全为可打印 ASCII）。
    ⛔ 旧版只判 rid ⇒ 在 MathConnect 的可变长数据里误命中（路径 id 与 rid 撞车）
    ⇒ 偏移算飞 ⇒ "unpack_from requires a buffer of at least N bytes"（US_ACV / US_MH60X
    这类较新的 prefab 上必然发生，④ 读取动画因此长期不可用）。
    """
    if off + 12 > len(raw):
        return None
    rid = struct.unpack_from("<q", raw, off)[0]
    if rid not in remaining:
        return None
    p = off + 8
    n = struct.unpack_from("<i", raw, p)[0]
    if n < 1 or n > 64 or p + 4 + n > len(raw):
        return None
    b = raw[p + 4:p + 4 + n]
    if not all(32 <= c < 127 for c in b):
        return None
    return rid, p


def _find_next_rid(raw, off, remaining):
    """从 off 起找**下一个合法条目头**的偏移；找不到返回 len(raw)。"""
    if not remaining:
        return len(raw)
    for i in range(off, len(raw) - 12):
        if _entry_header_at(raw, i, remaining):
            return i
    return len(raw)


def hub_to_dict(raw, name_of):
    off = 32
    arrays = {}
    all_rids = []
    for name in ARRAY_NAMES:
        n = struct.unpack_from("<i", raw, off)[0]
        off += 4
        rids = [struct.unpack_from("<q", raw, off + 8 * i)[0] for i in range(n)]
        off += 8 * n
        arrays[name] = rids
        all_rids.extend(rids)
    ver = struct.unpack_from("<i", raw, off)[0]; off += 4
    cnt = struct.unpack_from("<i", raw, off)[0]; off += 4
    remaining = set(all_rids)
    reg = {}
    rid_order = []
    for _ in range(cnt):
        if off + 8 > len(raw):
            break
        rid = struct.unpack_from("<q", raw, off)[0]; off += 8
        rid_order.append(rid)
        cls, off = _read_str(raw, off)
        ns, off = _read_str(raw, off)
        asm_, off = _read_str(raw, off)
        remaining.discard(rid)
        # 统一用扫描定位本条目数据长度（比按类算长度更稳，兼容未破译类）
        nxt = _find_next_rid(raw, off, remaining)
        data = raw[off:nxt]
        if cls == "AxisRandom":
            try:
                parsed = _parse_axisrandom(data, name_of)
            except Exception:
                parsed = {"type": cls, "rawB64": base64.b64encode(data).decode("ascii")}
        elif cls == "MathConnect":
            try:
                parsed = _parse_mathconnect(data, name_of)
            except Exception:
                parsed = {"type": cls, "rawB64": base64.b64encode(data).decode("ascii")}
        elif cls == "Torque" and len(data) >= TORQUE_LEN:
            try:
                parsed = _parse_torque(data, name_of)
            except Exception:
                parsed = {"type": cls, "rawB64": base64.b64encode(data).decode("ascii")}
        elif cls in EFFECT_FLOATS:
            try:
                parsed = _parse_effect(data, name_of, cls)
            except Exception:
                parsed = {"type": cls, "rawB64": base64.b64encode(data).decode("ascii")}
        else:
            parsed = {"type": cls, "rawB64": base64.b64encode(data).decode("ascii")}
        if isinstance(parsed, dict):
            parsed["_rid"] = rid
            parsed["_ns"] = ns
            parsed["_asm"] = asm_
            # ⛔ 每个条目都带上**原始数据字节**（hex）：面板的通用字段编辑器拿它做
            #    「元数据驱动的解析 → 编辑 → 原样写回」。以前只有"未破译"类才带 rawB64，
            #    已破译类走的是手写解析/序列化 —— 那样每支持一个类都要改代码 ✗。
            parsed.setdefault("rawB64", base64.b64encode(data).decode("ascii"))
        reg[rid] = parsed
        off = nxt
    out = {}
    for name in ARRAY_NAMES:
        out[name] = [reg[r] for r in arrays[name] if r in reg]
    out["_order"] = rid_order
    return out


def _axisrandom_bytes(d, pid_of):
    buf = bytearray()
    buf += struct.pack("<i", int(d.get("lod", 0)))
    _write_str(buf, d.get("name", ""))
    buf += struct.pack("<fff", float(d.get("speed", 0)), float(d.get("minTime", 0)),
                       float(d.get("maxTime", 0)))
    for ax in ("x", "y", "z"):
        a = d.get("axes", {}).get(ax, {})
        src = a.get("source", "")
        pid = pid_of(src) if src else 0
        if not pid and a.get("sourcePID"):
            # 目标不在当前 prefab 树里（或名字认不出来）⇒ 原样保留原 pid，保证往返不丢数据
            pid = int(a["sourcePID"])
        buf += struct.pack("<iqff", 0, pid,
                           float(a.get("min", 0)), float(a.get("max", 0)))
    return bytes(buf)


def _mathconnect_bytes(d, pid_of):
    if d.get("rawB64"):
        return base64.b64decode(d["rawB64"])
    buf = bytearray()
    buf += struct.pack("<i", int(d.get("lod", 0)))
    # ⛔ bool 一律 4 字节（v1.8.84）：`<fffB` + 3 字节填充是**错的**（那是 1 字节 bool 的写法）✗
    buf += struct.pack("<fffi", float(d.get("freq", 0)), float(d.get("damper", 0)),
                       float(d.get("reaction", 0)), 1 if d.get("objBool", True) else 0)
    buf += struct.pack("<iq", 0, pid_of(d.get("root", "")))
    buf += struct.pack("<iq", 0, pid_of(d.get("target", "")))
    f = d.get("freeze", {})
    buf += struct.pack("<iii", 1 if f.get("x") else 0, 1 if f.get("y") else 0,
                       1 if f.get("z") else 0)
    if d.get("shotForces"):
        # 按真布局重建（[键数][键…][值数][值…]；字节级往返有 rawB64 兜底）
        buf += _emit_shotforces(d["shotForces"], pid_of)
    elif d.get("shotsRawB64"):
        buf += base64.b64decode(d["shotsRawB64"])
    else:
        buf += struct.pack("<ii", 0, 0)   # 键数 0 + 值数 0
    return bytes(buf)


def _emit_behavior_bytes(item, typ, pid_of):
    r"""行为数据体 → 字节。返回 None 表示"本函数不认识这个条目"。

    ⛔ v1.8.67 起**优先走 `__vals__`（字段值 JSON）+ 构建期的 `pid_of`**：
    `__bytes__` 是面板在「读取动画」那一刻烘出来的死字节，里面的节点引用 pid 是按
    **源 prefab** 写的；而用户**自己新建的挂载点**（`Rotorangle_0` 这类）当时还没有 pid
    ⇒ 只能烘 0 ⇒ 进游戏指向空引用（**旋翼不转**）✗ —— 实测确认（一键加旋翼产出的
    `__bytes__` 里 pathID = 0）。
    `__vals__` 把字段值（含 `__name__` 挂载点名）留到构建期解析，pid 才是对的 ✓。

    顺序：`__vals__` → `__bytes__`（旧数据 / 手改 JSON 仍支持）→ 按类型重建 → rawB64。
    """
    vals = item.get("__vals__")
    if isinstance(vals, dict):
        try:
            import behavior_meta as BM
            import behavior_codec as BC
            return BC.emit(BM.load(), typ, vals, pid_of=pid_of)
        except Exception as e:  # noqa: BLE001
            print("[hub_edit] %s 的 __vals__ 写回失败，退回其它路径：%s" % (typ, e))
    if item.get("__bytes__"):
        return bytes.fromhex(item["__bytes__"])
    if typ == "AxisRandom":
        return _axisrandom_bytes(item, pid_of)
    if typ == "MathConnect":
        return _mathconnect_bytes(item, pid_of)
    if typ == "Torque":
        return _torque_bytes(item, pid_of)
    if typ in EFFECT_FLOATS:
        return _effect_bytes(item, pid_of, typ)
    if item.get("rawB64"):
        return base64.b64decode(item["rawB64"])
    return None


def _emit_entry(rid, item, pid_of):
    """写一个条目：rid + 三字符串（类/命名空间/程序集）+ 该类数据体。

    ⛔ 命名空间/程序集**必须沿用读到的值**（`_ns`/`_asm`）：本作里并非所有行为都在
    同一个命名空间 —— 实测 `ProceduralDirt` 的命名空间就是**空串** ✗（若写死
    `BrokenArrow.Client.Ecs.AnimationBehaviors` 会凭空多 44 字节、且反序列化失败）。
    新建的行为没有 `_ns` ⇒ 用默认值。
    """
    buf = bytearray()
    buf += struct.pack("<q", rid)
    typ = item.get("type", "AxisRandom")
    ns = item.get("_ns", NS_ANIM)
    asm = item.get("_asm", ASM_ANIM)
    if not isinstance(ns, str):
        ns = NS_ANIM
    if not isinstance(asm, str) or not asm:
        asm = ASM_ANIM
    for s in (typ, ns, asm):
        b = s.encode("utf-8")
        buf += struct.pack("<i", len(b)); buf += b
        while len(buf) % 4:
            buf += b"\0"
    body = _emit_behavior_bytes(item, typ, pid_of)
    if body:
        buf += body
    return bytes(buf)


def dict_to_hub(d, root_gpid, pid_of):
    buf = bytearray()
    buf += struct.pack("<iq", 0, root_gpid)
    buf += struct.pack("<B", 1)
    buf += bytes(3)
    buf += struct.pack("<iq", 0, HUB_SCRIPT)
    buf += struct.pack("<i", 0)
    # ⛔ rid 分配必须**全局唯一**：rid 是 Unity ManagedReferencesRegistry 的键，
    #    重复 ⇒ 行为绑定错乱/反序列化异常 ✗
    #    旧写法用"上一条 rid + 1"当计数器，读到 `[{_rid:5}, 新增项, {_rid:6}]` 时会给出
    #    `[5, 6, 6]` —— 中间插入的新项与后面既有项撞车（实测复现）。
    #    正确做法：先收集全部显式 rid，新项从「最大值 + 1」起分配，并跳过已占用的值。
    _used = set()
    for _name in ARRAY_NAMES:
        for _it in (d.get(_name) or []):
            _r = _it.get("_rid") if isinstance(_it, dict) else None
            if isinstance(_r, int):
                _used.add(_r)
    _next = (max(_used) + 1) if _used else 1

    def _alloc_rid():
        nonlocal _next
        while _next in _used:
            _next += 1
        _used.add(_next)
        return _next

    array_rids = {}
    for name in ARRAY_NAMES:
        items = d.get(name, []) or []
        rids = []
        for item in items:
            r = item.get("_rid") if isinstance(item, dict) else None
            if not isinstance(r, int) or r in rids:
                r = _alloc_rid()      # 缺失或与本数组内重复 ⇒ 重新分配
            rids.append(r)
        array_rids[name] = rids
        buf += struct.pack("<i", len(items))
        for r in rids:
            buf += struct.pack("<q", r)
    buf += struct.pack("<i", 2)
    total = sum(len(v) for v in array_rids.values())
    buf += struct.pack("<i", total)
    rid_to_item = {}
    for name in ARRAY_NAMES:
        for r, item in zip(array_rids[name], d.get(name, []) or []):
            rid_to_item[r] = item
    order = d.get("_order") or [r for name in ARRAY_NAMES for r in array_rids[name]]
    for r in order:
        item = rid_to_item.get(r)
        if item is None:
            continue
        buf += _emit_entry(r, item, pid_of)
    # 写剩余（新增行为：rid 不在 _order 里）
    written = {r for r in order}
    for name in ARRAY_NAMES:
        for r, item in zip(array_rids[name], d.get(name, []) or []):
            if r in written:
                continue
            buf += _emit_entry(r, item, pid_of)
    return bytes(buf)


def hub_to_json(raw, name_of, indent=1):
    return json.dumps(hub_to_dict(raw, name_of), ensure_ascii=False, indent=indent)


def json_to_hub(json_str, root_gpid, pid_of):
    return dict_to_hub(json.loads(json_str), root_gpid, pid_of)
