# -*- coding: utf-8 -*-
r"""通用 MonoBehaviour 字段编辑器（引擎层，CLI 可独立验证）。

思路（为什么这样设计）：
  * `behavior_codec.parse/emit` 是**与类无关**的顺序读写器，判据是「写回与原字节完全一致」
    ⇒ 只要给出正确的**类名**，任何 MonoBehaviour 都能被解析成命名字段字典并原样写回 ✓
  * 类名从哪来：bundle 里的 `MonoScript.m_ClassName` / `m_Namespace`
    （units bundle 里带 23 个 MonoScript ⇒ 每个挂在 prefab 上的脚本都能解析出类名）
  * 字段起点：MonoBehaviour 原生头 = m_GameObject(12) + m_Enabled(4) + m_Script(12)
    + m_Name(string) ⇒ 起点 = 28 + 4 + 对齐后的名字长度（名字通常为空 ⇒ 32）
    ⛔ 不能一律写 32：`m_Name` 非空时字段会往后挪，硬写 32 会解析失败

用法：
    python component_edit.py survey  <prefab名或 pid>      # 该 prefab 上所有组件能否编辑（=⑧面板列表）
    python component_edit.py list    <prefab名或 pid>      # 只要组件明细表
    python component_edit.py dump    <prefab名> <类名>      # 打印某组件的字段 JSON
    python component_edit.py classes                       # 全 bundle 按类普查
"""
import io
import json
import os
import struct
import sys

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
# ⛔ 路径必须是 `HERE/..`，不能是 `HERE/../..`：
#    · 插件里 HERE = <addon>/_rev_tools ⇒ `HERE/..` = <addon> ⇒ <addon>/_unitypy ✓（cp313，Blender 用）
#    · 工具根 HERE = BA_Mod_Maker/_rev_tools ⇒ `HERE/..` = BA_Mod_Maker ⇒ 同目录 _unitypy ✓
#    写成 `../..` 在插件里会指到 **BA_Mod_Maker/_unitypy（cp314）**，那是给系统 Python 的，
#    在 Blender 里被抢先 import 会直接崩（且是"有时好有时坏"那种最难查的 ✗）。
# ⛔ 一律 `append` 不 `insert`：`unitypy_bridge.add_paths()` 已经放好的路径优先级更高，
#    绝不能被我这里顶掉 ✓。
for _p in (os.path.abspath(os.path.join(HERE, "..", "_unitypy")), HERE):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.append(_p)

PRISTINE = r"<工作目录>\备份\units_assets_all_3cc1eb58d8b7f6cdf82bbfbf3b5b8aaf.bundle"


# ---------------------------------------------------------------- 头部/工具
def mb_fields_start(raw):
    """返回 (字段起点, 脚本 pid, GameObject pid, 名字)。"""
    if len(raw) < 32:
        return None
    go = struct.unpack_from("<q", raw, 4)[0]
    sp = struct.unpack_from("<q", raw, 20)[0]
    n = struct.unpack_from("<i", raw, 28)[0]
    if 0 <= n < 4096:
        name = raw[32:32 + n].decode("utf-8", "replace")
        start = 28 + 4 + ((n + 3) & ~3)
    else:
        name, start = "", 32
    return start, sp, go, name


def script_names(objs):
    """MonoScript 资产 → {path_id: 类全名}。"""
    out = {}
    for o in objs:
        if o.type.name != "MonoScript":
            continue
        try:
            d = o.read()
            cn = getattr(d, "m_ClassName", "") or ""
            ns = getattr(d, "m_Namespace", "") or ""
            out[o.path_id] = ("%s.%s" % (ns, cn)) if ns else cn
        except Exception:
            pass
    return out


def load_bundle(path=PRISTINE):
    import UnityPy
    env = UnityPy.load(path)
    sf = list(env.objects)[0].assets_file
    objs = list(sf.objects.values())
    return env, sf, objs, {o.path_id: o for o in objs}


def registry(dump=None):
    import behavior_meta
    return behavior_meta.load(dump)


def find_prefab(objs, key):
    """按名字或 pid 找 prefab 根 GameObject。返回 (pid, name)。"""
    want_pid = None
    try:
        want_pid = int(key)
    except ValueError:
        pass
    for o in objs:
        if o.type.name != "GameObject":
            continue
        if want_pid is not None and o.path_id == want_pid:
            try:
                return o.path_id, o.read().m_Name
            except Exception:
                return o.path_id, "?"
        if want_pid is None:
            try:
                nm = o.read().m_Name
            except Exception:
                continue
            if nm == key:
                return o.path_id, nm
    return None, None


def go_components(objs, by_pid, go_pid):
    """返回该 GameObject 上的组件对象列表。"""
    o = by_pid.get(go_pid)
    if o is None:
        return []
    try:
        go = o.read()
    except Exception:
        return []
    out = []
    for c in (getattr(go, "m_Component", None) or []):
        comp = getattr(c, "component", None)
        if comp is None:
            continue
        cp = getattr(comp, "m_PathID", 0)
        co = by_pid.get(cp)
        if co is not None:
            out.append(co)
    return out


def walk_prefab(objs, by_pid, root_pid, limit=4000):
    """广度遍历 prefab 下所有 GameObject（用 Transform 层级）。"""
    tr_go, go_tr, child = {}, {}, {}
    for o in objs:
        if o.type.name != "Transform":
            continue
        try:
            t = o.read()
        except Exception:
            continue
        g = t.m_GameObject.m_PathID if t.m_GameObject else 0
        tr_go[o.path_id] = g
        go_tr[g] = o.path_id
    for o in objs:
        if o.type.name != "Transform":
            continue
        try:
            t = o.read()
        except Exception:
            continue
        f = t.m_Father.m_PathID if t.m_Father else 0
        child.setdefault(f, []).append(o.path_id)

    out, seen, stack = [], set(), [root_pid]
    while stack and len(out) < limit:
        g = stack.pop()
        if g in seen:
            continue
        seen.add(g)
        out.append(g)
        for t in child.get(go_tr.get(g, 0), []):
            cg = tr_go.get(t, 0)
            if cg:
                stack.append(cg)
    return out


# ---------------------------------------------------------------- 解析/写回
def parse_component(reg, cls, raw):
    """返回 (values, start) 或抛异常。

    ★ 无序列化字段的类（`DriverMarker` / `DestroyInBattleMarker` / `UnitPersistentObjectMarker`
    这些**纯标记组件**）不算失败：它们本来就 0 字段，整段字节进 `__tail__` 原样保留 ✓。
    以前把这类当"解析失败"，普查里凭空多出 516 个"失败"。
    """
    st = mb_fields_start(raw)
    if st is None:
        raise ValueError("MB 头太短")
    start = st[0]
    import behavior_codec
    try:
        values = behavior_codec.parse(reg, cls, raw[start:])
    except behavior_codec.CodecError as e:
        if "没有" in str(e) and "序列化字段定义" in str(e):
            body = raw[start:]
            v = {"__n__": 0}
            if body:
                v["__tail__"] = body.hex()
            return v, start
        raise
    return values, start


def emit_component(reg, cls, raw, values, pid_of=None):
    """用编辑后的 values 重建整段 MB 字节。"""
    st = mb_fields_start(raw)
    start = st[0]
    import behavior_codec
    body = behavior_codec.emit(reg, cls, values, pid_of)
    return raw[:start] + body


def verify_component(reg, cls, raw, values):
    """把 values 写回字节后**再解析一次**，比对两次的字段字典。

    返回 (bytes, ok, diff)：`ok=True` 说明"面板 → 字节 → 回读"闭环一致。
    ⛔ 只比字节长度不够：`behavior_codec` 里长度相同但内容错的字段（枚举/字符串对齐）
       用长度判会漏 ✗ —— 这里比对**解析出来的值**，用的是同一套代码，闭环自洽 ✓。
    """
    new = emit_component(reg, cls, raw, values)
    st = mb_fields_start(new)
    back, _ = parse_component(reg, cls, new)
    a = {k: v for k, v in values.items() if not k.startswith("__")}
    b = {k: v for k, v in back.items() if not k.startswith("__")}
    diff = [k for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]
    return new, (not diff), diff


def node_paths(objs, by_pid, limit=4000):
    """GameObject pid → `根/子/孙` 节点路径（用 Transform 父子链）。

    为什么需要：同一个 prefab 上常有**好几个同类组件**（`US_ACV` 上就有 2 个
    `ContainerSeatInitializer`）—— 只给类名根本无法区分，必须带上节点路径 ✓
    """
    tr_go, go_tr, father, name = {}, {}, {}, {}
    for o in objs:
        try:
            tn = o.type.name
        except Exception:  # noqa: BLE001
            continue
        if tn == "Transform":
            try:
                t = o.read()
            except Exception:  # noqa: BLE001
                continue
            tr_go[o.path_id] = t.m_GameObject.m_PathID if t.m_GameObject else 0
            go_tr[tr_go[o.path_id]] = o.path_id
            father[o.path_id] = t.m_Father.m_PathID if t.m_Father else 0
        elif tn == "GameObject":
            try:
                name[o.path_id] = o.read().m_Name
            except Exception:  # noqa: BLE001
                name[o.path_id] = "?"

    def path(gpid):
        parts, t = [], go_tr.get(gpid, 0)
        for _ in range(64):
            if not t:
                break
            parts.append(name.get(tr_go.get(t, 0), "?"))
            t = father.get(t, 0)
        return "/".join(reversed(parts)) or "?"

    return path


def scan_components(reg, objs, by_pid, root_gpid, sn=None):
    """整个 prefab 上**每一个** MonoBehaviour 的解析情况（⑧ 面板的列表数据源）。

    返回 [{node, cls, cls_full, pid, gpid, script_pid, size, nfields, ok, tail, err}]
    顺序 = `walk_prefab` 的遍历顺序（根在前），同一个节点上的组件按 GO 上挂的顺序 ✓
    """
    import behavior_codec
    if sn is None:
        sn = script_names(objs)
    path = node_paths(objs, by_pid)
    rows = []
    for g in walk_prefab(objs, by_pid, root_gpid):
        gp = path(g)
        for co in go_components(objs, by_pid, g):
            try:
                if co.type.name != "MonoBehaviour":
                    continue
                raw = co.get_raw_data()
            except Exception as e:  # noqa: BLE001
                continue
            h = mb_fields_start(raw)
            full = sn.get(h[1], "") if h else ""
            short = full.split(".")[-1] if full else ""
            row = {"node": gp, "gpid": g, "pid": co.path_id, "cls": short,
                   "cls_full": full, "script_pid": (h[1] if h else 0),
                   "size": len(raw), "nfields": 0, "ok": False, "tail": 0, "err": ""}
            if h is None:
                row["err"] = "MB 头太短(%d 字节)" % len(raw)
                rows.append(row)
                continue
            if not short:
                row["err"] = "脚本未在包内(script pid=%d)" % h[1]
                rows.append(row)
                continue
            try:
                v, _st = parse_component(reg, short, raw)
                row["nfields"] = len([k for k in v if not k.startswith("__")])
                row["tail"] = len(v.get("__tail__", "") or "") // 2
                body = behavior_codec.emit(reg, short, v)
                row["ok"] = (body == raw[h[0]:])
                if not row["ok"]:
                    row["err"] = "往返不一致（字段定义与真实字节不符）"
            except Exception as e:  # noqa: BLE001
                row["err"] = str(e)[:70]
            rows.append(row)
    return rows


def maps_from_raw(objects):
    """从**原始字节**建 Transform/GameObject 关系表（不走 UnityPy typetree）。

    objects: `copy_full.collect_prefab_objects` 的返回值 [{pid, type_name, raw(base64)}]

    为什么必须走字节：整个 units bundle 有 **65,340 个 Transform**（实测），
    用 UnityPy 逐个 `o.read()` 建层级表要读十几万个对象 —— 在 3.2GB 的包上
    慢到面板点不动 ✗。而 prefab 自己的对象集本来就已经是原始字节，手解两个字段就够。

    手解依据 + 判据：`技术资料/scripts/verify_transform_offsets.py`
    （实测 1520 个 Transform 的 (GameObject, Father) 与 1520 个 GameObject 的名字
     **全部与 UnityPy 一致，0 处不符** ⇒ 快路径可信 ✓）
    """
    import base64
    raw_of = {}
    for o in objects:
        try:
            raw_of[int(o["pid"])] = base64.b64decode(o["raw"])
        except Exception:  # noqa: BLE001
            continue
    tr_go, go_tr, father, name, comps = {}, {}, {}, {}, {}
    for o in objects:
        tn = o.get("type_name")
        raw = raw_of.get(int(o["pid"]), b"")
        if tn == "Transform" and len(raw) >= 12:
            g = struct.unpack_from("<q", raw, 4)[0]
            tr_go[int(o["pid"])] = g
            go_tr[g] = int(o["pid"])
            father[int(o["pid"])] = struct.unpack_from("<q", raw, len(raw) - 8)[0]
        elif tn == "GameObject" and len(raw) >= 8:
            try:
                n = struct.unpack_from("<i", raw, 0)[0]
                off = 4 + n * 12 + 4
                ln = struct.unpack_from("<i", raw, off)[0]
                name[int(o["pid"])] = raw[off + 4:off + 4 + ln].decode("utf-8", "replace")
                # ⛔ 组件的**挂载顺序**只在这里：GO 的 m_Component 数组。
                #    按"对象在文件里的先后"排是不对的 —— 实测 US_ACV 上
                #    UnitPrefabRoot / AnimationManager 两个组件会**整行颠倒**
                #    （cmp_scan.py 抓到的就是这个 ✗）。
                comps[int(o["pid"])] = [
                    struct.unpack_from("<q", raw, 4 + i * 12 + 4)[0] for i in range(n)
                ] if n >= 0 and 4 + n * 12 + 4 <= len(raw) else []
            except Exception:  # noqa: BLE001
                name[int(o["pid"])] = "?"
    return raw_of, tr_go, go_tr, father, name, comps


def scan_components_raw(reg, objects, sn, root_gpid, limit=4000):
    """⑧ 面板实际用的扫描：**纯字节**，只碰这一个 prefab 的对象集。

    返回结构与 `scan_components` 完全相同（两者的差异由 `cmp_scan.py` 逐字段核对）。
    """
    import base64
    import behavior_codec
    raw_of, tr_go, go_tr, father, name, comps = maps_from_raw(objects)

    def path(gpid):
        parts, t = [], go_tr.get(gpid, 0)
        for _ in range(64):
            if not t:
                break
            parts.append(name.get(tr_go.get(t, 0), "?"))
            t = father.get(t, 0)
        return "/".join(reversed(parts)) or "?"

    # 用 Transform 层级做 BFS（顺序与 walk_prefab 一致，便于两边逐行 diff）
    child = {}
    for tpid, f in father.items():
        child.setdefault(f, []).append(tpid)
    rows, seen, stack = [], set(), [root_gpid]
    order = []
    while stack and len(order) < limit:
        g = stack.pop()
        if g in seen:
            continue
        seen.add(g)
        order.append(g)
        for t in child.get(go_tr.get(g, 0), []):
            cg = tr_go.get(t, 0)
            if cg:
                stack.append(cg)
    owner, mbs = {}, []
    # MB → 属于哪个 GameObject（`m_GameObject` 是 MB 头的**第一个**字段，pathID 在偏移 4）
    for o in objects:
        if o.get("type_name") != "MonoBehaviour":
            continue
        pid = int(o["pid"])
        raw = raw_of.get(pid, b"")
        mbs.append((pid, raw))
        if len(raw) >= 28:
            owner[pid] = struct.unpack_from("<q", raw, 4)[0]
    by_go = {}
    for pid, raw in mbs:
        by_go.setdefault(owner.get(pid, 0), []).append((pid, raw))
    for g in order:
        gp = path(g)
        # 按 GO 的 m_Component 顺序排（与 UnityPy 的 go_components 完全一致）；
        # 数组里没列到、却指向本 GO 的 MB 追加在后面兜底（异常数据也不丢组件 ✓）
        lst, listed = [], set()
        for cpid in comps.get(g, []):
            for pid, raw in by_go.get(g, []):
                if pid == cpid and pid not in listed:
                    lst.append((pid, raw))
                    listed.add(pid)
                    break
        for pid, raw in by_go.get(g, []):
            if pid not in listed:
                lst.append((pid, raw))
        for pid, raw in lst:
            h = mb_fields_start(raw)
            full = sn.get(h[1], "") if h else ""
            short = full.split(".")[-1] if full else ""
            row = {"node": gp, "gpid": g, "pid": pid, "cls": short, "cls_full": full,
                   "script_pid": (h[1] if h else 0), "size": len(raw),
                   "nfields": 0, "ok": False, "tail": 0, "err": ""}
            if h is None:
                row["err"] = "MB 头太短(%d 字节)" % len(raw)
                rows.append(row)
                continue
            if not short:
                row["err"] = "脚本未在包内(script pid=%d)" % h[1]
                rows.append(row)
                continue
            try:
                v, _st = parse_component(reg, short, raw)
                row["nfields"] = len([k for k in v if not k.startswith("__")])
                row["tail"] = len(v.get("__tail__", "") or "") // 2
                row["ok"] = (behavior_codec.emit(reg, short, v) == raw[h[0]:])
                if not row["ok"]:
                    row["err"] = "往返不一致（字段定义与真实字节不符）"
            except Exception as e:  # noqa: BLE001
                row["err"] = str(e)[:70]
            rows.append(row)
    return rows


# ---------------------------------------------------------------- CLI
def cmd_list(reg, args):
    _env, _sf, objs, by_pid = load_bundle()
    sn = script_names(objs)
    pid, name = find_prefab(objs, args[0])
    if pid is None:
        print("没找到 prefab:", args[0])
        return 1
    print("prefab %s (pid=%d)" % (name, pid))
    for g in walk_prefab(objs, by_pid, pid):
        comps = go_components(objs, by_pid, g)
        if not comps:
            continue
        gn = by_pid[g].read().m_Name
        print("  [%s]" % gn)
        for co in comps:
            if co.type.name == "MonoBehaviour":
                h = mb_fields_start(co.get_raw_data())
                cn = sn.get(h[1], "pid=%d" % h[1])
                print("      MonoBehaviour %-58s len=%d" % (cn, len(co.get_raw_data())))
            else:
                print("      %s" % co.type.name)
    return 0


def cmd_survey(reg, args):
    """⑧ 面板列表的 CLI 版（**与面板同用一个 scan_components**，保证两边结论一致）。"""
    _env, _sf, objs, by_pid = load_bundle()
    pid, name = find_prefab(objs, args[0])
    if pid is None:
        print("没找到 prefab:", args[0])
        return 1
    rows = scan_components(reg, objs, by_pid, pid)
    ok = len([r for r in rows if r["ok"] and r["nfields"]])
    blank = len([r for r in rows if r["ok"] and not r["nfields"]])

    def show(rs, title):
        print("\n== %s ==" % title)
        for r in rs:
            if r["ok"]:
                tag = "✓ %-3d 字段" % r["nfields"]
            elif r["nfields"]:
                tag = "✗ %-3d 字段 %s" % (r["nfields"], r["err"])
            elif r["err"]:
                tag = "· %s" % r["err"]
            else:
                tag = "· 0 字段（仅字节 %d）" % r["tail"]
            print("  [%-32s] %-34s %s" % (r["node"][-32:], r["cls"] or "?", tag))

    show([r for r in rows if r["ok"] and r["nfields"]], "可编辑（往返回读一致）")
    show([r for r in rows if not (r["ok"] and r["nfields"])], "只读 / 不支持")
    print("\nprefab %s (pid=%d)：组件 %d 个，可编辑 %d 个（另有 %d 个 0 字段，只能整段保留）"
          % (name, pid, len(rows), ok, blank))
    return 0


def cmd_dump(reg, args):
    _env, _sf, objs, by_pid = load_bundle()
    sn = script_names(objs)
    pid, name = find_prefab(objs, args[0])
    want = args[1]
    for g in walk_prefab(objs, by_pid, pid):
        for co in go_components(objs, by_pid, g):
            if co.type.name != "MonoBehaviour":
                continue
            raw = co.get_raw_data()
            h = mb_fields_start(raw)
            cn = sn.get(h[1], "")
            if cn.split(".")[-1] != want:
                continue
            v, st = parse_component(reg, want, raw)
            print(json.dumps(v, ensure_ascii=False, indent=1, default=str))
            return 0
    print("该 prefab 上没有组件:", want)
    return 1


def cmd_classes(reg, args):
    """全 bundle 普查：每个组件类出现多少实例、多少能被解析成字段、多少只能原样保留。

    ⛔ 判据修正（v1.8.90）：以前要求"有字段**且**没有尾部字节"才算可编辑，
       于是 `WeaponPrefabInfo`（45 个）和 `AnimationManagerBridge`（98+ 个）被算成
       "仅字节" —— 但实测它们**字段全部解得出、写回字节完全一致**，
       只是末尾多 4 个 `00 00 00 00`（dump.cs 里没有对应字段，恒为 0，原样带回即可）。
       把它们算成"不可编辑"会**低估**能力、也让用户以为改不了 ✗
       现在分三类：完全可编辑 / 可编辑(末尾有保留字节) / 只能整段保留。
    """
    import collections
    import behavior_codec
    env, _sf, objs, by_pid = load_bundle()
    sn = script_names(objs)
    # 实例, 完全可编辑, 可编辑(带尾部), 只能整段, 失败
    stat = collections.defaultdict(lambda: [0, 0, 0, 0, 0])
    for o in objs:
        if o.type.name != "MonoBehaviour":
            continue
        raw = o.get_raw_data()
        h = mb_fields_start(raw)
        short = sn.get(h[1], "").split(".")[-1]
        if not short:
            stat["<?脚本未在包内>"][4] += 1
            continue
        st = stat[short]
        st[0] += 1
        try:
            v, _s = parse_component(reg, short, raw)
            n = len([k for k in v if not k.startswith("__")])
            has_tail = bool(v.get("__tail__"))
            if behavior_codec.emit(reg, short, v) != raw[h[0]:]:
                st[4] += 1
            elif n > 0 and not has_tail:
                st[1] += 1
            elif n > 0:
                st[2] += 1
            else:
                st[3] += 1
        except Exception:  # noqa: BLE001
            st[4] += 1
    rows = sorted(stat.items(), key=lambda kv: -kv[1][0])
    print("%-46s %6s %7s %8s %7s %6s"
          % ("组件类", "实例", "可编辑", "带尾保留", "整段保留", "失败"))
    tot = [0, 0, 0, 0, 0]
    for cn, s in rows:
        print("%-46s %6d %7d %8d %7d %6d" % (cn[:46], s[0], s[1], s[2], s[3], s[4]))
        for i in range(5):
            tot[i] += s[i]
    print("-" * 84)
    print("%-46s %6d %7d %8d %7d %6d"
          % ("合计", tot[0], tot[1], tot[2], tot[3], tot[4]))
    print("\n类数=%d（有字段可编辑的类=%d）"
          % (len(rows), len([1 for _c, s in rows if s[1] or s[2]])))
    return 0


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    cmd, args = sys.argv[1], sys.argv[2:]
    reg = registry()
    if cmd == "classes":
        return cmd_classes(reg, args)
    return {"list": cmd_list, "survey": cmd_survey, "dump": cmd_dump}[cmd](reg, args)

if __name__ == "__main__":
    raise SystemExit(main())
