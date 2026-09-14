# -*- coding: utf-8 -*-
r"""通用字段视图：把「任意行为的值树」拍平成**可绑定的字段列表**，再收回去。

为什么要拍平：Blender 面板只能用 `layout.prop()` 绑定到**真实属性**上。行为的字段是
按类动态变化的（24 个类、嵌套组、数组、字典），没法给每个类写死 PropertyGroup ✗。
所以：
  - **值的权威来源**是行为的 JSON（`src_json`，同时也是写回基底）；
  - 本模块按元数据把值树**拍平成一维字段列表**（带缩进层级）供面板渲染；
  - 面板改一个值 → 通过 `json_path` 直接写回 JSON → 下次重绘重新拍平 ✓。
这样「新增/编辑/删除任意行为、任意字段」都不需要为某个类改代码 ✓。
"""
import json

import behavior_meta as BM
import behavior_codec as BC


# ---------------------------------------------------------------------------
# JSON 路径读写（支持列表下标）
# ---------------------------------------------------------------------------
def jget(root, path):
    cur = root
    for seg in path:
        if isinstance(seg, int):
            if not isinstance(cur, list) or seg >= len(cur):
                return None
            cur = cur[seg]
        else:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(seg)
    return cur


def jset(root, path, value):
    cur = root
    for i, seg in enumerate(path[:-1]):
        nxt = path[i + 1]
        if isinstance(seg, int):
            if not isinstance(cur, list):
                return False
            while len(cur) <= seg:
                cur.append({} if not isinstance(nxt, int) else [])
            cur = cur[seg]
        else:
            if not isinstance(cur, dict):
                return False
            if seg not in cur or cur[seg] is None:
                cur[seg] = [] if isinstance(nxt, int) else {}
            cur = cur[seg]
    last = path[-1]
    if isinstance(last, int):
        if not isinstance(cur, list):
            return False
        while len(cur) <= last:
            cur.append(None)
        cur[last] = value
    else:
        if not isinstance(cur, dict):
            return False
        cur[last] = value
    return True


# ---------------------------------------------------------------------------
# 拍平 / 收回
# ---------------------------------------------------------------------------
MAX_ROWS = 400      # 防止畸形数据把面板撑爆


# ⛔ v1.8.84：字段标签必须走 `behavior_dict.label(cls, path, name)` 的**中文表**
#    （面板上要显示「地形 → 进/出 Trigger」而不是 `terrainTypeTriggers` ✗）
try:
    import behavior_dict as _BD
except Exception:  # noqa: BLE001
    _BD = None


def _label(name, cls=None, path=None):
    if _BD is not None and cls:
        try:
            return _BD.label(cls, path or [], name)
        except Exception:  # noqa: BLE001
            pass
    return name.replace("_", " ").strip() or name


def flatten(reg, cls, values):
    """值树 → [{path(list), depth, label, kind, cs, value, ...}, ...]。"""
    fields = reg.ser_fields(cls)
    n = values.get("__n__")
    n = min(n, len(fields)) if isinstance(n, int) and 0 <= n else len(fields)
    rows = []
    for f in fields[:n]:
        _walk(reg, reg.wire(f["type"]), values.get(f["name"]), [f["name"]],
              f["name"], 0, rows, cls=cls)
        if len(rows) > MAX_ROWS:
            break
    return rows


def _walk(reg, wire, val, path, name, depth, rows,
          list_path=None, list_index=-1, cls=None, label=None):
    """把值树拍平成行。

    `label`（v1.8.91）：**行自带的语义标签**。以前标签一律由 `_label(name,…)` 从
    行为词典推——而词典里没有的词只会退化成"下划线转空格"。
    曲线那三个整数（`preInfinity`/`postInfinity`/`rotationOrder`）就是这样，
    显示成英文原名 ✗ ⇒ 允许调用方直接给中文标签 ✓
    """
    if len(rows) > MAX_ROWS:
        return
    k = wire.get("kind")
    if k in ("prim", "enum", "vec", "string", "pptr"):
        r = _row(wire, val, path, name, depth, cls=cls)
        if label:
            r["label"] = label
        r["list_path"] = list(list_path) if list_path else None
        r["list_index"] = list_index
        rows.append(r)
        return
    if k == "inline":
        rows.append({"kind": "group", "path": list(path), "name": name,
                     "depth": depth, "label": label or _label(name, cls, path),
                     "cs": wire.get("cs", ""), "value": None,
                     "list_path": list(list_path) if list_path else None,
                     "list_index": list_index})
        fields = wire["fields"]
        n = val.get("__n__") if isinstance(val, dict) else None
        n = min(n, len(fields)) if isinstance(n, int) and 0 <= n else len(fields)
        for f in fields[:n]:
            sub = val.get(f["name"]) if isinstance(val, dict) else None
            _walk(reg, reg.wire(f["type"]), sub, list(path) + [f["name"]],
                  f["name"], depth + 1, rows, cls=cls)
        return
    if k == "list":
        items = val if isinstance(val, list) else []
        rows.append({"kind": "list", "path": list(path), "name": name,
                     "depth": depth, "label": _label(name, cls, path), "count": len(items),
                     "cs": wire.get("cs", ""), "value": None})
        for i, it in enumerate(items):
            _walk(reg, wire["elem"], it, list(path) + [i], "[%d]" % i,
                  depth + 1, rows, list_path=list(path), list_index=i, cls=cls)
        return
    if k == "unity_dict":
        d = val if isinstance(val, dict) else {}
        keys = d.get("keys") or []
        vals = d.get("values") or []
        rows.append({"kind": "dict", "path": list(path), "name": name,
                     "depth": depth, "label": _label(name, cls, path), "count": len(keys),
                     "cs": wire.get("cs", ""), "value": None})
        for i in range(len(keys)):
            _walk(reg, wire["key"], keys[i], list(path) + ["keys", i],
                  "键%d" % i, depth + 1, rows, list_path=list(path), list_index=i,
                  cls=cls)
            _walk(reg, wire["val"], vals[i] if i < len(vals) else None,
                  list(path) + ["values", i], "值%d" % i, depth + 1, rows,
                  list_path=list(path), list_index=i, cls=cls)
        return
    if k == "curve":
        # v1.8.90：`AnimationCurve` 以前是"不支持"⇒ 整条记录落进 `__tail__`（看不见也改不了）。
        # 现在拆成「一个组 + 3 个可编辑整数 + 关键帧计数」：
        #   · pre/post/rot（WrapMode 与旋转顺序）是真正的**可调参数** ✓
        #   · 关键帧本身**原样保留**（面板不重写它 ⇒ 字节级往返不受影响 ✓）
        #     逐帧编辑留待后续（要动就得连曲线编辑器一起设计，见 KB）
        rows.append({"kind": "group", "path": list(path), "name": name,
                     "depth": depth, "label": _label(name, cls, path),
                     "cs": wire.get("cs", ""), "value": None,
                     "list_path": list(list_path) if list_path else None,
                     "list_index": list_index})
        d = val if isinstance(val, dict) else {}
        for key, nm, cn in (("pre", "preInfinity", "循环前"),
                            ("post", "postInfinity", "循环后"),
                            ("rot", "rotationOrder", "旋转顺序")):
            _walk(reg, {"kind": "prim", "size": 4, "cs": "int"}, d.get(key),
                  list(path) + [key], nm, depth + 1, rows, cls=cls, label=cn)
        nk = len(d.get("keys") or [])
        # ⛔ 用 `keep`（原样保留）而**不是** `unsupported`：`unsupported` 那一支在面板里
        #    写死成「<字段名>（不支持）」，会把这行显示成"不支持"——**误导** ✗
        #    （draw 冒烟测试抓到的：曲线明明支持、只是关键帧暂不逐帧编辑）
        rows.append({"kind": "keep", "path": list(path) + ["keys"],
                     "name": "keys", "depth": depth + 1,
                     "label": "关键帧", "count": nk,
                     "cs": "Keyframe[]", "value": None})
        return
    rows.append({"kind": "unsupported", "path": list(path), "name": name,
                 "depth": depth, "label": _label(name, cls, path),
                 "cs": wire.get("cs", k), "value": None})


def _row(wire, val, path, name, depth, cls=None):
    k = wire.get("kind")
    row = {"kind": BC.ui_kind(wire), "path": list(path), "name": name,
           "depth": depth, "label": _label(name, cls, path), "cs": wire.get("cs", ""),
           "value": val}
    if k == "enum":
        row["options"] = list(wire.get("members") or [])
        row["size"] = wire.get("size", 4)
    elif k == "pptr":
        row["pid"] = int((val or {}).get("pathID", 0)) if isinstance(val, dict) else 0
        row["fileID"] = int((val or {}).get("fileID", 0)) if isinstance(val, dict) else 0
    elif k == "vec":
        row["count"] = wire.get("count", 0)
        row["elem"] = wire.get("elem", "float")
    return row


# ---------------------------------------------------------------------------
# 值 ↔ Blender 属性槽
# ---------------------------------------------------------------------------
def row_to_slots(row):
    """把一行字段的值映射到属性槽（面板用）。返回 dict。"""
    k = row["kind"]
    v = row.get("value")
    if k == "bool":
        return {"b": bool(v)}
    if k == "int" or k == "enum":
        return {"i": int(v or 0)}
    if k == "float":
        return {"f": float(v or 0.0)}
    if k == "string":
        return {"s": "" if v is None else str(v)}
    if k == "node":
        return {"s": str(row.get("pid", 0))}
    if k == "vec":
        vals = list(v or []) + [0] * row.get("count", 0)
        return {"f": float(vals[0]), "f2": float(vals[1]),
                "f3": float(vals[2]), "i": int(vals[0]),
                "i2": int(vals[1]), "i3": int(vals[2]),
                "elem": row.get("elem", "float"), "count": row.get("count", 0)}
    return {}


def slots_to_value(row, slots):
    """属性槽 → 值（面板改完后写回 JSON 用）。"""
    k = row["kind"]
    if k == "bool":
        return bool(slots.get("b"))
    if k in ("int", "enum"):
        return int(slots.get("i") or 0)
    if k == "float":
        return float(slots.get("f") or 0.0)
    if k in ("string", "node"):
        s = slots.get("s")
        return "" if s is None else str(s)
    if k == "vec":
        cnt = row.get("count", 0)
        if row.get("elem") == "int":
            v = [int(slots.get("i") or 0), int(slots.get("i2") or 0),
                 int(slots.get("i3") or 0)]
        else:
            v = [float(slots.get("f") or 0.0), float(slots.get("f2") or 0.0),
                 float(slots.get("f3") or 0.0)]
        return v[:cnt]
    return None


def json_to_slots(reg, cls, values):
    """整棵树 → 拍平后的「行 + 槽初始值」列表（面板重建时用）。"""
    out = []
    for r in flatten(reg, cls, values):
        r = dict(r)
        r["slots"] = row_to_slots(r)
        out.append(r)
    return out


def env_defaults():
    """新建行为时的默认 JSON。"""
    return {}
