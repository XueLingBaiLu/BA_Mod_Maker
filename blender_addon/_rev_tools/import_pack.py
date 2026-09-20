# -*- coding: utf-8 -*-
r""".bamod 素材包导入器：把包里的对象合并进游戏 bundle 并计算 CRC。

流程：
  1. 读 manifest；
  2. 加载目标 bundle，清理旧 mod 对象（0x4355424500000000 段）+ 旧容器条目 + 旧 preload 条目（重映射索引）；
  3. 按 (class_id, script_id / tree_hash) 匹配目标 bundle 的类型条目（type_id）；
  4. 给每个对象分配新 pid，字节级把 raw 里所有旧 pid 引用替换为新 pid；
  5. 追加容器条目（prefab 路径 -> 新 root）+ preload（MonoScript 常量 pid 前置）；
  6. 保存 bundle + 更新 catalog CRC。

用法：
    from import_pack import import_pack
    import_pack(pack_path, bundle_path, catalog_path=None)
"""
import os, sys, struct, zipfile, json, base64

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
try:
    import UnityPy  # noqa: F401
except ImportError:
    sys.path.insert(0, os.path.join(HERE, "..", "_unitypy"))
    import UnityPy  # noqa: F401

from UnityPy.files.ObjectReader import ObjectReader
from UnityPy.classes.PPtr import PPtr
from UnityPy.classes.generated import AssetInfo
from UnityPy.enums import ClassIDType

# 自动备份开关（默认**关**）：GB 级 bundle 不再每次导入都留一份 .bak
try:
    from backup_policy import maybe_backup as _maybe_backup
except Exception:                                        # 旧打包/独立运行
    def _maybe_backup(path, log=None, label=None):
        return None

BASE = 0x4355424500000000
BASE_RANGE = 0x10000
# 允许留在 preload 里的"非包内 pid"白名单：这些是 MonoScript 常量（脚本资产），
# 它们本来就不该出现在包对象里。其它未知 pid 一律视为坏引用并剔除。
try:
    from hub_edit import HUB_SCRIPT as _HUB
    from build_turret_direct import UNITPREFABTURRETINFO_SCRIPT as _TI
    MONOSCRIPT_CONSTS = {int(_HUB), int(_TI)}
except Exception:  # noqa: BLE001 - 导入失败时退回最小集合
    # ⚠ 兜底值：**随游戏版本变**（= `hub_edit.HUB_SCRIPT`，AnimationHub 的 m_Script pathID）。
    #   走到这里说明 `hub_edit` / `build_turret_direct` 导入不了（打包/路径出问题）
    #   ⇒ 这个集合只影响"preload 白名单"的松紧，不会写坏数据；但**它过期时会少认一个脚本**，
    #     表现为 preload 里那条被当坏引用剔掉（静默）✗ ⇒ 正常路径上不该走到这里 ✓
    MONOSCRIPT_CONSTS = {4665939560152279323}


def _match_type(sf, class_id, script_id, tree_hash):
    """在目标文件里找匹配的类型：返回 (type_id, serialized_type)。"""
    if script_id:
        target_sid = bytes.fromhex(script_id)
        for i, st in enumerate(sf.script_types):
            sid = getattr(st, "script_id", None)
            if isinstance(sid, bytes) and sid == target_sid:
                for ti, t in enumerate(sf.types):
                    if getattr(t, "script_type_index", -1) == i and getattr(t, "class_id", 0) == 114:
                        return ti, t
        # 某些 bundle 的 script_types 未填充 script_id（UnityPy 解析限制），
        # script_id 匹配不到时回退到 tree_hash，而不是直接失败。
    if tree_hash:
        target_th = bytes.fromhex(tree_hash)
        for ti, t in enumerate(sf.types):
            oth = getattr(t, "old_type_hash", None)
            if isinstance(oth, bytes) and oth == target_th and getattr(t, "class_id", 0) == class_id:
                return ti, t
    for ti, t in enumerate(sf.types):
        if getattr(t, "class_id", 0) == class_id:
            return ti, t
    raise ValueError("目标 bundle 里找不到 script_id=%s / tree_hash=%s 的类型" % (script_id, tree_hash))


def _clean_target(sf, prefab_path):
    """清理目标里旧 mod 对象 + 旧容器条目 + 旧 preload 条目（含索引重映射）。

    返回 (removed, ab, ab_obj)：
      ab/ab_obj 是清理后的 AssetBundle 解析实例 + ObjectReader。
      注意：UnityPy 的 ObjectReader.read() 每次从流重新解析（无实例缓存），
      而 save_typetree 只写内存 self.data；如果清理后再 read()，会把清理结果丢掉，
      最终保存的是「旧状态 + 追加」→ 容器/preload 条目重复累积（已实测踩坑）。
      所以调用方必须复用这里返回的实例，不要再 read()。
    """
    ab_obj = next(o for o in sf.objects.values() if o.type.name == "AssetBundle")
    ab = ab_obj.read()
    old_pl = list(ab.m_PreloadTable)
    keep = [not (BASE <= (p.m_PathID or 0) < BASE + BASE_RANGE) for p in old_pl]
    remap = {}
    ni = 0
    for i in range(len(old_pl)):
        remap[i] = ni
        if keep[i]:
            ni += 1
    ab.m_PreloadTable = [p for i, p in enumerate(old_pl) if keep[i]]
    for n, a in ab.m_Container:
        if a.preloadIndex in remap:
            a.preloadIndex = remap[a.preloadIndex]
    ab.m_Container = [(n, a) for n, a in ab.m_Container if n != prefab_path]
    ab_obj.save_typetree(ab)
    removed = 0
    for pid in list(sf.objects.keys()):
        if BASE <= pid < BASE + BASE_RANGE:
            sf.objects.pop(pid, None)
            removed += 1
    return removed, ab, ab_obj


def _alloc_pids(sf, count):
    """从 BASE 起分配 count 个空闲 pid。

    ⛔ 上限 `BASE_RANGE`：清理（`_clean_target`）只覆盖 `[BASE, BASE+BASE_RANGE)`，
    超出这个范围的 pid **下一次导入不会被清掉** ⇒ 越积越多。所以这里直接拒绝 ✗。
    """
    if count >= BASE_RANGE:
        raise ValueError("包对象数 %d 超过可清理范围 %d —— 需要扩大 BASE_RANGE 或拆包"
                         % (count, BASE_RANGE))
    existing = set(sf.objects.keys())
    out = []
    i = 0
    while len(out) < count:
        cand = BASE + i
        i += 1
        if cand not in existing:
            existing.add(cand)
            out.append(cand)
    return out


def _remap_pids(raw, pid_map):
    r"""单遍原子替换旧 pid 引用为新 pid。

    不能用 for old, new in pid_map.items(): raw.replace(...) 的顺序替换：
    当某个条目的 new 恰是另一个条目的 old（新旧 pid 都在 BASE 段，区间重叠）时，
    后一次 replace 会把前一次已改好的引用再污染掉（例：body TR 4115→4121，
    而 shell TR 旧 pid 恰为 4121→4124，会把已改好的 body 引用再改成 4124），
    导致层级/组件引用错乱、游戏崩溃。
    """
    if not pid_map:
        return bytes(raw)
    raw = bytes(raw)
    n = len(raw)
    # ★ 2026-09-13 性能：原来无条件逐 4 字节跑 Python 循环（34 MB blob ≈ 850 万次迭代，
    #   实测 4.5 s / 单次导入）。Unity 的 PPtr pathID(i64) 按 4 字节对齐、且 pid_map
    #   通常只有几个条目 ⇒ 先用 C 速度的 bytes.find 枚举"低 16 位命中"的候选位置，
    #   再按原贪心语义（命中后跳过 8 字节）替换。低 16 位模式过多时退回原扫描。
    #   等价性：400 例模糊对拍（含"新 pid 恰为另一个旧 pid"的重叠用例）逐字节一致。
    lows = {v & 0xFFFF for v in pid_map}
    if len(lows) <= 64 and n >= 8:
        cand = []
        for lv in lows:
            pat2 = struct.pack("<H", lv)
            st = 0
            while True:
                p2 = raw.find(pat2, st)
                if p2 < 0:
                    break
                if (p2 & 3) == 0 and p2 + 8 <= n:
                    cand.append(p2)
                st = p2 + 2
        if not cand:
            return raw
        cand.sort()
        res = None
        end = -1
        for p2 in cand:
            if p2 < end:
                continue
            nv = pid_map.get(struct.unpack_from("<q", raw, p2)[0])
            if nv is None:
                continue
            if res is None:
                res = bytearray(raw)
            res[p2:p2 + 8] = struct.pack("<q", nv)
            end = p2 + 8
        return bytes(res) if res is not None else raw
    out = bytearray()
    i = 0
    # ⛔ Unity 的 PPtr 里 pathID（i64）至少按 4 字节对齐，所以只扫 4 字节边界：
    #    既避免在任意偏移误命中，也把逐字节扫描的开销降到 1/4
    #    （实测 34MB 的包逐字节要 8 秒，其中单个 28MB Shader 占 6.3 秒 ✗）。
    #    再只用低两字节做预筛，绝大多数位置一次比较就跳过。
    pre = lows
    while i < n:
        if i + 8 <= n:
            if (raw[i] | (raw[i + 1] << 8)) in pre:
                v = struct.unpack_from("<q", raw, i)[0]
                nv = pid_map.get(v)
                if nv is not None:
                    out += struct.pack("<q", nv)
                    i += 8
                    continue
            if i + 4 <= n:
                # 整体平移 4 字节（保持对齐），比逐字节快得多
                out += raw[i:i + 4]
                i += 4
                continue
        out.append(raw[i])
        i += 1
    return bytes(out)


def _tex_colorspace(it):
    """★⑱：PNG 重导入必须**显式**写 m_ColorSpace —— 不写就跟着"模板贴图"走 ✗

    病灶（v1.12.0 实测）：`_import_textures` / `_import_tex_and_mats` 都拿
    **目标包里第一个 Texture2D** 当模板（`next(o for o in objs if o.type.name == "Texture2D")`），
    只改 `m_Name/m_Width/m_Height/m_TextureFormat` 就 `set_image()` —— `m_ColorSpace` 于是**继承模板**。
    模板恰好是法线图（`__Rust_Normal`，colorSpace=0=Linear）⇒ 重导入的**基色图**被标成 Linear ⇒
    游戏按线性采样一张 sRGB 图 ⇒ **颜色发白/发灰，玩家看到的是"颜色明显对不上"** ✗
    （实测：`FLYACV` 包里两张内嵌图都成了 colorSpace=0，而游戏里所有 BaseMap 原件都是 1=sRGB。）

    取值优先级：manifest 里的 `colorspace` 显式值 → 默认 **1（sRGB）**。
    默认给 sRGB 是因为这个工具的面板（贴图导出/编辑/重导入、材质替换、皮肤）处理的
    **绝大多数是基色图与发光图**，游戏里这两类原件都是 sRGB=1（`US_ACV_1_BaseMap`=1、`emm`=1）；
    只有法线图才是 0（`__Rust_Normal`=0、`plane_divided_Decal_Normal`=0）。
    真要做法线图重导入，调用方在条目里带 `{"colorspace": 0}` 即可 ✓
    ⛔ 别改成"继承模板"——那正是这个 bug。
    ⛔ 也别指望 `set_image()` 会补：它只写像素 + `m_CompleteImageSize`（顺带把 `m_MipCount` 置 1），不碰色彩空间。
    """
    cs = it.get("colorspace")
    return 1 if cs is None else int(cs)


def _set_image_full_chain(t, img):
    r"""★㉒：把 `img` 写进 `t`，并**把 mip 链建到 1×1**（与游戏原件同级；2048² ⇒ 12 级）。

    ⛔ 为什么不用 `t.set_image(img, mipmap_count=N)`：UnityPy 的 `_Texture2d_set_image` 在
      **任一边 < 4 时收敛**（2048² ⇒ 10 级、末级 4×4），而原件实测是 **12 级（到 1×1）**
      ⇒ 会留下"链不完整"的缺口（判据 `probe_mip_chain.py --full-chain` 判不过）。
    ✅ 本助手改用 UnityPy **自己的编码器**逐级编码（语义与 set_image 一致，仅收敛阈值不同）：
      `Texture2DConverter.image_to_texture2d(level, fmt)`，从 w×h 一路缩到 1×1（每级 BICUBIC 折半、
      非 2 的幂时向下取整且不小于 1）。实测：2048²⇒12 级/22,369,620 B；104×80⇒7 级；32²⇒6 级 ✓
    """
    from PIL import Image as _I
    from UnityPy.export import Texture2DConverter
    fmt = t.m_TextureFormat
    data = b""
    lv = img
    n = 0
    while True:
        chunk, fmt2 = Texture2DConverter.image_to_texture2d(lv, fmt)
        data += chunk
        n += 1
        if lv.width == 1 and lv.height == 1:
            break
        lv = lv.resize((max(1, lv.width // 2), max(1, lv.height // 2)), _I.Resampling.BICUBIC)
    t.image_data = data
    if t.m_MipMap is not None:
        t.m_MipMap = True
    if t.m_MipCount is not None:
        t.m_MipCount = n
    t.m_CompleteImageSize = len(data)
    t.m_TextureFormat = fmt2


def _import_textures(sf, tex_items, alloc_base):
    """把纹理条目写进目标文件：Texture2D（内嵌 RGBA32）+ Sprite 成对。返回 (新增对象数, 容器条目列表)。"""
    from PIL import Image
    import io as _io
    objs = list(sf.objects.values())
    tex_src = next(o for o in objs if o.type.name == "Texture2D")
    spr_src = next(o for o in objs if o.type.name == "Sprite")
    n = 0
    entries = []
    for it in tex_items:
        img = Image.open(_io.BytesIO(base64.b64decode(it["png"]))).convert("RGBA")
        w, h = img.size
        # Texture2D：读模板 -> 改字段 -> 存为新对象（内嵌 RGBA32）
        t = tex_src.read()
        t.m_Name = os.path.basename(it["path"])[:-4] if it["path"].endswith(".png") else it["path"]
        t.m_Width = w
        t.m_Height = h
        t.m_TextureFormat = 4  # RGBA32
        # ★㉒：把 mip 链建到 1×1（与游戏原件同级）—— ⛔ 不用 set_image(mipmap_count=N)，
        #   那支实现任一边 <4 就收敛 ⇒ 2048² 只到 10 级（4×4），原件是 12 级
        _set_image_full_chain(t, img)
        t.m_ColorSpace = _tex_colorspace(it)      # ★⑱ 必须显式写，否则继承模板（见函数注释）
        sd = t.m_StreamData
        sd.offset = 0
        sd.size = 0
        sd.path = ""
        tpid = alloc_base + n
        n += 1
        r = ObjectReader(assets_file=sf, reader=sf.reader, path_id=tpid,
                         type_id=tex_src.type_id, serialized_type=tex_src.serialized_type,
                         class_id=tex_src.class_id, type=tex_src.type,
                         byte_start=tex_src.byte_start, byte_size=0,
                         is_destroyed=tex_src.is_destroyed, is_stripped=tex_src.is_stripped)
        r.save_typetree(t)
        sf.objects[tpid] = r
        # Sprite：读模板 -> 指向新纹理
        s = spr_src.read()
        s.m_Name = t.m_Name
        s.m_RD.texture = PPtr(m_FileID=0, m_PathID=tpid, assetsfile=sf)
        rect = s.m_RD.textureRect
        rect.x = 0.0
        rect.y = 0.0
        rect.width = float(w)
        rect.height = float(h)
        off = s.m_RD.textureRectOffset
        off.x = 0.0
        off.y = 0.0
        s.m_Rect = rect
        pv = s.m_Pivot
        pv.x = 0.5
        pv.y = 0.5
        s.m_PixelsToUnits = 100.0
        spid = alloc_base + n
        n += 1
        r2 = ObjectReader(assets_file=sf, reader=sf.reader, path_id=spid,
                          type_id=spr_src.type_id, serialized_type=spr_src.serialized_type,
                          class_id=spr_src.class_id, type=spr_src.type,
                          byte_start=spr_src.byte_start, byte_size=0,
                          is_destroyed=spr_src.is_destroyed, is_stripped=spr_src.is_stripped)
        r2.save_typetree(s)
        sf.objects[spid] = r2
        entries.append((it["path"], tpid, spid))
    return n, entries


def register_address(catalog_path, address, internal_path, ref_substr="ModelPrefabs"):
    """注册一个新地址：address -> internal_path（复制现有同类条目的 bundle 关联）。

    ref_substr：参考条目筛选（内部路径包含该子串）。模型用 "ModelPrefabs"，
    图片用 "Images/..."（按类别传入，保证依赖集指向正确的图片 bundle）。
    """
    import catalog_mod
    cat = catalog_mod.Catalog(catalog_path)
    iids = cat.cat.get("m_InternalIds", [])
    ref_entry = None
    for ki, (typ, addr) in enumerate(cat.keys):
        if typ != 0:
            continue
        for ei in cat.buckets[ki]["entries"]:
            e = cat.entries[ei]
            if e[0] < len(iids) and ref_substr in iids[e[0]] and "DLC" not in iids[e[0]]:
                ref_entry = (ki, ei, e)
                break
        if ref_entry:
            break
    if ref_entry is None:
        if ref_substr != "Images/":
            # 退回更宽泛的图片条目
            return register_address(catalog_path, address, internal_path, ref_substr="Images/")
        raise ValueError("catalog 里找不到可参考的 %s 条目" % ref_substr)
    if any(t == 0 and v == address for t, v in cat.keys):
        return "已存在"
    if internal_path in iids:
        new_iid = iids.index(internal_path)
    else:
        cat.cat["m_InternalIds"].append(internal_path)
        new_iid = len(iids) - 1
    cat.keys.append((0, address))
    new_key = len(cat.keys) - 1
    _, _, ref = ref_entry
    e = list(ref)
    e[0] = new_iid
    e[5] = new_key
    cat.entries.append(tuple(e))
    new_entry = len(cat.entries) - 1
    cat.buckets.append({"dataOffset": 0, "entries": [new_entry]})
    _maybe_backup(catalog_path, label=os.path.basename(catalog_path))
    cat.save(catalog_path)
    return "已注册"


def _find_stream_tex_src(sf):
    r"""本包里**第一个流式** `Texture2D`（`m_StreamData.path` 非空）—— 建流式引用贴图的模板。

    ★ v1.9.1（★⑮）：流式贴图必须从**同包同规格**的流式模板克隆（字段布局才对得上）；
      本包全是内嵌贴图时返回 None，调用方给出"改用 PNG 内嵌"的明确错误 ✓
    """
    for o in sf.objects.values():
        if o.type.name != "Texture2D":
            continue
        try:
            sd = getattr(o.read(), "m_StreamData", None)
        except Exception:                                         # noqa: BLE001
            continue
        if sd and getattr(sd, "path", ""):
            return o
    return None


def _new_stream_texture(sf, it, tpl_o):
    r"""新建一个**流式引用** `Texture2D`（**不搬像素**）：把 `m_StreamData` 指到游戏 `.resS` 的那一段。

    `it["stream"]` = `{path, offset, size, w, h, fmt, mip, img_size}`（`tex_stream.make_stream_item` 产）。
    → 返回 `(新 pid, 这份 typetree)` —— **typetree 要带出去**：改名后补 path 时**不能**在这个
      还没落盘的对象上 `read_typetree()`（新对象的 reader 定位在**模板**的 `byte_start`、
      `byte_size` 又是 0 ⇒ 读回来的是模板字段，一存就把自己写的字段全冲掉 ✗，实测踩到）✓
    ⛔ 走 **typetree 字典**改（`read_typetree`/`save_typetree`），不要用属性赋值 ——
       `Texture2D.image` 的 setter 会顺手**清空 `m_StreamData`**（`_Texture2d_set_image`）⇒ 白干 ✗
       （这条是 `自制mod\_tools\rotor_tex_fix.py` 实测跑通的那套写法，照抄 ✓）
    ⛔ `image data` / `m_ImageData` 必须**清空**：流式贴图的像素不在对象里，留着会读出垃圾 ✗
    """
    st = it["stream"]
    tt = tpl_o.read_typetree()
    tt["m_Name"] = it.get("name") or tt.get("m_Name") or "tex"
    tt["m_Width"] = int(st["w"])
    tt["m_Height"] = int(st["h"])
    tt["m_TextureFormat"] = int(st["fmt"])
    if "m_MipCount" in tt:
        tt["m_MipCount"] = int(st.get("mip") or 1)
    tt["m_CompleteImageSize"] = int(st.get("img_size") or st["size"])
    sd = tt.get("m_StreamData") or {}
    sd["path"] = st["path"]
    sd["offset"] = int(st["offset"])
    sd["size"] = int(st["size"])
    tt["m_StreamData"] = sd
    for k in ("image data", "m_ImageData", "image_data"):
        if k in tt:
            tt[k] = b""
    tpid = _alloc_pids(sf, 1)[0]
    r = ObjectReader(assets_file=sf, reader=sf.reader, path_id=tpid,
                     type_id=tpl_o.type_id, serialized_type=tpl_o.serialized_type,
                     class_id=tpl_o.class_id, type=tpl_o.type,
                     byte_start=tpl_o.byte_start, byte_size=0,
                     is_destroyed=tpl_o.is_destroyed, is_stripped=tpl_o.is_stripped)
    r.save_typetree(tt)
    sf.objects[tpid] = r
    return tpid, tt


def _retarget_new_stream_paths(sf, new_stream, mapping):
    r"""★ v1.9.1（★⑮）：把**我们刚建**的流式引用贴图的 `m_StreamData.path` 跟着内部 CAB 改名。

    ⛔ 为什么不能指望 `stream_save._retarget_stream_paths`：它**故意跳过有未落盘改动的对象**
      （`if o.data is not None: continue` —— 为了不覆盖别的工具做的等长字节补丁）；而流式引用
      贴图正是"刚 `save_typetree` 过、`data` 非 None" ⇒ **会被跳过** ⇒ path 里留着旧 CAB 名 ⇒
      重新加载时 `FileNotFoundError: Resource file CAB-….resS not found` ✗
      （`_uniqify_cab_names` 每次保存都会改名 ⇒ 这条**必然**触发，不是偶发）
    ⛔⛔ 而且**不能在这个对象上 `read_typetree()` 再改**：对新对象 `reset()` 把 reader 定位到
      `byte_start`（= **模板**的位置）、`byte_size` 又是 0 ⇒ 读回来的是**模板的字段**，
      一保存就把自己刚写好的 `m_Name`/`offset`/`size` 全冲掉 ✗（实测踩到，症状是 path 里
      留着**最初**那个 CAB 名）。所以这里**直接用建对象时那份 typetree**，不读 ✓
    ⇒ 只对自己新建的那几个 pid 返工（**不碰**任何别的对象 ⇒ 不改变既有工具的行为）✓
    """
    pairs = []
    for old, new in (mapping or {}).items():
        ob, nb = old.partition(".")[0], new.partition(".")[0]
        if ob != nb:
            pairs.append((ob, nb))
    if not pairs:
        return 0
    n = 0
    for pid, tt in (new_stream or ()):
        o = sf.objects.get(pid)
        if o is None or o.type.name != "Texture2D":
            continue
        sd = tt.get("m_StreamData") or {}
        p = sd.get("path") or ""
        if not p:
            continue
        np_ = p
        for ob, nb in pairs:
            if ob in np_:
                np_ = np_.replace(ob, nb)
        if np_ == p:
            continue
        sd["path"] = np_
        tt["m_StreamData"] = sd
        o.save_typetree(tt)
        n += 1
    return n


def _import_tex_and_mats(sf, mats, textures):
    """共用：新建纹理 + 克隆材质替换纹理槽。返回 (新对象数, {orig_pid: new_pid}, 新建的流式贴图 pid 表)。

    ★ v1.9.1（★⑮）：`textures` 条目支持 `{"name":…, "stream":{…}}`
      （**流式引用**，不搬像素、包只 + 几十 KB）—— 与 PNG 内嵌（RGBA32）可混用 ✓
    """
    from PIL import Image
    import io as _io
    objs = list(sf.objects.values())
    tex_src = next((o for o in objs if o.type.name == "Texture2D"), None)
    if tex_src is None:
        raise ValueError("目标 bundle 里没有 Texture2D 模板")
    stream_src = None
    n = 0
    new_tex = {}
    new_stream = []
    for idx, it in enumerate(textures or []):
        if it.get("stream"):
            if stream_src is None:
                stream_src = _find_stream_tex_src(sf)
                if stream_src is None:
                    raise ValueError(
                        "这个包里没有**流式** Texture2D 模板 ⇒ 不能用「流式引用」，"
                        "请改用 PNG 内嵌（贴图来源选 PNG）")
            new_tex[idx], tt_new = _new_stream_texture(sf, it, stream_src)
            new_stream.append((new_tex[idx], tt_new))
            n += 1
            continue
        img = Image.open(_io.BytesIO(base64.b64decode(it["png"]))).convert("RGBA")
        w, h = img.size
        t = tex_src.read()
        t.m_Name = it.get("name") or ("tex_%d" % idx)
        t.m_Width = w
        t.m_Height = h
        t.m_TextureFormat = 4  # RGBA32
        # ★㉒：把 mip 链建到 1×1（与游戏原件同级）—— ⛔ 不用 set_image(mipmap_count=N)，
        #   那支实现任一边 <4 就收敛 ⇒ 2048² 只到 10 级（4×4），原件是 12 级
        _set_image_full_chain(t, img)
        t.m_ColorSpace = _tex_colorspace(it)      # ★⑱ 必须显式写，否则继承模板（见 _tex_colorspace）
        sd = t.m_StreamData
        sd.offset = 0
        sd.size = 0
        sd.path = ""
        tpid = _alloc_pids(sf, 1)[0]
        r = ObjectReader(assets_file=sf, reader=sf.reader, path_id=tpid,
                         type_id=tex_src.type_id, serialized_type=tex_src.serialized_type,
                         class_id=tex_src.class_id, type=tex_src.type,
                         byte_start=0, byte_size=0,
                         is_destroyed=False, is_stripped=False)
        r.save_typetree(t)
        sf.objects[tpid] = r
        new_tex[idx] = tpid
        n += 1
    mat_map = {}
    entries = []          # ★★ ★⑯：[(条目序号, 目标材质 pid, 新材质 pid), …] —— 同名目标材质的**每一条**
    #                        都要留自己的克隆（`mat_map` 是"目标 pid→新 pid"，两条指向同一个目标时
    #                        只留得下最后一个 ⇒ 会给错克隆、★⑮ 的病会以另一种形式回来 ✗）
    for idx, m in enumerate(mats or []):
        orig = sf.objects.get(m.get("orig"))
        if orig is None or orig.type.name != "Material":
            raise ValueError("找不到原材质 pid %s" % m.get("orig"))
        mat = orig.read()
        hit = 0
        for s in m.get("texs") or []:
            nt = new_tex.get(s.get("tex"))
            if nt is None:
                continue
            for te in (mat.m_SavedProperties.m_TexEnvs or []):
                if te[0] == s.get("slot"):
                    te[1].m_Texture = PPtr(m_FileID=0, m_PathID=nt, assetsfile=sf)
                    hit += 1
                    break
        if not hit:
            continue  # 没有任何实际替换的材质跳过（不会产生新对象）
        npid = _alloc_pids(sf, 1)[0]
        r = ObjectReader(assets_file=sf, reader=sf.reader, path_id=npid,
                         type_id=orig.type_id, serialized_type=orig.serialized_type,
                         class_id=orig.class_id, type=orig.type,
                         byte_start=0, byte_size=0,
                         is_destroyed=False, is_stripped=False)
        r.save_typetree(mat)
        sf.objects[npid] = r
        mat_map[m["orig"]] = npid
        entries.append((idx, m["orig"], npid))
        n += 1
    return n, mat_map, new_stream, entries


def _norm_go_name(nm):
    """去掉 Blender 的重复后缀（`Ah_1z.001` → `Ah_1z`）再比名字。

    ⛔ 实测：同一场景里同名网格会被 Blender 加 `.001`（★⑯ 证据链 2 里就是 `Ah_1z.001`）⇒
      不归一化就匹配不上 ✗
    """
    s = (nm or "").strip()
    if len(s) > 4 and s[-4] == "." and s[-3:].isdigit():
        return s[:-4]
    return s


def _build_name_index(sf):
    r"""扫一遍目标包，建"按名字定位"需要的索引（**只在 pid 查不到时才跑**，快路径不受影响）。

    → `(ren, mats_by_name, mat_pid_name, mat_slots)`
      · `ren[渲染器 pid] = (GO 名, [它当前用的材质 pid])`（renderer 自己的名字挂在它的 GameObject 上）
      · `mats_by_name[材质名] = [材质 pid]`；`mat_pid_name[材质 pid] = 名字`
      · `mat_slots[材质 pid] = [槽名]`（用来核对"记录的槽在目标材质里在不在"）
    """
    go_name = {}
    mat_pid_name = {}
    mats_by_name = {}
    mat_slots = {}
    objs = list(sf.objects.values())
    for o in objs:                                   # ① GameObject 名字
        if o.type.name != "GameObject":
            continue
        try:
            go_name[o.path_id] = o.read().m_Name or ""
        except Exception:                                          # noqa: BLE001
            pass
    for o in objs:                                   # ② 材质：名字 + 槽名
        if o.type.name != "Material":
            continue
        try:
            m = o.read()
        except Exception:                                          # noqa: BLE001
            continue
        nm = m.m_Name or ""
        mat_pid_name[o.path_id] = nm
        mats_by_name.setdefault(nm, []).append(o.path_id)
        mat_slots[o.path_id] = [te[0] for te in (m.m_SavedProperties.m_TexEnvs or [])]
    ren = {}
    for o in objs:                                   # ③ 渲染器：GO 名 + 当前用的材质 pid
        if o.type.name not in ("MeshRenderer", "SkinnedMeshRenderer"):
            continue
        try:
            r = o.read()
        except Exception:                                          # noqa: BLE001
            continue
        g = getattr(getattr(r, "m_GameObject", None), "m_PathID", 0) or 0
        ren[o.path_id] = (go_name.get(g, ""),
                          [m.m_PathID for m in (r.m_Materials or []) if m and m.m_PathID])
    return ren, mats_by_name, mat_pid_name, mat_slots


def _resolve_matswap_targets(sf, ms, step=None):
    r"""★★ v1.11.0（★⑯）：把 manifest 里的**源包 pid** 解析成**目标包**里真正要动的对象。

    背景（★⑯ 四条证据链）：`mats[].orig` / `renderers[]` 是 ① 导入模型时写进
    `ba_renderer_pid`/`ba_materials` 的 **units 源包 pid**，而"自建包"（`copy_full` 导入时重建的包）
    **重新分配过 pid** ⇒ 目标包里按 pid 一个都查不到 ⇒ 老代码直接
    `ValueError: 找不到原材质 pid …` ✗（这条在实际用法上等于"工具白做"）
    ⇒ 现在：pid 查得到就用 pid（同一包的老用法不变）；查不到就**按名字**：
      渲染器 = GO 名（`Ah_1z`）+ 当前材质名交集；
      材质 = ① 目标包里叫这个名字的 ⇒ 用它（存在就直接用）；
             ② 目标包里**没有**这个名字（典型：自建包里零件用的是 `copy_full` 从本车继承来的材质，
                而不是它"原机"那套材质）⇒ 落到**零件现在用的那个材质**上：我们本来就是要克隆它、
                把 manifest 里记录的**槽**换成新贴图（实测该游戏各车材质的基准槽名同为
                `Layer_A97CDC25` ⇒ 槽名可跨包用 ✓）
    返回 `(renderer_pids:set, mats:list[dict], report:dict)`；`mats` 里的 `orig` 已换成**目标包** pid。
    """
    by_pid = sf.objects
    want_ren = []
    for e in (ms.get("renderer_info") or []):
        if isinstance(e, dict) and e.get("pid"):
            want_ren.append((int(e["pid"]), e.get("go") or "", [str(x) for x in (e.get("mats") or [])]))
    if not want_ren:
        want_ren = [(int(r), "", []) for r in (ms.get("renderers") or [])]
    mats_in = list(ms.get("mats") or [])
    # 快路径：所有 pid 都在目标包里 ⇒ 行为与旧版**逐字节一致**，一个索引都不建 ✓
    ok_ren = all(p in by_pid for p, _n, _m in want_ren)
    ok_mat = all(m.get("orig") in by_pid for m in mats_in)
    if ok_ren and ok_mat:
        return (set(p for p, _n, _m in want_ren), mats_in,
                {"pid": len(want_ren), "name": 0, "renderers": len(want_ren),
                 "mats_pid": len(mats_in), "mats_name": 0, "fast": True})
    ren_idx, mats_by_name, mat_pid_name, mat_slots = _build_name_index(sf)
    norm2pid = {}
    for pid, (gname, _mp) in ren_idx.items():
        norm2pid.setdefault(_norm_go_name(gname), []).append(pid)
    rep = {"pid": 0, "name": 0, "renderers": 0, "mats_pid": 0, "mats_name": 0,
           "mats_pid2": 0, "miss_ren": [], "miss_mat": [], "ambiguous": [], "fast": False,
           "pairs": []}
    # ---- ① 渲染器 ----
    # ★★ ★⑰：整车模式（`scope.mode == "subtree"`）下，**同名目标渲染器要全部重指** ——
    #   实测构建期克隆会重名（`000_skinned_Chassis` 有两个，各挂自己那一级 LOD 的网格）
    #   ⇒ 老逻辑"同名取第一个"会漏掉另一个 ⇒ 用户拉远时那一级还是旧图 ✗
    #   （只对整车生效：★⑮「只对选中对象」的语义是"只动我选的那些"，不许被这条放大 ✓）
    expand_all = ((ms.get("scope") or {}).get("mode") == "subtree")
    # ★★ ★⑰（**不许静默**）：manifest 的 `scope` 是**随包留证**的覆盖面说明 —— 导入时把它讲出来，
    #   否则"打包时没展开成 / 有几个按保守规则没换"在导入侧完全看不见 ⇒ 用户以为整车换干净了 ✗
    _sc = ms.get("scope") or {}
    if step:
        if _sc.get("mode") == "subtree":
            _sk = _sc.get("skipped") or []
            step("★⑰ 这个包是按 prefab 子树打的：归属 %s 个 / 子树共 %s 个"
                 % (_sc.get("assigned"), _sc.get("total")))
            if _sk:
                step("★⑰ ⚠ 打包时按保守规则**没换**这 %d 个（原因随包记录）：%s"
                     % (len(_sk), "；".join("%s（%s）" % (x.get("go"), x.get("reason"))
                                            for x in _sk[:6])))
        elif _sc.get("mode") == "scene_only":
            step("⛔ ★⑰ 这个包**没有**展开构建期渲染器 ⇒ 覆盖面**退回旧行为**（只改场景里的网格）；"
                 "打包时给的原因：%s；构建期那些渲染器（LOD 各级克隆）仍可能是旧贴图"
                 % _sc.get("reason", "?"))

    wanted = set()
    for pid, gname, mnames in want_ren:
        if pid in by_pid:
            wanted.add(pid)
            rep["pid"] += 1
            rep["pairs"].append((pid, mnames))        # ★ 归属映射：pid 直接命中时也要记
            continue
        cands = list(norm2pid.get(_norm_go_name(gname), []))
        if len(cands) > 1 and mnames:                 # 同名多个 ⇒ 用"当前材质名"收窄（★⑯ 关键判据）
            hit = [p for p in cands
                   if set(mat_pid_name.get(x, "") for x in ren_idx[p][1]) & set(mnames)]
            if hit:
                cands = hit
        if not cands:
            rep["miss_ren"].append(gname or ("pid=%d" % pid))
            continue
        if expand_all and len(cands) > 1:
            for p in cands:                           # ★⑰：整车的同名克隆**一个都不落**
                wanted.add(p)
                rep["name"] += 1
                rep["pairs"].append((p, mnames))
            rep.setdefault("expanded", []).append("%s ×%d" % (gname, len(cands)))
            continue
        if len(cands) > 1:
            rep["ambiguous"].append(gname or ("pid=%d" % pid))
        wanted.add(cands[0])
        rep["name"] += 1
        rep["pairs"].append((cands[0], mnames))       # ★ 归属映射用：目标渲染器 ← 它的源包材质名
    rep["renderers"] = len(wanted)
    # ---- ② 材质 ----
    #   "零件现在用的材质 pid"（已解析渲染器引用的，按出现顺序去重）
    used_pids = []
    for p in sorted(wanted):
        for mp in (ren_idx.get(p) or ("", []))[1]:
            if mp not in used_pids:
                used_pids.append(mp)
    mats_out = []
    for m in mats_in:
        orig = m.get("orig")
        if orig in by_pid:
            mats_out.append(m)
            rep["mats_pid"] += 1
            continue
        slots = [s.get("slot") for s in (m.get("texs") or []) if s.get("slot")]
        nm = m.get("orig_name") or ""
        cands = list(mats_by_name.get(nm, []))
        how = "材质名"
        if cands and slots:                           # 同名多个 ⇒ 用"记录的槽在不在它身上"收窄
            hit = [p for p in cands if all(s in mat_slots.get(p, []) for s in slots)]
            if hit:
                cands = hit
        if not cands:
            cands = list(used_pids)                   # ★ 落到"零件现在用的材质"
            how = "零件当前材质"
            if cands and slots:
                hit = [p for p in cands if all(s in mat_slots.get(p, []) for s in slots)]
                if hit:
                    cands = hit
        if not cands:
            rep["miss_mat"].append(nm or ("pid=%s" % orig))
            continue
        # ★★ v1.12.1（★⑲）：**同名目标材质可能有多份** —— 同一个包被反复合并时，每一轮都会给
        #   每个目标留一份自己的克隆（★⑯ 的设计）⇒ 包里会同时有 3 个叫 `US_ACV 1` 的材质。
        #   老代码这里只取 `cands[0]`，**其余同名材质一份都不克隆** ⇒ 用着它们的渲染器这一轮
        #   完全没被更新（实测：整车替换**第二次**合并后，车身 5 个渲染器换上了新车身克隆，
        #   **旋翼 `Ah_1z` 仍挂在上一轮的旧克隆上**、贴图还是旧的 ✗ —— 用户看到的就是"改了没反应"）。
        #   ⇒ 收窄到"**本次要动的渲染器实际在用**"的那些（`used_pids`），并**逐个**产出克隆条目：
        #      下游重指用的是"材质 pid → 克隆 pid"（`_import_matswap` 里的 `mine[orig]`），
        #      所以每个候选各自对上自己的渲染器，不会互相顶掉 ✓
        #   ★⑮「只对选中对象生效」不受影响：`used_pids` 只来自 `wanted`（已按白名单过滤）✓
        used_here = [p for p in used_pids if p in cands]
        targets = used_here or cands
        if len(targets) > 1:
            rep.setdefault("multi_mat", []).append("%s ×%d" % (nm or orig, len(targets)))
        for _tg in targets:
            mm = dict(m)
            mm["orig"] = _tg
            mats_out.append(mm)
            rep["mats_name" if how == "材质名" else "mats_pid2"] += 1
            if slots and not all(s in mat_slots.get(_tg, []) for s in slots):
                # ⛔ 槽对不上 ⇒ 克隆了材质也换不了那个槽 ⇒ 明确记一条（否则会"看着成功、其实没改"）
                rep.setdefault("slot_miss", []).append(
                    "%s（材质 %s 里没有槽 %s；它有 %s）"
                    % (nm or orig, mat_pid_name.get(_tg, "?"), "/".join(slots),
                       "/".join(mat_slots.get(_tg, [])[:6])))
    if step:
        step("贴图替换包：渲染器按 pid 命中 %d / 按名字命中 %d；材质按 pid 命中 %d / 按名字命中 %d / "
             "按「零件当前材质」命中 %d%s"
             % (rep["pid"], rep["name"], rep["mats_pid"], rep["mats_name"], rep["mats_pid2"],
                ("｜歧义（取了第一个）：%s" % "、".join(rep["ambiguous"][:3])) if rep["ambiguous"] else ""))
        if rep.get("expanded"):
            step("★⑰ 整车覆盖面：同名渲染器**全部重指**（%s）" % "、".join(rep["expanded"][:4]))
        if rep.get("multi_mat"):
            step("★⑲ 同名目标材质有 %d 份 ⇒ **逐个克隆**（%s）—— 反复合并同一个包时必需，"
                 "否则用着其余同名材质的渲染器这轮不会被更新"
                 % (len(rep["multi_mat"]), "、".join(rep["multi_mat"][:4])))
        if rep.get("slot_miss"):
            step("⚠ 这些材质的**槽名对不上**（换了槽也换不了）：%s" % "；".join(rep["slot_miss"][:3]))
    # ---- ③ 解析不了 ⇒ **明确报错**，别静默什么都不改 ----
    if rep["miss_ren"] or rep["miss_mat"]:
        raise ValueError(
            "贴图替换包里的 pid 是**源包**的，而目标包里按名字也定位不到：\n"
            "  · 渲染器找不到：%s\n  · 材质找不到：%s\n"
            "目标包：对象 %d 个 / 渲染器 %d 个 / 材质 %d 个。\n"
            "⇒ 三种情形：① 这个包是**旧版**打的（没带网格名/材质名）⇒ 用 v1.11.0 及以后的插件"
            "重新打包；② 目标包里**真的没有**这个零件（先确认它已经构建进去了）；"
            "③ 网格名被改过（名字要跟构建后目标包里的 GameObject 名一致，如 `Ah_1z`）。"
            % ("、".join(rep["miss_ren"][:6]), "、".join(rep["miss_mat"][:6]),
               len(by_pid), len(ren_idx), len(mat_pid_name)))
    return wanted, mats_out, rep


def _import_matswap(sf, ms, step=None):
    r"""模型贴图替换包导入（枪械等无皮肤桥的模型）：新建纹理 + 克隆材质 +
    原地更新指定渲染器的 `m_Materials` 引用。返回 (新对象数, 更新的渲染器数, 新建流式贴图 pid 表)。

    ★ v1.9.1（★⑮）「只对选中对象生效」：`ms["renderers"]` 就是**允许动**的渲染器白名单 ——
      **没列出的渲染器一律不动**（车身 6 个渲染器一个不碰的那种效果）✓
    ★★ v1.11.0（★⑯）：先过 `_resolve_matswap_targets` 把**源包 pid** 解析成**目标包**里的对象
      （pid → 名字两级；自建包 pid 全变，只有按名字才找得到）✓
    """
    wanted, mats, rep = _resolve_matswap_targets(sf, ms, step=step)
    n, mat_map, new_stream, entries = _import_tex_and_mats(sf, mats, ms.get("textures"))
    if not mat_map:
        return n, 0, new_stream
    # ★★ ★⑯：条目**归属**哪个渲染器 —— manifest 的 `renderer_info[i].mats`（源包材质名）里
    #   出现了 `mats[j].orig_name` ⇒ 第 j 条就是第 i 个渲染器要换的那条。
    #   ⛔ 为什么必须这么做：两条源材质**可能解析到同一个目标材质**（典型：不勾「只对选中生效」的整车替换，
    #      旋翼的源材质与车身的源材质在自建包里都落到 `US_ACV 1`）⇒ 只按 `mat_map[目标 pid]` 重指会把
    #      所有渲染器都指到**最后那个克隆**上 ⇒ 旋翼又贴上车身图（★⑮ 的病换个形式回来）✗
    name2rens = {}
    for tpid, mnames in (rep.get("pairs") or []):
        for nm in mnames:
            name2rens.setdefault(nm, set()).add(tpid)
    own = {}
    for idx, _orig, _npid in entries:
        nm = (mats[idx].get("orig_name") or "") if idx < len(mats) else ""
        own[idx] = set(name2rens.get(nm, ()))
    n_own = sum(1 for s in own.values() if s)
    if step and n_own:
        step("贴图替换包：%d/%d 条材质能按名字认出「归属渲染器」（同名目标材质时按归属分别重指）"
             % (n_own, len(entries)))
    updated = 0
    for o in sf.objects.values():
        if o.type.name not in ("MeshRenderer", "SkinnedMeshRenderer"):
            continue
        if wanted and o.path_id not in wanted:
            continue
        try:
            ren = o.read()
        except Exception:
            continue
        cur = [m.m_PathID for m in (ren.m_Materials or []) if m and m.m_PathID]
        mine = {}
        for idx, orig, npid in entries:
            if own.get(idx) and o.path_id in own[idx] and orig in cur:
                mine[orig] = npid
        changed = False
        for m in (ren.m_Materials or []):
            if m.m_PathID in mine:
                newp = mine[m.m_PathID]
            elif m.m_PathID in mat_map:
                newp = mat_map[m.m_PathID]
            else:
                continue
            m.m_FileID = 0
            m.m_PathID = newp
            changed = True
        if changed:
            o.save_typetree(ren)
            updated += 1
    return n, updated, new_stream


def _skin_bridge_anchor_mats(sf, target_id):
    r"""★[★㉖-续·D P15] 皮肤包"**桥锚点**"：这条皮肤（`target_id`）**现在**指向哪些材质 pid。

    为什么要有它：**旧皮肤包**（`skin.mats[].orig` 是**源包** pid、且没带 `orig_name`）在"自建包"
    （`copy_full` 导入时**重分配过 pid**）里一个 pid 都查不到 ⇒ 老代码直接
    `raise ValueError("找不到原材质 pid …")` ✗（＝L533；实测 `exp05_skin_leg.py` 腿1）。
    而 `SkinStorageBridge` 里 `{id: target_id, mats:[{pid:…}]}` 的材质 pid 就是**目标侧**的
    "这条皮肤当前用的材质" ⇒ 天然锚点（与 matswap 那条的"零件当前材质"同口径）✓
    """
    if not target_id:
        return []
    try:
        import skin_data as _sd
    except ImportError:                                                   # pragma: no cover
        HERE = os.path.dirname(os.path.abspath(__file__))
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        import skin_data as _sd
    out = []
    for o in sf.objects.values():
        if o.type.name != "MonoBehaviour":
            continue
        try:
            raw = o.get_raw_data()
            if len(raw) < 32:
                continue
            items, _end = _sd.parse_bridge_items(raw)
        except Exception:                                                 # noqa: BLE001
            continue
        for it in items:
            if int(it.get("id") or 0) != int(target_id):
                continue
            for m in (it.get("mats") or []):
                p = int(m.get("pid") or 0)
                if p:
                    out.append(p)
    seen, uniq = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def _resolve_skin_mats(sf, skin, step=None):
    r"""★[★㉖-续·D P15] 皮肤包的**按名／按锚点**材质解析（与 matswap 的 `_resolve_matswap_targets` **同口径**）。

    判据顺序（⛔ 不许只在一侧打补丁）：
      ① `orig` 在**目标桶**里 ⇒ 直接用（**快路径**：行为与旧版逐字节一致）
      ② 否则按 `orig_name` 在目标桶里找**同名**材质（多个 ⇒ 用"记录的槽在不在它身上"收窄）
      ③ 再否则 ⇒ 落到**桥锚点**（该 `target_id` 当前指向的材质）
      ④ 都解析不到 ⇒ **明确报错**（点名 target_id／材质名／槽），⛔ 不再抛 L533 那种裸 pid 错

    返回 `(mats_resolved, report)`；`report = {"fast","pid","name","anchor","miss"}`（⛔ 不静默）。
    """
    mats_in = list(skin.get("mats") or [])
    rep = {"fast": False, "pid": 0, "name": 0, "anchor": 0, "miss": []}
    if not mats_in:
        return mats_in, rep
    by_pid = sf.objects
    if all(m.get("orig") in by_pid for m in mats_in):
        rep["fast"] = True
        rep["pid"] = len(mats_in)
        return mats_in, rep
    _ren, mats_by_name, _mat_pid_name, mat_slots = _build_name_index(sf)
    anchor = _skin_bridge_anchor_mats(sf, skin.get("target_id") or 0)
    out = []
    for m in mats_in:
        orig = m.get("orig")
        if orig in by_pid:
            out.append(m)
            rep["pid"] += 1
            continue
        slots = [s.get("slot") for s in (m.get("texs") or []) if s.get("slot")]
        nm = m.get("orig_name") or ""
        cands = [p for p in mats_by_name.get(nm, [])] if nm else []
        how = "name"
        if cands and slots:
            hit = [p for p in cands if all(s in mat_slots.get(p, []) for s in slots)]
            if hit:
                cands = hit
        if not cands and anchor:
            # ⚠ 桥里的 pid 可能是**别的包**的（本桶里没有该对象）⇒ 必须**过滤成"本桶里真的存在"**
            #   （⛔ 不过滤就会把一个外来的 pid 传给下游 ⇒ 又变成 L533 那种裸 pid 错 ✗ 实测踩到）
            cands = [p for p in anchor if p in by_pid]
            how = "anchor"
            if slots:
                hit = [p for p in cands if all(s in mat_slots.get(p, []) for s in slots)]
                if hit:
                    cands = hit
        if not cands:
            rep["miss"].append("%s(pid=%s)" % (nm or "（名未知）", orig))
            continue
        if cands[0] not in by_pid:      # ★ 兜底守卫：解析结果**必须**在本桶里（⛔ 不许把外来的传下去）
            rep["miss"].append("%s(pid=%s，解析到 %s 但本桶里没有)" % (nm or "（名未知）", orig, cands[0]))
            continue
        mm = dict(m)
        mm["orig"] = cands[0]
        out.append(mm)
        rep[how] += 1
    if step:
        step("皮肤包材质解析：快路径=%s ｜ 按 pid=%d／按名=%d／按桥锚点=%d ｜ 未解析=%d"
             % (rep["fast"], rep["pid"], rep["name"], rep["anchor"], len(rep["miss"])))
    if rep["miss"]:
        raise ValueError(
            "皮肤包里的材质在**目标包**里定位不到（⛔ 不是裸 pid 找不到，而是按名与桥锚点都没落点）：\n"
            "  · target_id=%s\n  · 定位不到的材质：%s\n  · 目标包同名材质表条目=%d\n"
            "⇒ 三种情形：① 包是**旧版**打的（无材质名）且目标里没有该 target_id 的皮肤桥；\n"
            "   ② 目标包里真没有这套材质（先确认皮肤/桥已导入）；③ 材质名被改过。\n"
            "★ 可操作补救：① 用**新版插件**（带 `orig_name` 的那版）**重新打包**这个皮肤包\n"
            "   （新包会带上 `orig_name`，\n"
            "   目标桶 pid 变过也能**按名定位**）；② 或先确认该 `target_id` 的**皮肤桥**已经导入\n"
            "   （「桥锚点」那条路要靠它）。"
            % (skin.get("target_id"), "、".join(rep["miss"][:6]), len(mats_by_name)))
    return out, rep


def _import_skin(sf, skin_manifest, step=None):
    """皮肤包导入：新建纹理 + 克隆材质 + 更新全部 SkinStorageBridge（同 bundle 内）。

    返回 (新对象数, 更新的桥数, 新建流式贴图 pid 表)。
    """
    objs = list(sf.objects.values())
    # ★[★㉖-续·D P15] **先解析**（⛔ 不再把清单里那些**源包 pid** 直接喂给下游 ⇒ 那正是 L533 的来源）
    _resolved, _rep = _resolve_skin_mats(sf, skin_manifest, step=step)
    n, mat_map, new_stream, _entries = _import_tex_and_mats(sf, _resolved,
                                                  skin_manifest.get("textures"))
    # 更新全部桥（按脚本名找 SkinStorageBridge，避免依赖 script_id 一致性）
    sbb_pid = 0
    for o in objs:
        if o.type.name == "MonoScript":
            try:
                if o.read().m_Name == "SkinStorageBridge":
                    sbb_pid = o.path_id
                    break
            except Exception:
                pass
    updated = 0
    if sbb_pid and mat_map:
        try:
            from skin_data import replace_skin_mats
        except ImportError:
            import sys
            HERE = os.path.dirname(os.path.abspath(__file__))
            if HERE not in sys.path:
                sys.path.insert(0, HERE)
            from skin_data import replace_skin_mats
        target = skin_manifest.get("target_id") or 0
        new_id = skin_manifest.get("new_id") or 0
        for o in objs:
            if o.type.name != "MonoBehaviour":
                continue
            raw = o.get_raw_data()
            if len(raw) < 32 or struct.unpack_from("<q", raw, 20)[0] != sbb_pid:
                continue
            try:
                new_raw = replace_skin_mats(raw, target, mat_map, new_id)
            except Exception:
                continue
            if new_raw is not None:
                o.set_raw_data(new_raw)
                updated += 1
    return n, updated, new_stream


def import_pack(pack_path, bundle_path, catalog_path=None, address=None, progress=None, do_crc=True):
    """合并 .bamod 包到 bundle。返回 (对象数, 新 root pid, crc 或 None, 清理数)。

    address: 可选，导入后注册该地址 -> prefab_path。
    progress: 可选，回调 progress(msg) 报告进度。
    皮肤包（manifest.skin）：重涂/新增皮肤槽 —— 对象数含纹理+材质克隆，
    桥更新数通过 progress 消息报告（"皮肤桥更新 K 个"）。
    """
    def step(msg):
        if progress:
            progress(msg)
    step("读取 manifest...")
    with zipfile.ZipFile(pack_path) as z:
        manifest = json.loads(z.read("manifest.json"))
    if manifest.get("format") != "bamod-assets":
        raise ValueError("不是有效的 .bamod 包（format=%r）" % manifest.get("format"))

    step("加载 bundle（3.4GB，约 10-30 秒）...")
    env = UnityPy.load(bundle_path)
    sf = list(env.objects)[0].assets_file
    # ★ O4（2026-10 v1.8.113）：**在做任何修改之前**把每个对象的原始字节区间采下来。
    #   等长替换快路径（`inplace_surgery.try_fast_save`）要靠它算"改动落在哪个块"；
    #   改完再采就没有"原始"了 ⇒ 必须在**这里**采，不能挪到保存前。
    orig_ranges = {pid: (o.byte_start, o.byte_size) for pid, o in sf.objects.items()}
    has_objects = bool(manifest.get("objects"))
    has_skin = bool(manifest.get("skin"))
    has_matswap = bool(manifest.get("matswap"))
    prefab_path = manifest.get("prefab_path", "")
    cleaned_ab = None
    cleaned_ab_obj = None
    # 皮肤/贴图替换包不清理容器条目（prefab_path 只是记录参考；原版容器必须保留）
    if prefab_path and has_objects:
        removed, cleaned_ab, cleaned_ab_obj = _clean_target(sf, prefab_path)
    else:
        removed = 0
    step("清理旧对象 %d 个..." % removed)

    # 类型匹配（先全部匹配，避免半途失败留下脏状态）
    matched = []
    for o in (manifest.get("objects") or []):
        type_id, st = _match_type(sf, o["class_id"], o.get("script_id"), o.get("tree_hash"))
        matched.append((o, type_id, st))

    # 分配新 pid + 建映射
    new_pids = _alloc_pids(sf, len(matched))
    pid_map = {}
    for (o, _, _), np_ in zip(matched, new_pids):
        pid_map[o["pid"]] = np_
    new_root = pid_map.get(manifest.get("root_pid"), 0)
    # ⛔ root 取不到就写 pid 0 ⇒ 容器条目变成空引用、prefab 根本加载不出来，
    #    但函数却"正常返回" ✗。这里直接失败，别造一个看起来成功的坏包。
    # ★★ 2026-10-16（★⑮ 实测抓到）：**只有"带 objects 的模型包"才有 root 这回事** ——
    #    贴图替换包 / 皮肤包的 manifest 里**根本没有** `objects` / `root_pid`
    #    （它们只改纹理+材质+渲染器引用，不建 prefab 容器）⇒ 原来无条件判 `not new_root`
    #    会把这两类包**一律拒掉**：`ValueError: manifest.root_pid=None 不在包对象里` ✗✗
    #    （v1.9.0 及更早的发行版都带着这条 ⇒ 用户"打包贴图替换包"后导入**必然失败**）
    if has_objects and not new_root:
        raise ValueError("manifest.root_pid=%r 不在包对象里 —— 包坏了（用创建它的工具重新打包）"
                         % manifest.get("root_pid"))

    # 逐对象写入（字节级原子替换旧 pid 引用）
    for (o, type_id, st), np_ in zip(matched, new_pids):
        raw = _remap_pids(base64.b64decode(o["raw"]), pid_map)
        r = ObjectReader(assets_file=sf, reader=sf.reader, path_id=np_,
                         type_id=type_id, serialized_type=st,
                         class_id=o["class_id"], type=ClassIDType(o["class_id"]),
                         byte_start=0, byte_size=len(raw),
                         is_destroyed=False, is_stripped=False,
                         data=raw)
        sf.objects[np_] = r
    step("写入 %d 个对象..." % len(matched))

    # 容器条目 + preload
    # 复用 _clean_target 返回的实例（不能再 read()，否则清理结果丢失、条目重复累积）
    if cleaned_ab_obj is not None:
        ab_obj = cleaned_ab_obj
        ab = cleaned_ab
    else:
        ab_obj = next(o for o in sf.objects.values() if o.type.name == "AssetBundle")
        ab = ab_obj.read()
    preload_pids = []
    for p in (manifest.get("preload") or []):
        if p in pid_map:
            preload_pids.append(pid_map[p])
        elif p in sf.objects:
            # 不在包里、但**目标 bundle 里本来就有** ⇒ 正常（MonoScript 常量、共享资产）。
            # ⛔ 判据必须是"目标里到底有没有"，不能只看 pid 是否像脚本常量：
            #    早期版本用固定白名单，把 9 个合法 pid 当坏引用剔除了 ✗（端到端实测）。
            preload_pids.append(p)
        else:
            print("[导入] ⚠ preload 里的 pid %s 在包和目标 bundle 里都不存在，已剔除"
                  "（避免悬挂引用）" % p)
    if prefab_path and has_objects:
        new_start = len(ab.m_PreloadTable)
        ab.m_PreloadTable.extend([PPtr(m_FileID=0, m_PathID=p, assetsfile=sf) for p in preload_pids])
        ab.m_Container.append((prefab_path, AssetInfo(
            asset=PPtr(m_FileID=0, m_PathID=new_root, assetsfile=sf),
            preloadIndex=new_start, preloadSize=len(preload_pids))))
    ab_obj.save_typetree(ab)

    # 皮肤包（重涂 / 新增皮肤槽）：纹理 + 材质克隆 + 桥更新（同一 bundle，保存前完成）
    n_skin = 0
    stream_pids = []
    if has_skin:
        n_skin, n_bridges, sp = _import_skin(sf, manifest["skin"], step=step)
        stream_pids += sp
        step("皮肤：%d 个新对象（纹理+材质），更新 %d 个桥..." % (n_skin, n_bridges))
    if has_matswap:
        ms = manifest["matswap"]
        n_stream = sum(1 for t in (ms.get("textures") or []) if t.get("stream"))
        n_ms, n_ren, sp = _import_matswap(sf, ms, step=step)
        n_skin += n_ms
        stream_pids += sp
        # ★ v1.9.1（★⑮）：把"只动了哪几个渲染器 / 有没有流式引用贴图"报出来，
        #   用户能一眼确认「只对选中对象生效」真的生效了（不勾=整车时这里就是全部渲染器）✓
        step("模型贴图替换：%d 个新对象（流式引用贴图 %d 张），更新 %d 个渲染器：%s ..."
             % (n_ms, n_stream, n_ren, sorted(int(r) for r in (ms.get("renderers") or []))[:8]))

    step("保存 bundle（约 20 秒）...")
    from stream_save import ensure_stream_save, _uniqify_cab_names, _warn_dangling_scripts
    ensure_stream_save()
    # ⛔ **原子替换，且必须在释放 env 之后做**：
    #    UnityPy 是 mmap/内存映读取 bundle 的 ⇒ 映射会一直持有文件，Windows 不允许
    #    `os.replace` 覆盖它（实测 `PermissionError [WinError 5]` ✗）。
    #    所以先把新 bundle 写到 `.new`（写新文件不受影响），再丢掉 env + gc 释放映射，
    #    最后原子替换。这样"导入到一半失败 ⇒ 游戏 bundle 被截断"就不可能发生了。
    new_path = bundle_path + ".new"
    # ══════════════════════════════════════════════════════════════════════════
    # ★ O4 快路径（2026-10 v1.8.113）：**所有改动等长 ⇒ 字节手术 + 复用原压缩块**
    #   实测（PoC）：整包 save 16.2 s → 3.1 s，块复用率 99.97%。
    #   ⛔ 前提不成立 / 自检不过 ⇒ `try_fast_save` **自己**返回 False 并删掉产物，
    #      这里立刻回退到原来的全量 `save_stream` ⇒ **行为与以前完全一致**（只是可能更快）。
    #   ⚠ 必须先跑 `_uniqify_cab_names`（`save_stream` 里也做这件事）：不改内部 CAB 名的话
    #      自建包必然与游戏包撞名、**永远加载不了**（`[发布-02]` 实测踩过）。
    #      改名后**节点表的名字变了** ⇒ 把 `env.file.files.keys()` 当 `node_names` 传进去
    #      （`CAB-<32hex>` 改名前后同长 ⇒ 对象字节长度不变 ⇒ 仍然等长 ✓）。
    #      ⛔ 曾经写成"改名了就跳过快路径"，实测等于**永远不触发**（改名每次都会发生）✗
    # ══════════════════════════════════════════════════════════════════════════
    fast_ok = False
    try:
        renamed = _uniqify_cab_names(env.file)
        # ★★ v1.9.1（★⑮）：`_uniqify_cab_names` 内部会把**已有**流式贴图的 `m_StreamData.path`
        #   跟着改名，但它**故意跳过有未落盘改动的对象** ⇒ 我们刚建的流式引用贴图正好被跳过 ✗
        #   ⇒ 这里只补自己那几个 pid（不碰任何别的对象）✓
        if renamed and stream_pids:
            n_fx = _retarget_new_stream_paths(sf, stream_pids, renamed)
            if n_fx:
                step("流式引用贴图：%d 张的 m_StreamData.path 已跟随内部 CAB 改名" % n_fx)
        _warn_dangling_scripts(env.file)
        if renamed:
            step("字节手术：内部 CAB 名已唯一化（%d 个）—— 节点名随包改写" % len(renamed))
        from inplace_surgery import try_fast_save
        changed = {pid: bytes(o.data) for pid, o in sf.objects.items()
                   if getattr(o, "data", None) is not None and pid in orig_ranges}
        if changed:
            fast_ok = try_fast_save(env, bundle_path, new_path, orig_ranges, changed,
                                    node_names=list(env.file.files.keys()), log=step)
        else:
            step("字节手术：跳过（没有任何对象被改过）")
    except Exception as e:                                            # noqa: BLE001
        step("字节手术：异常（%s: %s）⇒ 回退全量保存" % (type(e).__name__, e))
        fast_ok = False
    if not fast_ok:
        env.file.save_stream(new_path, "lz4")
    try:
        _maybe_backup(bundle_path, log=step, label=os.path.basename(bundle_path))
        del env
        import gc
        gc.collect()
        try:
            os.replace(new_path, bundle_path)
            step("已原子替换 %s" % os.path.basename(bundle_path))
        except OSError as e:
            # 还有别的映射没释放（例如调用方仍持有同一个 env）⇒ 退回原地覆盖
            # （原地覆盖不原子，所以这里只在万不得已时走；开了自动备份的话有 .bak 兜底）
            import shutil
            step("⚠ 无法原子替换（%s）⇒ 改为原地覆盖" % e)
            with open(new_path, "rb") as fs, open(bundle_path, "wb") as fd:
                shutil.copyfileobj(fs, fd, 8 * 1024 * 1024)
            try:
                os.remove(new_path)
            except OSError:
                pass
    except Exception as e:  # noqa: BLE001
        try:
            if os.path.exists(new_path):
                os.remove(new_path)
        except OSError:
            pass
        raise

    crc = None
    if catalog_path and do_crc:
        step("计算 CRC（约 1-2 分钟）...")
        from finalize_crc import run
        crc, _ = run(bundle_path, catalog_path)
    if address and catalog_path and prefab_path:
        step("注册地址 %s ..." % address)
        register_address(catalog_path, address, prefab_path)

    # 纹理（图标/肖像等图片）：按 manifest.bundle 类别找对应图片 bundle 导入 + CRC + 注册地址
    n_tex = 0
    if manifest.get("textures"):
        import glob as _glob
        kind = manifest.get("bundle") or "unitportraits_assets_all"
        pd = os.path.dirname(bundle_path)
        base = os.path.basename(bundle_path)
        pcs = None
        if base.startswith(kind):
            pcs = [bundle_path]  # 目标本身就是该类别图片 bundle
        else:
            pcs = _glob.glob(os.path.join(pd, kind + "_*.bundle"))
        if not pcs:
            # 兜底：按图片类别顺序找（肖像/标签/武器图标/弹药图标/国家专精）
            for fallback in ("unitportraits_assets_all", "unitlabels_assets_all",
                             "unitweaponicons_assets_all", "ammoicons_assets_all",
                             "nations&specs_assets_all"):
                pcs = _glob.glob(os.path.join(pd, fallback + "_*.bundle"))
                if pcs:
                    break
        if not pcs:
            raise ValueError("找不到图片 bundle（%s 目录下）" % pd)
        p_env = UnityPy.load(pcs[0])
        p_sf = list(p_env.objects)[0].assets_file
        n_tex, entries = _import_textures(p_sf, manifest["textures"], 0x4355424600000000)
        p_ab = next(o for o in p_sf.objects.values() if o.type.name == "AssetBundle").read()
        # 原版每条容器条目 = 纹理+精灵成对进 preload 表（preloadIndex 指向、preloadSize=2）
        new_start = len(p_ab.m_PreloadTable)
        new_pl = []
        for path, tpid, spid in entries:
            new_pl.append(PPtr(m_FileID=0, m_PathID=tpid, assetsfile=p_sf))
            new_pl.append(PPtr(m_FileID=0, m_PathID=spid, assetsfile=p_sf))
        p_ab.m_PreloadTable.extend(new_pl)
        for i, (path, tpid, spid) in enumerate(entries):
            idx = new_start + i * 2
            p_ab.m_Container.append((path, AssetInfo(
                asset=PPtr(m_FileID=0, m_PathID=tpid, assetsfile=p_sf), preloadIndex=idx, preloadSize=2)))
            p_ab.m_Container.append((path, AssetInfo(
                asset=PPtr(m_FileID=0, m_PathID=spid, assetsfile=p_sf), preloadIndex=idx, preloadSize=2)))
        p_ab_obj = next(o for o in p_sf.objects.values() if o.type.name == "AssetBundle")
        p_ab_obj.save_typetree(p_ab)
        ensure_stream_save()
        p_env.file.save_stream(pcs[0], "lz4")
        if catalog_path:
            from finalize_crc import run
            crc2, _ = run(pcs[0], catalog_path)
            crc = "%s / icons:%s" % (crc, crc2)
            # 注册映射地址（像模型导入一样：manifest 每条纹理可带 address）
            ref_substr = {"unitportraits_assets_all": "Images/UnitPortraits",
                          "unitlabels_assets_all": "Images/Labels",
                          "unitweaponicons_assets_all": "Images/Weapons",
                          "ammoicons_assets_all": "Images/Ammunition",
                          "nations&specs_assets_all": "Images/"}.get(kind, "Images/")
            for it in (manifest.get("textures") or []):
                addr = it.get("address")
                if addr and it.get("path"):
                    try:
                        register_address(catalog_path, addr, it["path"], ref_substr=ref_substr)
                    except Exception as e:  # noqa: BLE001 - 地址注册失败不阻断导入
                        print("注册地址失败 %s: %s" % (addr, e))

    return len(matched) + n_skin, new_root, crc, removed
