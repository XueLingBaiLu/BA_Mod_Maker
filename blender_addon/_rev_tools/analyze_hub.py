# -*- coding: utf-8 -*-
r"""AnimationHub SerializeReference 完整解析器（断箭逆向分析工具箱）。

背景：军械库演示动画由 AnimationHub 的 _demoState 驱动。Hub 是 MonoBehaviour，
5 个 IAnimationBehaviour[] 数组（SerializeReference）+ 尾部 ManagedReferencesRegistry。

字节布局（全部实测自 RU_BMP2M 炮塔，764 字节）：
    0x00  m_GameObject PPtr（fileID i32 + pathID i64）
    0x0C  m_Enabled u8 + 3 pad
    0x10  m_Script PPtr（pathID @0x14 = 4665939560152279323）
    0x1C  m_Name string（len i32 + chars，4 对齐）
    0x20  5 个数组：size(i32) + size×rid(i64)
    然后  references: version(i32，恒 2) + 条目数(i32)
          每条目 = rid(i64) + class 字符串 + ns 字符串 + asm 字符串（各 4 对齐）+ 数据
    rid 是数组与注册表之间的对应键（任意唯一 int64 即可，运行时按值匹配）。

行为类数据布局（[SerializeField] 字段按声明顺序，PPtr=12B）：
    AxisRandom    = lod(i32) + name(str) + speed/minT/maxT(3×f32)
                    + 3×AxisRandContainer(Source PPtr + Min/MaxAngle 2×f32)  → 空名时 80B
    MathConnect   = lod(i32) + ObjectFollower(16B: freq/damper/reaction/bool)
                    + root PPtr + target PPtr + 3×bool(4B)
                    + WeaponShotForces 字典 [keys: n(i32)+n×PPtr | values: n(i32)+n×f32]
                    → 52+16n 字节
    AxisRepeater  = name(str) + 6×PPtr（x/y/z 的 source+destination）

军械库演示行为（RU_BMP2M 原版实测）：
    demo = 1×AxisRandom(speed=20, 2~10s 随机, Y: turret_0 -45°~+45°)
    universal = 2×MathConnect(turret_0 跟随左右天线) + 1×AxisRandom(武器/瞄准镜晃动)

用法：
    python analyze_hub.py <bundle路径> <hub_pid>     # 解析指定 Hub
    python analyze_hub.py <bundle路径> --list        # 列出所有非空 Hub
"""
import sys, os, struct

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from analyze_common import load_bundle, mb_header, resolve

HUB_SCRIPT = 4665939560152279323
NS_ANIM = "BrokenArrow.Client.Ecs.AnimationBehaviors"


def read_str(buf, off):
    """Unity 字符串：len(i32) + chars + 4 对齐。"""
    n = struct.unpack_from("<i", buf, off)[0]
    off += 4
    s = buf[off:off + n].decode("utf-8", "replace")
    off += n
    return s, (off + 3) & ~3


def parse_hub(raw, by_pid):
    """解析 Hub 原始字节，返回结构化 dict。"""
    out = {"m_GameObject": struct.unpack_from("<q", raw, 4)[0],
           "m_Script": struct.unpack_from("<q", raw, 20)[0],
           "arrays": {}, "registry": []}
    off = 32
    for name in ("universal", "demo", "game", "preDeath", "death"):
        n = struct.unpack_from("<i", raw, off)[0]
        off += 4
        out["arrays"][name] = [struct.unpack_from("<q", raw, off + 8 * i)[0] for i in range(n)]
        off += 8 * n
    ver = struct.unpack_from("<i", raw, off)[0]
    off += 4
    cnt = struct.unpack_from("<i", raw, off)[0]
    off += 4
    out["registry_version"] = ver
    for _ in range(cnt):
        rid = struct.unpack_from("<q", raw, off)[0]
        off += 8
        cls, off = read_str(raw, off)
        ns, off = read_str(raw, off)
        asm_, off = read_str(raw, off)
        data_off = off
        dl = data_len(cls, raw, off)
        out["registry"].append({
            "rid": rid, "class": cls, "ns": ns, "asm": asm_,
            "data": raw[data_off:data_off + dl], "data_len": dl,
        })
        off += dl
    out["parsed_end"] = off
    out["total_len"] = len(raw)
    return out


def data_len(cls, raw, off):
    if cls == "AxisRandom":
        o = 4
        n = struct.unpack_from("<i", raw, off + o)[0]
        o += 4 + n
        return (o + 12 + 60 + 3) & ~3
    if cls == "MathConnect":
        o = 4 + 16 + 12 + 12 + 4
        n = struct.unpack_from("<i", raw, off + o)[0]
        o += 4 + n * 12
        n2 = struct.unpack_from("<i", raw, off + o)[0]
        o += 4 + n2 * 4
        return (o + 3) & ~3
    return 0


def describe_axisrandom(d, by_pid):
    lod = struct.unpack_from("<i", d, 0)[0]
    name, o = read_str(d, 4)
    speed, mint, maxt = struct.unpack_from("<fff", d, o)
    o += 12
    axes = []
    for ax in ("x", "y", "z"):
        _, pid = struct.unpack_from("<iq", d, o)
        o += 12
        amin, amax = struct.unpack_from("<ff", d, o)
        o += 8
        axes.append((ax, pid, amin, amax))
    return (f"lod={lod} name='{name}' speed={speed} minT={mint} maxT={maxt} "
            + str([(a, resolve(p, by_pid) if p else None, mn, mx) for a, p, mn, mx in axes]))


def describe_mathconnect(d, by_pid):
    lod = struct.unpack_from("<i", d, 0)[0]
    freq, damp, reac = struct.unpack_from("<fff", d, 4)
    _, root = struct.unpack_from("<iq", d, 20)
    _, tgt = struct.unpack_from("<iq", d, 32)
    fx, fy, fz = struct.unpack_from("<BBB", d, 44)
    n = struct.unpack_from("<i", d, 48)[0]
    pairs = []
    for i in range(n):
        _, kp = struct.unpack_from("<iq", d, 52 + 12 * i)
        v = struct.unpack_from("<f", d, 52 + 12 * n + 4 + 4 * i)[0]
        pairs.append((resolve(kp, by_pid) if kp else None, v))
    return (f"lod={lod} freq={freq} damper={damp} reaction={reac} "
            f"root={resolve(root, by_pid) if root else None} target={resolve(tgt, by_pid) if tgt else None} "
            f"freeze=({fx},{fy},{fz}) shots={pairs}")


def print_hub(raw, by_pid, title=None):
    h = parse_hub(raw, by_pid)
    if title:
        print(f"=== {title} ===")
    print(f"m_GameObject -> {resolve(h['m_GameObject'], by_pid)}")
    print(f"registry version={h['registry_version']} entries={len(h['registry'])} "
          f"parsed {h['parsed_end']}/{h['total_len']} bytes")
    for name, rids in h["arrays"].items():
        print(f"[{name}]")
        for r in rids:
            e = next((x for x in h["registry"] if x["rid"] == r), None)
            if not e:
                print(f"   rid={r} -> 注册表缺失!")
                continue
            print(f"   rid={r} -> {e['class']} ({e['data_len']}B)")
            if e["class"] == "AxisRandom":
                print(f"      {describe_axisrandom(e['data'], by_pid)}")
            elif e["class"] == "MathConnect":
                print(f"      {describe_mathconnect(e['data'], by_pid)}")
            else:
                print(f"      未解析类，数据: {e['data'][:32].hex()}")
    return h


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    path = argv[0]
    _, sf, objs, by_pid = load_bundle(path)
    if argv[1] == "--list":
        for o in objs:
            if o.type.name != "MonoBehaviour":
                continue
            h = mb_header(o.get_raw_data())
            if h and h[1] == HUB_SCRIPT and len(o.get_raw_data()) > 60:
                print(f"Hub pid={o.path_id} len={len(o.get_raw_data())} GO={h[0]} name={h[2]!r}")
        return 0
    pid = int(argv[1])
    o = by_pid[pid]
    print_hub(o.get_raw_data(), by_pid, f"Hub {pid}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
