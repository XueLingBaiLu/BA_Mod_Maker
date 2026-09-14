# -*- coding: utf-8 -*-
r"""通用 AnimationBehaviour 编解码器：**任意行为类** ↔ 命名字段字典。

依赖 behavior_meta（从 dump.cs 推出的字段布局）。本模块不含任何「某个类专属」的
硬编码 —— 新增/编辑/移除任何行为都走同一条路径 ✓。

线格式（Unity 对 [Serializable] 托管引用的序列化，实测反推）：
  - 字段按**声明顺序**（父类在前）逐个写；每个字段按 min(自身大小, 4) 对齐
  - bool/byte = 1、short = 2、int/float/enum(int) = 4、long/double = 8
  - string = i32 长度 + utf8 + 补齐到 4
  - UnityEngine.Object 派生 = PPtr(i32 m_FileID + i64 m_PathID) = 12 字节
  - 数组/List<T> = i32 个数 + 元素
  - UnitySerializedDictionary<K,V> = i32 a + i32 b + i32 N + N×键 + N×值
    （实测：a=1、b=0；键值各自按自己的规则写；键/值个数都写 N）
  - 内联 [Serializable] 类 = 自己的序列化字段递归展开

失败的字段一律**保留原始字节**（rawB64 兜底），保证解析不丢数据。
"""
import struct

import behavior_meta as BM


class CodecError(Exception):
    pass


# 实测字段数：类名 -> 该类的资源里**实际序列化了几个字段**
# （由 parse() 在解析真机实例时自动记录；用于 make_defaults 选对布局）
_MEASURED_N = {}


def measured_field_count(cls):
    """该类在真机资源里实测的字段数（没测到返回 None）。"""
    return _MEASURED_N.get(cls)


def _note_measured(reg, cls, values):
    """记录"某个类在真机资源里实际用了几个字段"（供 make_defaults 选布局）。

    ⛔ **只能在"整段解析成功且写回与原字节一致"之后调用**：内联对象的解析会从大往小
    试前缀，**失败的尝试中途也会产出更短的 `__n__`** —— 早期版本在 read_value 里就地记录，
    于是把 `ObjectFollower`（定义 4 字段）记成了 **1**、`VfxHandler`（定义 9）记成了 **1** ✗
    （实测确认）。现在改成成功之后按 wire 树走一遍取值。

    取**最小值**：同一类在不同 prefab 上可能用不同前缀（`AnimatorConnect` 有 15/17 两种），
    保守取最小——写更少字段比多写更安全。
    """
    if not isinstance(values, dict):
        return
    root = {"kind": "inline", "cs": cls, "full": cls,
            "fields": list(reg.ser_fields(cls))}
    _rec_measured(reg, root, values)


def _rec_measured(reg, wire, value):
    if not isinstance(wire, dict):
        return
    k = wire.get("kind")
    if k == "inline":
        full = wire.get("full") or wire.get("cs")
        n = value.get("__n__") if isinstance(value, dict) else None
        if isinstance(n, int) and full:
            cur = _MEASURED_N.get(full)
            _MEASURED_N[full] = n if cur is None else min(cur, n)
        fields = wire.get("fields") or []
        limit = n if isinstance(n, int) else len(fields)
        for f in fields[:limit]:
            _rec_measured(reg, reg.wire(f["type"]), (value or {}).get(f["name"]))
        return
    if k == "list":
        for it in (value if isinstance(value, list) else []):
            _rec_measured(reg, wire.get("elem"), it)
        return
    if k == "unity_dict":
        if isinstance(value, dict):
            for kk in (value.get("keys") or []):
                _rec_measured(reg, wire.get("key"), kk)
            for vv in (value.get("values") or []):
                _rec_measured(reg, wire.get("val"), vv)
        return


# ---------------------------------------------------------------------------
# 读写原语
# ---------------------------------------------------------------------------
class _R(object):
    def __init__(self, data, ov=None, seen=None):
        self.d = data
        self.p = 0
        self.n = len(data)
        self.ov = ov or {}
        self.seen = seen if seen is not None else set()

    def align(self, size):
        a = min(max(size, 1), 4)
        self.p = (self.p + a - 1) // a * a

    def need(self, k):
        if self.p + k > self.n:
            raise CodecError("越界：还需 %d 字节，只剩 %d" % (k, self.n - self.p))

    def raw(self, k):
        self.need(k)
        b = self.d[self.p:self.p + k]
        self.p += k
        return b

    def i32(self):
        self.need(4)
        v = struct.unpack_from("<i", self.d, self.p)[0]
        self.p += 4
        return v

    def u(self, size):
        self.need(size)
        if size == 1:
            v = self.d[self.p]
        elif size == 2:
            v = struct.unpack_from("<H", self.d, self.p)[0]
        elif size == 4:
            v = struct.unpack_from("<I", self.d, self.p)[0]
        elif size == 8:
            v = struct.unpack_from("<Q", self.d, self.p)[0]
        else:
            raise CodecError("bad int size %d" % size)
        self.p += size
        return v

    def f32(self):
        self.need(4)
        v = struct.unpack_from("<f", self.d, self.p)[0]
        self.p += 4
        return v

    def f64(self):
        self.need(8)
        v = struct.unpack_from("<d", self.d, self.p)[0]
        self.p += 8
        return v


class _W(object):
    def __init__(self):
        self.b = bytearray()

    def align(self, size):
        a = min(max(size, 1), 4)
        while len(self.b) % a:
            self.b += b"\0"

    def raw(self, b):
        self.b += b

    def i32(self, v):
        self.b += struct.pack("<i", int(v))

    def u(self, v, size):
        if size == 1:
            self.b += struct.pack("<B", int(v) & 0xFF)
        elif size == 2:
            self.b += struct.pack("<H", int(v) & 0xFFFF)
        elif size == 4:
            self.b += struct.pack("<I", int(v) & 0xFFFFFFFF)
        elif size == 8:
            self.b += struct.pack("<Q", int(v) & 0xFFFFFFFFFFFFFFFF)
        else:
            raise CodecError("bad int size %d" % size)

    def f32(self, v):
        self.b += struct.pack("<f", float(v))

    def f64(self, v):
        self.b += struct.pack("<d", float(v))


# ---------------------------------------------------------------------------
# 单个值
# ---------------------------------------------------------------------------
def read_value(reg, wire, rd):
    k = wire.get("kind")
    if k == "prim":
        sz = wire["size"]
        cs = wire.get("cs", "")
        rd.align(sz)
        if cs in ("float",):
            return rd.f32()
        if cs in ("double",):
            return rd.f64()
        return rd.u(sz)
    if k == "enum":
        sz = wire.get("size", 4)
        rd.align(sz)
        return rd.u(sz)
    if k == "string":
        rd.align(4)
        n = rd.i32()
        if n < 0 or n > 0x100000:
            raise CodecError("字符串长度异常 %d" % n)
        b = rd.raw(n)
        rd.align(4)
        return b.decode("utf-8", "replace")
    if k == "pptr":
        rd.align(4)
        rd.need(12)
        fid = rd.i32()
        pid = struct.unpack_from("<q", rd.d, rd.p)[0]
        rd.p += 8
        return {"__pptr__": True, "fileID": fid, "pathID": pid}
    if k == "vec":
        elem = wire.get("elem", "float")
        cnt = wire.get("count", 0)
        step = 1 if elem == "byte" else (8 if elem == "double" else 4)
        rd.align(4)
        vals = []
        for _ in range(cnt):
            if elem == "float":
                vals.append(rd.f32())
            elif elem == "double":
                vals.append(rd.f64())
            else:
                vals.append(rd.u(step))
        return vals
    if k == "inline":
        # ⛔ **字段前缀自适应**：实测同一份 dump 里，有的内联类的「资源里实际序列化的
        #    字段数」少于类定义 —— 例：`ShowVFX.VfxData` 定义了 7 个字段，但资源里
        #    只有前 3 个（Points / Vfx / DefaultVfxType），28 字节数据正好对上 3 个。
        #    （Unity 只重存被改动过的资源 ⇒ 资源可能停留在旧版类布局上。）
        #    从"允许的字段数"往下试，取第一个不越界的前缀，并记下用了几个（__n__）。
        fields = wire["fields"]
        full = wire.get("full") or wire.get("cs")
        if rd.seen is not None:
            rd.seen.add(full)
        limit = rd.ov.get(full, len(fields))
        limit = max(1, min(limit, len(fields)))
        last = None
        for k2 in range(limit, 0, -1):
            snap = rd.p
            try:
                out = {f["name"]: read_value(reg, reg.wire(f["type"]), rd)
                       for f in fields[:k2]}
            except CodecError as e:
                rd.p = snap
                last = e
                continue
            if k2 < len(fields):
                out["__n__"] = k2
                out["__more__"] = [f["name"] for f in fields[k2:]]
            return out
        raise last or CodecError("内联类 %s 读不出来" % wire.get("cs"))
    if k == "list":
        rd.align(4)
        n = rd.i32()
        if n < 0 or n > 0x100000:
            raise CodecError("数组长度异常 %d" % n)
        return [read_value(reg, wire["elem"], rd) for _ in range(n)]
    if k == "unity_dict":
        # ⛔⛔ v1.8.84：Unity 的 `UnitySerializedDictionary<TKey,TValue>` 序列化是
        #     **[键数][键…][值数][值…]** —— **两个独立的长度**，不是"一个长度管两边" ✗。
        #     老实现读 [a][b][n] 然后键值各读 n 个 ⇒ 第一个 `_weaponTriggers` 就错位
        #     ⇒ 整条 AnimatorConnect 只解出 15/19 个字段，剩下 4 张字典全落在 `__tail__` 里，
        #     面板上既看不见也改不了（`_terrainTypeTriggers` 的 OnWater/WaterExit 就在其中）✗。
        #     实测 71 个真实实例：按"两个长度"解析**全部刚好用完整段字节**（剩 0 字节）✓
        rd.align(4)
        kn = rd.i32()
        if kn < 0 or kn > 0x100000:
            raise CodecError("字典键数异常 %d" % kn)
        keys = [read_value(reg, wire["key"], rd) for _ in range(kn)]
        rd.align(4)
        vn = rd.i32()
        if vn < 0 or vn > 0x100000:
            raise CodecError("字典值数异常 %d" % vn)
        vals = [read_value(reg, wire["val"], rd) for _ in range(vn)]
        return {"__dict__": True, "keys": keys, "values": vals}
    if k == "curve":
        # ⛔ v1.8.90：**`AnimationCurve` 以前是"不支持"** ⇒ 只要有组件带曲线字段，
        #    整条记录就落进 `__tail__`（实测 `AnimationManagerBridge._recoils` 的
        #    `RecoilData` 里有曲线 ⇒ 269 个实例里 135 个完全解不出、98 个带尾保留 ✗）。
        #    布局（**从字节实测推出来的**，见 `技术资料/scripts/decode_one.py`）：
        #        [int32 关键帧数]
        #        n × Keyframe = 4 个 float(time/value/inSlope/outSlope)
        #                       + 4 字节 weightedMode
        #                       + 2 个 float(inWeight/outWeight)     ⇒ 28 字节/帧
        #        [int32 m_PreInfinity][int32 m_PostInfinity][int32 m_RotationOrder]
        #    实证：某实例 `06 00 00 00` 之后 6×28 字节全是合理浮点（首帧 inSlope==outSlope ✓），
        #    再接 `02 00 00 00 02 00 00 00 04 00 00 00`（三个小整数）⇒ 28 字节成立 ✓。
        #    问：为什么不是 20 字节/帧（老版 Keyframe）？按 20 算，那三个位置读出来是浮点数
        #    （`4d e9 52 bd …`）而不是 2/2/4 这种 WrapMode 取值 ⇒ 只有 28 才对得上 ✓
        rd.align(4)
        n = rd.i32()
        if n < 0 or n > 0x100000:
            raise CodecError("关键帧数异常 %d" % n)
        keys = []
        for _ in range(n):
            keys.append([rd.f32(), rd.f32(), rd.f32(), rd.f32(),
                         rd.i32(), rd.f32(), rd.f32()])
        return {"__curve__": True, "keys": keys,
                "pre": rd.i32(), "post": rd.i32(), "rot": rd.i32()}
    raise CodecError("不支持的字段类型：%s" % wire.get("cs", k))


def write_value(reg, wire, w, v, pid_of=None):
    r"""写一个值。

    ⛔ `pid_of`（v1.8.67 新增）：**延迟解析节点引用的 pid**。
    为什么必须有：面板把行为编码成 `__bytes__` 时，节点引用的 pid 是按
    「读动画时的源 prefab」写死的；而**用户自己新建的挂载点**（② 面板加的
    `Rotorangle_0` 这类）的 pid 是**构建期才分配**的 ⇒ 那时只能写 0
    ⇒ 旋翼行为指向空引用、进游戏不转 ✗（实测确认：`__bytes__` 里烘的就是 0）。
    解法：值里额外带一个 `__name__`（挂载点名），构建期用 `pid_of(name)` 覆盖 pid。
    `pid_of=None` 时行为与以前完全一致（解析→写回的字节级往返不受影响）。
    """
    k = wire.get("kind")
    if k == "prim":
        sz = wire["size"]
        cs = wire.get("cs", "")
        w.align(sz)
        if cs == "float":
            w.f32(v)
        elif cs == "double":
            w.f64(v)
        else:
            w.u(v, sz)
        return
    if k == "enum":
        sz = wire.get("size", 4)
        w.align(sz)
        w.u(v, sz)
        return
    if k == "string":
        b = ("" if v is None else str(v)).encode("utf-8")
        w.align(4)
        w.i32(len(b))
        w.raw(b)
        w.align(4)
        return
    if k == "pptr":
        fid = int((v or {}).get("fileID", 0))
        pid = int((v or {}).get("pathID", 0))
        nm = (v or {}).get("__name__")
        if pid_of is not None and nm:
            # 名字解析成功才覆盖：解析不出（这个名字不在本次要构建的 prefab 里）
            # 时保留原 pid —— 保持原样比写成 0 更安全（写 0 = 一定是空引用）。
            try:
                r = pid_of(nm)
            except Exception:  # noqa: BLE001
                r = None
            if r:
                pid = int(r)
        w.align(4)
        w.i32(fid)
        w.raw(struct.pack("<q", pid))
        return
    if k == "inline":
        fields = wire["fields"]
        n = (v or {}).get("__n__")
        # ⛔ `__n__=0`（整段原样保留）必须与"没设置"区分开：旧写法用 `n <= 0` 判"未设置"，
        #    结果 0 会被当成"写全部字段" ⇒ 凭空多写 4 字节（SpawnVFX 324→328 实测踩坑 ✗）
        n = min(n, len(fields)) if isinstance(n, int) and n >= 0 else len(fields)
        for f in fields[:n]:
            write_value(reg, reg.wire(f["type"]), w, (v or {}).get(f["name"]), pid_of)
        return
    if k == "vec":
        elem = wire.get("elem", "float")
        step = 1 if elem == "byte" else (8 if elem == "double" else 4)
        vals = list(v or [])
        w.align(4)
        for i in range(wire.get("count", 0)):
            x = vals[i] if i < len(vals) else 0
            if elem == "float":
                w.f32(x)
            elif elem == "double":
                w.f64(x)
            else:
                w.u(x, step)
        return
    if k == "curve":
        # v1.8.90：**布局已从字节实测推出**（不是猜的，见 read_value 里 curve 分支的说明）
        #   ⇒ 现在真的能读写。以前这里故意报错"本作行为类里没出现过"——
        #   那是**只在行为类里**查过的结论；⑧ 组件普查发现组件里到处是曲线
        #   （`AnimationManagerBridge._recoils` 的 `RecoilData`），必须支持 ✗
        d = v if isinstance(v, dict) else {}
        keys = list(d.get("keys") or [])
        w.align(4)
        w.i32(len(keys))
        for kf in keys:
            kf = list(kf or []) + [0] * 7
            w.f32(kf[0]); w.f32(kf[1]); w.f32(kf[2]); w.f32(kf[3])
            w.i32(int(kf[4]))
            w.f32(kf[5]); w.f32(kf[6])
        w.i32(int(d.get("pre", 0) or 0))
        w.i32(int(d.get("post", 0) or 0))
        w.i32(int(d.get("rot", 0) or 0))
        return
    if k == "list":
        items = list(v or [])
        w.align(4)
        w.i32(len(items))
        for it in items:
            write_value(reg, wire["elem"], w, it, pid_of)
        return
    if k == "unity_dict":
        # 与读对称：**[键数][键…][值数][值…]**（v1.8.84 修正，见 read_value 处的说明）
        v = v or {}
        keys = list(v.get("keys") or [])
        vals = list(v.get("values") or [])
        w.align(4)
        w.i32(len(keys))
        for kk in keys:
            write_value(reg, wire["key"], w, kk, pid_of)
        w.align(4)
        w.i32(len(vals))
        for vv in vals:
            write_value(reg, wire["val"], w, vv, pid_of)
        return
    raise CodecError("不支持的字段类型：%s" % wire.get("cs", k))


# ---------------------------------------------------------------------------
# 整个行为对象
# ---------------------------------------------------------------------------
def default_value(wire):
    """新建行为时用的默认值（结构形状必须完整，否则写不出字节）。"""
    k = wire.get("kind")
    if k in ("prim", "enum"):
        return 0
    if k == "string":
        return ""
    if k == "pptr":
        return {"__pptr__": True, "fileID": 0, "pathID": 0}
    if k == "inline":
        return {f["name"]: default_value(wire.get("__reg__") and
                                        reg_wire(wire["__reg__"], f) or _wire_of(f))
                for f in wire["fields"]} if False else \
               {f["name"]: default_value(f.get("__wire__") or {"kind": "prim", "size": 4})
                for f in wire["fields"]}
    if k == "list":
        return []
    if k == "unity_dict":
        return {"__dict__": True, "keys": [], "values": []}
    return None


def _wire_of(f):
    return f.get("__wire__") or {"kind": "prim", "size": 4}


def reg_wire(reg, f):
    return reg.wire(f["type"])


def make_defaults(reg, cls, _depth=0):
    """为某个行为类生成一份完整默认值（结构完整，可写出合法字节）。

    ⛔ **优先按"实测字段数"生成**：实测有 5 个类的资源布局少于类定义
    （AudioPlayer 76 字节字段 +4、MathConnect +4、SpawnCorrector +6、
    AnimatorConnect 少 2~4 个字段、SpawnVFX 少 1 个甚至全不可解）。
    资源是**游戏此刻实际在读的**布局，所以新建实例按实测字段数写更安全；
    没有实测数据时才退回类定义的全部字段。并在返回值里带 `__n__` 让 emit 一致。
    """
    if cls in _MEASURED_N:
        k = _MEASURED_N[cls]
        full = len(reg.ser_fields(cls))
        if 0 < k < full:
            v = {}
            for f in reg.ser_fields(cls)[:k]:
                v[f["name"]] = _default_for(reg, reg.wire(f["type"]), _depth)
            v["__n__"] = k
            v["__more__"] = [f["name"] for f in reg.ser_fields(cls)[k:]]
            return v
    if _depth > 10:
        return {}
    out = {}
    for f in reg.ser_fields(cls):
        out[f["name"]] = _default_for(reg, reg.wire(f["type"]), _depth)
    return out


def _default_for(reg, wire, depth=0):
    k = wire.get("kind")
    if k in ("prim", "enum"):
        return 0
    if k == "string":
        return ""
    if k == "pptr":
        return {"__pptr__": True, "fileID": 0, "pathID": 0}
    if k == "vec":
        # ⛔ 必须处理：漏了它 ⇒ 任何带 Vector3/Quaternion/Color… 的类都生成不了默认值
        #    （实测 Torque / PositionRandom / RotateBySpeed / ShellCasingDrop 全部失败 ✗）
        return [0] * int(wire.get("count", 0))
    if k == "inline":
        full = wire.get("full") or wire.get("cs")
        fields = wire["fields"]
        # 优先按**实测字段数**生成（资源可能比类定义旧），并用 `__n__` 让 emit 一致
        n = _MEASURED_N.get(full)
        if not (isinstance(n, int) and 0 < n < len(fields)):
            n = len(fields)
        out = {f["name"]: _default_for(reg, reg.wire(f["type"]), depth + 1)
               for f in fields[:n]}
        if n < len(fields):
            out["__n__"] = n
            out["__more__"] = [f["name"] for f in fields[n:]]
        return out
    if k == "list":
        return []
    if k == "unity_dict":
        return {"__dict__": True, "keys": [], "values": []}
    if k == "curve":
        # 同上：不猜布局，明确报错（调用方退回原始字节）
        raise CodecError("AnimationCurve 字段尚未支持（本作行为类里未出现）")
    raise CodecError("无法为类型 %s 生成默认值" % wire.get("cs", k))


def _parse_once(reg, cls, data, ov=None):
    """一次解析尝试。返回 (values, 用掉的字节数) 或抛 CodecError。"""
    fields = reg.ser_fields(cls)
    if not fields:
        raise CodecError("没有 %s 的序列化字段定义（dump 里找不到这个类？）" % cls)
    rd = _R(data, ov=ov)
    out = {f["name"]: read_value(reg, reg.wire(f["type"]), rd)
           for f in fields}
    if rd.p > len(data):
        raise CodecError("解析越界（%d > %d）" % (rd.p, len(data)))
    return out, rd.p, set(rd.seen)


def _inline_classes(reg, cls, _seen=None):
    """静态遍历 wire 树，收集所有内联类（供"降字段数"搜索用）。

    ⛔ 不能只靠"失败前访问到的类"：解析可能在**更早的地方**就失败（例如内层数组的
    个数读成了天文数字），那时内层类根本没被访问过 ⇒ 搜索空间里少了关键候选 ✗。
    """
    out = set()
    stack = [cls]
    guard = 0
    while stack and guard < 500:
        guard += 1
        c = stack.pop()
        if c in out:
            continue
        out.add(c)
        for f in reg.ser_fields(c):
            for name in _wire_class_names(reg.wire(f["type"])):
                if name not in out:
                    stack.append(name)
    return out


def _wire_class_names(wire, _d=0):
    if _d > 8 or not isinstance(wire, dict):
        return []
    k = wire.get("kind")
    if k == "inline":
        return [wire.get("full") or wire.get("cs")]
    if k == "list":
        return _wire_class_names(wire.get("elem"), _d + 1)
    if k == "unity_dict":
        return (_wire_class_names(wire.get("key"), _d + 1)
                + _wire_class_names(wire.get("val"), _d + 1))
    return []


def _try(reg, cls, data, ov):
    """候选解析：必须「整体恰好用完数据」**且**「写回与原字节完全一致」才算成功。

    ⛔ 只判"恰好用完"是不够的：实测会接受**错误的**解释（消费长度碰巧对，
    但重写出来少 8 字节 ✗）。判据必须直接是**字节级往返**。
    """
    try:
        out, used, seen = _parse_once(reg, cls, data, ov)
    except CodecError:
        return None
    if used != len(data):
        return None
    try:
        if emit(reg, cls, out) != data:
            return None
    except Exception:  # noqa: BLE001
        return None
    return out


def parse(reg, cls, data, exact=True):
    """字节 → 命名字段字典。无法解析时抛 CodecError（调用方用 rawB64 兜底）。

    自适应顺序（**不靠任何类专属硬编码**，判据一律是「解析后能原样写回」）：
      1. 全字段直解 —— 15/17 个类走这条路；
      2. 按**类**降字段数（资产可能比类定义旧：`ShowVFX.VfxData` 实测只用 3/7 个字段），
         单个类 + 两个类的组合；
      3. 单个内联对象退回更短前缀（对象级 `__n__`）；
      4. 最坏：最长可用前缀 + 剩余字节进 `__tail__` 原样保留 ⇒ 仍能字节级往返 ✓。
    """
    fields = reg.ser_fields(cls)
    if not fields:
        raise CodecError("没有 %s 的序列化字段定义（dump 里找不到这个类？）" % cls)

    r = _try(reg, cls, data, {})
    if r is not None:
        _note_measured(reg, cls, r)
        return r

    # 收集涉及的类：静态遍历（保证嵌套类都在候选里）+ 失败前已访问到的
    seen = set()
    try:
        rd = _R(data)
        for f in fields:
            read_value(reg, reg.wire(f["type"]), rd)
        seen |= rd.seen
    except CodecError:
        pass
    seen |= _inline_classes(reg, cls)

    cand = sorted(c for c in seen if c != cls and reg.ser_fields(c))
    for c in cand:
        for k in range(len(reg.ser_fields(c)) - 1, 0, -1):
            r = _try(reg, cls, data, {c: k})
            if r is not None:
                return r
    for i, c1 in enumerate(cand):
        for c2 in cand[i + 1:]:
            n1, n2 = len(reg.ser_fields(c1)), len(reg.ser_fields(c2))
            for k1 in range(n1 - 1, 0, -1):
                for k2 in range(n2 - 1, 0, -1):
                    r = _try(reg, cls, data, {c1: k1, c2: k2})
                    if r is not None:
                        return r
    return _fallback(reg, cls, data)


def _fallback(reg, cls, data):
    """最坏情况：尽量多解析字段，剩下的全部当 __tail__ 保留（保证往返一致）。

    允许 k=0（整段进 __tail__）—— 这样**任何**字节串都必然能往返，
    只是此时没有可编辑字段（UI 会显示为"原始字节"）。
    """
    fields = reg.ser_fields(cls)
    for k in range(len(fields), -1, -1):
        rd = _R(data)
        try:
            out = {f["name"]: read_value(reg, reg.wire(f["type"]), rd)
                   for f in fields[:k]}
        except CodecError:
            continue
        if rd.p > len(data):
            continue
        if k < len(fields):
            out["__n__"] = k
            out["__more__"] = [f["name"] for f in fields[k:]]
        if rd.p < len(data):
            out["__tail__"] = data[rd.p:].hex()
        return out
    raise CodecError("解析失败（%s，%d 字节）" % (cls, len(data)))


def emit(reg, cls, values, pid_of=None):
    """命名字段字典 → 字节。

    `pid_of`：可选，把值里的 `__name__`（挂载点名）解析成 pid 覆盖 `pathID`。
    构建期必须传（否则新建挂载点的 pid 拿不到，见 `write_value` 的说明）；
    解析/往返验证时**不能**传（要的是逐字节原样写回）。
    """
    fields = reg.ser_fields(cls)
    n = (values or {}).get("__n__")
    # ⛔ 同 inline：0 是合法值（整段进 __tail__），不能与"未设置"混为一谈
    n = min(n, len(fields)) if isinstance(n, int) and n >= 0 else len(fields)
    w = _W()
    for f in fields[:n]:
        write_value(reg, reg.wire(f["type"]), w, values.get(f["name"]), pid_of)
    tail = (values or {}).get("__tail__")
    if tail:
        w.raw(bytes.fromhex(tail))
    return bytes(w.b)


def roundtrip_ok(reg, cls, data):
    """能不能做到字节级往返（解析 + 写回完全一致）。"""
    v = parse(reg, cls, data)
    return emit(reg, cls, v) == data


def type_summary(reg, cls):
    """给 UI 用：字段清单 + 每个字段的界面类型。"""
    out = []
    for f in reg.ser_fields(cls):
        w = reg.wire(f["type"])
        out.append({"name": f["name"], "cs": f["type"], "wire": w})
    return out


def ui_kind(wire):
    """把线格式归纳成界面控件种类。"""
    k = wire.get("kind")
    if k == "enum":
        return "enum"
    if k == "prim":
        cs = wire.get("cs", "")
        if cs == "bool":
            return "bool"
        if cs in ("float", "double"):
            return "float"
        return "int"
    if k == "string":
        return "string"
    if k == "pptr":
        return "node"          # Transform/GameObject/Mesh/... 统一用挂载点选择器
    if k == "inline":
        return "group"
    if k == "vec":
        # ⛔ v1.8.84：以前这里没有 vec 分支 ⇒ Vector3 / Vector2Int 字段一律显示成
        #    「仅原始字节」（SpawnVFX.MinMaxSpawnCount 实测就是这样）✗
        #    row_to_slots / slots_to_value 早就支持 vec 了，缺的只是这个映射 ✓
        return "vec"
    if k == "list":
        return "list"
    if k == "unity_dict":
        return "dict"
    return "unsupported"
