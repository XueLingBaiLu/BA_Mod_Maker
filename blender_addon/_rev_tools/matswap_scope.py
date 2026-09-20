# -*- coding: utf-8 -*-
r"""★⑰ 整车替换的**覆盖面**：按 prefab 子树取渲染器 + **保守归属判定**。

问题（doc 17 ★⑰ —— ⛔ 这不是 bug，是**能力边界**）
====================================================
「不勾『只对选中对象生效』= 整车」以前只取**当前 `.blend` 场景里存在的网格**
（实测用户那台飞行 ACV：**3 个** = `Chassis` / `BaseArmor` / `Ah_1z`），
而 prefab 子树里还有 **5 个构建期生成的渲染器**（LOD 各级克隆等）不在场景里
⇒ 它们保持原材质 ⇒ 用户**拉远 / 切到低 LOD 时又看到旧贴图**，看着像"没换干净"。

做法（两步）
============
① `subtree_renderers(bundle, prefab_path)`：读出 prefab **子树**里的全部渲染器
   （GO 名 / 类型 / **Mesh 资产名** / 当前材质名），不写任何东西；
② `attribute(renderers, owners)`：把每个子树渲染器归到**场景里的某个网格**（= 它"那件部件"），
   只认两条硬证据：
     · **Tier A**：渲染器 GO 名归一化后 == 拥有者网格名（`Chassis` / `BaseArmor` / `Ah_1z`；
       `000_skinned_Chassis` 去掉前导序号 + `skinned` 后也是 `chassis` ✓）
     · **Tier B**：渲染器的 **Mesh 资产名**去掉前导序号后 == 拥有者网格名
       （实测 `02_Chassis` / `03_Chassis` → `Chassis`，`02_BaseArmor` → `BaseArmor`）✓
   ⛔ **仅共享词元不算归属**：实测 `Armored` 的 mesh 叫 `Uparmored`，与 `BaseArmor` 只共享 `armor`
      四个字母 —— 猜这种会把 A 部件的图贴到 B 部件上，**比"没换"更难发现**（条目原文的风险条款）
      ⇒ 一律归入 `skipped` 并写明理由，调用方**逐条打印**「换了哪些 / 没换哪些 / 为什么」✓
   ⛔ 没有 `m_Mesh` 的渲染器（实测 `lod_1x1x1` 就是：MeshRenderer 且 `m_Mesh=None`）同样不动 ✓

判据（可复跑）
==============
`python 测试\test_star17_scope.py` —— 用**自建包副本** + 用户 `.blend` 里那 3 个网格名，
断言：子树总数 **8**、归属 **6**、未归属 **2**（理由分别是"只共享词元 / 无 m_Mesh"），
并且**总数一个不少**（⛔ 不许靠删/合并渲染器凑数）。
"""
import os
import re
import sys

_ORD_RE = re.compile(r"^(\d+[_-]?)+")


def norm_key(s):
    r"""名字归一化：去 `.001` 后缀 → 小写 → 去前导序号（`000_skinned_`）/`skinned` → 只留字母数字。

    例：`Ah_1z.001` → `ah1z`；`000_skinned_Chassis` → `chassis`；`02_Chassis` → `chassis` ✓
    """
    t = (s or "").strip()
    if len(t) > 4 and t[-4] == "." and t[-3:].isdigit():      # Blender 的 .001
        t = t[:-4]
    t = t.lower()
    t = _ORD_RE.sub("", t)                                    # 前导序号
    t = t.replace("skinned", "")                              # 构建期克隆的标记词
    return re.sub(r"[^0-9a-z]", "", t)


def mesh_key(s):
    r"""Mesh **资产名**的归一化（`02_Chassis` → `chassis`；`03_BaseArmor` → `basearmor`）。"""
    return norm_key(s)


def _load_env(bundle):
    """→ UnityPy 环境（复用 `skin_data._load_env` 的**解释器 tag** 选择逻辑，别自己再写一份）。"""
    try:
        import skin_data as sd
        return sd._load_env(bundle)
    except Exception:                                                     # noqa: BLE001
        pass
    HERE = os.path.dirname(os.path.abspath(__file__))
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    try:
        from unitypy_path import import_unitypy
        UnityPy = import_unitypy()
    except Exception:                                                     # noqa: BLE001
        import UnityPy                                                    # noqa: F401
    import UnityPy as _U
    env = _U.load(os.path.abspath(bundle))
    objs = list(env.objects)
    return env, objs, {o.path_id: o for o in objs}


def prefab_root_pid(by_pid, prefab_path=None):
    """→ prefab 的根 GameObject pid（按容器条目名匹配；`prefab_path` 为空时取**唯一**的 .prefab）。"""
    cands = []
    for o in by_pid.values():
        if o.type.name != "AssetBundle":
            continue
        try:
            ab = o.read()
        except Exception:                                                 # noqa: BLE001
            continue
        for k, v in (ab.m_Container or []):
            nm = str(k)
            if not nm.lower().endswith(".prefab"):
                continue
            pid = getattr(v, "m_PathID", None)
            if pid is None and getattr(v, "asset", None) is not None:
                pid = getattr(v.asset, "path_id", None)
            if pid:
                cands.append((nm, pid))
    if prefab_path:
        tail = prefab_path.replace("\\", "/").rstrip("/").split("/")[-1]
        for nm, pid in cands:
            if nm.replace("\\", "/").endswith(tail) or nm.endswith(prefab_path):
                return pid, nm
    if len(cands) == 1:
        return cands[0][1], cands[0][0]
    return 0, ""


def subtree_renderers(bundle, prefab_path=None, by_pid=None):
    r"""→ `(renderers, info)`；`renderers` = prefab **子树**里的全部渲染器：

        [{"pid", "go", "type", "mesh", "mats": [材质名]}]

    `info` = `{"prefab", "total_objects", "subtree_gos", "reason"}`（供调用方打印/自检）
    ⛔ 只读：不写、不保存、不改任何对象 ✓
    """
    env = None
    if by_pid is None:
        env, _objs, by_pid = _load_env(bundle)
    root, pname = prefab_root_pid(by_pid, prefab_path)
    info = {"prefab": pname, "total_objects": len(by_pid), "subtree_gos": 0, "reason": ""}
    if not root:
        info["reason"] = "包内没找到 prefab 容器条目（或有多份、无法唯一确定）"
        return [], info

    # GO ↔ Transform 映射
    tr2go, go2tr = {}, {}
    for pid, o in by_pid.items():
        if o.type.name != "Transform":
            continue
        try:
            d = o.read_typetree()
            g = (d.get("m_GameObject") or {}).get("m_PathID")
        except Exception:                                                 # noqa: BLE001
            g = None
        if g:
            tr2go[pid] = g
            go2tr[g] = pid
    if root not in go2tr:
        info["reason"] = "prefab 根不是 GameObject/Transform（结构异常）"
        return [], info

    sub, stack, seen = set(), [root], set()
    while stack:
        g = stack.pop()
        if g in seen:
            continue
        seen.add(g)
        sub.add(g)
        tr = go2tr.get(g)
        if tr is None:
            continue
        try:
            d = by_pid[tr].read_typetree()
        except Exception:                                                 # noqa: BLE001
            continue
        for ch in (d.get("m_Children") or []):
            cg = tr2go.get(ch.get("m_PathID"))
            if cg:
                stack.append(cg)
    info["subtree_gos"] = len(sub)

    go_name, mat_name = {}, {}
    for pid, o in by_pid.items():
        if o.type.name == "GameObject":
            try:
                go_name[pid] = o.read().m_Name or ""
            except Exception:                                             # noqa: BLE001
                pass
        elif o.type.name == "Material":
            try:
                mat_name[pid] = o.read().m_Name or ""
            except Exception:                                             # noqa: BLE001
                pass
    out = []
    for pid, o in by_pid.items():
        if o.type.name not in ("MeshRenderer", "SkinnedMeshRenderer"):
            continue
        try:
            d = o.read_typetree()
        except Exception:                                                 # noqa: BLE001
            continue
        g = (d.get("m_GameObject") or {}).get("m_PathID")
        if g not in sub:
            continue
        mp = (d.get("m_Mesh") or {}).get("m_PathID") or 0
        mesh_nm = ""
        if mp and mp in by_pid:
            try:
                mesh_nm = by_pid[mp].read().m_Name or ""
            except Exception:                                             # noqa: BLE001
                mesh_nm = ""
        out.append({
            "pid": pid,
            "go": go_name.get(g, "?"),
            "type": o.type.name,
            "mesh": mesh_nm,
            "mesh_pid": mp,
            "mats": [mat_name.get(m.get("m_PathID"), "?")
                     for m in (d.get("m_Materials") or []) if m.get("m_PathID")],
        })
    return out, info


def attribute(renderers, owners):
    r"""把子树渲染器归到场景网格（= "它那件部件"）。→ `(assigned, skipped)`

    `owners`: `[{"name": 场景网格名, ...}]`（整车模式下 = 场景里全部已导入网格）
    只认硬证据（Tier A 同名 / Tier B Mesh 资产名同 token）；
    ⛔ 只共享词元或没有 Mesh 的 ⇒ `skipped` + 理由（**不动它**，由调用方打印）✓
    """
    own_keys = {}
    for o in owners:
        k = norm_key(o.get("name") or "")
        if k:
            own_keys.setdefault(k, o.get("name"))
    assigned, skipped = [], []
    for r in renderers:
        gk, mk = norm_key(r.get("go")), mesh_key(r.get("mesh"))
        if gk in own_keys:
            assigned.append({"pid": r["pid"], "go": r.get("go"), "mesh": r.get("mesh"),
                             "owner": own_keys[gk], "why": "Tier A：渲染器名与网格名相同"})
            continue
        if mk and mk in own_keys:
            assigned.append({"pid": r["pid"], "go": r.get("go"), "mesh": r.get("mesh"),
                             "owner": own_keys[mk],
                             "why": "Tier B：Mesh 资产名（%s）与网格名相同" % r.get("mesh")})
            continue
        # ---- 判不出归属：写清理由，⛔ 绝不上猜 ----
        cand, reason = None, ""
        if not r.get("mesh"):
            reason = "没有 m_Mesh（占位/LOD 代理，本身不画东西）"
        else:
            for k, nm in own_keys.items():
                if len(k) >= 4 and (k in mk or k in gk):
                    cand, reason = nm, "只共享词元（%s ⊃ %s）⇒ 归属不清" % (r.get("mesh"), nm)
                    break
            if not reason:
                reason = "子树里没有对应网格（构建期生成的、场景里没有的那件）"
        skipped.append({"pid": r["pid"], "go": r.get("go"), "mesh": r.get("mesh"),
                        "mats": r.get("mats") or [], "reason": reason, "candidate": cand})
    return assigned, skipped


def expand_owners(bundle, prefab_path, owners, log=None, by_pid=None):
    r"""整车模式的展开：→ `{"assigned", "skipped", "renderers", "info", "error"}`

    `log` 给了就把「换了哪些 / 没换哪些 / 为什么」**逐条打印**（条目要求：自检里逐条打印）✓
    任何异常都**不抛**（退回"只改场景网格"的老行为），只把原因放进 `error` —— 建包不能被它搞挂 ✓
    """
    res = {"assigned": [], "skipped": [], "renderers": [], "info": {}, "error": ""}
    try:
        if not (bundle and os.path.isfile(bundle)):
            # ⛔ 别让"文件不存在"落进 UnityPy 的空环境、最后报成"没找到 prefab"（误导）✗
            res["error"] = "包不存在或不是文件：%s" % bundle
            if log:
                log("⚠ ★⑰ 子树展开跳过：%s" % res["error"])
            return res
        rs, info = subtree_renderers(bundle, prefab_path, by_pid=by_pid)
        res["renderers"], res["info"] = rs, info
        if not rs:
            res["error"] = info.get("reason") or "子树里没有渲染器"
            return res
        assigned, skipped = attribute(rs, owners)
        res["assigned"], res["skipped"] = assigned, skipped
        if log:
            log("★⑰ 整车覆盖面：prefab 子树共 **%d** 个渲染器（场景里只有 %d 个）"
                % (len(rs), len(owners)))
            for a in assigned:
                log("   ✓ 会换：%-24s ← %s（%s）" % (a["go"], a["owner"], a["why"]))
            for s in skipped:
                log("   · 不动：%-24s ← %s%s"
                    % (s["go"], s["reason"],
                       ("（疑似 %s，按保守规则不动）" % s["candidate"]) if s["candidate"] else ""))
        return res
    except Exception as e:                                                # noqa: BLE001
        res["error"] = "%s: %s" % (type(e).__name__, e)
        if log:
            log("⚠ ★⑰ 子树展开失败，退回「只改场景里的网格」：%s" % res["error"])
        return res
