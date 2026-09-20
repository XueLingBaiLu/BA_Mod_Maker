# -*- coding: utf-8 -*-
r"""完整复制模式（copy-full）：整段字节复制原 prefab，只改网格 + 挂点。

与 build_model 的"从零重建"不同，这里把原 prefab 的 preload 范围内所有对象
字节复制进 .bamod，只对网格（重建顶点数据）和挂点 Transform（改位置/旋转/缩放）
做 patch，其余对象（材质/AnimationHub/AnimationManagerBridge/Fmod/SkinStorage/LOD...）
原样保留。

用法：
    from copy_full import collect_prefab_objects, build_copy
    objs, preload, src_path = collect_prefab_objects(bundle, root_gpid)
    build_copy(bundle, out_pack, new_prefab, new_name, root_gpid, bone_tree, meshes)
"""
import os, sys, struct, base64, json, zipfile, re as _re

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
# ⛔ `_unitypy` 有两份（工具根=cp314 / 插件内=cp313），指错会崩在 `lz4._version`。
#    以前这里写死 `HERE/../..` 并 `insert(0)` ⇒ 在 Blender(3.13) 里必崩，而且因为
#    "谁先 import 谁说了算"而表现为**偶发**。统一走 `unitypy_path.ensure`（按解释器 tag 挑）✓
from unitypy_path import ensure as _ensure_unitypy  # noqa: E402
_ensure_unitypy(HERE)

import UnityPy
from UnityPy.files.ObjectReader import ObjectReader
from UnityPy.streams import EndianBinaryReader
from UnityPy.helpers import TypeTreeHelper
from UnityPy.classes.PPtr import PPtr
from UnityPy.classes.generated import ComponentPair
from UnityPy.classes.math import Vector3f, Quaternionf

from pack_model import _type_identity
from build_model import _world_matrices, _invert, _mul

# ── ★26 完整修法（2026-09-18 中枢批准）：新网格材质**按源 pid 带入**，⛔ 不再继承"本车材质" ──
def mat_name_of(pid, assets):
    """按 pid 取材质名（★v1.12.3 补件 · 修 子⑥ 观察①）。

    ⛔ 旧写法拿 **ObjectReader**（`smr_src`）当 assets 文件用、去点它的 `.objects` —— ObjectReader
    **没有**那个属性 ⇒ 恒抛 TypeError ⇒ 那条日志**永远只印 pid**（v1.12.2 同件还没有这个块）。
    这里改成从**正在写的 assets 文件**取（`assets.objects[pid].read().m_Name`）✓；
    真读不出时**出声**并返回 None，调用方按 pid 降级（⛔ 不静默）。
    """
    try:
        return getattr(assets.objects[pid].read(), "m_Name", None)
    except Exception as e:                                              # noqa: BLE001
        print("⚠ [copy-full] 材质名读不出（pid=%s，%s: %s）⇒ 按 pid 列出"
              % (pid, type(e).__name__, e))
        return None


def decide_source_mats(src_pids, src_names, pid_exists, pid_by_name, fallback_pids):
    """★v1.12.3 **安全网**（中枢裁定）：决定新网格用哪些材质，**任何情况下都不丢件**。

    返回 (ppids, mode, missing)：
      "source"        源材质按 pid/名映射成功 ⇒ 用**源件材质**（目标行为）
      "no_source_info"清单没带源材质（老流程/别的入口） ⇒ 沿用模板(本车)材质
      "fallback_host" ★映射不到 ⇒ **保住对象**、暂用本车材质，`missing` 逐项点名
    ⛔ 底线：三种 mode 下调用方都必须**继续建这个网格**（⛔ 不许丢件；"能看见的错"⛔ 不许修成"丢件"）。
    """
    ppids, missing = resolve_source_mats(list(src_pids or []), list(src_names or []),
                                        pid_exists, pid_by_name)
    if not (src_pids or src_names):
        return list(fallback_pids or []), "no_source_info", []
    if missing:
        return list(fallback_pids or []), "fallback_host", missing
    return ppids, "source", []


# ★甲2（2026-09-20）：**静默兜底名册** —— ⛔ 一处兜底不许无声。
#   本文件从 ★P11 起就有这条口径（见下面这个函数的 docstring，以及函数内
#   `_ENV_UNREAD.append(...)` ＋ 出声那段范式），但审计器
#   （`_rev_tools/audit_except_sites.py`）实测该口径**没被自己兑现**：同一函数里还有 4 处
#   `except …: continue/None` 是静的 ⇒ 甲2 就是把这条口径补齐到全文件。
#   口径：一处兜底**要么出声、要么记账**；`_note_swallow` 是两者的合一。
#   ⚠ 判定面还要能拿它区分「**真的没有**」与「**有过读失败 ⇒ 判不了**」（三态收口，另批做）。
_SWALLOWS = []


def _note_swallow(site, what, ex, level="warn"):
    """记一笔兜底并**出声**；返回 None（方便 `o = _note_swallow(...)` 式写法）。

    `site` 用「函数@改前现读行号」—— 行号会随后续编辑漂移，审计器核对时以本器站点表为准。
    """
    _SWALLOWS.append({"site": str(site), "what": str(what),
                      "ex": "%s: %s" % (type(ex).__name__, ex)})
    print("%s [copy-full] 兜底被触发 %s：%s（%s: %s）⇒ **判不了，⛔ 不当「没有」**"
          % ("⚠" if level == "warn" else "ⓘ", site, what, type(ex).__name__, ex))
    return None


def _fill_mat_srcs_from_bundle(sf, entries, pids, names, bundle=None):
    r"""★[★㉖-续·D] 把 `mat_srcs` 里**缺源字节**的条目，**从已打开的源包现场补齐**。

    产出与 `mat_srcs` **同型**：`{pid, name, raw_b64, texs:[{prop, src_pid, name, raw_b64}...]}`。
    ⚠ 流式贴图的 `.resS` 字节这里不处理（交给既有注入器按 reuse/embedded/foreign_ref 分类，
    并在 `mat_injected`/`mat_src_unresolved` 里如实标注 —— ⛔ 不假装成功）。
    ⚠ `m_TexEnvs` 的元素**是 `(prop, UnityTexEnv)` 元组** ⇒ 解包后直接取 `te.m_Texture`；
      ⛔ 不能写 `te[1].m_Texture`（重复下标会抛 TypeError，被下面的 except 吞成"没有贴图"）。
    ★P11：本函数里**每一处兜底都出声**，并把"读失败"记进 `_ENV_UNREAD`
      （**「确实没有」与「读失败」必须可区分** —— 这是正本 §1 第 11 条 (d) 补反面的要求）。
    """
    import base64 as _b64
    _ENV_UNREAD = []      # [(材质 pid, texEnv 属性名, 失败原因)...]：texEnv **解不开**（≠ 空引用）
    by_pid = {}
    for e in (entries or []):
        try:
            by_pid[int(e.get("pid") or 0)] = dict(e)
        except Exception as _e:                                                 # noqa: BLE001
            _note_swallow('_fill_mat_srcs_from_bundle@L88', "守护块在写 by_pid[int(e.get('pid') or 0)]", _e)
            continue
    out = []
    for i, p in enumerate(pids or []):
        try:
            p = int(p)
        except Exception as _e:                                                 # noqa: BLE001
            _note_swallow('_fill_mat_srcs_from_bundle@L94', '守护块在写 p', _e)
            continue
        e = by_pid.get(p, {"pid": p, "name": (names[i] if i < len(names) else None)})
        if not e.get("raw_b64"):
            o = None
            try:
                o = sf.objects.get(p)
            except Exception as _e:                                             # noqa: BLE001
                _note_swallow('_fill_mat_srcs_from_bundle@L101', '守护块在写 o', _e)
                o = None
            if o is not None and getattr(o.type, "name", "") == "Material":
                try:
                    _raw = o.get_raw_data()
                    if _raw:
                        e["raw_b64"] = _b64.b64encode(_raw).decode("ascii")
                except Exception as _e:                                   # noqa: BLE001
                    # ⛔ 不静默（★P11）：读失败 ≠ "这材质没字节可带" ⇒ 出声（下游会落 mat_src_unresolved）
                    print("⚠ [copy-full] 材质 pid=%s 的 raw **读失败**（%s: %s）⇒ 该材质无字节可带"
                          % (p, type(_e).__name__, _e))
                try:
                    if not e.get("name"):
                        e["name"] = o.read().m_Name
                except Exception as _e:                                   # noqa: BLE001
                    print("⚠ [copy-full] 材质 pid=%s 的**名字读不出**（%s: %s）⇒ 清单里按 pid 列出"
                          % (p, type(_e).__name__, _e))
                if not e.get("texs"):
                    texs = []
                    try:
                        d = o.read()
                        sp = getattr(d, "m_SavedProperties", None)
                        for prop, te in (list(getattr(sp, "m_TexEnvs", []) or [])
                                         if sp is not None else []):
                            try:
                                tp = int(getattr(te.m_Texture, "m_PathID", 0) or 0)
                            except Exception as _e:                       # noqa: BLE001
                                # ★P11：**读失败 ≠ 空引用**（空引用是 `m_PathID==0`，那是材质真没挂图）
                                #   ⇒ 出声并计数；⛔ 不能静静当"没图"（同族教训：兜底把 TypeError
                                #   吞成"没有贴图" ⇒ texs=0）
                                _ENV_UNREAD.append((p, str(prop), "%s: %s" % (type(_e).__name__, _e)))
                                print("⚠ [copy-full] 材质 pid=%s 的 texEnv %r **解不开**"
                                      "（%s: %s）⇒ 该条按「不可判」记（⛔ ≠ 没挂图）"
                                      % (p, prop, type(_e).__name__, _e))
                                continue
                            if not tp:
                                continue
                            try:
                                to = sf.objects.get(tp)
                            except Exception as _e:                             # noqa: BLE001
                                _note_swallow('_fill_mat_srcs_from_bundle@L140', '守护块在写 to', _e)
                                to = None
                            if to is None or getattr(to.type, "name", "") != "Texture2D":
                                continue
                            row = {"prop": str(prop), "src_pid": tp}
                            try:
                                row["name"] = to.read().m_Name
                            except Exception as _e:                       # noqa: BLE001
                                print("⚠ [copy-full] 图 pid=%s 的**名字读不出**（%s: %s）"
                                      % (tp, type(_e).__name__, _e))
                            try:
                                _traw = to.get_raw_data()
                                if _traw:
                                    row["raw_b64"] = _b64.b64encode(_traw).decode("ascii")
                            except Exception as _e:                       # noqa: BLE001
                                print("⚠ [copy-full] 图 pid=%s 的 raw **读失败**（%s: %s）"
                                      "⇒ 这张图无字节可带" % (tp, type(_e).__name__, _e))
                            # ★[★㉖-续·D] **图字节**：内联分支（inject_material_entries L358）
                            #   要求 `raw_b64` **＋** `bytes_b64` 同时具备 ⇒ 这里把图字节也取上：
                            #   流式（m_StreamData.path 非空）走 `tex_stream.read_stream_bytes`
                            #   （**从当前包里的 .resS 节点读**，见其 docstring 的规矩）；
                            #   内联 ⇒ 直接取解出来的 `m_ImageData`。
                            try:
                                import tex_stream as _TS
                                _td = to.read()
                                _sd = getattr(_td, "m_StreamData", None)
                                _spath = getattr(_sd, "path", "") if _sd is not None else ""
                                if _spath:
                                    # ★ 只要**有** path 就记下来（⛔ 不在"取字节成功"时才记）：
                                    #   下游据此区分「**内联图**（raw 里就有图）」与「**流式图**」，
                                    #   后者若取不到 .resS 字节 ⇒ ⛔ 不许"照抄对象"充数（照样缺图）
                                    row["stream_path"] = _spath
                                if _spath and bundle:
                                    _info = _TS.stream_info(to)
                                    _raw = _TS.read_stream_bytes(bundle, _info,
                                                                sf=getattr(to, "assets_file", None))
                                    if _raw:
                                        row["bytes_b64"] = _b64.b64encode(_raw).decode("ascii")
                                        row["stream"] = {"path": _spath,
                                                         "offset": getattr(_sd, "offset", 0),
                                                         "size": getattr(_sd, "size", 0)}
                                else:
                                    _img = getattr(_td, "m_ImageData", None)
                                    if _img:
                                        row["bytes_b64"] = _b64.b64encode(bytes(_img)).decode("ascii")
                            except Exception as _e:                       # noqa: BLE001
                                # ⛔ 不静默：取不到字节要说出来（注入器会走 foreign_ref 并记 report）
                                print("⚠ [copy-full] 兜底：贴图 %s 的图字节取不到（%s: %s）"
                                      % (row.get("name") or tp, type(_e).__name__, _e))
                            texs.append(row)
                    except Exception as _e:                               # noqa: BLE001
                        print("⚠ [copy-full] 材质 pid=%s 的 **texEnv 列表读不出**（%s: %s）"
                              "⇒ 贴图依赖**无从核对**（⛔ 不当「没有贴图」）"
                              % (p, type(_e).__name__, _e))
                    if texs:
                        e["texs"] = texs
        out.append(e)
    if _ENV_UNREAD:
        print("⚠ [copy-full] 本次共 %d 条 texEnv **解不开**（⛔ 不等于材质没挂图；已逐条出声）：%s"
              % (len(_ENV_UNREAD),
                 "；".join("pid=%s/%s" % (a, b) for a, b, _c in _ENV_UNREAD[:4])))
    return out


def make_tex_name_in_pack(sf, *pack_lists):
    r"""★[★㉖-续·D P8] 「**包内** Texture2D 名 → pid」解析器（带缓存）。

    ⛔ 不能用 `_find_named(sf, ...)`：那是**源包**口径 —— 源包里同名图可能多个、挑中的那个
       **不在包里** ⇒ 材质带进来了、texEnv 还指着包外 pid ✗（实测探针 real_tex 腿：
       `Layer_A97CDC25=reuse_name` 而 `new_tex=1834790033242913627` **不在包内**）。
       判据＝「**本包会不会带它**」。
    ⚠ 只取 `m_Name`（peek 节点）：⛔ **不整解析** —— 内联图 16 MB，整解析会把图数据读一遍。
    ⚠ 找不到（或 peek 不可用）⇒ 返回 0 ⇒ 调用方落"带字节/照抄"，⛔ 永不指向包外。
    """
    import base64 as _b64
    cache = {}
    fails = {}                     # ★P11：pid → 读失败原因（"确实没有" ≠ "读失败"）
    tpl = _first_of_class(sf, "Texture2D")
    pk_node, pk_key = None, "m_Name"
    if tpl is not None:
        try:
            _pk = tpl._get_typetree_node().get_name_peek_node()
            if _pk:
                pk_node, pk_key = _pk[0], _pk[1]
        except Exception as _e:                                                 # noqa: BLE001
            _note_swallow('make_tex_name_in_pack@L224', '守护块在写 _pk', _e)
            pk_node = None

    def _lookup(nm):
        if not nm or pk_node is None:
            return 0
        if nm in cache:
            return cache[nm]
        hit = 0
        for lst in pack_lists:
            for o in list(lst or []):
                if int(o.get("class_id") or 0) != 28 or not o.get("raw"):
                    continue
                try:
                    raw = _b64.b64decode(o["raw"])
                    er = EndianBinaryReader(raw, endian=sf.reader.endian)
                    d = TypeTreeHelper.read_typetree(pk_node, er, as_dict=True, assetsfile=sf,
                                                     byte_size=len(raw), check_read=False)
                    cur = d.get(pk_key) if isinstance(d, dict) else None
                except Exception as _e:                                   # noqa: BLE001
                    # ★P11：**"包内没有这张图"与"这张图的 raw 读不出"必须可区分** ——
                    #   后者会让该名字按"包内没有"处理（⇒ 多带一份副本），所以要出声＋计数
                    fails[int(o["pid"])] = "%s: %s" % (type(_e).__name__, _e)
                    print("⚠ [copy-full] 包内图 pid=%s 的**名字读不出**（%s: %s）⇒ 该名字按"
                          "「包内没有」处理（可能多带一份副本，⛔ 不会悬空）"
                          % (o.get("pid"), type(_e).__name__, _e))
                    continue
                if cur == nm:
                    hit = int(o["pid"])
                    break
            if hit:
                break
        cache[nm] = hit
        return hit

    _lookup.fails = fails          # ★P11：给调用方一个"读失败"计数（⛔ 与"包内没有"分开记）
    return _lookup


def read_pack_object(sf, pack_raw_by_pid, cls, pid):
    r"""★[★㉖-续·D P8] 从**包内 dump 的 raw** 读对象（⛔ 不用 `sf.objects[pid].read()`）。

    为什么：`_new_reader` 建出来的 reader `byte_size==0` ⇒ 回读报
    `ValueError: Expected to read 0 bytes, but only read 4892 bytes` ⇒ 贴图依赖检查**判不了**
    （实测：用户形态构建日志里这条检查**一句输出都没有**）。包内 raw 才是导入端真正看到的东西。
    """
    import base64 as _b64
    raw = (pack_raw_by_pid or {}).get(int(pid))
    if raw is None:
        return None
    tpl = _first_of_class(sf, cls)
    if tpl is None:
        return None
    data = raw if isinstance(raw, (bytes, bytearray)) else _b64.b64decode(raw)
    er = EndianBinaryReader(data, endian=sf.reader.endian)
    return TypeTreeHelper.read_typetree(tpl._get_typetree_node(), er, as_dict=False,
                                        assetsfile=sf, byte_size=len(data), check_read=False)


def record_injected_tex_deps(rep, mesh_name, sink=None, log=print):
    r"""★[★㉖-续·D P8] 把**带入材质**的贴图依赖落进 ★㉖-续·C 的 `tex_missing` 通道。

    `how == "foreign_ref"` ＝ 图**既不在本包、又没带源字节** ⇒ 产物里那张图**仍然缺**
    （实测探针 bogus 腿：2 张图被摘掉 ⇒ 旧写法零记录零告警、`tex_missing` 还是 [] ✗）。
    ⛔ 不许静默：每条都出声＋进清单。返回 (miss_rows, n_ok)；miss_rows 非空 ⇒ **能失败**。
    """
    miss, ok = [], 0
    for r in (rep or []):
        nm = r.get("name") or ("pid=%s" % r.get("src_pid"))
        for t in (r.get("texs") or []):
            if t.get("how") == "foreign_ref":
                miss.append({"mesh": mesh_name, "material": nm, "prop": str(t.get("prop")),
                             "tex_pid": int(t.get("src_tex") or 0), "mode": "injected_foreign"})
            elif t.get("new_tex"):
                ok += 1
    for row in miss:
        log("⚠ [copy-full] 带入材质 %r 的图**既不在本包、又没带源字节**：%s(pid=%s) ⇒ "
            "仍指向源 pid（进游戏大概率缺图）；已记清单 `tex_missing`"
            % (row["material"], row["prop"], row["tex_pid"]))
    if sink is not None:
        sink.extend(miss)
    return miss, ok


def _carry_dangling_materials(sf, new_objects, new_objs_extra, dump_new, smr_src,
                              mat_injected=None, mat_unresolved=None, log=print,
                              _bundle_path=None, tex_missing=None, expect_mats=None):
    r"""★[★㉖-续·D 2026-09-19] **终局补带**：包内 SMR 的 `m_Materials` 若指向"不在包内"的 pid
    ⇒ 从**源包**把该材质带入（复用既有注入器）＋**就地重写**该 SMR 的 `m_Materials` 后重 dump。

    为什么需要（实测）：prefab **既有网格**不走"新网格材质解析"那段（clist `mats` 为空 ⇒ 跳过）
    ⇒ SMR 照抄源 pid ⇒ 产物悬空 ✗（用户形态 6/7）。判据＝**以目标包为准**。
    ⛔ 不静默：源字节缺失 ⇒ 出声并逐条记进 `mat_src_unresolved`（⛔ 不当绿也不当红）。
    """
    import base64 as _b64
    from build_turret_direct import _make_alloc, _save
    pack = {int(o["pid"]) for o in new_objects} | {int(e["pid"]) for e in new_objs_extra}
    alloc = None
    n_smr = 0
    # ★[★㉖-续·D P9] 「源材质 pid → 本次补带里带入的新 pid」：同一个源材质被多个 SMR 引用时
    #   **只带一份**（实测用户形态：6 个 SMR 里 5 个共用 'US_ACV 1' ⇒ 旧写法带 5 份同内容副本、
    #   产物 201 个对象）。⛔ 不影响"不悬空"：每个 SMR 仍被就地重写到**包内** pid。
    _src2new = {}
    # ★[★㉖-续·D] 「**包内** Texture2D 名 → pid」解析器（带缓存）：给注入器的 `name_in_pack` 用。
    #   ⛔ 不能用 `_find_named(sf, ...)`（那是源包口径 —— 同名图在源包里可能多个、挑中的不在包里）。
    #   P8：口径抽成模块级 `make_tex_name_in_pack`（**新网格路径也用同一处**，⛔ 不再各写一份）
    _name_in_pack = make_tex_name_in_pack(sf, new_objects, new_objs_extra)
    for o in list(new_objects) + list(new_objs_extra):
        if int(o.get("class_id") or 0) != 137:
            continue
        pid = int(o["pid"])
        try:
            s = _read_smr(_b64.b64decode(o["raw"]), smr_src)
            mats = [int(m.m_PathID) for m in (getattr(s, "m_Materials", None) or [])
                    if m and m.m_PathID]
        except Exception as e:                                            # noqa: BLE001
            log("⚠ [copy-full] 终局补带：SMR %s 解不开（%s: %s）⇒ **判不了**（⛔ 不当绿）"
                % (pid, type(e).__name__, e))
            continue
        missing = [p for p in mats if p not in pack]
        if not missing:
            # ★[★㉖-续·D P12] 已经自洽的 SMR 也要进产物侧对拍 —— ⛔ 否则 `pack_verify.checked_mats`
            #   在用户形态里恒为 0（实测：材质绑定腿**零覆盖**）
            if expect_mats is not None and pid not in expect_mats:
                expect_mats[pid] = [int(p) for p in mats]
            continue
        # ★[★㉖-续·D P9] 已经带过的源材质**不再带第二份**（新 pid 从 `_src2new` 取）
        _todo = [p for p in missing if p not in _src2new]
        entries = (_fill_mat_srcs_from_bundle(sf, [], _todo, [None] * len(_todo),
                                              bundle=_bundle_path) if _todo else [])
        plan, unres = plan_mat_injections(entries, lambda _p: int(_p) in pack)
        for u in unres:
            if mat_unresolved is not None:
                mat_unresolved.append(dict(u, mesh="(终局补带)", smr=pid))
            log("⚠ [copy-full] 终局补带：材质 %r 源字节缺失 ⇒ **仍悬空**（已记 mat_src_unresolved）"
                % (u.get("name") or u.get("src_pid"),))
        rep = []
        if plan:
            if alloc is None:
                alloc = _make_alloc(set(sf.objects.keys()), MOD_PID_BASE)
            new_pids, rep = inject_material_entries(sf, alloc, plan, log=log,
                                                    in_pack=lambda _p: int(_p) in pack,
                                                    name_in_pack=_name_in_pack)
        elif not _todo:
            log("[copy-full] ★ 终局补带：SMR %s 的悬空材质**已在本次补带里带过** ⇒ 复用"
                "（⛔ 不重复带入）" % pid)
        # ★[★㉖-续·D P9] 重写引用：**缓存里已带过的** ∪ **本次刚带的**
        src2new = {p: _src2new[p] for p in missing if p in _src2new}
        for _r in rep:
            if _r.get("new_pid"):
                src2new[int(_r["src_pid"])] = int(_r["new_pid"])
        _src2new.update(src2new)
        if not any(p in src2new for p in mats):
            continue
        s.m_Materials = [PPtr(m_FileID=0, m_PathID=src2new.get(p, p), assetsfile=sf)
                         for p in mats]
        try:
            _save(sf, smr_src, pid, s)         # ★ 用它**自己的 pid** ⇒ 就地重写（不换 pid）
            dump_new(pid)
        except Exception as e:                                            # noqa: BLE001
            log("⚠ [copy-full] 终局补带：SMR %s 就地重写失败（%s: %s）"
                % (pid, type(e).__name__, e))
            continue
        # ★[★㉖-续·D P12] 记"重写后应当落在产物里"的绑定（**写盘路径**对拍，见本补丁器头部说明）
        if expect_mats is not None and pid not in expect_mats:
            expect_mats[pid] = [int(src2new.get(p, p)) for p in mats]
        for r in rep:
            dump_new(int(r["new_pid"]))
            for t in (r.get("texs") or []):
                # ★[★㉖-续·D] `copied_bytes`（内联图逐字节照抄）也必须 dump 进 manifest
                if t.get("how") in ("embedded", "copied_bytes") and t.get("new_tex"):
                    dump_new(int(t["new_tex"]))
            if mat_injected is not None:
                mat_injected.append(dict(r, mesh="(终局补带)", smr=pid))
        # ★[★㉖-续·D P8] 补带路径同样**不许静默**：foreign_ref 进 `tex_missing`
        _miss_rows, _n_ok = record_injected_tex_deps(rep, "(终局补带)", sink=tex_missing, log=log)
        if _miss_rows:
            log("⚠ [copy-full] 终局补带：SMR %s 有 %d 张图**仍不在包内**（已记 `tex_missing`）"
                % (pid, len(_miss_rows)))
        elif _n_ok:
            # ★ 回执（⛔ 不是"没输出就算过"）：证明这条腿逐张查过、且都在包里
            log("[copy-full] ✓ 终局补带：SMR %s 带入材质的贴图依赖**全在包里**（%d 张逐条查过）"
                % (pid, _n_ok))
        # ⛔ 同 pid 不许两条：重 dump 的条目若已在 new_objects 里 ⇒ **就地更新那一条**
        for e in list(new_objs_extra):
            for tgt in new_objects:
                if int(tgt["pid"]) == int(e["pid"]):
                    tgt.update(e)
                    new_objs_extra.remove(e)
                    break
        # 包内集合跟着更新（同一次调用里后面还有对象要比对）
        pack |= {int(r["new_pid"]) for r in rep if r.get("new_pid")}
        for r in rep:
            for t in (r.get("texs") or []):
                if t.get("new_tex"):
                    pack.add(int(t["new_tex"]))
        n_smr += 1
    log("[copy-full] ★ 终局补带：处理 %d 个 SMR（悬空材质已从源包带入并就地重写引用）" % n_smr)
    return n_smr


def resolve_source_mats(src_pids, src_names, pid_exists, pid_by_name):
    """把"源网格的材质"解析成**目标包里的材质 pid 列表**。

    入参（都可注入，便于脱离真包做判据）：
      src_pids     : 源材质 pid 列表（来自导出清单 `meshes[].mats`）
      src_names    : 源材质名列表（清单 `meshes[].mat_names`，用于按名兜底）
      pid_exists(p): 目标包里有没有这个 pid
      pid_by_name(n): 目标包里按名找材质 ⇒ pid（找不到给 0）
    返回 (ppids, missing)：
      ppids   : 解析成功的目标材质 pid（按输入顺序）
      missing : **逐项点名**的失败项（⛔ 调用方必须出声并跳过该网格，⛔ 不许静默顶替）

    ⛔ 语义要点：**只借模板布局，不借模板材质**；源材质在目标包里找不到 ⇒ 明确失败，
       绝不"就近拿本车材质"（那正是 ★26 的成因）。
    """
    ppids, missing = [], []
    for i, sp in enumerate(src_pids):
        nm = src_names[i] if i < len(src_names) else None
        # ★甲1（2026-09-20）：`pid_exists` 现在可返回 **None＝判不了** ⇒ ⛔ 不许把它当"不存在"静默处理
        try:
            _ok = (pid_exists(sp) if sp else False)
        except Exception as _e:                                         # noqa: BLE001
            _ok = None
            print("⚠ [copy-full] `pid_exists` **抛异常**（pid=%s，%s: %s）⇒ **判不了**"
                  % (sp, type(_e).__name__, _e))
        if _ok is None:
            missing.append("%s(pid=%s：⛔ 判不了，检查未答)" % (nm or "（名未知）", sp))
            continue
        if _ok is True:
            ppids.append(sp)
            continue
        hit = pid_by_name(nm) if nm else 0
        if hit:
            ppids.append(hit)
            continue
        missing.append("%s(pid=%s)" % (nm or "（名未知）", sp))
    if not src_pids and src_names:
        for nm in src_names:
            hit = pid_by_name(nm)
            if hit:
                ppids.append(hit)
            else:
                missing.append("%s(pid=未知)" % nm)
    return ppids, missing


def _first_of_class(sf, cls):
    for o in sf.objects.values():
        if getattr(o.type, "name", "") == cls:
            return o
    return None


def _find_named(sf, cls, name):
    if not name:
        return 0
    for o in sf.objects.values():
        if getattr(o.type, "name", "") != cls:
            continue
        try:
            if getattr(o.read(), "m_Name", None) == name:
                return o.path_id
        except Exception as _e:                                             # noqa: BLE001
            _note_swallow('_find_named@L487', '循环内取数（读不出 ⇒ 该轮静默少一条）', _e)
            continue
    return 0


def plan_mat_injections(src_entries, pid_exists):
    r"""★v1.12.5（★㉖-续·B·甲）：决定"哪些源材质要**带入**"（纯函数，可注入 ⇒ 脱离真包也能判）。

    入参 `src_entries` = 导出清单里该网格的 `mat_srcs`（**向后兼容**：老 clist 没有 ⇒ 空 ⇒ 行为不变）
      每项形如 {"pid":int, "name":str, "raw_b64":str, "texs":[{"prop":str,"src_pid":int,"name":str,
                "raw_b64":str,"bytes_b64":str,"stream":{...}}...]}
    返回 (plan, unresolved)：
      plan       = [那些**目标包里没有、且带了源字节**的材质] ⇒ 交给 inject_material_entries 真拷
      unresolved = [那些**目标包里没有、但没带源字节**的材质] ⇒ ⛔ 不静默：调用方要出声并保留安全网
    判据口径：`pid_exists(pid)` 为真 ⇒ **不带入**（⛔ 不重复带入、包体积不增长）。
    ★[★㉖-续·D 2026-09-19] **该谓词必须回答「本包（目标包）会不会带它」** ——
      ⛔ 传「源包里有没有」是错的：源包里有、而本包不会 dump 的材质会被判成「不需要带入」
      ⇒ 产物**沿用源 pid** ⇒ 悬空 ✗（实测：`Ah_1z` 的 `m_Materials` 指向包外 pid，随后
      `import_pack.py` L533 `找不到原材质 pid`）。
    """
    plan, unresolved = [], []
    for e in (src_entries or []):
        try:
            pid = int(e.get("pid") or 0)
        except Exception as _e:                                             # noqa: BLE001
            _note_swallow('plan_mat_injections@L511', '守护块在写 pid', _e)
            continue
        if not pid or pid_exists(pid):
            continue                                                  # 已在**本包（目标包）** ⇒ 不重复带入
        if e.get("raw_b64"):
            plan.append(dict(e))
        else:
            unresolved.append({"src_pid": pid, "name": e.get("name"),
                               "why": e.get("why") or "跨包且未带源字节（mat_srcs 无 raw_b64）"})
    return plan, unresolved


def inject_material_entries(sf, alloc, plan, log=print, _parse=None, _save=None,
                            in_pack=None, name_in_pack=None):
    # ★[★㉖-续·D 2026-09-19] `in_pack(p)`："**本包（目标包）会不会带它**"的谓词。
    #   ★ P14 起：传 None **不再**退回源包口径，而是按"**不在包内**"处理（安全侧）；
    #   `name_in_pack` 传 None ⇒ **不做同名复用**。⇒ 注入器里**不存在**可退回源包口径的路径。
    r"""★v1.12.5：把 plan 里的材质（及其贴图依赖）**真拷进** sf，并把 texEnv 的 pid 重映射到目标包内。

    返回 (new_pids, report)：
      new_pids = [新材质 pid...]（顺序与 plan 一致）
      report   = [{"src_pid","name","new_pid","texs":[{"prop","src_tex","new_tex","how"}]}...]
    `how` 取值（可判）：
      "reuse_pid"    目标包里**本来就有**同 pid 的图 ⇒ 直接用（⛔ 不重复带入）
      "reuse_name"   目标包里有**同名**图 ⇒ 重映射到它
      "embedded"     带了图字节 ⇒ 新建一份（内联）
      "foreign_ref"  ⚠ 图既不在包内、又没带字节 ⇒ **仍指向源 pid**（跨包 ⇒ 游戏里大概率缺图），已记进 report
    """
    import base64 as _b64
    if _parse is None:
        def _parse(tpl, raw):
            node = tpl._get_typetree_node()
            er = EndianBinaryReader(raw, endian=sf.reader.endian)
            return TypeTreeHelper.read_typetree(node, er, as_dict=False, assetsfile=sf,
                                                byte_size=len(raw), check_read=False)
    if _save is None:
        _save = globals()["_save"]
    mat_tpl = _first_of_class(sf, "Material")
    tex_tpl = _first_of_class(sf, "Texture2D")
    new_pids, report = [], []
    for e in plan:
        try:
            d = _parse(mat_tpl, _b64.b64decode(e["raw_b64"]))
        except Exception as ex:                                       # noqa: BLE001
            log("⚠ [copy-full] 源材质 %r 的字节解不开（%s: %s）⇒ 跳过带入（仍走安全网）"
                % (e.get("name"), type(ex).__name__, ex))
            continue
        tex_rows = []
        for t in (e.get("texs") or []):
            src_tex = int(t.get("src_pid") or 0)
            new_tex, how = 0, None
            # ★[★㉖-续·D P14] ⛔ **删掉"退回源包口径"的兜底**：`in_pack` 缺省 ＝ **没有包内信息**
            #   ⇒ 一律按"不在包内"处理（宁可多带一份副本，⛔ 绝不许 texEnv 指向包外）。
            #   为什么必须删：同一"源包口径"错误在这条链上已出现三次（材质谓词／生产侧不带字节／
            #   贴图谓词）⇒ 留着 `None ⇒ 源包` 这条路，任何**新调用点漏传**都会静默踩回同一个 bug
            #   （＝打地鼠）。删掉之后，"源包口径"在注入器里**不存在可达路径**。
            _here = bool(in_pack(src_tex)) if in_pack is not None else False
            if src_tex and _here:
                new_tex, how = src_tex, "reuse_pid"
            else:
                # ★[★㉖-续·D] "同**名**复用"也必须限定在**包内**：旧写法 `_find_named(sf, ...)`
                #   查的是**源包**，而源包里同名图可能有多个 ⇒ 挑中一个不在包里的 ⇒ 产物悬空 ✗
                #   （实测：12 条不达标降到 2 条后，剩下的正是这条）。找不到 ⇒ 落到 embed ✓
                # ★P14：同理 —— 没有 `name_in_pack` ⇒ **不做同名复用**（⛔ 不回退
                #   `_find_named(sf, …)`：那是**源包**同名查找，会挑到包外那张图）；直接走带字节/照抄
                hit = (name_in_pack(t.get("name")) if name_in_pack is not None else 0)
                if hit:
                    new_tex, how = hit, "reuse_name"
                elif t.get("raw_b64") and t.get("bytes_b64") and tex_tpl is not None:
                    try:
                        td = _parse(tex_tpl, _b64.b64decode(t["raw_b64"]))
                        img = _b64.b64decode(t["bytes_b64"])
                        sd = getattr(td, "m_StreamData", None)
                        if sd is not None:
                            sd.path, sd.offset, sd.size = "", 0, 0  # ⇒ 内联，不再引用源 CAB
                        try:
                            td.m_ImageData = img
                        except Exception:                             # noqa: BLE001
                            raise RuntimeError("该 Texture2D 模板没有 m_ImageData 字段（无法内联）")
                        try:
                            td.m_CompleteImageSize = len(img)
                        except Exception as _e:                       # noqa: BLE001
                            log("⚠ [copy-full] 图 %r 的 `m_CompleteImageSize` 设不上"
                                "（%s: %s）⇒ 该字段保持模板原值"
                                % (t.get("name"), type(_e).__name__, _e))
                        new_tex = _save(sf, tex_tpl, alloc(), td).path_id
                        how = "embedded"
                    except Exception as ex:                           # noqa: BLE001
                        log("⚠ [copy-full] 图 %r 内联失败（%s: %s）⇒ **仍指向源 pid**（跨包大概率缺图）"
                            % (t.get("name"), type(ex).__name__, ex))
                        new_tex, how = src_tex, "foreign_ref"
                elif (t.get("raw_b64") and tex_tpl is not None
                      and not t.get("stream_path")):
                    # ★[★㉖-续·D] **内联图**（`m_StreamData.path` 空）：图数据**就在对象 raw 里**
                    #   ⚠ **流式图不许走这里**（P7 收紧）：照抄的 raw 只带"指向 .resS 的引用"，
                    #     目标包里没有那个 CAB ⇒ 游戏里照样缺图 ✗ ⇒ 流式图取不到字节应落 foreign_ref
                    #   ⇒ **逐字节照抄**一份新对象。⛔ 不走 `_save` 的 typetree 往返：typetree 读出来的
                    #   `m_ImageData` 是 None，回写会把图数据抹掉 ✗（实测 raw 16 MB＝图就在里面）。
                    try:
                        _src_o = sf.objects.get(src_tex)
                        if _src_o is None:
                            raise RuntimeError("源包里没有这张图（pid=%s）" % src_tex)
                        _r = _new_reader(_src_o, sf, alloc())
                        _r.data = _b64.b64decode(t["raw_b64"])      # ★ 原始字节照抄
                        sf.objects[_r.path_id] = _r
                        new_tex, how = _r.path_id, "copied_bytes"
                    except Exception as ex:                           # noqa: BLE001
                        log("⚠ [copy-full] 内联图 %r 逐字节照抄失败（%s: %s）⇒ **仍指向源 pid**"
                            % (t.get("name"), type(ex).__name__, ex))
                        new_tex, how = src_tex, "foreign_ref"
                else:
                    new_tex, how = src_tex, "foreign_ref"
            tex_rows.append({"prop": t.get("prop"), "src_tex": src_tex, "new_tex": new_tex, "how": how})
            if new_tex and src_tex and new_tex != src_tex:
                for te in (list(getattr(getattr(d, "m_SavedProperties", None), "m_TexEnvs", []) or [])):
                    try:
                        if str(te[0]) == str(t.get("prop")) and te[1].m_Texture is not None:
                            te[1].m_Texture.m_PathID = int(new_tex)
                    except Exception as _e:                                 # noqa: BLE001
                        _note_swallow('inject_material_entries@L629', '守护块在写 te[1].m_Texture.m_PathID', _e)
                        continue
        new_pid = _save(sf, mat_tpl, alloc(), d).path_id
        new_pids.append(new_pid)
        report.append({"src_pid": int(e.get("pid") or 0), "name": e.get("name"),
                       "new_pid": new_pid, "texs": tex_rows})
        log("[copy-full] ★ 跨包**带入**材质 %r：源 pid=%s ⇒ 新 pid=%s（图 %d 张：%s）"
            % (e.get("name"), e.get("pid"), new_pid, len(tex_rows),
               ", ".join("%s=%s" % (x["prop"], x["how"]) for x in tex_rows[:4])))
    return new_pids, report


# ── ★甲1 配套检查（2026-09-20 中枢放行）：**同 pid 名字一致性** ─────────────────────────────
# ★ 白名单收紧（中枢 00:3x 逐字核源）：**只列该件里真实存在的占位字面量**，逐字完整 ＋ 出处（行号为
#   "当时读到"的快照，⛔ 引用时按符号/上下文重新定位）：
#     · `（名未知）`   ← `resolve_source_mats()` 的 `missing.append(... nm or "（名未知）" ...)`（快照 L451/L460）
#     · `（名读不出）` ← `build_copy()` 的 `_inh.append(... _nm or "（名读不出）" ...)`（快照 L1781）
#   ⛔ 半角变体与 `（读不出）` 等**不在白名单**：它们由下面的**谓词**覆盖（避免"白名单看着有、实际永远
#      匹配不上"的假安全感）；若将来新增占位拼法 ⇒ **按同规矩（逐字＋出处）补进本白名单** ✓
_PLACEHOLDER_WHITELIST = ("（名未知）", "（名读不出）")
# ★ 兜底谓词的关键词集**去掉单字「名」**（太宽 ⇒ 括号里含「名」的真名会被误判为空＝假绿 ✗）
_PLACEHOLDER_HINTS = ("名未知", "名读不出", "读不出", "未知", "unknown", "nameless", "placeholder")


def _placeholder_kind(s):
    r"""★甲1 配套：把"空/占位"判成**可显形的三档** ⇒ `"empty"`／`"whitelist"`／`"predicate"`／`None`。
    ★ 为什么返回档位而不是 bool：谓词命中（最弱的一档）**必须在输出里显形**（原串＋计数）⇒
      申报的"残余风险"从"人工碰运气"变成"**可发现**" ✓
    字符集**写死**：全角 `（`U+FF08/`）`U+FF09 与半角 `(`U+0028/`)`U+0029 都覆盖。
    ⛔ 真名里含括号（`AC-130 (BaseCamo) 5`／`(US) AH-1Z`）**不得**判空 ⇒ 谓词能失败 ✓。
    """
    if s is None:
        return "empty"
    t = str(s).strip()
    if t == "":
        return "empty"
    if t in _PLACEHOLDER_WHITELIST:
        return "whitelist"
    if len(t) >= 2 and t[0] in "（(" and t[-1] in "）)" \
            and any(k in t.lower() for k in _PLACEHOLDER_HINTS):
        return "predicate"
    return None


def is_placeholder_name(s):
    r"""★甲1 配套：判定"名字为空/占位"（＝ `_placeholder_kind(s)` 非 None）。"""
    return _placeholder_kind(s) is not None


def render_invisible(s):
    r"""★甲1 配套：把**首尾空白 ＋ 真·不可见字符**渲染成 `<U+00XX>`（红档必须**人眼可辨**）。
    ⚠ 口径（照中枢示例 `源侧"AC-130 BaseCamo 5"` ⇔ `目标侧"AC-130 BaseCamo 5<U+0020>"`）：
      **内部空格保留**（可读性），只对**首尾空白**与**零宽/软连字等不可见字符**转义 ✓
    """
    t = str(s)
    lead = len(t) - len(t.lstrip(" \t\n\r"))
    trail = len(t) - len(t.rstrip(" \t\n\r"))
    out = []
    for i, ch in enumerate(t):
        o = ord(ch)
        _edge = (i < lead) or (i >= len(t) - trail) if trail else (i < lead)
        if (_edge and ch in " \t\n\r") or o in (0x200B, 0x200C, 0x200D, 0xFEFF, 0x00AD):
            out.append("<U+%04X>" % o)
        else:
            out.append(ch)
    return "".join(out)


def check_mat_name_consistency(man):
    r"""★甲1 配套：**同 pid 名字一致性**（**豁免面 ＝ 空**）。

    三态**互斥且穷尽**（`D ＝ R ＋ G ＋ U`；**不成立即在输出里报错**，⛔ 不许出"红 0"式结果）：
      D ＝ 进入判据面的 pid 总数（该 pid 至少出现在 1 个比较面字段里）
      R ＝ ≥2 处出现，且存在不一致（两处**真名不同**；或一处真名、另一处占位/空）
      G ＝ ≥2 处出现，且**真名全非空且全同**（＝真的比过且通过）
      U ＝ 只现一处，**或**各处**全为占位/空**
    范围边界（⛔ 不是豁免）：① 字段**缺失** ≠ 空 ⇒ 不纳入比较；② 只现一处 ⇒ 未比；③ 全占位/全空 ⇒ 未比。
    比较**逐字相等**（⛔ 不归一化）；红档用 `render_invisible` 渲染差异 ✓。
    """
    fields = ("mat_fallbacks", "mat_src_unresolved", "mat_srcs", "mat_srcs_extra", "mat_injected")
    present, absent, per_pid = [], [], {}
    for f in fields:
        if f not in (man or {}):
            absent.append(f)
            continue
        present.append(f)
        v = man.get(f)
        if not isinstance(v, list):
            continue
        for e in v:
            if not isinstance(e, dict):
                continue
            pid = e.get("pid", e.get("src_pid"))
            # ★甲1 配套·修：`mat_fallbacks` 的条目**没有 pid 字段**，pid 藏在 `missing` 串里
            #   （形如 `（名未知）(pid=-9222933195559888265)`）⇒ ⛔ 不许因 `pid is None` 就 `continue`
            #   （实测：D1/D6/E2 全因此不进判据面 ⇒ 该红的没红 ✗）
            if pid is None and isinstance(e.get("missing"), list):
                for _m0 in e["missing"]:
                    _t0 = str(_m0)
                    if "(pid=" in _t0:
                        _m3 = _re.match(r"\s*(-?\d+)", _t0.split("(pid=", 1)[1])
                        if _m3:
                            pid = int(_m3.group(1))
                            break
            if pid is None:
                continue
            names = []
            if e.get("name") is not None:
                names.append(e.get("name"))
            if isinstance(e.get("missing"), list):
                for m in e["missing"]:
                    t = str(m)
                    # ★甲1 配套：`missing` 是**字符串**（形如 `名(pid=N)` 或 `（名未知）(pid=N：⛔ 判不了…)`）
                    #   ⇒ **必须从串里把 pid 抠出来**，否则本字段的条目永远进不了判据面（实测 D1/D6/E2 全因此不红）✗
                    if "(pid=" in t:
                        _nm_part = t.split("(pid=")[0]
                        _pid_part = t.split("(pid=", 1)[1]
                        _m2 = _re.match(r"\s*(-?\d+)", _pid_part)
                        names.append(_nm_part)
                        if _m2:
                            _s2 = int(_m2.group(1))
                            per_pid.setdefault(_s2, {}).setdefault(f, []).append(_nm_part)
                        continue
            if not names:
                continue
            per_pid.setdefault(int(pid), {}).setdefault(f, []).extend(names)
    red, green, uncompared, ph_hits = [], [], [], []
    for pid, byf in sorted(per_pid.items()):
        alln = [n for ns in byf.values() for n in ns]
        for _n in alln:
            if _placeholder_kind(_n) == "predicate":
                ph_hits.append(str(_n))          # ★谓词命中**原串**（最弱一档 ⇒ 必须显形）
        real, uniq = [n for n in alln if not is_placeholder_name(n)], []
        for n in real:
            if n not in uniq:
                uniq.append(n)
        # ★红条件②（件内口径）：**一处真名、另一处占位/空** ⇒ 红（按**字段级**判，⛔ 不是按 pid 级）
        _has_real_field = any(any(not is_placeholder_name(n) for n in ns) for ns in byf.values())
        _has_ph_field = any(all(is_placeholder_name(n) for n in ns) for ns in byf.values())
        if len(byf) >= 2 and _has_real_field and _has_ph_field:
            red.append({"pid": pid, "fields": sorted(byf),
                        "names": {f: [chr(34) + render_invisible(x) + chr(34) for x in ns] for f, ns in byf.items()},
                        "why": "一处真名、另一处占位/空（红条件②）"})
        elif len(byf) < 2 or not real:
            uncompared.append({"pid": pid, "fields": sorted(byf),
                               "why": "字段数<2 或 名字全为占位/空"})
        elif len(uniq) == 1:
            green.append({"pid": pid, "name": uniq[0], "fields": sorted(byf)})
        else:
            red.append({"pid": pid, "fields": sorted(byf),
                        "names": {f: [chr(34) + render_invisible(x) + chr(34) for x in ns] for f, ns in byf.items()},
                        "why": "两处真名不同"})
    D, R, G, U = len(per_pid), len(red), len(green), len(uncompared)
    return {"fields_present": present, "fields_absent": absent,
            "D": D, "R": R, "G": G, "U": U, "comparable": R + G,
            "red": red, "green": green, "uncompared": uncompared,
            "placeholder_predicate_hits": ph_hits,
            "placeholder_predicate_hits_n": len(ph_hits),
            "eq_ok": (D == R + G + U)}


def verify_pack_transforms(out_pack, expect_tr, read_transform, expect_mats=None, read_mats=None):
    r"""★v1.12.5（★㉕-续）：**产物侧回读** —— 打开刚写出的包，逐对象对拍 transform（与材质绑定）。

    为什么要有它：★㉕ 只做了 **Blender 侧**自检（`ba_mod.selfcheck_xform`）⇒ "Blender 里看着对、
    **产物里其实不对**"仍只能等实机才发现。本函数把判据延伸到**产物**：读回 manifest 里的对象字节，
    按模板 typetree 解出 Transform 的 loc/rot/scale 与 SMR 的 `m_Materials`，与"写出去时的期望值"
    逐项比，不一致就**点名**。

    ⚠ **边界（照 ★㉕ 原文，⛔ 不许省）**：约束驱动节点（`AimConstraint`/`RotationConstraint`）
      与动画行为在 Blender 侧**不复现** ⇒ 本自检**不能替代实机确认** —— 它只保证"产物字节 == 导出期望"。

    入参（都可注入 ⇒ 脱离真包也能做判据）：
      expect_tr     : {tr_pid: (名, pos, rot, sc)}（写出时的期望值）
      read_transform(raw: bytes) -> (pos, rot, sc)（按模板 typetree 解）
      expect_mats   : {smr_pid: [材质 pid]}
      read_mats(raw: bytes) -> [材质 pid]
    返回 {"checked":N, "checked_mats":M, "mismatches":[{object,field,expected,got}], "not_in_pack":[pid...]}
      · `not_in_pack` = 期望里有、但 manifest 里找不到的对象（⛔ 单独计数，不当"通过"）
    """
    import base64 as _b64
    import zipfile as _zip
    out = {"checked": 0, "checked_mats": 0, "mismatches": [], "not_in_pack": [],
           "decode_failed": []}     # ★甲1：manifest 里 raw **解不开**的对象（⛔ 与 not_in_pack 分档）
    try:
        with _zip.ZipFile(out_pack) as z:
            man = json.loads(z.read("manifest.json").decode("utf-8"))
    except Exception as e:                                            # noqa: BLE001
        print("⚠ [copy-full] 产物回读**打不开**（%s: %s）⇒ **判不了**（⛔ 不当通过）"
              % (type(e).__name__, e))
        out["mismatches"].append({"object": "<pack>", "field": "open", "expected": "可读",
                                  "got": "%s: %s" % (type(e).__name__, e)})
        return out
    by_pid = {}
    for o in (man.get("objects") or []):
        try:
            by_pid[int(o.get("pid"))] = _b64.b64decode(o.get("raw") or "", validate=True)
        except Exception as e:                                        # noqa: BLE001
            # ★甲1 修（2026-09-20 中枢放行）：⛔ 旧写法 `continue` 把"**解不开**"错记成"**不在包里**"
            #   ⇒ 污染 `not_in_pack` 读数（本函数的存在意义就是给"悬空 N/7"这类读数定值）。
            #   现在：出声 ＋ 记**独立一档** `decode_failed` ＋ 落进 `mismatches`（⇒ 调用方**非绿**）。
            out["decode_failed"].append({"pid": o.get("pid"),
                                         "why": "%s: %s" % (type(e).__name__, e)})
            out["mismatches"].append({"object": "pid=%s" % o.get("pid"), "field": "manifest_decode",
                                      "expected": "可解", "got": "%s: %s" % (type(e).__name__, e)})
            print("⚠ [copy-full] 产物 manifest 里 pid=%s 的 raw **解不开**（%s: %s）"
                  "⇒ 记 `manifest_decode`（⛔ 不当『不在包里』）" % (o.get("pid"), type(e).__name__, e))

    def _neq(a, b, tol=1e-5):
        return any(abs(float(x) - float(y)) > tol for x, y in zip(a, b))

    _df_pids = {int(_d["pid"]) for _d in out["decode_failed"] if str(_d.get("pid") or "").lstrip("-").isdigit()}
    for pid, (nm, ep, er_, es) in (expect_tr or {}).items():
        raw = by_pid.get(int(pid))
        if raw is None:
            # ★甲1：**解不开 ≠ 不在包里** ⇒ 已在 `decode_failed`/`mismatches` 记过的 pid **不再塞进
            #   `not_in_pack`**（否则"悬空 N/7"这类读数会把两件事混成一个数 ✗）
            if int(pid) not in _df_pids:
                out["not_in_pack"].append(int(pid))
            continue
        try:
            gp, gr, gs = read_transform(raw)
        except Exception as e:                                        # noqa: BLE001
            out["mismatches"].append({"object": nm, "field": "decode",
                                      "expected": "可解", "got": "%s: %s" % (type(e).__name__, e)})
            continue
        out["checked"] += 1
        for fname, exp, got in (("m_LocalPosition", ep, gp), ("m_LocalRotation", er_, gr),
                                ("m_LocalScale", es, gs)):
            if _neq(exp, got):
                out["mismatches"].append({"object": nm, "field": fname,
                                          "expected": [round(float(v), 6) for v in exp],
                                          "got": [round(float(v), 6) for v in got]})
    for pid, exp_mats in (expect_mats or {}).items():
        raw = by_pid.get(int(pid))
        if raw is None or read_mats is None:
            if int(pid) not in _df_pids and read_mats is not None:      # ★甲1：同上去重（解不开 ≠ 不在包里）
                out["not_in_pack"].append(int(pid))
            elif read_mats is None:
                out["not_in_pack"].append(int(pid))
            continue
        try:
            got_mats = [int(x) for x in read_mats(raw)]
        except Exception as e:                                        # noqa: BLE001
            out["mismatches"].append({"object": "SMR(pid=%s)" % pid, "field": "m_Materials",
                                      "expected": list(exp_mats),
                                      "got": "%s: %s" % (type(e).__name__, e)})
            continue
        out["checked_mats"] += 1
        if sorted(got_mats) != sorted(int(x) for x in exp_mats):
            out["mismatches"].append({"object": "SMR(pid=%s)" % pid, "field": "m_Materials",
                                      "expected": [int(x) for x in exp_mats], "got": got_mats})
    return out


def check_material_texture_deps(mat_pids, get_material, pid_exists, mesh_name, mode="source"):
    r"""★v1.12.5（★㉖-续·C）：逐个 texture env 检查"这张图在目标包里吗"。

    为什么要有它：★26 那条链只查**材质**能不能解析；材质在包里、但它引用的 `Texture2D` 不在包里时
    **零告警** ⇒ 只能进游戏才发现"这块发白/贴图不对"。

    入参（都可注入 ⇒ 脱离真包也能做判据）：
      mat_pids       : 目标包里的材质 pid 列表
      get_material(p): 取该材质的可读对象（读不出给 None ⇒ 跳过该材质，⛔ 不猜）
      pid_exists(p)  : 目标包里有没有这个 pid
      mesh_name/mode : 只用于记录
    返回 [{"mesh","material","prop","tex_pid","mode","kind"}...]；⛔ **空列表 = 逐个 texEnv 查过且全在**。
      · `kind="missing"` ＝ 该 texEnv 指向的图**不在目标包**
      · `kind="unparsed"` ＝ 该 texEnv **解不开**（★甲1：⛔ 旧写法把它 `continue` 吞成"没有缺图"⇒判据自身假绿）

    ⚠ 口径（照 ★㉖-续·C 的告示）：**所有** texEnv 一律纳入检查 —— 含通用图（`Soap_T`、`emm_1to80`
      这类名字）与非常规属性名（本作基图叫 `Layer_<哈希>`，⛔ 不是 `_BaseMap`/`_MainTex`）；
      只有"空引用"（`m_PathID == 0`）不算缺图（那是材质本来就没挂图）。
    """
    miss = []
    for mp in (mat_pids or []):
        d = get_material(mp)
        if d is None:
            continue
        name = getattr(d, "m_Name", None) or ("pid=%s" % mp)
        sp = getattr(d, "m_SavedProperties", None)
        envs = list(getattr(sp, "m_TexEnvs", []) or []) if sp is not None else []
        for te in envs:
            try:
                prop = te[0]
                tex = te[1].m_Texture
                tpid = int(getattr(tex, "m_PathID", 0) or 0) if tex is not None else 0
            except Exception as e:                                      # noqa: BLE001
                # ★甲1 修（2026-09-20 中枢放行）：⛔ 旧写法 `continue` 把"**解不开**"吞成"没有缺图"
                #   ⇒ **判据自身假绿**（本检查存在的意义就是抓缺图）。现在：出声 ＋ 记成**独立一档**
                #   `kind="unparsed"` ⇒ 调用方据此**判不了**（⛔ 不许当"没问题"）。
                miss.append({"mesh": mesh_name, "material": name, "prop": "(解不开)",
                             "tex_pid": 0, "mode": mode, "kind": "unparsed",
                             "why": "%s: %s" % (type(e).__name__, e)})
                print("⚠ [copy-full] 材质 %r 的 texEnv **解不开**（%s: %s）⇒ **判不了**"
                      "（⛔ 不当『没有缺图』）" % (name, type(e).__name__, e))
                continue
            if not tpid:
                continue                                            # ⛔ 空引用不是缺图
            if not pid_exists(tpid):
                miss.append({"mesh": mesh_name, "material": name, "prop": str(prop),
                             "tex_pid": tpid, "mode": mode, "kind": "missing"})
    return miss


from build_turret_direct import (_find_sources, _save, _make_go, _make_alloc,
                                     _read_from_data, _new_reader)
from bone_hashes import KNOWN_HASHES, bone_hash
from hub_edit import HUB_SCRIPT, json_to_hub

FORMAT = "bamod-assets"
VERSION = 2
MOD_PID_BASE = 0x4355424500000000


def _load_env(bundle):
    """→ (env, sf, objs, by_pid)。

    ⛔ 2026-10 修（B 分支）：这里原来直接 `list(env.objects)[0]`，
       一旦 bundle **不存在 / 读不出来**，`UnityPy.load()` **不报错**、只返回空 Environment
       ⇒ 抛一个光秃秃的 `IndexError: list index out of range`，
       表现为"代码有 bug"，其实是**路径过时**（备份包名带内容哈希，游戏更新就变）✗
       ⇒ 现在当场说清楚：文件在不在、对象数多少、该去哪找一份纯净包。
    """
    if not os.path.isfile(bundle):
        raise FileNotFoundError(
            "bundle 不存在：%s\n"
            "  ⛔ 备份包名里带 Addressables **内容哈希**，游戏更新一次就变（实测 3cc1eb58… → 1e6c04ce…）\n"
            "  ⇒ 别写死名字，用 glob 自动发现：`备份\\units_assets_all_*.bundle`\n"
            "  ⛔ 也不要拿游戏目录里那份当基准（它已被改过，判据会失真）" % bundle)

    def _finish(env, objs, by_pid, how):
        if not objs:
            raise ValueError(
                "UnityPy 从 %s 里读出 **0 个对象**（用 %s）—— 它不是空包就是读不出：\n"
                "  · 路径对不对？大小 %s 字节\n"
                "  · 这是不是被截断/覆盖过的包？（要用**纯净**备份）"
                % (bundle, how, os.path.getsize(bundle)))
        sf = sf = objs[0].assets_file
        return env, sf, objs, by_pid

    # 复用 extract_model 的缓存，避免同一 bundle 反复解压（3.4GB）导致 lz4 内存不足/缓冲区不够
    try:
        from extract_model import _load as _cached_load
        env, objs, by_pid = _cached_load(bundle)
        return _finish(env, objs, by_pid, "extract_model 缓存")
    except FileNotFoundError:
        raise
    except Exception as _e:                                                 # noqa: BLE001
        _note_swallow('_load_env@L977', '守护块在写 (env, objs, by_pid)', _e)
        env = UnityPy.load(bundle)
        objs = list(list(env.objects)[0].assets_file.objects.values()) if list(env.objects) else []
        by_pid = {o.path_id: o for o in objs}
        return _finish(env, objs, by_pid, "直接 UnityPy.load")


def container_root_pid(bundle, prefab_path):
    """容器条目里记的 `asset` pid = 这个 prefab **真正的根 GameObject** pid。

    ⛔ v1.8.79：必须用它、不能用调用方传进来的"复制源 pid"。导入 `.bamod` 会把容器
       asset 指向新分配的根 pid ⇒ 传入的旧 pid 会过期；这时我们是**按路径回退**找到条目的，
       若 manifest 仍写旧 pid，导入端一查"root_pid 不在包对象里"就判**包坏了** ✗（实测报错：
       `manifest.root_pid=6302881677221457888 不在包对象里`）。
    """
    env, sf, objs, by_pid = _load_env(bundle)
    ab = next(o for o in objs if o.type.name == "AssetBundle").read()
    for n, a in ab.m_Container:
        if n == prefab_path and a is not None and a.asset:
            return a.asset.m_PathID
    return None


def collect_prefab_objects(bundle, root_gpid, want_path=None):
    """收集原 prefab 的完整对象集（preload 范围内全部对象，MonoScript 除外）。

    返回 (objects, preload_pids, prefab_path)：
      objects: [{pid, class_id, type_name, script_id, tree_hash, raw(base64)}]
      preload_pids: 原 prefab 的 preload 顺序（含 MonoScript 常量 pid）
      prefab_path: 容器条目路径

    want_path: 可选，场景里记的 prefab 内部路径。pid 查不到时按路径回退（见下）。
    """
    env, sf, objs, by_pid = _load_env(bundle)
    ab = next(o for o in objs if o.type.name == "AssetBundle").read()
    prefab_path = None
    preload_index = preload_size = None
    for n, a in ab.m_Container:
        if a.asset and a.asset.m_PathID == root_gpid:
            prefab_path = n
            preload_index = a.preloadIndex
            preload_size = a.preloadSize
            break
    if preload_index is None and want_path:
        # ⛔ v1.8.78：**pid 会过期**。导入 .bamod 时工具会把容器的 asset 指向
        #    **新分配**的根 pid ⇒ 之后再拿"导入前记下的 pid"来找就必然找不到 ✗
        #    （实测用户点「读取动画」直接抛 `找不到 root=... 的容器条目`，完全看不懂）。
        #    这里退化成按**路径**找（场景里存了 `ba_copy_source_path`），能自愈 ✓
        base = want_path.split("/")[-1].lower()
        for n, a in ab.m_Container:
            if n == want_path or n.split("/")[-1].lower() == base:
                prefab_path = n
                preload_index = a.preloadIndex
                preload_size = a.preloadSize
                print("[copy-full] ⚠ 复制源 pid %d 在当前 bundle 里已不存在（多半是这个 "
                      "prefab 被导入过、容器 asset 已改指新 pid）⇒ 按路径回退命中 %s"
                      % (root_gpid, n))
                break
    if preload_index is None:
        raise ValueError(
            "找不到 prefab 的容器条目：root pid=%d%s\n"
            "  ⛔ 常见原因：这个 bundle 里的 prefab 容器**已被上一次导入改成新分配的根 pid**\n"
            "     ⇒ 场景里记的『复制源』pid 过期了。三种解法（任选其一）：\n"
            "     1) 偏好设置 →『游戏 bundle 文件』指回**没被导入过**的那份"
            "（如 <工作目录>\\备份\\units_assets_all_…bundle），再重新 ① 导入；\n"
            "     2) 直接用当前 bundle 重新 ① 导入所选模型（刷新复制源）；\n"
            "     3) 或点 ④ 面板里的『把①所选模型设为当前模型』。"
            % (root_gpid, ("，路径=%r" % want_path) if want_path else ""))
    seg = [p.m_PathID for p in ab.m_PreloadTable[preload_index:preload_index + preload_size]]
    objects = []
    for pid in seg:
        o = by_pid.get(pid)
        if o is None or o.type.name == "MonoScript":
            continue
        raw = o.data if getattr(o, "data", None) is not None else o.get_raw_data()
        if not raw:
            continue
        obj = _type_identity(o)
        obj["pid"] = pid
        obj["raw"] = base64.b64encode(raw).decode("ascii")
        objects.append(obj)
    return objects, seg, prefab_path


def _read_mesh(raw, mesh_src):
    r = ObjectReader(assets_file=mesh_src.assets_file, reader=mesh_src.reader, path_id=1,
                     type_id=mesh_src.type_id, serialized_type=mesh_src.serialized_type,
                     class_id=mesh_src.class_id, type=mesh_src.type,
                     byte_start=0, byte_size=len(raw), is_destroyed=False, is_stripped=False,
                     data=raw)
    node = r._get_typetree_node()
    er = EndianBinaryReader(raw, endian=mesh_src.reader.endian)
    m = TypeTreeHelper.read_typetree(node, er, as_dict=False, assetsfile=mesh_src.assets_file,
                                     byte_size=len(raw), check_read=False)
    m.set_object_reader(r)
    return m, r


def _read_go(raw, go_src):
    r = ObjectReader(assets_file=go_src.assets_file, reader=go_src.reader, path_id=1,
                     type_id=go_src.type_id, serialized_type=go_src.serialized_type,
                     class_id=go_src.class_id, type=go_src.type,
                     byte_start=0, byte_size=len(raw), is_destroyed=False, is_stripped=False,
                     data=raw)
    node = r._get_typetree_node()
    er = EndianBinaryReader(raw, endian=go_src.reader.endian)
    g = TypeTreeHelper.read_typetree(node, er, as_dict=False, assetsfile=go_src.assets_file,
                                     byte_size=len(raw), check_read=False)
    g.set_object_reader(r)
    return g, r


def _read_smr(raw, smr_src):
    """用 SMR 的类型树读一段 raw（新网格要借它当模板：材质/AABB/骨骼都从这里来）。"""
    r = ObjectReader(assets_file=smr_src.assets_file, reader=smr_src.reader, path_id=1,
                     type_id=smr_src.type_id, serialized_type=smr_src.serialized_type,
                     class_id=smr_src.class_id, type=smr_src.type,
                     byte_start=0, byte_size=len(raw), is_destroyed=False, is_stripped=False,
                     data=raw)
    node = r._get_typetree_node()
    er = EndianBinaryReader(raw, endian=smr_src.reader.endian)
    s = TypeTreeHelper.read_typetree(node, er, as_dict=False, assetsfile=smr_src.assets_file,
                                     byte_size=len(raw), check_read=False)
    s.set_object_reader(r)
    return s


def _read_tr(raw, tr_src):
    """用 Transform 的类型树读一段 raw（本地 TRS + 父级）。"""
    r = ObjectReader(assets_file=tr_src.assets_file, reader=tr_src.reader, path_id=1,
                     type_id=tr_src.type_id, serialized_type=tr_src.serialized_type,
                     class_id=tr_src.class_id, type=tr_src.type,
                     byte_start=0, byte_size=len(raw), is_destroyed=False, is_stripped=False,
                     data=raw)
    node = r._get_typetree_node()
    er = EndianBinaryReader(raw, endian=tr_src.reader.endian)
    return TypeTreeHelper.read_typetree(node, er, as_dict=False, assetsfile=tr_src.assets_file,
                                        byte_size=len(raw), check_read=False)


def _src_channel_map(mesh):
    """源网格的「(位置, UV0) -> (法线, 切线, UV1)」表 —— 用于"能对上就照抄"。

    ⛔ v1.8.82：工具原来**重算法线**（同位置面法线求平均）并把 **UV1 覆盖成 UV0**，
       这两件事都会改变外观（硬边被抹平 / 依赖 UV1 的采样从常量变成变化）✗
       见 `turret_swap.pack_by_layout` 的说明。返回 {} 表示没有可用数据。
    """
    try:
        import extract_model as _em
        vd = mesh.m_VertexData
        data = bytes(vd.m_DataSize or b"")
        if not data:
            return {}
        N, data, chans = _em._read_mesh_data(mesh, lambda f: _em._FMT_SIZE.get(f, 4), data,
                                             padded=True)
        by = {i: (d, o, s, c) for (_a, _b), (_k, d, o, s, c, i) in chans.items()}
        if 0 not in by or 4 not in by:
            return {}

        def chan(idx):
            if idx not in by:
                return None
            d, o, s, c = by[idx]
            return _em._read_chan(data, N, o, s, c, d)

        pos, uv0 = chan(0), chan(4)
        nrm, tan, uv1 = chan(1), chan(2), chan(5)
        if not pos or not uv0:
            return {}
        out = {}
        for i in range(min(N, len(pos), len(uv0))):
            k = (round(pos[i][0], 3), round(pos[i][1], 3), round(pos[i][2], 3),
                 round(uv0[i][0], 4), round(uv0[i][1], 4))
            out.setdefault(k, (nrm[i] if nrm else None,
                               tan[i] if tan else None,
                               uv1[i] if uv1 else None))
        return out
    except Exception as e:  # noqa: BLE001
        print("[copy-full] ⚠ 源网格通道表读不出（法线/UV1 将按重算写）：%s: %s"
              % (type(e).__name__, e))
        return {}


def _find_hub_template(objs):
    """在 bundle 里找一个**现成的** AnimationHub MonoBehaviour 当类型模板。

    为什么要它：MonoBehaviour 的类型身份（`script_id` / tree_hash）在 bundle 的
    SerializedFile 类型表里，新建一个 MonoBehaviour 必须沿用**同一个**类型条目，
    否则导入端 `_match_type` 只能退化成"第一个 class_id=114 的类型" ⇒ 挂错脚本 ✗。
    返回 ObjectReader 或 None。
    """
    for o in objs:
        if o.type.name != "MonoBehaviour":
            continue
        try:
            d = o.data if getattr(o, "data", None) is not None else o.get_raw_data()
        except Exception as _e:  # noqa: BLE001
            _note_swallow('_find_hub_template@L1173', '守护块在写 d', _e)
            continue
        if d and len(d) >= 28 and struct.unpack_from("<q", d, 20)[0] == HUB_SCRIPT:
            return o
    return None


def build_copy(bundle, out_pack, new_prefab, new_name, source_root_gpid, bone_tree, meshes,
               source_path=None, hub_json=None, comp_edits=None):
    r"""patch 模式导出 .bamod：字节复制原 prefab 全部对象，只 patch 网格 + 挂点。

    bone_tree: [(name, parent_name, pos_unity, rot_unity(xyzw), scale_unity), ...] 父在前
    meshes:    [{name, positions, triangles, uv, bones[(b0,b1)每顶点], weights[(w0,w1)每顶点],
                bone_names[骨骼名列表]}]
    source_path: 可选，复制源的 prefab 内部路径（pid 失效时按它回退定位，见
                `collect_prefab_objects`）。
    comp_edits: 可选，{pid字符串: {cls, node, values}} —— ⑧ 面板改过的**其它组件**。
                按 MB 的 pid 精确命中，用 `component_edit.emit_component` 重序列化 ✓
    """
    from turret_swap import build_mesh_data
    env, sf, objs, by_pid = _load_env(bundle)
    # 源对象集
    src_objects, src_preload, src_path = collect_prefab_objects(bundle, source_root_gpid,
                                                               want_path=source_path)
    # ⛔ 真正的根 pid 以**容器条目**为准（见 container_root_pid 的说明）：
    #    复制源 pid 过期时我们是按路径回退找到条目的，若继续沿用旧 pid，
    #    manifest.root_pid 会指向包里不存在的对象 ⇒ 导入端直接判"包坏了"✗
    root_pid = container_root_pid(bundle, src_path) or source_root_gpid
    if root_pid != source_root_gpid:
        print("[copy-full] ⚠ 复制源 pid %d 已过期（这个 prefab 被导入过，容器 asset 已改指"
              "新 pid）⇒ 按容器条目改用真实根 pid %d"
              % (source_root_gpid, root_pid))
    _, _, smr_src, mesh_src, _, _, _, _, hash_lookup = _find_sources(objs, by_pid)
    hash_lookup.update(KNOWN_HASHES)
    go_src = next(o for o in objs if o.type.name == "GameObject")
    tr_src = next(o for o in objs if o.type.name == "Transform")

    world = _world_matrices(bone_tree)

    # 骨骼路径链（root 起）→ 用与 build_model 相同的算法直接算哈希。
    # ⛔ 必须这样算，不能用 KNOWN_HASHES 查表：自定义挂点名（如 Rotorangle_0 / 自建
    #    桨叶骨架）不在表里 ⇒ 旧代码 `hash_lookup[b]` 直接 KeyError ✗（v1.8.57 修）。
    _parent_of = {n: p for n, p, _, _, _ in bone_tree}
    _tree_names = [n for n, _, _, _, _ in bone_tree]
    _node = {n: (p, pos, rot, sc) for n, p, pos, rot, sc in bone_tree}

    def _path_of(name):
        parts = []
        cur = name
        for _ in range(64):
            if cur is None:
                break
            parts.append(cur)
            cur = _parent_of.get(cur)
        return list(reversed(parts))

    bone_hashes = {n: bone_hash(_path_of(n)) for n in _tree_names}

    # ---- 建名字索引：GO 名 -> pid；mesh 名 -> pid ----
    go_by_name = {}   # name -> go pid
    tr_by_go = {}     # go pid -> tr pid
    mesh_by_name = {} # name -> mesh pid
    mesh_pid_to_name = {}   # ★ pid -> mesh 名（**按 pid 反查**，⛔ 不再用 name→pid 反查：同名多实例会塌陷）
    _name_count = {}        # 名字 → 出现次数（用于"同名多实例"告警）
    for o in src_objects:
        raw = base64.b64decode(o["raw"])
        if o["type_name"] == "GameObject":
            try:
                g, _ = _read_go(raw, go_src)
                go_by_name[g.m_Name] = o["pid"]
            except Exception as _e:
                _note_swallow('build_copy@L1243', '守护块在写 (g, _)', _e)
                pass
        elif o["type_name"] == "Transform":
            try:
                r = ObjectReader(assets_file=sf, reader=sf.reader, path_id=1,
                                 type_id=tr_src.type_id, serialized_type=tr_src.serialized_type,
                                 class_id=tr_src.class_id, type=tr_src.type,
                                 byte_start=0, byte_size=len(raw), is_destroyed=False,
                                 is_stripped=False, data=raw)
                node = r._get_typetree_node()
                er = EndianBinaryReader(raw, endian=sf.reader.endian)
                t = TypeTreeHelper.read_typetree(node, er, as_dict=False, assetsfile=sf,
                                                 byte_size=len(raw), check_read=False)
                gid = t.m_GameObject.m_PathID if t.m_GameObject else 0
                tr_by_go[gid] = o["pid"]
            except Exception as _e:
                _note_swallow('build_copy@L1258', '守护块在写 r', _e)
                pass
        elif o["type_name"] == "Mesh":
            try:
                m, _ = _read_mesh(raw, mesh_src)
                mesh_by_name[m.m_Name] = o["pid"]
                mesh_pid_to_name[o["pid"]] = m.m_Name
                _name_count[m.m_Name] = _name_count.get(m.m_Name, 0) + 1
            except Exception as _e:
                _note_swallow('build_copy@L1266', '守护块在写 (m, _)', _e)
                pass

    # ★26 收口（2026-09-18）：**同名多实例必须让人看见** —— 按名字查表在"同名族"里会命中错的那个
    #   （实测：包内 15 份 `US_ACV 1`、多份 `Chassis`；`_pid_of(name)` 也按名查）⇒ 这里出声。
    _dups = sorted(n for n, c in _name_count.items() if c > 1)
    if _dups:
        print("[copy-full] ⚠ 源表里 %d 个网格名有**多实例**（按名查表不可靠，★26 同族）：%s"
              % (len(_dups), ", ".join(_dups[:8])))

    # ---- 新增节点：骨骼树里有、源 prefab 里没有的（如挂载点 Rotorangle_0）----
    # ⛔ v1.8.57 之前 copy-full **只会 patch 已存在的 Transform** ⇒ 用户新加的挂载点被
    #    静默丢掉（④ 面板里能看到、构建立刻消失）✗。这里补上：新建 GameObject +
    #    Transform、挂到父节点、写回父的 m_Children，并把新对象并入 manifest。
    tr_pid_of = {}          # 节点名 -> Transform pid（原有 + 新建）
    go_pid_of = {}          # 节点名 -> GameObject pid
    for nm, gpid in go_by_name.items():
        t = tr_by_go.get(gpid)
        if t is not None:
            tr_pid_of[nm] = t
            go_pid_of[nm] = gpid
    added = [n for n in _tree_names if n not in go_by_name]
    new_objs_extra = []     # [{pid, class_id, type_name, script_id, tree_hash, raw(b64)}]
    _MAT_FALLBACKS = []     # ★v1.12.3 安全网：源材质未带入、暂用本车材质的**逐件记录**（写进 manifest）
    _BONE_FALLBACKS = []    # ★v1.12.4（裁定 2·第一步）：为"骨骼名缺失"新建的**占位骨骼**记录（写进 manifest）
    _NODE_SKIPS = []        # ★v1.12.5（★㉖-续·A 第二步）：**丢件/降级**逐条记录（写进 manifest；空列表也照写）
    _TEX_MISSING = []       # ★v1.12.5（★㉖-续·C）：材质解析成功、但其 Texture2D 不在包内的**逐条**记录
    _SWALLOWS.clear()   # ★甲2：名册是模块级的，⛔ 同进程重复构建不许累加
    _TEX_UNPARSED = []      # ★甲1（2026-09-20）：texEnv **解不开** ⇒ **判不了**（⛔ 与"缺图"分档，⛔ 不当"没问题"）
    _EXPECT_TR = {}         # ★v1.12.5（★㉕-续）：本产物**写出的 transform 期望值**（tr pid → (名,pos,rot,sc)）
    _EXPECT_MATS = {}       # ★v1.12.5（★㉕-续）：本产物**写出的材质绑定期望值**（SMR pid → [材质 pid]）
    _MAT_INJECTED = []      # ★v1.12.5（★㉖-续·B·甲）：跨包**带入**的材质/贴图逐条记录
    _MAT_SRC_UNRESOLVED = []  # ★v1.12.5：跨包但**源字节缺失**（⇒ 仍走安全网）的逐条记录
    patch_children = {}     # 原有 Transform pid -> [新子 Transform pid]

    def _dump_new(pid):
        """把刚写到 sf 里的新对象导成 manifest 条目（**幂等**：同 pid 覆盖，不重复）。

        ⛔ 必须用 `.data`：`get_raw_data()` 对**刚 _save 出**的对象是按 byte_size 从流里
        重读的，而新建 reader 的 byte_size=0 ⇒ 只会拿到空字节 ✗（实测踩坑）。
        ⛔ 同 pid 不能出现两条：导入端 `pid_map` 只会记住最后一次映射，
        前面那条会变成没人引用的孤儿对象。
        """
        o = sf.objects[pid]
        raw_new = o.data if getattr(o, "data", None) is not None else o.get_raw_data()
        # ★[★㉖-续·D P13] **重 dump 不是"新对象"** ⇒ 既有条目的 `script_id`/`tree_hash` 要**继承**，
        #   ⛔ 一律写 None 会把它们抹掉（实测：SMR 的 `tree_hash=3b77c18a…` 被抹成 None；对
        #   MonoBehaviour(114) 更危险 —— `import_pack._match_type` 靠它匹配类型条目）。
        #   ⚠ `new_objects` 在"网格循环"阶段**还没赋值** ⇒ 直接引用会抛
        #   `NameError: cannot access free variable`（＝ ★㉕-续 踩过的那个闭包坑）⇒ 必须兜住。
        _sid, _th = None, None
        try:
            _pools = (new_objs_extra, new_objects)
        except NameError:                                                 # noqa: PERF203
            _pools = (new_objs_extra,)
        for _pool in _pools:
            for _e in _pool:
                if int(_e.get("pid") or 0) == int(pid):
                    _sid, _th = _e.get("script_id"), _e.get("tree_hash")
                    break
            if _sid or _th:
                break
        for e in [x for x in new_objs_extra if x["pid"] == pid]:
            new_objs_extra.remove(e)
        new_objs_extra.append({
            "pid": pid, "class_id": int(o.class_id), "type_name": o.type.name,
            "script_id": _sid, "tree_hash": _th,
            "raw": base64.b64encode(raw_new).decode("ascii")})

    # ⛔⛔ v1.8.80：原版各网格的**绑定姿势 / 骨骼名哈希**（按骨骼名索引）
    #    网格被替换时**必须照抄**这些值，绝不能重算。
    #    原版烘焙出的 `m_BindPose` **不一定等于** `inverse(节点世界矩阵)` ——
    #    实测 ACV：`Shield`/`Shield_01`/`apparel*` 这些节点**带旋转**（±74°/80°/96°），
    #    但原版 BindPose 的旋转部分是**单位阵**。按"重算"写回去 ⇒ 静止时
    #    `v' = W_node · BindPose_new · v ≠ v` ⇒ 绑在这些骨骼上的几何**脱离模型** ✗
    #    （现场症状：正面护甲偏 7.68、后门/裙板偏 5.53；轮子那 16 根旋转≈0 的骨骼正常）
    #    重算只作兜底：原版没有该骨骼名时（如新加的 `rot_blade_1`）✓
    src_bind, src_hash = {}, {}
    _mesh_by_pid = {o["pid"]: o for o in src_objects if o["type_name"] == "Mesh"}
    for o in src_objects:
        if o["type_name"] != "SkinnedMeshRenderer":
            continue
        try:
            s0 = _read_smr(base64.b64decode(o["raw"]), smr_src)
            mo = _mesh_by_pid.get(s0.m_Mesh.m_PathID) if s0.m_Mesh else None
            if mo is None:
                continue
            m0, _r0 = _read_mesh(base64.b64decode(mo["raw"]), mesh_src)
            bps = list(getattr(m0, "m_BindPose", None) or [])
            hs = list(getattr(m0, "m_BoneNameHashes", None) or [])
            for i, b in enumerate(s0.m_Bones or []):
                tpid = b.m_PathID if b else 0
                nm = next((k for k, v in tr_pid_of.items() if v == tpid), None)
                if not nm or i >= len(bps):
                    continue
                src_bind.setdefault(nm, bps[i])
                if i < len(hs):
                    src_hash.setdefault(nm, hs[i])
        except Exception as e:  # noqa: BLE001
            # ⛔ 不要静默！这里 swallow 过一次真 bug（块里用了不存在的 `pid_of`
            #    ⇒ src_bind 永远为空、照抄逻辑整个失效，却只表现为"照抄 0 根"）✗
            print("[copy-full] ⚠ 读原版绑定数据失败（这条 SMR 跳过）：%s: %s"
                  % (type(e).__name__, e))
            continue
    if src_bind:
        print("[copy-full] 原版绑定数据：%d 根骨骼的 BindPose、%d 根的名字哈希（替换网格时照抄）"
              % (len(src_bind), len(src_hash)))

    # ⛔ v1.8.81：源 prefab 里各**骨骼节点**的世界矩阵 —— 用于"保持外观"的绑定姿势换算。
    #    只照抄 BindPose 是不够的：用户**移动过**挂载点时（该挂载点已在上一次构建里存在，
    #    于是它这次就出现在"原版"里），照抄旧值 ⇒ **节点动了、绑定没动** ⇒ 那块几何整体
    #    偏移（实测：把 `rot_blade_1` 抬高 2.567 后，旋翼在游戏里**悬空 2.567** ✗）。
    #    正确做法 = 保持外观：`BindPose_new = inverse(W_new) · W_old · BindPose_old` ✓
    #      · 节点没动（W_new == W_old）⇒ 结果就是照抄 ✓
    #      · 节点动了、且原绑定自洽 ⇒ inverse(W_new) ⇒ **几何留在原位、只有轴心跟着移动** ✓
    src_world = {}
    try:
        _loc, _nm2, _tr_go2 = {}, {}, {}
        for o in src_objects:
            if o["type_name"] == "GameObject":
                g, _ = _read_go(base64.b64decode(o["raw"]), go_src)
                _nm2[o["pid"]] = g.m_Name
        for o in src_objects:
            if o["type_name"] != "Transform":
                continue
            t = _read_tr(base64.b64decode(o["raw"]), tr_src)
            gid = t.m_GameObject.m_PathID if t.m_GameObject else 0
            fa = t.m_Father.m_PathID if (t.m_Father and t.m_Father.m_PathID) else 0
            _tr_go2[o["pid"]] = gid
            _loc[o["pid"]] = (gid, fa,
                              (t.m_LocalPosition.x, t.m_LocalPosition.y, t.m_LocalPosition.z),
                              (t.m_LocalRotation.x, t.m_LocalRotation.y,
                               t.m_LocalRotation.z, t.m_LocalRotation.w),
                              (t.m_LocalScale.x, t.m_LocalScale.y, t.m_LocalScale.z))
        ordered, done = [], set()
        for _ in range(len(_loc) + 2):
            for pid, (gid, fa, p, q, s) in _loc.items():
                if pid in done:
                    continue
                if fa and fa in _loc and fa not in done:
                    continue
                # ⛔ 父级名必须由 **Transform pid → GO pid → 名字** 两步查：
                #    直接把 Transform pid 丢给 GO 名字表会得到 None ⇒ 父级不乘 ✗
                #    （实测：Lantenna/Rantenna 少了 body 的 1.2174，绑定姿势就错了）
                pname = _nm2.get(_tr_go2.get(fa, -1)) if fa else None
                ordered.append((_nm2.get(gid, ""), pname, p, q, s))
                done.add(pid)
            if len(done) == len(_loc):
                break
        src_world = _world_matrices(ordered)
    except Exception as e:  # noqa: BLE001
        print("[copy-full] ⚠ 源节点世界矩阵算不出（移动过的骨骼会退化为直接重算）：%s: %s"
              % (type(e).__name__, e))

    # 源 prefab 里没有同名的用户网格 = **全新网格**（例如 ACV 的旋翼桨叶）
    src_mesh_names = set(mesh_by_name.keys())
    new_meshes = [m for m in meshes
                  if m.get("name") and m.get("name") not in src_mesh_names]

    if added or new_meshes:
        # ⛔ 用 `sf.objects.keys()`（**当前**对象表）而不是缓存的 `by_pid` 快照：
        #    同一会话里第二次构建时，`by_pid` 还是加载时的旧快照，不含上一次新建的对象
        #    ⇒ 每次都从 BASE+0 重新分配、把 `sf.objects[BASE+0]` 覆盖掉 ✗
        alloc = _make_alloc(set(sf.objects.keys()), MOD_PID_BASE)
        go_src2 = next(o for o in objs if o.type.name == "GameObject")
        tr_src2 = next(o for o in objs if o.type.name == "Transform")

    if added:
        for nm in _tree_names:          # bone_tree 保证父在前
            if nm not in added:
                continue
            par = _parent_of.get(nm)
            ptr = tr_pid_of.get(par, 0) if par else 0
            if par is None:
                # ⛔ v1.8.77：无父级的新节点会变成 prefab 里的独立根节点（与车体平级）
                #    ⇒ 游戏里它不会跟着车动（用户看到的"旋翼飘在原地"就是这么来的）。
                print("[copy-full] ⚠ 新节点 %s **没有父级** ⇒ 会写成独立根节点、不跟着车体 ✗"
                      "（② 面板添加挂载点时要先在大纲视图选中父挂载点，如 body）" % nm)
            elif not ptr:
                # 父不在树里（最常见：② 面板添加挂载点时选中的是**网格**而不是挂载点 Empty，
                # 于是父子关系没进树）⇒ 明确报出来，别静默丢节点 ✗
                print("[copy-full] ⚠ 新节点 %s 的父 %r 不在挂载点树里，已跳过"
                      "（添加挂载点时要选中**挂载点 Empty** 作父级）" % (nm, par))
                continue
            gpid, trpid = _make_go(sf, alloc, go_src2, tr_src2, nm, ptr)
            # _make_go 只给单位 TRS；这里按 Blender 侧的值写回
            node = _node.get(nm)
            if node:
                _, pos, rot, sc = node
                t = _read_from_data(sf.objects[trpid])
                t.m_LocalPosition = Vector3f(pos[0], pos[1], pos[2])
                t.m_LocalRotation = Quaternionf(rot[0], rot[1], rot[2], rot[3])
                t.m_LocalScale = Vector3f(sc[0], sc[1], sc[2])
                sf.objects[trpid].save_typetree(t)
                _EXPECT_TR[trpid] = (nm, tuple(pos), tuple(rot), tuple(sc))   # ★㉕-续：记期望值
            go_pid_of[nm] = gpid
            tr_pid_of[nm] = trpid
            if par and ptr:
                # 父可能是**原有节点**（主循环里补 m_Children）也可能是**同为新建的节点**
                # （下面的后处理里直接写）⇒ 两种都登记，由后处理按 tr pid 分流。
                # ⛔ 旧写法只登记「父在 go_by_name 里」的情形 ⇒ 新节点套新节点时父的
                #    m_Children 为空 ✗（Rotorangle_1 挂在 Rotorangle_0 下就踩这个坑）。
                patch_children.setdefault(ptr, []).append(trpid)
            _dump_new(gpid)
            _dump_new(trpid)

    # ---- 新增网格：Mesh + GameObject + SkinnedMeshRenderer（挂到 root）----
    # ⛔ 同样是 v1.8.57 才有的：以前 copy-full 只替换**源 prefab 里同名**的网格，
    #    用户自己新建的桨叶网格会被静默丢掉 ✗ —— 而「给 ACV 加旋翼」恰恰需要新网格。
    #    蒙皮网格的变形只取决于骨骼，所以挂点选 root 不影响观感。
    new_smr_pids = []
    if new_meshes:
        from turret_swap import build_mesh_data
        attach_parent = "root" if "root" in tr_pid_of else _tree_names[0]
        # ★v1.12.4（裁定 2·第一步）：**骨骼名缺失也不丢件** —— 新网格引用的骨骼名若没能在上面
        #   建出来（最常见：它的父链在挂载点树里断掉 ⇒ 上面那个 `continue` 把它跳过了），
        #   这里**按 tree 的局部变换强建占位骨骼**、挂到 `attach_parent`，并记进 `bone_fallbacks`。
        #   ⛔ 底线：新网格对象**不许消失**（与材质维度的安全网同口径）。
        _need_bones = []
        for _m in new_meshes:
            for _b in (_m.get("bone_names") or []):
                if _b and _b not in tr_pid_of and _b not in _need_bones:
                    _need_bones.append(_b)
        if _need_bones:
            print("[copy-full] ⚠ %d 个骨骼名在目标树里缺失：%s ⇒ **建占位骨骼**（挂到 %s；⛔ 不丢件）"
                  % (len(_need_bones), ", ".join(_need_bones[:8]), attach_parent))
            for _b in _need_bones:
                _par = _parent_of.get(_b)
                _ptr = tr_pid_of.get(_par, 0) if _par else 0
                if not _ptr:
                    _ptr = tr_pid_of[attach_parent]
                    print("   ⓘ 占位骨骼 %s 的父 %r 不在树里 ⇒ 改挂 %s" % (_b, _par, attach_parent))
                _g, _t = _make_go(sf, alloc, go_src2, tr_src2, _b, _ptr)
                _nd = _node.get(_b)
                if _nd:
                    _, _pos, _rot, _sc = _nd
                    _tt = _read_from_data(sf.objects[_t])
                    _tt.m_LocalPosition = Vector3f(_pos[0], _pos[1], _pos[2])
                    _tt.m_LocalRotation = Quaternionf(_rot[0], _rot[1], _rot[2], _rot[3])
                    _tt.m_LocalScale = Vector3f(_sc[0], _sc[1], _sc[2])
                    sf.objects[_t].save_typetree(_tt)
                    _EXPECT_TR[_t] = (_b, tuple(_pos), tuple(_rot), tuple(_sc))   # ★㉕-续：记期望值
                else:
                    print("   ⓘ 占位骨骼 %s 在 tree 里也没条目 ⇒ 用单位变换（形变可能偏，已在清单标记）" % _b)
                tr_pid_of[_b] = _t
                go_pid_of[_b] = _g
                patch_children.setdefault(_ptr, []).append(_t)
                _dump_new(_g)
                _dump_new(_t)
                _BONE_FALLBACKS.append({"bone": _b, "parent": _par or attach_parent,
                                        "in_tree": bool(_nd)})
        # ⛔ v1.8.76：新网格的 SMR 模板优先取**目标 prefab 自己**的 SMR。
        #    以前用 `_find_sources` 随便挑的"bundle 里第一个 SMR"当模板 ⇒ 新网格继承到
        #    **别人的材质**：实测给 ACV 加的旋翼拿到了 `RU_1BTR80_82 1`（BTR-80 的贴图）✗
        #    现在优先用「本次要替换的那个网格」的 SMR ⇒ 新网格继承本车材质 ✓
        _user_names = {mm.get("name") for mm in meshes if mm.get("name")}
        pref_raw = None
        for o in src_objects:
            if o["type_name"] != "SkinnedMeshRenderer":
                continue
            try:
                s_ = _read_smr(base64.b64decode(o["raw"]), smr_src)
                mp = s_.m_Mesh.m_PathID if s_.m_Mesh else 0
                nm_ = mesh_pid_to_name.get(mp)   # ★ 按 pid 反查（⛔ 旧写法 next((k for k,v in mesh_by_name.items() if v==mp), None) 在同名多实例时会挑错名）
                if nm_ in _user_names:
                    pref_raw = base64.b64decode(o["raw"])
                    break
                if pref_raw is None:
                    pref_raw = base64.b64decode(o["raw"])
            except Exception as _e:  # noqa: BLE001
                _note_swallow('build_copy@L1534', '守护块在写 s_', _e)
                continue
        for mesh in new_meshes:
            bn = list(mesh.get("bone_names") or [])
            _bn_synth = False
            if not bn:
                # ★v1.12.5（★㉖-续·A **第二步**）：`bn` 为空 ⇒ ⛔ **不再丢件**（原来是一句静默 `continue`）
                #   **保件**写法＝按"静止网格"建：合成一根骨骼＝**挂点自己**（它的 pid 与世界矩阵必然都在），
                #   顶点权重 1 绑到索引 0 ⇒ 绑姿取既有公式 `inv(world[挂点])` ⇒ 几何落在**预制体根空间**
                #   （与本工具导出侧的 `ba_mesh_export_matrix` 口径一致）。⛔ 不形变、⛔ 不猜源索引
                #   （`bn` 空时 `mesh["bones"]/weights` 通常也是空的，照老路取索引会 IndexError）。
                #   ⚠ 形变/位置以实机为准（本类网格没有骨骼信息）——清单 `node_skips` 里可查。
                _NODE_SKIPS.append({"mesh": mesh.get("name"), "missing_bones": [],
                                    "mode": "no_bone_names", "synth_bone": attach_parent})
                print("[copy-full] ⚠ 新网格 %s **完全没有骨骼名**（bn 为空）⇒ **保件**："
                      "按静止网格建（合成骨骼＝%s、权重 1）⛔ 不丢件；已记清单 `node_skips`"
                      % (mesh.get("name"), attach_parent))
                bn = [attach_parent]
                _bn_synth = True
            if not all(b in tr_pid_of for b in bn):
                # ★v1.12.5：这一条**仍然是丢件**（占位骨骼也没补上）—— 但⛔ **不静默**：
                #   清单里点名，让"丢件"与"保件"在产物里**可区分**（本条原始诉求）
                _miss_b = [b for b in bn if b not in tr_pid_of]
                _NODE_SKIPS.append({"mesh": mesh.get("name"), "missing_bones": _miss_b,
                                    "mode": "missing_bones_dropped", "synth_bone": None})
                print("[copy-full] ⛔ 新网格 %s 的骨骼名有缺失（占位骨骼也没补上）：%s ⇒ **丢件**"
                      "（已记清单 `node_skips`）" % (mesh.get("name"), ", ".join(_miss_b)))
                continue
            # ── ★26 完整修法（2026-09-18 中枢批准）：**材质按源 pid 带入**，⛔ 不继承"本车材质" ──
            _src_mats = [int(x) for x in (mesh.get("mats") or []) if str(x).strip()]
            _src_mat_names = list(mesh.get("mat_names") or [])
            _tgt_mats, _missing_mats = ([], [])
            if _src_mats or _src_mat_names:
                # ★[★㉖-续·D 2026-09-19] 判据改成「**本包（目标包）会不会带它**」：旧写法
                #   `_p in _sf.objects` 查的是**源（被补丁的）包** ⇒ 源包里有、但不会被 dump 进
                #   manifest 的材质被判"不需要带入" ⇒ 产物沿用源 pid ⇒ **悬空** ✗（用户实测：
                #   `Ah_1z` 的 m_Materials 指向包外 pid；随后 `import_pack.py` L533 ValueError）。
                #   ⛔ 不用 `sf.objects`：以**本包将要写入的对象**（`new_objs_extra`）为准。
                def _pid_exists(_p, _sf=sf):
                    r"""★甲1 修（2026-09-20 中枢放行）：**三态** —— True／False／**None＝判不了**。
                    ⛔ 旧写法 `except: return False` 把"**检查抛异常**"吞成"**不存在**" ⇒ **直接改分支**
                    （源材质被误判为"包里没有" ⇒ 走安全网/顶替宿主材质），且日志里看不出来 ✗。
                    """
                    try:
                        return any(int(e["pid"]) == int(_p) for e in new_objs_extra)
                    except Exception as e:                              # noqa: BLE001
                        print("⚠ [copy-full] `_pid_exists(pid=%s)` **判不了**（%s: %s）"
                              "⇒ 按『未知』处理（⛔ 不当『不存在』）" % (_p, type(e).__name__, e))
                        return None

                def _pid_by_name(_nm, _sf=sf, _cache={}):
                    if not _nm:
                        return 0
                    if _nm in _cache:
                        return _cache[_nm]
                    _hit = 0
                    for _o in _sf.objects.values():
                        if getattr(_o.type, "name", "") != "Material":
                            continue
                        try:
                            if getattr(_o.read(), "m_Name", None) == _nm:
                                _hit = _o.path_id
                                break
                        except Exception as _e:                               # noqa: BLE001
                            _note_swallow('_pid_by_name@L1597', '守护块在写 _hit', _e)
                            continue
                    _cache[_nm] = _hit
                    return _hit

                _tgt_mats, _missing_mats = resolve_source_mats(_src_mats, _src_mat_names,
                                                               _pid_exists, _pid_by_name)
            # ★v1.12.5（★㉖-续·B·甲）：**跨包带入** —— 源材质不在目标包里时，若导出清单带了它的源字节
            #   （`mat_srcs`）⇒ 真拷进目标包并按 pid 重映射；⛔ 没带字节 ⇒ 出声并保留安全网（不静默）
            #   ★ 向后兼容：老 clist 没有 `mat_srcs` ⇒ 这一段整体不生效（行为与改动前一致）
            # ★[★㉖-续·D] **字节兜底**：Blender 侧 `_collect_mat_srcs`（`__init__.py` L1865）在
            #   「导入包 == 构建包」时**只写 pid/名、不收集源字节** ⇒ 光改判据仍会悬空（没有
            #   `raw_b64` 可注入）。而本函数此刻**正开着源包**（`sf`）⇒ 直接从它补齐，最可靠。
            _src_entries = _fill_mat_srcs_from_bundle(sf, mesh.get("mat_srcs"),
                                                      _src_mats, _src_mat_names,
                                                      bundle=bundle)
            if _missing_mats and _src_entries:
                _pack_now = lambda _p: any(int(e["pid"]) == int(_p)
                                           for e in new_objs_extra)   # ★ 本包口径
                _plan, _unres = plan_mat_injections(_src_entries, _pack_now)
                for _u in _unres:
                    _MAT_SRC_UNRESOLVED.append(dict(_u, mesh=mesh.get("name")))
                    print("[copy-full] ⚠ 新网格 %s 的源材质**跨包且未带源字节**：%s ⇒ 仍走安全网"
                          "（清单 `mat_src_unresolved`）" % (mesh.get("name"), _u.get("name") or _u.get("src_pid")))
                if _plan:
                    # ★[★㉖-续·D P8] `name_in_pack` **必须传**：⛔ 不传 ⇒ 注入器退回
                    #   `_find_named(sf, ...)`（**源包**口径）⇒"同名复用"挑中一张**不在包里**的图
                    #   ⇒ 材质带进来了、texEnv 仍指包外（实测探针 real_tex 腿：
                    #   `Layer_A97CDC25=reuse_name` 而 `new_tex=1834790033242913627` 不在包内）。
                    #   ⚠ 此处的"包"＝`new_objs_extra`（`new_objects` 要到装配 manifest 时才建）
                    _new_pids, _inj_rep = inject_material_entries(
                        sf, alloc, _plan, in_pack=_pack_now,
                        name_in_pack=make_tex_name_in_pack(sf, new_objs_extra))
                    if _new_pids:
                        for _r in _inj_rep:
                            _MAT_INJECTED.append(dict(_r, mesh=mesh.get("name")))
                            # ★★ 必须把**带入的对象**也导进 manifest：⛔ 否则产物引用了"不在包里的 pid"
                            #    （实测：B1④ 判据"取不到新材质 raw"就是这么来的 —— 夹具抓出来的真 bug）
                            _dump_new(int(_r["new_pid"]))
                            for _tx in (_r.get("texs") or []):
                                # ★[★㉖-续·D] `copied_bytes`（内联图逐字节照抄）也要 dump
                                if _tx.get("how") in ("embedded", "copied_bytes") \
                                        and _tx.get("new_tex"):
                                    _dump_new(int(_tx["new_tex"]))
                        # ★[★㉖-续·D P8] **`foreign_ref` 不许静默**：图既不在包内、又没带源字节
                        #   ⇒ 老老实实落进 ★㉖-续·C 的 `tex_missing` 通道（实测旧写法：2 张图被
                        #   摘掉 ⇒ 零记录零告警、`tex_missing` 还是 [] ✗）
                        _miss_rows, _n_ok = record_injected_tex_deps(
                            _inj_rep, mesh.get("name"), sink=_TEX_MISSING)
                        if _n_ok and not _miss_rows:
                            print("[copy-full] ✓ 新网格 %s：带入材质的贴图依赖**全在包里**"
                                  "（%d 张逐条查过）" % (mesh.get("name"), _n_ok))
                        # ⚠ 必须按**源 pid/源名**把已带入的从 `_missing_mats` 里摘掉
                        #   （`_new_pids` 是**新** pid，拿它去比 `_missing_mats` 里的源 pid ⇒ 永远不匹配 ✗）
                        import re as _re
                        _done_src = set(int(_r["src_pid"]) for _r in _inj_rep)
                        _done_names = set(str(_r["name"]) for _r in _inj_rep if _r.get("name"))
                        _keep = []
                        for _m in _missing_mats:
                            _mm = _re.search(r"pid=(-?\d+)", str(_m))
                            if _mm and int(_mm.group(1)) in _done_src:
                                continue
                            _nm = str(_m).split("(pid=")[0].strip()
                            if _nm and _nm in _done_names:
                                continue
                            _keep.append(_m)
                        _missing_mats = _keep
                        _tgt_mats = list(_tgt_mats) + list(_new_pids)
                        print("[copy-full] ✓ 新网格 %s：带入 %d 个源材质 ⇒ 绑定改为带入后的 pid（%s）；"
                              "仍未解决 %d 项" % (mesh.get("name"), len(_new_pids),
                                                ", ".join("pid=%d" % _p for _p in _new_pids), len(_missing_mats)))
            _mat_mode = ("no_source_info" if not (_src_mats or _src_mat_names)
                         else ("source" if (_tgt_mats and not _missing_mats)
                               else ("fallback_host" if _missing_mats else "source")))
            # ★v1.12.5（★㉖-续·C）：**贴图依赖缺失检查** —— 材质解析成功、但它指向的 Texture2D
            #   不在目标包里 ⇒ 以前**零告警**（进游戏才发现发白/贴图不对）⇒ 逐个 texEnv 查、缺则点名＋标记
            if _mat_mode == "source" and _tgt_mats:
                try:
                    # ★[★㉖-续·D P8] 判据改成**包内口径**：
                    #   ① 对象从**包内 dump 的 raw** 读（刚注入的 reader `byte_size==0` ⇒
                    #      `read()` 报 `Expected to read 0 bytes, but only read 4892 bytes`
                    #      ⇒ 这条检查以前**整段判不了**、日志里一句都没有）
                    #   ② `pid_exists` ＝ **本包会不会带它**（⛔ 不再 `_p in sf.objects` —— 那是
                    #      **源包**口径：源包里有、产物里没有的图会被判"在"，正是 ★㉖-续·D 的病根）
                    _pack_raw = {}
                    for _o in list(new_objs_extra):
                        if _o.get("raw") is not None:
                            _pack_raw[int(_o["pid"])] = _o["raw"]
                    _tm = check_material_texture_deps(
                        _tgt_mats,
                        lambda _p: read_pack_object(sf, _pack_raw, "Material", _p),
                        lambda _p: int(_p) in _pack_raw,
                        mesh.get("name"), _mat_mode)
                    if _tm:
                        _MISS = [x for x in _tm if x.get("kind") != "unparsed"]
                        _UNP = [x for x in _tm if x.get("kind") == "unparsed"]
                        _TEX_MISSING.extend(_MISS)
                        _TEX_UNPARSED.extend(_UNP)
                        print("[copy-full] ⚠ 新网格 %s 的材质**贴图依赖**：缺失 %d 处 ／ **解不开 %d 处（判不了）** "
                              "⇒ ⛔ 已分别记清单 `tex_missing` / `tex_unparsed`"
                              % (mesh.get("name"), len(_MISS), len(_UNP)))
                        for _x in _MISS[:4]:
                            print("      ✗ 缺图：%s@%s(pid=%s)" % (_x["prop"], _x["material"], _x["tex_pid"]))
                        for _x in _UNP[:4]:
                            print("      ? 判不了：%s@%s（%s）" % (_x["prop"], _x["material"], _x.get("why")))
                    else:
                        print("[copy-full] ✓ 新网格 %s 的材质贴图依赖**全在包里**（逐个 texEnv 查过）"
                              % mesh.get("name"))
                except Exception as _e:                              # noqa: BLE001
                    print("⚠ [copy-full] 贴图依赖检查失败（%s: %s）⇒ **判不了**（⛔ 不当通过）"
                          % (type(_e).__name__, _e))
            # ★v1.12.3 安全网（中枢裁定）：⛔ **绝不丢件** —— 源材质映射不到时，
            #   保住这个网格对象、暂用本车材质，并在此**逐项点名**（清单标记见 manifest.mat_fallbacks）
            if _mat_mode == "fallback_host":
                _MAT_FALLBACKS.append({"mesh": mesh.get("name"), "missing": list(_missing_mats),
                                       "mode": "fallback_host"})
                print("[copy-full] ⚠ 新网格 %s 的源材质**映射不到目标包**：%s ⇒ "
                      "**安全网：对象保留、暂用本车材质**（★26 下批＝把源材质/贴图一并带入；本版⛔ 不丢件）"
                      % (mesh.get("name"), ", ".join(_missing_mats)))
            if _bn_synth or (len(mesh.get("bones") or []) < len(mesh["positions"])
                             or len(mesh.get("weights") or []) < len(mesh["positions"])):
                # ★v1.12.5：bn 为空（或源骨骼/权重索引不够长）⇒ **权重全给骨骼 0**（⛔ 不猜索引、⛔ 不越界）
                skin = [(0, 0, 1.0, 0.0)] * len(mesh["positions"])
                if not _bn_synth:
                    _NODE_SKIPS.append({"mesh": mesh.get("name"), "missing_bones": [],
                                        "mode": "skin_index_short", "synth_bone": bn[0]})
                    print("[copy-full] ⚠ 新网格 %s 的骨骼索引/权重长度不足（bones=%d/weights=%d < 顶点=%d）"
                          "⇒ 权重全给骨骼 0（已记清单 `node_skips`）"
                          % (mesh.get("name"), len(mesh.get("bones") or []),
                             len(mesh.get("weights") or []), len(mesh["positions"])))
            else:
                skin = [(int(mesh["bones"][i][0]), int(mesh["bones"][i][1]),
                         float(mesh["weights"][i][0]), float(mesh["weights"][i][1]))
                        for i in range(len(mesh["positions"]))]
            faces = []
            ui = 0
            for tt in range(0, len(mesh["triangles"]), 3):
                a, b, c = (mesh["triangles"][tt], mesh["triangles"][tt + 1],
                           mesh["triangles"][tt + 2])
                faces.append(((a, ui), (b, ui + 1), (c, ui + 2)))
                ui += 3
            # ⛔ v1.8.76：新网格也照模板网格自己的通道表写（同上）
            #    `m = mesh_src.read()` 必须先执行 —— 布局要从它的 m_Channels 读
            m = mesh_src.read()
            vbytes, ibytes, vcount = build_mesh_data(
                mesh["positions"], mesh["uv"], faces, skin, skin_mode=2,
                layout_from=m)
            m.m_Name = mesh["name"]
            m.m_VertexData.m_VertexCount = vcount
            m.m_VertexData.m_DataSize = vbytes
            m.m_IndexBuffer = list(ibytes)
            m.m_IndexFormat = 1 if vcount > 65535 else 0
            if m.m_SubMeshes:
                sub = m.m_SubMeshes[0]
                sub.indexCount = vcount
                sub.firstByte = 0
                sub.firstVertex = 0
                sub.vertexCount = vcount
                sub.topology = 0
            # ⛔ v1.8.80：新网格若绑到**原版已有**的骨骼上，绑定姿势/哈希也要照抄原版
            #    （重算 != 原版，会让这块几何相对车体错位 —— 见 src_bind 处的说明）
            m.m_BoneNameHashes = [(src_hash[b] if b in src_hash else bone_hashes[b]) & 0xFFFFFFFF
                                  for b in bn]
            m.m_RootBoneNameHash = ((bone_hashes.get("body", m.m_BoneNameHashes[0])
                                     if "body" in bn else m.m_BoneNameHashes[0])
                                    & 0xFFFFFFFF)
            m.m_BindPose = [
                (_mul(_invert(world[b]), _mul(src_world[b], src_bind[b]))
                 if (b in src_bind and b in src_world and b in world)
                 else _invert(world[b]))
                for b in bn]
            mesh_pid = _save(sf, mesh_src, alloc(), m).path_id
            _dump_new(mesh_pid)

            mgpid, mgtrpid = _make_go(sf, alloc, go_src2, tr_src2,
                                      mesh["name"], tr_pid_of[attach_parent])
            patch_children.setdefault(tr_pid_of[attach_parent], []).append(mgtrpid)
            _dump_new(mgpid)
            _dump_new(mgtrpid)

            s = _read_smr(pref_raw, smr_src) if pref_raw else smr_src.read()
            # ★26 完整修法（2026-09-18 中枢批准）：模板**只借布局**（通道/AABB/骨骼在下面覆写）；
            #   `m_Materials` 改成**源 pid 映射结果**（`_tgt_mats`）⇒ ⛔ 不再继承本车材质。
            if _mat_mode == "source" and _tgt_mats:
                s.m_Materials = [PPtr(m_FileID=0, m_PathID=_p, assetsfile=sf) for _p in _tgt_mats]
                print("[copy-full] ✓ 新网格 %s 材质 = **源件材质族**（%s）"
                      % (mesh["name"], ", ".join("pid=%d" % _p for _p in _tgt_mats)))
            elif _mat_mode == "fallback_host":
                print("[copy-full] ⓘ 新网格 %s 材质 = **本车材质**（★安全网：材质未带入，已在清单 "
                      "`mat_fallbacks` 标记；⛔ 对象未丢）" % mesh["name"])
            # ★26 收口（2026-09-18）：新 SMR 的模板来自 `pref_raw`＝**目标车**那份 SMR ⇒
            #   它的 `m_Materials`（本车材质）会被整份继承 ⇒ 实测「给 ACV 加 AH-1Z 旋翼」时旋翼拿到
            #   车体材质族 `US_ACV 1` ✗。⛔ 这里**不静默**：把"新网格名 ＋ 继承到的材质"打出来，
            #   让人一眼看到"源网格自己的材质没有带入"（清单里没有 `mats` 的老流程才走这条）。
            _inh = []
            try:
                for _mr in (getattr(s, "m_Materials", None) or []):
                    _p = _mr.m_PathID if _mr else 0
                    if not _p:
                        continue
                    # ★ 修 子⑥ 观察①：从**正在写的 assets 文件**取名字（旧写法用 `smr_src.objects`
                    #   —— 它是 ObjectReader、没有 `.objects` ⇒ 恒 TypeError ⇒ 日志永远只印 pid）
                    _nm = mat_name_of(_p, sf)
                    _inh.append("%s(pid=%d)" % (_nm or "（名读不出）", _p))
            except Exception as _e:                                     # noqa: BLE001
                print("⚠ [copy-full] 列模板材质失败（%s: %s）⇒ 继承情况未知" % (type(_e).__name__, _e))
            print("[copy-full] %s 新网格 %s 的**该 SMR 材质**（%s）：%s"
                  % ("ⓘ" if _mat_mode == "source" else "⚠", mesh["name"],
                     "已按源 pid 绑定" if _mat_mode == "source" else "未带入·用目标车模板材质",
                     ", ".join(_inh) or "（模板未带材质）"))
            if _mat_mode != "source":
                print("[copy-full] ⚠↑ 源网格自己的材质**没有带入**（★26：映射不到 ⇒ 安全网）")
            s.m_Mesh = PPtr(m_FileID=0, m_PathID=mesh_pid, assetsfile=sf)
            s.m_GameObject = PPtr(m_FileID=0, m_PathID=mgpid, assetsfile=sf)
            s.m_Bones = [PPtr(m_FileID=0, m_PathID=tr_pid_of[b], assetsfile=sf) for b in bn]
            s.m_RootBone = PPtr(m_FileID=0,
                                m_PathID=(tr_pid_of[bn[0]] if _bn_synth
                                          else tr_pid_of.get("body", tr_pid_of[bn[0]])),
                                assetsfile=sf)   # ★v1.12.5：合成骨骼时根骨骼必须＝它自己（否则 RootBone 不在 Bones 里）
            xs = [p[0] for p in mesh["positions"]]
            ys = [p[1] for p in mesh["positions"]]
            zs = [p[2] for p in mesh["positions"]]
            if xs:
                s.m_AABB.m_Center = Vector3f((min(xs) + max(xs)) / 2,
                                             (min(ys) + max(ys)) / 2,
                                             (min(zs) + max(zs)) / 2)
                s.m_AABB.m_Extent = Vector3f((max(xs) - min(xs)) / 2 or 0.5,
                                             (max(ys) - min(ys)) / 2 or 0.5,
                                             (max(zs) - min(zs)) / 2 or 0.5)
            smr_pid = _save(sf, smr_src, alloc(), s).path_id
            new_smr_pids.append(smr_pid)
            if _tgt_mats:
                _EXPECT_MATS[smr_pid] = [int(_p) for _p in _tgt_mats]   # ★㉕-续：记材质绑定期望值
            _dump_new(smr_pid)
            mg = _read_from_data(sf.objects[mgpid])
            mg.m_Component.append(ComponentPair(
                component=PPtr(m_FileID=0, m_PathID=smr_pid, assetsfile=sf)))
            sf.objects[mgpid].save_typetree(mg)
            _dump_new(mgpid)          # 组件列表改了 ⇒ 覆盖式重导（幂等）

    # 网格替换清单：mesh pid -> 用户网格的骨骼名顺序（用于同步 SMR.m_Bones）
    mesh_bone_names = {}
    for nm, mpid in mesh_by_name.items():
        user = next((m for m in meshes if m.get("name") == nm), None)
        if user is not None and user.get("bone_names"):
            mesh_bone_names[mpid] = list(user["bone_names"])

    # 父节点**也是新建的**那些 patch_children：主循环只遍历源对象 ⇒ patch 不到，
    # 这里直接写进新 Transform（例：桨叶挂在 Rotorangle_0 下、而 Rotorangle_0 是新建节点）。
    _orig_tr_pids = set(tr_by_go.values())
    for ptrl, kids in list(patch_children.items()):
        if ptrl in _orig_tr_pids:
            continue                      # 原节点，留给主循环
        try:
            t = _read_from_data(sf.objects[ptrl])
            cur = [c.m_PathID for c in (t.m_Children or []) if c and c.m_PathID]
            for k in kids:
                if k not in cur:
                    cur.append(k)
            t.m_Children = [PPtr(m_FileID=0, m_PathID=k, assetsfile=sf) for k in cur]
            sf.objects[ptrl].save_typetree(t)
            _dump_new(ptrl)           # 子列表改了 ⇒ 覆盖式重导（幂等）
        except Exception as e:  # noqa: BLE001
            print("[copy-full] 新节点子列表写入失败 pid=%s：%s" % (ptrl, e))
        del patch_children[ptrl]

    # ---- 名字 → Transform pid 解析器（构建期才定得下 pid，见下）----
    # ⛔ v1.8.67：行为里的节点引用现在**按名字**留给构建期解析（`behavior_codec.emit`
    #    的 `pid_of` 参数）。原因：用户自己新建的挂载点（② 面板加的 Rotorangle_0）
    #    pid 是构建期才分配的，面板编码时只能写 0 ⇒ 进游戏指向空引用（旋翼不转）✗。
    #    优先级：本 prefab 内的名字 → **本次新建的**节点 → 全 bundle 的名字。
    name_to_tr = {}
    for gname, gpid in go_by_name.items():
        trpid = tr_by_go.get(gpid)
        if trpid is not None:
            name_to_tr[gname] = trpid
    full_go_tr = {}
    full_go_name = {}
    for oo in objs:
        if oo.type.name == "Transform":
            try:
                tr = oo.read()
                gid = tr.m_GameObject.m_PathID if tr.m_GameObject else 0
                full_go_tr[gid] = oo.path_id
            except Exception as _e:
                _note_swallow('build_copy@L1881', '守护块在写 tr', _e)
                pass
        elif oo.type.name == "GameObject":
            try:
                full_go_name[oo.path_id] = oo.read().m_Name
            except Exception as _e:
                _note_swallow('build_copy@L1886', '守护块在写 full_go_name[oo.path_id]', _e)
                pass
    full_name_to_tr = {}
    for gpid, trpid in full_go_tr.items():
        nm = full_go_name.get(gpid, "")
        if nm and nm not in full_name_to_tr:
            full_name_to_tr[nm] = trpid

    def _pid_of(name):
        if not name:
            return 0
        # hub JSON 往返时 source/root/target 常是 pid 数字字符串
        # （如 "-1521521995993491084"）：直接用该 pid（导入端会重映射）
        if isinstance(name, str):
            try:
                return int(name)
            except ValueError:
                pass
        p = name_to_tr.get(name)
        if p:
            return p
        p = tr_pid_of.get(name)      # 含本次新建的挂载点（Rotorangle_0 等）
        if p:
            return p
        return full_name_to_tr.get(name, 0)

    hub_created = [False]            # 是否已经处理过（改过或新建过）AnimationHub

    def _emit_comp(ed, raw):
        """⑧ 面板改过的组件：用编辑后的字段重序列化整段 MB 字节。

        `pid_of=_pid_of` 是必须的：字段里可能有**节点引用**（PPtr），面板存的是
        节点**名字**（`__name__`），构建期才拿得到真实 pid ✓
        """
        import component_edit as _CE
        return _CE.emit_component(_CE.registry(), ed["cls"], raw, ed["values"],
                                  pid_of=_pid_of)

    # ---- 重写对象：默认原样，patch 网格 + 挂点 ----
    # v1.8.90：⑧ 组件改动的 pid 一律按字符串比（Unity 的 pathID 是 int64，
    #   Blender 的 IntProperty 存不下，面板那边就是字符串 —— 两边统一 ✓）
    comp_edits = {int(k): v for k, v in (comp_edits or {}).items()}
    comp_done = [0]
    comp_hit = set()
    new_objects = []
    for o in src_objects:
        raw = base64.b64decode(o["raw"])
        if o["type_name"] == "Mesh" and o["pid"] in mesh_by_name.values():
            # 找到对应网格名 -> 用户网格
            nm = next(k for k, v in mesh_by_name.items() if v == o["pid"])
            user = next((m for m in meshes if m.get("name") == nm), None)
            if user is not None:
                m, r = _read_mesh(raw, mesh_src)
                # ⛔ 先把源网格的「法线/切线/UV1」按 (位置,UV0) 收好 —— 覆盖 m 的顶点数据之前
                src_attrs = _src_channel_map(m)
                if src_attrs:
                    print("[copy-full] %s：源网格法线/UV1 照抄表 %d 条" % (m.m_Name, len(src_attrs)))
                skin = [(int(user["bones"][i][0]), int(user["bones"][i][1]),
                         float(user["weights"][i][0]), float(user["weights"][i][1]))
                        for i in range(len(user["positions"]))]
                faces = []
                ui = 0
                for tt in range(0, len(user["triangles"]), 3):
                    a, b, c = user["triangles"][tt], user["triangles"][tt + 1], user["triangles"][tt + 2]
                    faces.append(((a, ui), (b, ui + 1), (c, ui + 2)))
                    ui += 3
                # ⛔ v1.8.76：把目标网格本体传下去，让顶点数据照**它自己的通道表**写
                #    （bundle 里有 29 种顶点布局，写死 68 字节的那种只占 4.8%）
                vbytes, ibytes, vcount = build_mesh_data(
                    user["positions"], user["uv"], faces, skin, skin_mode=2,
                    layout_from=m, src_attrs=src_attrs)
                m.m_VertexData.m_VertexCount = vcount
                m.m_VertexData.m_DataSize = vbytes
                m.m_IndexBuffer = list(ibytes)
                m.m_IndexFormat = 1 if vcount > 65535 else 0
                if m.m_SubMeshes:
                    sub = m.m_SubMeshes[0]
                    sub.indexCount = vcount
                    sub.firstByte = 0
                    sub.firstVertex = 0
                    sub.vertexCount = vcount
                    sub.topology = 0
                # 蒙皮哈希/绑定姿势：仅当用户网格带了骨骼名才更新（否则保留原样，保贴图动画）
                bn = user.get("bone_names")
                if bn:
                    hashes = []
                    bp = []
                    n_copy_bp = n_copy_h = 0
                    for b in bn:
                        # ① 哈希：原版有就照抄（原版才是权威），否则查表
                        h = src_hash.get(b)
                        if h is not None:
                            n_copy_h += 1
                        else:
                            h = bone_hashes.get(b)
                            if h is None:
                                h = hash_lookup.get(b)
                            if h is None:
                                raise ValueError("骨骼 %s 既不在挂载点树里、也没有已知哈希" % b)
                        hashes.append(h & 0xFFFFFFFF)
                        # ② 绑定姿势：保持外观（见 src_world 处的推导）
                        mb = src_bind.get(b)
                        if b not in world:
                            raise ValueError("骨骼 %s 不在骨骼树里，无法算绑定姿势" % b)
                        wo = src_world.get(b)
                        if mb is not None and wo is not None:
                            bp.append(_mul(_invert(world[b]), _mul(wo, mb)))
                            n_copy_bp += 1
                        else:
                            bp.append(_invert(world[b]))
                    m.m_BoneNameHashes = hashes
                    root_h = bone_hashes.get("body")
                    if root_h is None:
                        root_h = hash_lookup.get("body")
                    m.m_RootBoneNameHash = (root_h if ("body" in bn and root_h is not None)
                                            else hashes[0]) & 0xFFFFFFFF
                    m.m_BindPose = bp
                    print("[copy-full] %s：绑定姿势照抄原版 %d 根 / 重算 %d 根；哈希照抄 %d 根"
                          % (m.m_Name, n_copy_bp, len(bn) - n_copy_bp, n_copy_h))
                r.save_typetree(m)
                raw = r.data
        elif o["type_name"] == "SkinnedMeshRenderer":
            # 网格被替换 ⇒ SMR 的 m_Bones 必须与 Mesh 的 m_BindPose / m_BoneNameHashes
            # **同序同长**，否则顶点骨索引指向错的骨（新挂载点最明显：桨叶不跟着旋翼转）。
            # 全部名字都能解析时才改；任一缺失就原样保留（宁可不改，也不制造错位）。
            try:
                r = ObjectReader(assets_file=sf, reader=sf.reader, path_id=1,
                                 type_id=smr_src.type_id, serialized_type=smr_src.serialized_type,
                                 class_id=smr_src.class_id, type=smr_src.type,
                                 byte_start=0, byte_size=len(raw), is_destroyed=False,
                                 is_stripped=False, data=raw)
                node = r._get_typetree_node()
                er = EndianBinaryReader(raw, endian=sf.reader.endian)
                s = TypeTreeHelper.read_typetree(node, er, as_dict=False, assetsfile=sf,
                                                 byte_size=len(raw), check_read=False)
                mp = s.m_Mesh.m_PathID if s.m_Mesh else 0
                bn = mesh_bone_names.get(mp)
                if bn and all(b in tr_pid_of for b in bn):
                    s.m_Bones = [PPtr(m_FileID=0, m_PathID=tr_pid_of[b], assetsfile=sf)
                                 for b in bn]
                    if "body" in tr_pid_of:
                        s.m_RootBone = PPtr(m_FileID=0, m_PathID=tr_pid_of["body"], assetsfile=sf)
                    r.save_typetree(s)
                    raw = r.data
            except Exception as e:  # noqa: BLE001 - 同步失败不阻断构建
                print("[copy-full] SMR 骨骼同步跳过：%s" % e)
        elif o["type_name"] == "Transform":
            # 找该 Transform 对应的 GO 名，若在用户挂点树里则 patch 位置
            gid = None
            # tr_by_go 是 go_pid -> tr_pid，反查
            gid = next((g for g, t in tr_by_go.items() if t == o["pid"]), None)
            if gid is not None:
                gname = next((n for n, g in go_by_name.items() if g == gid), None)
                if gname is not None:
                    user_node = next((n for n in bone_tree if n[0] == gname), None)
                    extra_kids = patch_children.get(o["pid"])
                    if user_node is not None or extra_kids:
                        r = ObjectReader(assets_file=sf, reader=sf.reader, path_id=1,
                                         type_id=tr_src.type_id, serialized_type=tr_src.serialized_type,
                                         class_id=tr_src.class_id, type=tr_src.type,
                                         byte_start=0, byte_size=len(raw), is_destroyed=False,
                                         is_stripped=False, data=raw)
                        node = r._get_typetree_node()
                        er = EndianBinaryReader(raw, endian=sf.reader.endian)
                        t = TypeTreeHelper.read_typetree(node, er, as_dict=False, assetsfile=sf,
                                                         byte_size=len(raw), check_read=False)
                        if user_node is not None:
                            nm, parent, pos, rot, scale = user_node
                            t.m_LocalPosition = Vector3f(pos[0], pos[1], pos[2])
                            t.m_LocalRotation = Quaternionf(rot[0], rot[1], rot[2], rot[3])
                            t.m_LocalScale = Vector3f(scale[0], scale[1], scale[2])
                            _EXPECT_TR[o["pid"]] = (nm, tuple(pos), tuple(rot), tuple(scale))
                        if extra_kids:
                            kids = [c.m_PathID for c in (t.m_Children or []) if c and c.m_PathID]
                            for k in extra_kids:
                                if k not in kids:
                                    kids.append(k)
                            t.m_Children = [PPtr(m_FileID=0, m_PathID=k, assetsfile=sf)
                                            for k in kids]
                        r.save_typetree(t)
                        raw = r.data
        elif o["type_name"] == "MonoBehaviour":
            # 应用编辑后的动画：找到 AnimationHub，用 JSON 重序列化替换其字节
            if hub_json and len(raw) >= 28 and struct.unpack_from("<q", raw, 20)[0] == HUB_SCRIPT:
                hub_root = struct.unpack_from("<q", raw, 4)[0]
                raw = json_to_hub(hub_json, hub_root, _pid_of)
                hub_created[0] = True
            # v1.8.90：⑧ 面板改过的**其它组件**（AnimationManager / UnitPrefabTurretInfo /
            # FmodTurretsTurn / SkinStorageBridge …）。按 pid 精确命中，逐个重序列化。
            # ⛔ 单个组件失败**不阻断**构建（只跳过并打印）—— 但它绝不会静默：
            #    面板上「写回模型」那一步已经做过"面板→字节→回读"校验 ✓
            ed = comp_edits.get(int(o["pid"])) if comp_edits else None
            if ed:
                try:
                    raw = _emit_comp(ed, raw)
                    comp_done[0] += 1
                    comp_hit.add(int(o["pid"]))
                except Exception as e:  # noqa: BLE001
                    print("[copy-full] ⚠ 组件 %s (pid=%s) 写回失败：%s —— 该组件保持原样"
                          % (ed.get("cls"), o["pid"], e))
        new_objects.append({"pid": o["pid"], "class_id": o["class_id"],
                            "type_name": o["type_name"], "script_id": o.get("script_id"),
                            "tree_hash": o.get("tree_hash"),
                            "raw": base64.b64encode(raw).decode("ascii")})

    # ---- 该 prefab 原本**没有** AnimationHub ⇒ 新建一个（从零自制动画）----
    # ⛔ 以前这里**静默什么都不做**：用户在 ④ 面板辛苦加的动画，构建时被无声丢掉，
    #    包导进游戏一看——毫无反应，也没有任何报错 ✗（最恶劣的一种失败）。
    #    现在：把 AnimationHub 作为**新 MonoBehaviour** 挂到 prefab 根 GameObject 上，
    #    并入 manifest（导入端会分配 pid + 重映射里面的全部 pid 引用）。
    if hub_json and not hub_created[0]:
        hub_tpl = _find_hub_template(objs)
        if hub_tpl is None:
            print("[copy-full] ⚠ 本 bundle 里找不到任何 AnimationHub 作类型模板 ⇒ "
                  "无法新建 AnimationHub；这个模型会**没有动画**")
        else:
            try:
                alloc2 = _make_alloc(set(sf.objects.keys()), MOD_PID_BASE)
                # 根 GameObject 就是容器条目指向的那个（= 上面解析出的真实根 pid）
                root_go = int(root_pid)
                hub_bytes = json_to_hub(hub_json, root_go, _pid_of)
                hub_pid = alloc2()
                ident = _type_identity(hub_tpl)
                sf.objects[hub_pid] = ObjectReader(
                    assets_file=sf, reader=sf.reader, path_id=hub_pid,
                    type_id=hub_tpl.type_id, serialized_type=hub_tpl.serialized_type,
                    class_id=hub_tpl.class_id, type=hub_tpl.type,
                    byte_start=0, byte_size=len(hub_bytes),
                    is_destroyed=False, is_stripped=False, data=hub_bytes)
                # 挂进根 GameObject 的组件表。
                # ⛔ 在**manifest 里那条已收集的字节**上改（而不是改 sf.objects）：
                #    根 GO 原样字节就在 `new_objects` 里，改它不会产生同 pid 的第二条；
                #    也不能用 `_read_from_data(sf.objects[root_go])` —— 那是**懒加载**
                #    对象，`.data` 是 None ⇒ EndianBinaryReader 直接 TypeError ✗（实测）。
                go_entry = next((e for e in new_objects if e["pid"] == root_go), None)
                if go_entry is None:
                    print("[copy-full] ⚠ 根 GameObject(pid=%s) 不在 manifest 对象集里 ⇒ "
                          "新建的 AnimationHub 挂不上去" % root_go)
                else:
                    raw_go = base64.b64decode(go_entry["raw"])
                    rg = ObjectReader(assets_file=sf, reader=sf.reader, path_id=1,
                                      type_id=go_src.type_id,
                                      serialized_type=go_src.serialized_type,
                                      class_id=go_src.class_id, type=go_src.type,
                                      byte_start=0, byte_size=len(raw_go),
                                      is_destroyed=False, is_stripped=False, data=raw_go)
                    gnode = rg._get_typetree_node()
                    ger = EndianBinaryReader(raw_go, endian=sf.reader.endian)
                    g = TypeTreeHelper.read_typetree(gnode, ger, as_dict=False,
                                                     assetsfile=sf, byte_size=len(raw_go),
                                                     check_read=False)
                    cur = [c.component.m_PathID for c in (g.m_Component or [])
                           if c and c.component]
                    if hub_pid in cur:
                        print("[copy-full] 根节点上已经有 AnimationHub 组件，跳过")
                    else:
                        comps = list(g.m_Component or [])
                        comps.append(ComponentPair(component=PPtr(
                            m_FileID=0, m_PathID=hub_pid, assetsfile=sf)))
                        g.m_Component = comps
                        rg.save_typetree(g)
                        go_entry["raw"] = base64.b64encode(rg.data).decode("ascii")
                        # ⛔ `script_id` / `tree_hash` 必须带上：导入端 `_match_type` 靠它
                        #    精确匹配到 **AnimationHub** 这个脚本类型；留空会退化成
                        #    "第一个 class_id=114 的类型" ⇒ 挂错脚本 ✗
                        _dump_new(hub_pid)
                        for e in new_objs_extra:
                            if e["pid"] == hub_pid:
                                e["script_id"] = ident.get("script_id")
                                e["tree_hash"] = ident.get("tree_hash")
                        hub_created[0] = True
                        print("[copy-full] 该 prefab 原本没有 AnimationHub ⇒ 已新建 "
                              "(pid=%s, script_id=%s, 挂到根 GO %s)"
                              % (hub_pid, ident.get("script_id"), root_go))
            except Exception as e:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                print("[copy-full] ⚠ 新建 AnimationHub 失败：%s" % e)

    # 新增节点并入 manifest（对象 + preload），导入端会给它们分配 pid 并重映射引用
    # ★[★㉖-续·D 2026-09-19] **终局补带**（顺序无关的安全网）：装配 manifest **之前**，
    #   凡**包内** SMR 的 `m_Materials` 仍指向"不在包内"的 pid ⇒ 从源包带入该材质、
    #   **就地重写**该 SMR 的 `m_Materials` 后重 dump（并记 `mat_injected`）。
    #   ⚠ 实测：prefab **既有网格**不走上面"新网格材质解析"那段（clist `mats` 为空 ⇒ 跳过）
    #   ⇒ SMR 照抄源 pid ⇒ 悬空（用户形态 6/7）⇒ 必须有这一层。
    _carry_dangling_materials(sf, new_objects, new_objs_extra, _dump_new, smr_src,
                              mat_injected=_MAT_INJECTED,
                              mat_unresolved=_MAT_SRC_UNRESOLVED,
                              _bundle_path=bundle,
                              tex_missing=_TEX_MISSING,
                              expect_mats=_EXPECT_MATS)
    new_objects.extend(new_objs_extra)
    # ⛔ v1.8.72：`new_prefab` **留空 = 原地替换"源 prefab"**（界面上的说明就是这么写的）。
    #    以前这里直接写 `new_prefab` ⇒ 空串 ⇒ 导入端 `if prefab_path and has_objects:`
    #    不成立 ⇒ **既不写容器条目、也不清理旧对象** ✗ ⇒
    #    游戏仍然按**原来的**容器条目加载**原来那个 prefab** ⇒
    #    新对象全成了没人引用的孤儿 ⇒ **改了半个小时的模型进游戏一点变化都没有** ✗✗
    #    （实测确认：UI 走「新 prefab 路径留空」时产出的 manifest.prefab_path 就是 ''）
    #    `src_path` 是 `collect_prefab_objects()` 返回的源 prefab 容器路径，
    #    用它 = 删掉同名条目 + 追加同名新条目 ⇒ 真正的"原地替换" ✓
    #    填了别的路径 = 注册成**新 prefab**（原 prefab 不动，即"克隆成一个新单位"）✓
    manifest = {
        "format": FORMAT, "version": VERSION, "mode": "copy-full",
        "bundle": "units_assets_all", "prefab_path": (new_prefab or src_path),
        "root_pid": root_pid,
        "preload": list(src_preload) + [e["pid"] for e in new_objs_extra],
        "objects": new_objects,
        "added_nodes": [n for n in _tree_names if n in go_pid_of and n in set(added)],
        # ★v1.12.3 安全网·清单标记（中枢裁定）：源材质未带入、暂用本车材质的**逐件记录**
        #   （⛔ 空列表也照写 ⇒ 消费者不用猜"键在不在"）
        "mat_fallbacks": _MAT_FALLBACKS,
        # ★v1.12.4（裁定 2·第一步）：为"骨骼名缺失"新建的**占位骨骼**逐条记录
        #   （⛔ 空列表也照写 ⇒ 消费者不用猜键在不在；骨架齐全 ⇒ 必为空）
        "bone_fallbacks": _BONE_FALLBACKS,
        # ★v1.12.5（★㉖-续·A 第二步）：**丢件/降级**逐条记录
        #   （no_bone_names=已保件／missing_bones_dropped=仍丢件／skin_index_short=权重降级）
        #   ⛔ 空列表也照写 ⇒ 消费者不用猜"键在不在"
        "node_skips": _NODE_SKIPS,
        # ★v1.12.5（★㉖-续·C）：材质解析成功、但贴图不在包内的逐条记录
        #   （⛔ 空列表也照写 ⇒ 消费者不用猜"键在不在"；贴图全在 ⇒ 必为空）
        "tex_missing": _TEX_MISSING,
        # ★v1.12.5（★㉖-续·B·甲）：跨包**带入**的材质/贴图（含每张图的处理方式）＋ 源字节缺失的逐条
        "mat_injected": _MAT_INJECTED,
        "mat_src_unresolved": _MAT_SRC_UNRESOLVED,
    }
    def _read_tr_raw(raw):                                            # ★㉕-续：按模板 typetree 解 Transform
        # ★[★㉕-续·修 2026-09-19] 模板取 **L628 那个无条件绑定的 `tr_src`**：旧写法用 `tr_src2`，
        #   而 `tr_src2` 只在 `if added or new_meshes:` 分支（原 L832）里绑定 ⇒「无新增节点」的
        #   构建里本回调每次调用都 NameError ⇒ 校验腿 **0 覆盖**（实测 checked=0、43 条伪 mismatch）。
        #   ⛔ 不用全局变量：typetree 只取决于**对象类型**，`tr_src` 是同来源、且已在分支外绑定。
        _node = tr_src._get_typetree_node()
        _er = EndianBinaryReader(raw, endian=sf.reader.endian)
        _t = TypeTreeHelper.read_typetree(_node, _er, as_dict=False, assetsfile=sf,
                                          byte_size=len(raw), check_read=False)
        return ((_t.m_LocalPosition.x, _t.m_LocalPosition.y, _t.m_LocalPosition.z),
                (_t.m_LocalRotation.x, _t.m_LocalRotation.y, _t.m_LocalRotation.z, _t.m_LocalRotation.w),
                (_t.m_LocalScale.x, _t.m_LocalScale.y, _t.m_LocalScale.z))

    def _read_mats_raw(raw):                                          # ★㉕-续：按模板解 SMR 的 m_Materials
        _s = _read_smr(raw, smr_src)
        return [int(_m.m_PathID) for _m in (getattr(_s, "m_Materials", None) or []) if _m and _m.m_PathID]

    # ★甲1 配套（2026-09-20 中枢放行）：**同 pid 名字一致性**（豁免面＝空；三态 D=R+G+U，不成立即报错）
    _mnc = check_mat_name_consistency(manifest)
    manifest["mat_name_consistency"] = _mnc
    print("[copy-full] mat_name_consistency: 比较面=%s（缺席=%s）｜ D=%d → R=%d ＋ G=%d ＋ U=%d（%d+%d+%d=%d %s）"
          "｜ 可比 pid=%d"
          % (_mnc["fields_present"], _mnc["fields_absent"], _mnc["D"], _mnc["R"], _mnc["G"], _mnc["U"],
             _mnc["R"], _mnc["G"], _mnc["U"], _mnc["R"] + _mnc["G"] + _mnc["U"],
             "✓" if _mnc["eq_ok"] else "✗", _mnc["comparable"]))
    if not _mnc["eq_ok"]:
        print("⛔ [copy-full] mat_name_consistency **等式不成立**（D=%d ≠ R+G+U=%d）"
              "⇒ 计数自检失败，请查实现（⛔ 不许出『红 0』式结果）"
              % (_mnc["D"], _mnc["R"] + _mnc["G"] + _mnc["U"]))
    if _mnc.get("placeholder_predicate_hits_n"):
        print("      ⓘ 占位串**谓词兜底命中 %d 次**（最弱一档，原串原样列出，便于发现误判）：%s"
              % (_mnc["placeholder_predicate_hits_n"],
                 " ｜ ".join(_mnc["placeholder_predicate_hits"][:6])))
    for _r in _mnc["red"][:6]:
        print("      ✗ 同 pid 名字不一致：pid=%s ｜ 字段 %s ｜ %s" % (_r["pid"], _r["fields"], _r["why"]))
        for _f, _ns in _r["names"].items():
            print("           %-20s %s" % (_f, _ns))

    # ★甲2（2026-09-20）：**静默兜底回读** —— 构建期每一次「兜底被触发」都进 manifest
    #   ⇒ 产物侧一眼能看出「这个包在某个判定面上丢过信息」（⛔ 不许只有 stdout 留痕）。
    manifest["swallows"] = list(_SWALLOWS)
    if _SWALLOWS:
        print("[copy-full] ★ 兜底名册：%d 条（⚠ 有兜底被触发 ⇒ 相应判定面请按「判不了」读，"
              "⛔ 别当「没有」）" % len(_SWALLOWS))
        for _s in _SWALLOWS[:6]:
            print("      ⚠ %s：%s（%s）" % (_s["site"], _s["what"], _s["ex"]))
    else:
        print("[copy-full] ★ 兜底名册：0 条（本次构建没有任何兜底被触发）")

    with zipfile.ZipFile(out_pack, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
    # ★v1.12.5（★㉕-续）：**写盘之后立刻回读产物**、逐对象对拍 —— 不一致就点名（⛔ 不静默）
    try:
        _pv = verify_pack_transforms(out_pack, _EXPECT_TR, _read_tr_raw, _EXPECT_MATS, _read_mats_raw)
        manifest["pack_verify"] = _pv
        # ★[★㉕-续·修 2026-09-19] **零覆盖守卫**：有期望却一个都没对拍到 ⇒ 判不了（⛔ 不许当"通过"）
        #   起因：校验腿曾因模板变量未绑定而整体退化成 0 覆盖，而当时只在"有 mismatch"时才吭声。
        if _EXPECT_TR and not _pv.get("checked"):
            print("[copy-full] ⛔ **校验腿零覆盖**：期望对拍 %d 个对象、实际 checked=0 ⇒ **判不了**"
                  "（⛔ 不当通过；先查模板/回调是否真的跑起来了）" % len(_EXPECT_TR))
        if _pv["mismatches"]:
            print("[copy-full] ⛔ **产物侧回读不一致 %d 处**（transform %d 个 / 材质绑定 %d 个已对拍）："
                  % (len(_pv["mismatches"]), _pv["checked"], _pv["checked_mats"]))
            for _mm in _pv["mismatches"][:8]:
                print("      ✗ %s.%s 期望 %s ⇒ 实得 %s"
                      % (_mm.get("object"), _mm.get("field"), _mm.get("expected"), _mm.get("got")))
            print("      ⚠ 边界：约束驱动节点/动画行为在 Blender 侧不复现 ⇒ 本判据**不能替代实机确认**")
        else:
            print("[copy-full] ✓ 产物侧回读通过：transform %d 个 ／ 材质绑定 %d 个逐项对拍一致%s"
                  % (_pv["checked"], _pv["checked_mats"],
                     ("" if not _pv["not_in_pack"] else
                      "（另有 %d 个期望对象不在 manifest 里 ⇒ 未对拍）" % len(_pv["not_in_pack"]))))
            print("      ⚠ 边界：约束驱动节点/动画行为在 Blender 侧不复现 ⇒ 本判据**不能替代实机确认**")
    except Exception as _e:                                           # noqa: BLE001
        print("⚠ [copy-full] 产物侧回读**跑不完**（%s: %s）⇒ **判不了**（⛔ 不当通过）"
              % (type(_e).__name__, _e))
        manifest["pack_verify"] = {"checked": 0, "checked_mats": 0, "not_in_pack": [],
                                   "mismatches": [{"object": "<pack>", "field": "verify",
                                                   "expected": "跑完", "got": "%s: %s"
                                                   % (type(_e).__name__, _e)}]}
    with zipfile.ZipFile(out_pack, "w", zipfile.ZIP_DEFLATED) as z:   # 回读结果写回 manifest
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
    # v1.8.90：组件改动必须**报出来**。⑧ 面板上写的那些字段（车身抖动、车速、炮塔转速…）
    #   如果因为 pid 变了没命中，用户从日志里必须看得见 —— 否则就是"调了半天没反应"✗
    comp_note = ""
    if comp_edits:
        comp_note = "| 组件改动 %d/%d" % (comp_done[0], len(comp_edits))
        if comp_done[0] != len(comp_edits):
            miss = [k for k in comp_edits if k not in comp_hit]
            print("[copy-full] ⚠ 有 %d 个组件改动**没命中**（pid 已变）：%s\n"
                  "           ⇒ 请回 ⑧ 面板重新「扫描组件」再写回一次"
                  % (len(miss), ", ".join(str(m) for m in miss[:8])))
    return ("DONE copy-full -> %s | %d 对象（新增节点 %d%s）| src=%s%s"
            % (out_pack, len(new_objects), len(new_objs_extra),
               ("：" + ", ".join(added) if added else ""), src_path, comp_note),
            manifest["root_pid"])
