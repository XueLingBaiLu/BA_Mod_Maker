# -*- coding: utf-8 -*-
r"""★⑥ **地址命名辅助**：注册地址时显示「DB 字段原值 + 字符数」，并校验"新值长度 ≤ 原值长度"。

为什么会有这条（T 清单 §一.9，实机踩到）
==========================================
游戏数据库的某些字段**存的就是 Addressables 地址**（`Units.ModelFileName` /
`PortraitFileName` / `ThumbnailFileName` …），而 `data.unity3d` 是**定长回填**的：
字段原来占几个字节，改完还得占几个字节 ⇒ **新地址的字符数必须 ≤ 原值**。

实机现场：给「飞行 ACV」起地址时先试 `FLYACV2`（**7** 字符）—— **写不进去**；
换成 `FLACV2`（**6** 字符，= 原值 `US_ACV` 的长度）才成功。白试了一轮 ✗

这个模块只做三件事（**纯函数**，不依赖 tkinter / UnityPy，方便直接测）：
  ① `find_db_usage(tables, needle)` —— 在已加载的 DB 里找出"哪个表哪一行的哪个字段 == 这个地址"
  ② `field_stats(tables)` —— 各地址字段的现有取值长度统计（**地址还没定下来时**给个参照）
  ③ `check_len(new, limit)` —— 长度校验，给出可直接照抄的建议（超了就建议一个截短的写法）

`tables` 的形状 = 产品数据库编辑器的形状：`{"Units": [{"Id": 707, "ModelFileName": "US_ACV", …}, …], …}`
"""
import collections
import os

# 数据库里"存 Addressables 地址"的字段（跨表通用；找不到就是空表）
ADDR_FIELDS = ("ModelFileName", "PortraitFileName", "ThumbnailFileName",
               "HUDIcon", "IconFileName", "BigIconFileName", "SmallIconFileName",
               "DecalFileName", "SoundFileName", "PrefabName", "AssetName")


def _iter_rows(tables):
    for tname, rows in (tables or {}).items():
        if not isinstance(rows, (list, tuple)):
            continue
        for idx, row in enumerate(rows):
            if isinstance(row, dict):
                yield tname, idx, row


def find_db_usage(tables, needle):
    r"""→ `[(表名, 行下标, Id, 字段名, 值), …]`：DB 里哪些行的哪些地址字段 == `needle`。

    ⛔ 只认 `ADDR_FIELDS` 里的字段名 —— 全表逐字段比对会把 `Name`/`HUDName` 之类的
      误报进来（那是内部名/显示名，不是地址，长度限制也不同）✗
    """
    if not needle:
        return []
    hits = []
    for tname, idx, row in _iter_rows(tables):
        for f in ADDR_FIELDS:
            v = row.get(f)
            if isinstance(v, str) and v == needle:
                hits.append((tname, idx, row.get("Id"), f, v))
    return hits


def field_stats(tables):
    r"""→ `{字段名: {"n": 条数, "min": 最短, "max": 最长, "例子": (值, 长度)}}`。

    用途：**地址还没定下来**的时候给参照。实机里最有用的一个数是 `min`
    —— 你要替换的那个单位，它的原值有多长，你的新地址就不能超过它 ✓
    """
    st = {}
    for _t, _i, row in _iter_rows(tables):
        for f in ADDR_FIELDS:
            v = row.get(f)
            if not isinstance(v, str) or not v:
                continue
            d = st.setdefault(f, {"n": 0, "min": None, "max": 0, "例子": None, "最长例子": None})
            d["n"] += 1
            L = len(v)
            if d["min"] is None or L < d["min"]:
                d["min"] = L
                d["例子"] = (v, L)
            if L > d["max"]:
                d["max"] = L
                d["最长例子"] = (v, L)
    return st


def check_len(new_addr, limit):
    r"""→ `(ok, msg)`。`limit=None` 时只报长度、不判对错（没有参照就不臆测 ✓）。"""
    if new_addr is None:
        return False, "地址是空的"
    L = len(new_addr)
    if limit is None:
        return True, "地址 %r 共 %d 字符（没有 DB 原值作参照 ⇒ 无法判断上限）" % (new_addr, L)
    if L <= limit:
        return True, ("✓ 地址 %r 共 %d 字符 ≤ 上限 %d ⇒ 定长回填**能放下**"
                      % (new_addr, L, limit))
    return False, ("✗ 地址 %r 共 **%d** 字符 > 上限 **%d** ⇒ 定长回填**写不进去**（实机踩过："
                   "`FLYACV2`(7) 不行，`FLACV2`(6) 才行）\n"
                   "   建议改成 ≤%d 字符的写法，例如：%s"
                   % (new_addr, L, limit, limit, suggest_shorter(new_addr, limit)))


def suggest_shorter(new_addr, limit):
    """给一个"能放下"的候选写法（去掉分隔符 / 取缩写 / 直接截断）——**只是建议，不替用户决定**。"""
    if limit is None or len(new_addr) <= limit:
        return new_addr
    cands = []
    for sep in ("_", "/", "\\", "-", "."):
        compact = new_addr.replace(sep, "")
        if 0 < len(compact) <= limit:
            cands.append(compact if compact != new_addr else None)
    # 取每段首字母（如 MyMod_Asset → MA）
    parts = [p for p in new_addr.replace("\\", "/").replace("_", "/").split("/") if p]
    if len(parts) > 1:
        abbr = "".join(p[0] for p in parts)
        if 0 < len(abbr) <= limit:
            cands.append(abbr)
    cands = [c for c in cands if c] or [new_addr[:limit]]
    return " 或 ".join(dict.fromkeys(cands))


def lookup(tables, address=None, tables_hint=6):
    r"""给 UI 用的一句话提示：**优先**用 `address` 找它自己的原值；找不到就退回字段统计。

    ⛔ 退回时**不给数字上限**（返回 None）—— 实测教训：第一版取"所有字段里最短的值"当上限，
      结果算出 **2**（`ModelFileName` 里真有个叫 `K1` 的单位）⇒ 等于告诉用户"地址只能 2 个字符"，
      纯属误导 ✗。上限只在**知道要替换哪一条**时才有意义（那就是它的原值长度）✓
    返回 `(hint_text, limit_or_None)`。
    """
    if address:
        hits = find_db_usage(tables, address)
        if hits:
            tname, _idx, rid, field, val = hits[0]
            extra = "" if len(hits) == 1 else "（共 %d 处引用）" % len(hits)
            return ("📏 DB 原值：%s #%s 的 %s = %r ⇒ **%d 字符**（新地址必须 ≤ %d）%s"
                    % (tname, rid, field, val, len(val), len(val), extra), len(val))
    st = field_stats(tables)
    if not st:
        return ("📏 没有可参照的 DB（先在『文件』菜单打开数据库；或直接按被替换值的长度起地址）", None)
    top = sorted(st.items(), key=lambda kv: -kv[1]["n"])[:tables_hint]
    txt = "；".join("%s %s~%s 字符(%d 条)" % (k, v["min"], v["max"], v["n"]) for k, v in top)
    return ("📏 地址字段现有取值：%s ⇒ **要替换哪个单位，就把它的原值填进地址框再点一次**"
            "（上限 = 那条原值的长度）" % txt, None)


def load_db_folder(folder, tables=None, log=None):
    r"""把导出的数据库目录（一堆 `<表名>.json`）读进 `tables` 字典。

    ⛔ 只读；目录不存在就返回原字典（**不报错** —— 没有 DB 只是少了参照，不该打断流程）✓
    """
    import json
    tables = tables if tables is not None else {}
    if not folder or not os.path.isdir(folder):
        return tables
    for fn in sorted(os.listdir(folder)):
        if not fn.lower().endswith(".json"):
            continue
        p = os.path.join(folder, fn)
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:                                        # noqa: BLE001
            if log:
                log("（跳过 %s：%s）" % (fn, e))
            continue
        name = fn[:-5]
        if isinstance(data, list):
            tables[name] = data
        elif isinstance(data, dict):
            for k, v in data.items():         # 有些导出是 {"表名": [...]}
                if isinstance(v, list):
                    tables.setdefault(k, v)
    return tables


def count_by_field(tables):
    """→ `{字段名: 条数}`（给"要不要显示这一行"用）。"""
    c = collections.Counter()
    for _t, _i, row in _iter_rows(tables):
        for f in ADDR_FIELDS:
            if isinstance(row.get(f), str) and row.get(f):
                c[f] += 1
    return dict(c)
