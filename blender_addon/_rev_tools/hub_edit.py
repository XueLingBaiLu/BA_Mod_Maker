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

行为（已破译的两种）：
  AxisRandom:
    {"type":"AxisRandom","lod":0,"name":"","speed":20.0,"minTime":2.0,"maxTime":10.0,
     "axes":{"x":{"source":"","min":0,"max":0},
             "y":{"source":"turret_0","min":-45,"max":45},
             "z":{"source":"","min":0,"max":0}}}
  MathConnect:
    {"type":"MathConnect","lod":0,"freq":0,"damper":0,"reaction":0,"objBool":true,
     "root":"","target":"","freeze":{"x":false,"y":false,"z":false},
     "shots":[{"source":"shell_spawn_0","value":1.0}]}

未破译的行为（如 AxisRepeater）保留原始字节 rawB64，序列化时不丢失。
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
        axes[ax] = {"source": name_of(pid), "min": amin, "max": amax}
    return {"type": "AxisRandom", "lod": lod, "name": name,
            "speed": speed, "minTime": mint, "maxTime": maxt, "axes": axes}


# MathConnect 固定头长度：lod(4) + ObjectFollower(16: freq/damper/reaction + defaultSerialized bool)
# + root PPtr(12) + target PPtr(12) + freeze 3 bool(3+1 pad) = 48 字节。
# 之后是 WeaponShotForces（UnitySerializedDictionary<Transform, float>），
# 布局已破解（v1.8.0，实测样本约束求解）：
#   i32 a=1, i32 b=0, i32 keyCount N, N × {i32 fileID=0, i64 transformPathID},
#   i32 valueCount(=N), N × float
MATHCONNECT_HEADER = 48


def _parse_shotforces(data, name_of):
    """解析 WeaponShotForces 字典字节（已破解布局）。失败返回 None。"""
    import struct
    if len(data) < 12:
        return None
    a, b, n = struct.unpack_from("<iii", data, 0)
    off = 12
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
    if off + vc * 4 > len(data):
        return None
    vals = list(struct.unpack_from("<%df" % vc, data, off)) if vc else []
    for i, s in enumerate(shots):
        s["value"] = vals[i] if i < len(vals) else 1.0
    return {"a": a, "b": b, "shots": shots}


def _emit_shotforces(d, pid_of):
    """重建 WeaponShotForces 字典字节（已破解布局）。"""
    import struct
    sf = d or {}
    shots = sf.get("shots") or []
    buf = struct.pack("<ii", int(sf.get("a", 1)), int(sf.get("b", 0)))
    buf += struct.pack("<i", len(shots))
    for s in shots:
        pid = pid_of(s.get("source", "")) if isinstance(s, dict) and s.get("source") else 0
        if isinstance(s, dict) and s.get("pathID"):
            pid = s["pathID"]
        buf += struct.pack("<iq", int(s.get("fileID", 0) if isinstance(s, dict) else 0), int(pid))
    buf += struct.pack("<i", len(shots))
    for s in shots:
        buf += struct.pack("<f", float(s.get("value", 1.0)) if isinstance(s, dict) else 1.0)
    return bytes(buf)


def _parse_mathconnect(d, name_of):
    lod = struct.unpack_from("<i", d, 0)[0]
    freq, damp, reac = struct.unpack_from("<fff", d, 4)
    obj_bool = d[16]
    _, root = struct.unpack_from("<iq", d, 20)
    _, tgt = struct.unpack_from("<iq", d, 32)
    fx, fy, fz = struct.unpack_from("<BBB", d, 44)
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


def _find_next_rid(raw, off, remaining):
    """扫描下一个 rid（数据长度靠它定位）。逐字节步进，命中剩余 rid 即停（rid 可能非 4 对齐）。"""
    if not remaining:
        return len(raw)
    for i in range(off, len(raw) - 8):
        v = struct.unpack_from("<q", raw, i)[0]
        if v in remaining:
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
        else:
            parsed = {"type": cls, "rawB64": base64.b64encode(data).decode("ascii")}
        if isinstance(parsed, dict):
            parsed["_rid"] = rid
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
        buf += struct.pack("<iqff", 0, pid_of(a.get("source", "")),
                           float(a.get("min", 0)), float(a.get("max", 0)))
    return bytes(buf)


def _mathconnect_bytes(d, pid_of):
    if d.get("rawB64"):
        return base64.b64decode(d["rawB64"])
    buf = bytearray()
    buf += struct.pack("<i", int(d.get("lod", 0)))
    buf += struct.pack("<fffB", float(d.get("freq", 0)), float(d.get("damper", 0)),
                       float(d.get("reaction", 0)), 1 if d.get("objBool", True) else 0)
    buf += bytes(3)
    buf += struct.pack("<iq", 0, pid_of(d.get("root", "")))
    buf += struct.pack("<iq", 0, pid_of(d.get("target", "")))
    f = d.get("freeze", {})
    buf += struct.pack("<BBB", 1 if f.get("x") else 0, 1 if f.get("y") else 0, 1 if f.get("z") else 0)
    buf += b"\0"
    if d.get("shotForces"):
        # 按破解布局重建（键/值成对；字节级往返有 rawB64 兜底）
        buf += _emit_shotforces(d["shotForces"], pid_of)
    elif d.get("shotsRawB64"):
        buf += base64.b64decode(d["shotsRawB64"])
    else:
        buf += struct.pack("<i", 1)  # a
        buf += struct.pack("<i", 0)  # b
        buf += struct.pack("<i", 0)  # keys count 0
        buf += struct.pack("<i", 0)  # values count 0
    return bytes(buf)


def dict_to_hub(d, root_gpid, pid_of):
    buf = bytearray()
    buf += struct.pack("<iq", 0, root_gpid)
    buf += struct.pack("<B", 1)
    buf += bytes(3)
    buf += struct.pack("<iq", 0, HUB_SCRIPT)
    buf += struct.pack("<i", 0)
    rid = 1
    array_rids = {}
    for name in ARRAY_NAMES:
        items = d.get(name, []) or []
        rids = []
        for item in items:
            r = item.get("_rid", rid) if isinstance(item, dict) else rid
            rids.append(r)
            rid = (r + 1) if isinstance(r, int) else (rid + 1)
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
        buf += struct.pack("<q", r)
        typ = item.get("type", "AxisRandom")
        cls_b = typ.encode("utf-8")
        ns_b = NS_ANIM.encode("utf-8")
        asm_b = ASM_ANIM.encode("utf-8")
        buf += struct.pack("<i", len(cls_b)); buf += cls_b
        while len(buf) % 4: buf += b"\0"
        buf += struct.pack("<i", len(ns_b)); buf += ns_b
        while len(buf) % 4: buf += b"\0"
        buf += struct.pack("<i", len(asm_b)); buf += asm_b
        while len(buf) % 4: buf += b"\0"
        if typ == "AxisRandom":
            buf += _axisrandom_bytes(item, pid_of)
        elif typ == "MathConnect":
            buf += _mathconnect_bytes(item, pid_of)
        elif item.get("rawB64"):
            buf += base64.b64decode(item["rawB64"])
    # 写剩余（新增行为：rid 不在 _order 里）
    written = {r for r in order}
    for name in ARRAY_NAMES:
        for r, item in zip(array_rids[name], d.get(name, []) or []):
            if r in written:
                continue
            buf += struct.pack("<q", r)
            typ = item.get("type", "AxisRandom")
            cls_b = typ.encode("utf-8")
            ns_b = NS_ANIM.encode("utf-8")
            asm_b = ASM_ANIM.encode("utf-8")
            buf += struct.pack("<i", len(cls_b)); buf += cls_b
            while len(buf) % 4: buf += b"\0"
            buf += struct.pack("<i", len(ns_b)); buf += ns_b
            while len(buf) % 4: buf += b"\0"
            buf += struct.pack("<i", len(asm_b)); buf += asm_b
            while len(buf) % 4: buf += b"\0"
            if typ == "AxisRandom":
                buf += _axisrandom_bytes(item, pid_of)
            elif typ == "MathConnect":
                buf += _mathconnect_bytes(item, pid_of)
            elif item.get("rawB64"):
                buf += base64.b64decode(item["rawB64"])
    return bytes(buf)


def hub_to_json(raw, name_of, indent=1):
    return json.dumps(hub_to_dict(raw, name_of), ensure_ascii=False, indent=indent)


def json_to_hub(json_str, root_gpid, pid_of):
    return dict_to_hub(json.loads(json_str), root_gpid, pid_of)
