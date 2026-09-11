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

BASE = 0x4355424500000000
BASE_RANGE = 0x10000


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
    """从 BASE 起分配 count 个空闲 pid。"""
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
    out = bytearray()
    i = 0
    n = len(raw)
    while i < n:
        if i + 8 <= n:
            v = struct.unpack_from("<q", raw, i)[0]
            nv = pid_map.get(v)
            if nv is not None:
                out += struct.pack("<q", nv)
                i += 8
                continue
        out.append(raw[i])
        i += 1
    return bytes(out)


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
        t.set_image(img)
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
    bak = catalog_path + ".bak"
    if not os.path.exists(bak):
        import shutil
        shutil.copy(catalog_path, bak)
    cat.save(catalog_path)
    return "已注册"


def _import_tex_and_mats(sf, mats, textures):
    """共用：新建纹理 + 克隆材质替换纹理槽。返回 (新对象数, {orig_pid: new_pid})。"""
    from PIL import Image
    import io as _io
    objs = list(sf.objects.values())
    tex_src = next((o for o in objs if o.type.name == "Texture2D"), None)
    if tex_src is None:
        raise ValueError("目标 bundle 里没有 Texture2D 模板")
    n = 0
    new_tex = {}
    for idx, it in enumerate(textures or []):
        img = Image.open(_io.BytesIO(base64.b64decode(it["png"]))).convert("RGBA")
        w, h = img.size
        t = tex_src.read()
        t.m_Name = it.get("name") or ("tex_%d" % idx)
        t.m_Width = w
        t.m_Height = h
        t.m_TextureFormat = 4  # RGBA32
        t.set_image(img)
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
    for m in (mats or []):
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
        n += 1
    return n, mat_map


def _import_matswap(sf, ms):
    """模型贴图替换包导入（枪械等无皮肤桥的模型）：新建纹理 + 克隆材质 +
    原地更新指定渲染器的 m_Materials 引用。返回 (新对象数, 更新的渲染器数)。"""
    n, mat_map = _import_tex_and_mats(sf, ms.get("mats"), ms.get("textures"))
    if not mat_map:
        return n, 0
    wanted = set(int(r) for r in (ms.get("renderers") or []))
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
        changed = False
        for m in (ren.m_Materials or []):
            if m.m_PathID in mat_map:
                m.m_FileID = 0
                m.m_PathID = mat_map[m.m_PathID]
                changed = True
        if changed:
            o.save_typetree(ren)
            updated += 1
    return n, updated


def _import_skin(sf, skin_manifest):
    """皮肤包导入：新建纹理 + 克隆材质 + 更新全部 SkinStorageBridge（同 bundle 内）。

    返回 (新对象数, 更新的桥数)。
    """
    objs = list(sf.objects.values())
    n, mat_map = _import_tex_and_mats(sf, skin_manifest.get("mats"),
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
    return n, updated


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
        else:
            preload_pids.append(p)  # MonoScript 常量 pid，原样使用
    if prefab_path and has_objects:
        new_start = len(ab.m_PreloadTable)
        ab.m_PreloadTable.extend([PPtr(m_FileID=0, m_PathID=p, assetsfile=sf) for p in preload_pids])
        ab.m_Container.append((prefab_path, AssetInfo(
            asset=PPtr(m_FileID=0, m_PathID=new_root, assetsfile=sf),
            preloadIndex=new_start, preloadSize=len(preload_pids))))
    ab_obj.save_typetree(ab)

    # 皮肤包（重涂 / 新增皮肤槽）：纹理 + 材质克隆 + 桥更新（同一 bundle，保存前完成）
    n_skin = 0
    if has_skin:
        n_skin, n_bridges = _import_skin(sf, manifest["skin"])
        step("皮肤：%d 个新对象（纹理+材质），更新 %d 个桥..." % (n_skin, n_bridges))
    if has_matswap:
        n_ms, n_ren = _import_matswap(sf, manifest["matswap"])
        n_skin += n_ms
        step("模型贴图替换：%d 个新对象，更新 %d 个渲染器..." % (n_ms, n_ren))

    step("保存 bundle（约 20 秒）...")
    from stream_save import ensure_stream_save
    ensure_stream_save()
    env.file.save_stream(bundle_path, "lz4")

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
