# -*- coding: utf-8 -*-
r"""AnimationBehaviour 元数据引擎：从 IL2CPP dump 自动推出**每个行为类的序列化布局**。

为什么需要它（v1.8.59 的架构改动）：
  以前每支持一个行为类就要人工反解一次字节布局、写一遍解析/序列化代码 ✗
  ⇒ 用户要什么功能就得改一次工具，不可持续 ✗。
  实际上这些类的字段布局**游戏自己就有**：`dump.cs` 里有完整类定义 + `[SerializeField]`
  标记 + 枚举底层类型 + 嵌套类。本模块把它解析成「序列化字段表」，再由 behavior_codec
  通用地读写 ⇒ **新增/编辑/移除任意行为都不需要改代码** ✓。

实测验证（推导布局 vs 真机字节，全部吻合）：
  Torque        32  = LODGroupEnum(1+3pad) + Transform(12) + Vector3(12) + float(4)
  FloatEffect   48  = LODGroupEnum(1+3) + Transform(12) + 8×float(32)
  HideMesh      12  = Transform(12)                      ← 只有一个字段，没有 lod ✓
  AxisRandom    92  = lod(4) + string("Pilot head") + 3×float(12) + 3×AxisRandContainer(20)
  MathConnect   48  = lod(4) + ObjectFollower(16) + Transform(12) + Transform(12) + 3×bool(4)
  ProceduralDirt 16 = lod(4) + Transform(12)

Unity 对齐规则（实测）：每个字段按其大小对齐，但**最多按 4 字节对齐**
  （即 long/double/i64 也只要求 4 字节对齐）—— 由 MathConnect 字典里 12 字节/键的
  键值对字节级往返一致反推得到。
"""
import os
import re

# ---------------------------------------------------------------------------
# 类型 → 线格式
# ---------------------------------------------------------------------------
PRIM_SIZE = {
    # ⛔⛔ v1.8.84：**这些资源里 `bool` 是 4 字节，不是 1 字节** ✗（原来写 1 ⇒ codec 读错）
    #    证据（三个类互相印证，且都能整段解析到字节末尾）：
    #      AudioPlayer      [string][bool=0][bool=1][float=40s] = 80 字节
    #                       —— 按 1 字节读时 `_destroyTimer` 变成 1.4e-45（非规格化垃圾）✗
    #      SpawnCorrector   [count][PPtr12][Vec3×2=24][bool=1][bool=1] = 48 字节 ✓
    #      MathConnect      lod(4)+ObjectFollower16+2×PPtr12+3×bool(12)+dict = 80 ✓
    #                       （`_freezeX/Y/ZPos` 各占 4 字节，字典键值对刚好收尾 ✓）
    #    注意：1 字节时"bool+3 填充"和"4 字节 bool"在**紧跟 float 的场合**字节数相同，
    #    所以只有"连续多个 bool"或"类末尾是 bool"的类才能验出来 —— 别用 float 后面那个 bool 去验 ✗
    "bool": 4, "byte": 1, "sbyte": 1, "char": 2,
    "short": 2, "ushort": 2, "int": 4, "uint": 4,
    "long": 8, "ulong": 8, "float": 4, "double": 8,
    "AnimationCurve": None,     # 变长（Unity 曲线）
    "string": None,             # 变长
}
# ⛔ v1.8.84：**枚举在这些资源里一律 4 字节**（`LODGroupEnum` 实测 `02 00 00 00`、
#    ACV 的 `_terrainTypeTriggers` 键 `TerrainType.Water` 实测 `05 00 00 00`）——
#    dump 里若写着 `: byte` 也不能按 1 字节读 ✗（那会把后面全部字段顶偏）
ENUM_SIZE = 4
# 多分量类型（不能当单个整数读）：短名 -> (元素类型, 元素个数)
VEC = {
    "Vector2": ("float", 2), "Vector3": ("float", 3), "Vector4": ("float", 4),
    "Quaternion": ("float", 4), "Color": ("float", 4), "Rect": ("float", 4),
    "Bounds": ("float", 6), "Vector2Int": ("int", 2), "Vector3Int": ("int", 3),
    "RectInt": ("int", 4), "Color32": ("byte", 4), "LayerMask": ("int", 1),
}
# Unity 里所有 UnityEngine.Object 派生类在资产里都是 PPtr：(i32 fileID + i64 pathID)
PPTR_SIZE = 12
UNITY_OBJ_ROOTS = ("UnityEngine.Object", "Object", "UnityEngine.Component",
                   "UnityEngine.MonoBehaviour", "UnityEngine.ScriptableObject",
                   "UnityEngine.Behaviour")
# Unity 自己的容器（不是用户 [Serializable] 类）
UNITY_CONTAINERS = ("UnitySerializedDictionary", "SerializedDictionary")

TYPE_DECL_RE = re.compile(
    r"^(?P<mods>(?:public|internal|private|protected|sealed|abstract|static|partial|\s)+)?"
    r"(?P<kind>class|struct|enum)\s+"
    r"(?P<name>[\w\.]+(?:<[^>]*>)?)"
    r"(?:\s*:\s*(?P<bases>[^{]+?))?"
    r"\s*//\s*TypeDefIndex:\s*(?P<idx>\d+)\s*$")
FIELD_RE = re.compile(
    r"^\t(?P<mods>(?:public|private|protected|internal|readonly|static|const|\s)+)"
    r"(?P<type>[\w\.]+(?:<[^;]*?>)?(?:\[\])?)\s+"
    r"(?P<name>\w+)\s*;\s*//\s*0x(?P<off>[0-9A-Fa-f]+)\s*$")
ENUM_CONST_RE = re.compile(
    r"^\tpublic const \w+ (?P<name>\w+)\s*=\s*(?P<val>-?\d+)\s*;\s*(?://.*)?$")
NS_RE = re.compile(r"^// Namespace:\s*(?P<ns>.*)$")


def _norm(t):
    """去掉命名空间前缀、泛型实参默认值等，得到用于查表的短名。"""
    t = (t or "").strip()
    t = t.replace("[]", "[]")
    if t.endswith("[]"):
        return t
    return t.split("<")[0].split(".")[-1] if "<" not in t else t


class TypeInfo(object):
    __slots__ = ("full", "ns", "name", "kind", "bases", "fields", "enum_under",
                 "enum_members", "idx")

    def __init__(self, full, ns, name, kind, bases, idx):
        self.full = full
        self.ns = ns
        self.name = name
        self.kind = kind
        self.bases = bases or []
        self.fields = []          # [{"name","type","ser","off"}]
        self.enum_under = "int"
        self.enum_members = []
        self.idx = idx

    def __repr__(self):
        return "<%s %s (%d fields)>" % (self.kind, self.full, len(self.fields))


def _strip_literals(text):
    """把字符串字面量与注释替换成空格 —— 大括号计数必须忽略它们。

    ⛔ 不这样做时，`[Tooltip("...{...}")]` 或 `/* ... */` 里的括号会让类体范围算错，
    后果是**后面的类整段丢失**（实测：`{}` 空类体会把 `UnityEngine.GameObject` 之后
    的所有类型全吞掉 ⇒ GameObject 找不到 ⇒ 所有含 GameObject 字段的行为类解析失败 ✗）。
    """
    out = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == '"':
            out.append(" ")
            i += 1
            while i < n:
                if text[i] == "\\":
                    out.append(" ")
                    i += 2
                    continue
                if text[i] == '"':
                    out.append(" ")
                    i += 1
                    break
                out.append("\n" if text[i] == "\n" else " ")
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                out.append(" ")
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            out.append("  ")
            i += 2
            while i < n and not (text[i] == "*" and i + 1 < n and text[i + 1] == "/"):
                out.append("\n" if text[i] == "\n" else " ")
                i += 1
            out.append("  ")
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def parse_dump(path, only_ns=None):
    """解析 dump.cs → {full_name: TypeInfo}。

    用**大括号深度**确定类型体范围（不靠行首缩进猜），因此 `{}` 空类体、嵌套类型都能
    正确处理；嵌套类型也登记（`Parent.Nested`），因为行为字段大量用到它们
    （`SpawnVFX.VfxData`、`MathConnect.WeaponShotForces`、`AxisRandContainer`…）。
    """
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    masked = _strip_literals(text)
    lines = text.splitlines()
    mlines = masked.splitlines()

    depth_of = [0] * (len(mlines) + 1)
    d = 0
    for i, ml in enumerate(mlines):
        depth_of[i] = d
        d += ml.count("{") - ml.count("}")
    depth_of[len(mlines)] = d

    reg = {}
    ns = ""
    stack = []              # [(TypeInfo, 起始深度, kind)]
    in_fields = False
    attrs = []
    for i, line in enumerate(lines):
        d_before = depth_of[i]
        d_after = depth_of[i + 1]
        m = NS_RE.match(line)
        if m:
            ns = m.group("ns").strip()
            continue
        s = line.strip()
        decl = TYPE_DECL_RE.match(s)
        if decl and (not stack or d_before == stack[-1][1] + 1):
            bases = _merge_generic_bases(decl.group("bases") or "")
            name = decl.group("name")
            if stack:
                full = stack[-1][0].name + "." + name
                tns = stack[-1][0].ns
            else:
                full = (ns + "." + name) if ns else name
                tns = ns
            ti = TypeInfo(full, tns, name, decl.group("kind"), bases,
                          int(decl.group("idx")))
            if not (only_ns and tns and tns != only_ns):
                reg[full] = ti
            stack.append((ti, d_before, decl.group("kind")))
            in_fields = False
            attrs = []
        elif stack and d_before == stack[-1][1] + 1:
            ti, _, kind = stack[-1]
            if s.startswith("// Fields"):
                in_fields = True
                attrs = []
            elif s.startswith("// "):
                in_fields = False
            elif in_fields:
                if s.startswith("["):
                    attrs.append(s)
                elif kind == "enum" and s.startswith("public const"):
                    em = ENUM_CONST_RE.match(line)
                    if em and ti.full in reg:
                        reg[ti.full].enum_members.append(
                            (em.group("name"), int(em.group("val"))))
                else:
                    fm = FIELD_RE.match(line)
                    if fm and ti.full in reg:
                        if kind == "enum" and fm.group("name") == "value__":
                            reg[ti.full].enum_under = fm.group("type")
                        else:
                            reg[ti.full].fields.append({
                                "name": fm.group("name"), "type": fm.group("type"),
                                "ser": _is_serialized(fm.group("mods"), attrs),
                                "off": int(fm.group("off"), 16)})
                        attrs = []
        # 弹掉本行结束后已经闭合的类型
        while stack and d_after <= stack[-1][1] and \
                (d_after < d_before or "}" in mlines[i]):
            stack.pop()
    return reg


def _merge_generic_bases(bases):
    """`UnitySerializedDictionary<Transform, float>` 里的逗号被裸切了 ⇒ 按尖括号合回去。"""
    parts = [b.strip() for b in bases.split(",") if b.strip()]
    out, acc = [], ""
    for b in parts:
        acc = (acc + ", " + b) if acc else b
        if acc.count("<") == acc.count(">"):
            out.append(acc)
            acc = ""
    if acc:
        out.append(acc)
    return out


def _is_serialized(mods, attrs):
    """Unity 序列化判据：public 且非 [NonSerialized]，或带 [SerializeField] / [SerializeReference]；
    一律排除 static / const / readonly。

    ⛔ v1.8.90 修：以前不认 `[SerializeReference]` ⇒ `AnimationHub` 的 5 个状态数组
    （全都是 `[SerializeReference] private IAnimationBehaviour[]`）被判成"非序列化"，
    于是 `ser_fields("AnimationHub")` 返回空、通用编解码直接报"找不到这个类" ✗。
    实证：`hub_edit.py` 一直能从同一份字节里解析出这 5 个数组 ⇒ 它们确实被序列化 ✓
    """
    low = mods
    if "static" in low or "const" in low or "readonly" in low:
        return False
    if any("NonSerialized" in a for a in attrs):
        return False
    if any(("SerializeField" in a) or ("SerializeReference" in a) for a in attrs):
        return True
    return "public" in low


class Registry(object):
    """类型注册表 + 线格式推导 + 序列化字段顺序。"""

    def __init__(self, types):
        self.types = types
        self._wire_cache = {}
        self._fields_cache = {}

    # -- 类型解析 ----------------------------------------------------------
    def _index(self):
        if getattr(self, "_by_short", None) is None:
            d = {}
            for full, ti in self.types.items():
                d.setdefault(ti.name.split(".")[-1], []).append(ti)
            self._by_short = d
        return self._by_short

    def get(self, name):
        if not name:
            return None
        if name in self.types:
            return self.types[name]
        short = name.split(".")[-1]
        cands = self._index().get(short) or []
        if not cands:
            return None
        if len(cands) == 1:
            return cands[0]
        # 同名多个时按优先级挑：带命名空间的精确后缀匹配 → UnityEngine → 动画命名空间 → 全局
        # ⛔ 之前只按固定顺序挑第一个，"自定义同名类"可能被 UnityEngine 的同名类顶掉 ✗
        for full, ti in self.types.items():
            if ti.full.endswith("." + name) or ti.full == name:
                return ti
        for pref in ("UnityEngine", "BrokenArrow.Client.Ecs.AnimationBehaviors", ""):
            for ti in cands:
                if ti.ns == pref:
                    return ti
        return cands[0]

    def is_unity_object(self, ti, _seen=None):
        if ti is None:
            return False
        seen = _seen or set()
        if ti.full in seen:
            return False
        seen.add(ti.full)
        if ti.full in ("UnityEngine.Object", "Object") or ti.name in ("Object",):
            return True
        for b in ti.bases:
            if b in UNITY_OBJ_ROOTS or b.split(".")[-1] in ("Object", "Component",
                                                            "Behaviour", "MonoBehaviour",
                                                            "ScriptableObject"):
                return True
            bt = self.get(b)
            # ⛔ `seen` 必须**按分支复制**：共享一份会让"先访问过的兄弟分支"污染后一个分支的
            #    判定 ⇒ 明明派生自 UnityEngine.Object 的类被判成 False，进而按 inline/unknown
            #    解析 ✗（实际未构造出反例，属防御性修正）
            if bt is not None and self.is_unity_object(bt, set(seen)):
                return True
        return False

    # -- 序列化字段（含继承，父类在前）------------------------------------
    def ser_fields(self, name):
        if name in self._fields_cache:
            return self._fields_cache[name]
        ti = self.get(name)
        out = []
        if ti is not None:
            chain = []
            cur = ti
            seen = set()
            while cur is not None and cur.full not in seen:
                seen.add(cur.full)
                chain.append(cur)
                nxt = None
                for b in cur.bases:
                    bt = self.get(b)
                    if bt is not None and bt.kind in ("class", "struct"):
                        nxt = bt
                        break
                cur = nxt
            for t in reversed(chain):
                for fl in t.fields:
                    if fl["ser"]:
                        out.append(dict(fl))
        self._fields_cache[name] = out
        return out

    # -- 线格式 ------------------------------------------------------------
    def wire(self, cs_type, _depth=0):
        """把 C# 字段类型翻译成线格式描述。失败返回 {"kind":"unknown"}。"""
        if _depth > 12:
            return {"kind": "unknown"}
        key = cs_type
        if key in self._wire_cache:
            return self._wire_cache[key]
        w = self._wire_impl(cs_type, _depth)
        self._wire_cache[key] = w
        return w

    def _wire_impl(self, t, depth):
        t = (t or "").strip()
        if not t:
            return {"kind": "unknown"}
        # 数组 / List
        if t.endswith("[]"):
            return {"kind": "list", "elem": self.wire(t[:-2], depth + 1), "cs": t}
        base = t.split("<")[0].split(".")[-1]
        if base in ("List", "IList", "HashSet", "IEnumerable", "Queue", "Stack"):
            inner = t[t.find("<") + 1:t.rfind(">")]
            parts = _split_generic(inner)
            if len(parts) >= 1:
                return {"kind": "list", "elem": self.wire(parts[0], depth + 1), "cs": t}
            return {"kind": "unknown"}
        if base in UNITY_CONTAINERS:
            return self._dict_wire(t, depth)
        if t == "string":
            return {"kind": "string"}
        short = t.split(".")[-1]
        ti = self.get(t)
        # UnitySerializedDictionary 的**子类**（如 MathConnect.WeaponShotForces、
        # AnimatorConnect.WeaponTriggersDictionary）需要顺着基类链把泛型参数找出来
        if ti is not None and ti.kind == "class":
            for b in _all_bases(self, ti):
                if b.split("<")[0].split(".")[-1] in UNITY_CONTAINERS:
                    return self._dict_wire(b, depth)
        # 枚举
        if ti is not None and ti.kind == "enum":
            # ⛔ 一律 4 字节（见 ENUM_SIZE 的说明）：dump 里 `: byte` 的枚举在资源里也是 4 字节 ✗
            return {"kind": "enum", "size": ENUM_SIZE, "cs": t,
                    "members": list(ti.enum_members), "under": ti.enum_under}
        # 向量/颜色等（多分量，不能当单个整数读）
        if short in VEC:
            elem, cnt = VEC[short]
            return {"kind": "vec", "cs": t, "elem": elem, "count": cnt,
                    "size": (1 if elem == "byte" else (8 if elem == "double" else 4)) * cnt}
        # 原生类型
        if short in PRIM_SIZE:
            sz = PRIM_SIZE[short]
            if sz is None:
                return {"kind": "curve" if short == "AnimationCurve" else "unknown", "cs": t}
            return {"kind": "prim", "size": sz, "cs": t}
        if short == "AnimationCurve":
            return {"kind": "curve", "cs": t}
        # UnityEngine.Object 派生 ⇒ PPtr
        if self.is_unity_object(ti):
            return {"kind": "pptr", "cs": t, "size": PPTR_SIZE}
        # [Serializable] 内联类/结构
        if ti is not None and ti.kind in ("class", "struct"):
            subs = self.ser_fields(ti.full)
            if subs:
                return {"kind": "inline", "cs": t, "full": ti.full, "fields": subs}
            return {"kind": "unknown", "cs": t}
        # ★ v1.8.84：**泛型类的实例化**（如 `SetVFXProperty.VFXPropertyData<float>`）。
        #   注册表里存的是定义 `SetVFXProperty.VFXPropertyData<T>`、字段类型写着 `T`，
        #   而字段类型串写的是 `<float>` ⇒ 不把实参代进去就解不了（实测唯一的
        #   `SetVFXProperty` 实例因此 0 字段 + 56 字节 tail ✗）。
        g = self._generic_instance(t, depth)
        if g is not None:
            return g
        return {"kind": "unknown", "cs": t}

    def _generic_instance(self, t, depth):
        """`Name<Arg1,Arg2>` → 用定义 `Name<T1,T2>` 的字段表 + 实参替换后生成 inline wire。"""
        if "<" not in t or t.endswith("[]"):
            return None
        if t.split("<")[0].split(".")[-1] in UNITY_CONTAINERS:
            return None
        head = t[:t.find("<")]
        args = _split_generic(t[t.find("<") + 1:t.rfind(">")])
        if not args:
            return None
        # 找定义（把实参位置换成 T1/T2…）
        ti = None
        for cand in ("<%s>" % ",".join("T%d" % (i + 1) for i in range(len(args))),
                     "<%s>" % ",".join("T" for _ in args),
                     "<T>"):
            ti = self.get(head + cand)
            if ti is not None:
                break
        if ti is None or ti.kind not in ("class", "struct"):
            return None
        params = _split_generic((ti.full[ti.full.find("<") + 1:ti.full.rfind(">")])
                               if "<" in ti.full else "")
        table = {p: a for p, a in zip(params, args)}
        fields = []
        for fl in self.ser_fields(ti.full):
            f2 = dict(fl)
            if f2["type"] in table:                     # 字段类型就是 T ⇒ 换成实参
                f2["type"] = table[f2["type"]]
            fields.append(f2)
        if not fields:
            return None
        return {"kind": "inline", "cs": t, "full": ti.full, "fields": fields,
                "__generic__": table}

    def _dict_wire(self, t, depth):
        inner = t[t.find("<") + 1:t.rfind(">")] if "<" in t else ""
        parts = _split_generic(inner)
        if len(parts) == 2:
            return {"kind": "unity_dict", "key": self.wire(parts[0], depth + 1),
                    "val": self.wire(parts[1], depth + 1), "cs": t}
        return {"kind": "unknown", "cs": t}


def _split_generic(s):
    """按顶层逗号拆分泛型实参（`A<B,C>, D` → ['A<B,C>', 'D']）。"""
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch in "<([":
            depth += 1
        elif ch in ">)]":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def _all_bases(reg, ti):
    """沿基类链收集所有基类名（含泛型实参原样）。"""
    out = []
    seen = set()
    stack = [ti]
    while stack:
        cur = stack.pop()
        for b in cur.bases:
            if b in seen:
                continue
            seen.add(b)
            out.append(b)
            bt = reg.get(b)
            if bt is not None:
                stack.append(bt)
    return out


# ---------------------------------------------------------------------------
# 便捷：默认注册表
# ---------------------------------------------------------------------------
DEFAULT_DUMP_CANDIDATES = (
    r"<工作目录>\工具制作资源\il2cpp\dump\dump.cs",
)
ANIM_NS = "BrokenArrow.Client.Ecs.AnimationBehaviors"
_CACHE = {}


def load(dump_path=None, ns=ANIM_NS):
    """加载（并缓存）注册表。dump_path 为空时按默认路径找。"""
    path = dump_path
    if not path:
        for c in DEFAULT_DUMP_CANDIDATES:
            if os.path.isfile(c):
                path = c
                break
    if not path or not os.path.isfile(path):
        raise IOError("找不到 dump.cs（用 dump_path 参数指定）")
    key = (path, os.path.getmtime(path) if os.path.exists(path) else 0)
    if key in _CACHE:
        return _CACHE[key]
    types = parse_dump(path)
    reg = Registry(types)
    _CACHE.clear()
    _CACHE[key] = reg
    return reg


def behaviour_classes(reg, ns=None):
    """所有 IAnimationBehaviour 派生类（按名排序）。

    ⛔ **不要按命名空间过滤**：实测 `ProceduralDirt` 的命名空间是空串（TypeDefIndex 13）
    ⇒ 按 ns 过滤会把它漏掉 ✗。判据只看「实现了 IAnimationBehaviour」。
    """
    out = []
    for full, ti in reg.types.items():
        if ti.kind != "class":
            continue
        if "." in ti.name:          # 嵌套类不算行为
            continue
        if any("IAnimationBehaviour" in b for b in ti.bases):
            out.append(ti.name)
    return sorted(set(out))


if __name__ == "__main__":
    import sys
    r = load()
    print("类型总数:", len(r.types))
    cls = behaviour_classes(r)
    print("IAnimationBehaviour 类:", len(cls))
    for c in cls:
        fs = r.ser_fields(c)
        print("%-22s %d 字段: %s" % (c, len(fs),
                                    ", ".join("%s:%s" % (f["name"], f["type"]) for f in fs)))
