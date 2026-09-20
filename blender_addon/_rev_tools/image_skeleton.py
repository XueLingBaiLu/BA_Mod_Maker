# -*- coding: utf-8 -*-
r"""★⑤ **肖像/图标骨架**：给自建包预置一张「占位图标」（Texture2D + Sprite + `Sprite(213)` 类型）。

为什么要这个（★⑤ + ★⑪ 实机教训）
==================================
① 从 149 KB 小模板建出来的自建包**没有任何图片骨架** —— 连 `Sprite(213)` 这个类型都不在
   `types` 表里 ⇒ ③b「把图片加进这个 bundle」的**「骨架图标」下拉是空的**，
   用户必须先"注入骨架"才能换图 ✗
② 而"图标 = `Texture2D` + `Sprite` 成对"是 vanilla 的事实：**只注册一条的后果是
   游戏按另一类型去取就取不到**（产品 ③b 注册时就是两条一起注册的）✓
   ⇒ 骨架必须**成对**搬，而且 `Sprite` 裸字节里指向旧贴图的 pathID **必须重映射**，
     否则"换的是贴图、Sprite 还指旧图 = 没换" ✗
③ ★⑤ 的"模板要**随游戏版本重建**"：模板里的 `types` 表与 CAB 名会随游戏版本脱节
   ⇒ 本模块提供 `make_skeleton_bundle()` / `rebuild_types_from_pack()` 两条一键重建 ✓

⛔ 跨文件复制对象的**两个坑**（都在这里踩过，注释留在代码里免得复发）
================================================================
1. `ObjectReader(..., type_id=src.type_id)` 里的 `type_id` 是**源文件** types 表的下标
   ⇒ 到目标文件里指向**另一个类型**（实测：写进去的 Texture2D 重新打开变成 `MonoScript`）✗
   ⇒ 必须按 `class_id` 在**目标**文件里找；找不到就**把源文件的 `SerializedType` 追加进目标** ✓
2. `copy.deepcopy(SerializedType)` 会炸 `TypeError: cannot pickle 'TypeTreeNode' object`
   ⇒ 用 `__new__` + 逐字段浅拷贝（`node` 是只读共享数据，**不需要**复制）✓

出处：`自制mod\_tools\img_skeleton_inject.py`（教学线已在真实包上验证；这里**移植**进产品，
      并把"写完重新解析自证"补成硬步骤）。移植件不许与源头分叉 —— 回归会同时跑两边比对。
"""
import io
import os
import struct

try:                                    # GUI/无控制台环境 sys.stdout 可能是 None
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                                                 # noqa: BLE001
    pass

_CAB_DIRS = ("blender_addon/_rev_tools", "_rev_tools")
ICON_PID_BASE = 0x69636F6E00000000      # = b"icon"，与产品 my_bundle / make_icons 同一段
SPRITE_CLASS_ID = 213
TEXTURE_CLASS_ID = 28


def _setup_paths():
    here = os.path.dirname(os.path.abspath(__file__))
    ws = os.path.dirname(os.path.dirname(os.path.dirname(here)))
    for p in (here, os.path.join(ws, "工具制作资源", "BA_Mod_Maker"),
              os.path.join(ws, "工具制作资源", "BA_Mod_Maker", "_unitypy")):
        if os.path.isdir(p) and p not in sys.path:
            sys.path.insert(0, p)


def _product_dirs():
    r"""产品自带的**数据文件**（模板 / 骨架包）可能落在哪几个目录 —— **源码布局与 exe 布局都要找得到**。

    ⛔ 实测布局差异（v1.8.118 发版时当场核对）：
      · ★★ 2026-09-20 起**产品已不再自带**独立骨架包（骨架烘进了 `units_warehouse_small.bundle`）；
        本节描述的是**旧布局**，保留供旧版/人工复核定位（源码布局：`<产品根>/portrait_skeleton.bundle`）
      · exe 布局：`--add-data X;.` 在 onedir 里落到 **`<发布目录>/_internal/`** ——
        实测 `BA_Mod_Maker_v1.8.118\_internal\portrait_skeleton.bundle` ✓
        （`sys._MEIPASS` 就是它，PyInstaller 给的权威值 ⇒ 优先查）
      第一版只查了"模块的上一级" ⇒ **exe 里会找不到**（源码里能跑，交付件里不能）✗
    """
    out = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        out.append(meipass)
    here = os.path.dirname(os.path.abspath(__file__))      # …/_rev_tools
    out.append(os.path.dirname(here))                      # 产品根（源码布局）
    out.append(here)                                       # 兜底：与模块同目录
    seen, uniq = set(), []
    for d in out:
        d = os.path.abspath(d)
        if d not in seen:
            seen.add(d)
            uniq.append(d)
    return uniq


def _find_product_file(name):
    for d in _product_dirs():
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    return None


def pid_of(x):
    return x.m_PathID if hasattr(x, "m_PathID") else int(x)


def load(bundle):
    """→ `(env, objs, sf, ab_obj)`；不是 UnityFS / 没有 AssetBundle 对象就抛错（不静默）。"""
    _setup_paths()
    import UnityPy
    env = UnityPy.load(os.path.abspath(bundle))
    objs = list(env.objects)
    if not objs:
        raise ValueError("UnityPy 读出来是空的：%s（路径不存在 / 不是 UnityFS）" % bundle)
    sf = objs[0].assets_file
    ab_obj = next((o for o in objs if o.type.name == "AssetBundle"), None)
    if ab_obj is None:
        raise ValueError("这个包里没有 AssetBundle 对象：%s" % bundle)
    return env, objs, sf, ab_obj


def entries_of(ab_dict, path):
    return [info for name, info in ab_dict.m_Container if str(name) == path]


def pick_texture_sprite(by_pid, infos):
    tex = spr = None
    for info in infos:
        o = by_pid.get(pid_of(info.asset))
        if o is None:
            continue
        if o.type.name == "Texture2D" and tex is None:
            tex = o
        elif o.type.name == "Sprite" and spr is None:
            spr = o
    return tex, spr


def has_sprite_type(sf):
    """★⑤ 的硬判据：`types` 表里有没有 `Sprite(213)`（小模板**没有**）。"""
    return any(int(getattr(t, "class_id", -1)) == SPRITE_CLASS_ID for t in sf.types)


def ensure_dst_type(d_sf, src_obj, log=print):
    r"""跨文件复制对象 ⇒ **必须用目标文件自己的 `types` 表下标**（见模块 docstring 坑 1）。"""
    cid = int(getattr(src_obj, "class_id", -1))
    for i, t in enumerate(d_sf.types):
        if int(getattr(t, "class_id", -1)) == cid:
            return i, t, False
    nt = _clone_serialized_type(src_obj.serialized_type)
    d_sf.types = list(d_sf.types) + [nt]
    return len(d_sf.types) - 1, nt, True


def _clone_serialized_type(st):
    """浅拷贝一个 `SerializedType`（⛔ 不能用 `deepcopy`，见模块 docstring 坑 2）。"""
    from UnityPy.files.SerializedFile import SerializedType
    names = ("class_id", "is_stripped_type", "script_type_index", "script_id",
             "old_type_hash", "node", "m_ClassName", "m_NameSpace", "m_AssemblyName",
             "type_dependencies")
    nt = SerializedType.__new__(SerializedType)
    for k in names:
        try:
            setattr(nt, k, getattr(st, k, None))
        except Exception:                                         # noqa: BLE001
            pass
    if getattr(nt, "script_type_index", None) is None:
        nt.script_type_index = -1
    return nt


def list_texture_assets(pack, log=None):
    """→ `[(容器路径, 有没有 Texture2D, 有没有 Sprite)]`：包里**可当骨架**的图片资产。"""
    env, objs, _sf, ab_obj = load(pack)
    d = ab_obj.read()
    by_pid = {o.path_id: o for o in objs}
    out = {}
    for name, info in d.m_Container:
        o = by_pid.get(pid_of(info.asset))
        if o is None:
            continue
        e = out.setdefault(str(name), [False, False])
        if o.type.name == "Texture2D":
            e[0] = True
        elif o.type.name == "Sprite":
            e[1] = True
    try:
        from stream_save import release_env
        release_env(env)
    except Exception:                                             # noqa: BLE001
        pass
    return sorted((k, v[0], v[1]) for k, v in out.items())


def _alloc_pid(existing):
    ctr = 0
    while True:
        c = ICON_PID_BASE + ctr
        ctr += 1
        if c not in existing:
            existing.add(c)
            return c


def inject_skeleton(dst_bundle, src_pack, asset_path=None, new_asset=None, png=None,
                    log=print, force=False, dry_run=False):
    r"""把源包里一条**图片资产的骨架（Texture2D + Sprite）**搬进我们的包，可选换成 `png` 的像素。

    返回 `{"tex_pid","spr_pid","asset","verified","notes":[...]}`。
    步骤：① 贴图**内联像素**（`set_image` 会清掉 `m_StreamData` ⇒ 不再依赖 `.resS`）
          ② Sprite **裸字节 + 重映射贴图 PPtr**（不重映射 = 换了图但 Sprite 还指旧图 ✗）
          ③ 容器 + `m_PreloadTable` 各追加（vanilla 就是 `Texture2D`+`Sprite` 两条、`preloadSize=2`）
          ④ **写完重新解析自证**：容器有两条、贴图能解码、`m_StreamData.path` 为空、Sprite 指向新贴图
    """
    _setup_paths()
    notes = []
    if not os.path.isfile(dst_bundle):
        raise FileNotFoundError("目标包不存在：%s" % dst_bundle)
    if not os.path.isfile(src_pack):
        raise FileNotFoundError("骨架来源包不存在：%s" % src_pack)
    # ---- 源：取骨架 ----
    s_env, s_objs, _s_sf, s_ab = load(src_pack)
    s_d = s_ab.read()
    s_by = {o.path_id: o for o in s_objs}
    if not asset_path:
        cands = [str(n) for n, info in s_d.m_Container
                 if (s_by.get(pid_of(info.asset)) is not None
                     and s_by[pid_of(info.asset)].type.name == "Texture2D")]
        if not cands:
            raise ValueError("源包里找不到任何 Texture2D 容器条目：%s" % src_pack)
        asset_path = sorted(cands)[0]
    infos = entries_of(s_d, asset_path)
    if not infos:
        raise ValueError("源包里没有容器路径 %r（用 list_texture_assets() 看有哪些）" % asset_path)
    s_tex, s_spr = pick_texture_sprite(s_by, infos)
    if s_tex is None:
        raise ValueError("源包 %r 下没有 Texture2D" % asset_path)
    new_asset = new_asset or asset_path
    tex = s_tex.read()
    img = tex.image
    if img is None:
        raise ValueError("源贴图解不出来（UnityPy 拿不到像素）—— 是不是流式贴图缺 .resS？")
    if png:
        from PIL import Image
        img = Image.open(png).convert("RGBA")
    notes.append("骨架源：%s → %s（Texture2D %sx%s 格式 %s；Sprite=%s）"
                 % (src_pack, asset_path, tex.m_Width, tex.m_Height, tex.m_TextureFormat,
                    s_spr.path_id if s_spr else "无"))

    # ---- 目标：找空位 ----
    d_env, d_objs, d_sf, d_ab = load(dst_bundle)
    d = d_ab.read()
    existing = set(d_sf.objects.keys())
    already = entries_of(d, new_asset)
    if already and not force:
        notes.append("目标包里已经有 %r 的容器条目（%d 条）⇒ 不需要再搬（要重搬加 force=True）"
                     % (new_asset, len(already)))
        return {"tex_pid": None, "spr_pid": None, "asset": new_asset,
                "verified": True, "skipped": True, "notes": notes}
    if dry_run:
        notes.append("（干跑：没有写任何东西）")
        return {"tex_pid": None, "spr_pid": None, "asset": new_asset,
                "verified": False, "dry_run": True, "notes": notes}

    from UnityPy.classes.PPtr import PPtr
    from UnityPy.classes.generated import AssetInfo
    from UnityPy.files.ObjectReader import ObjectReader
    from stream_save import ensure_stream_save
    ensure_stream_save()

    # ① 贴图：内联像素 ⇒ 新 pid
    tid, tst, t_added = ensure_dst_type(d_sf, s_tex, log=log)
    notes.append("贴图类型：目标 types[%d] class_id=%s%s"
                 % (tid, int(getattr(tst, "class_id", -1)), "（新追加）" if t_added else ""))
    tex.set_image(img, target_format=tex.m_TextureFormat)
    new_tex_pid = _alloc_pid(existing)
    nt = ObjectReader(assets_file=d_sf, reader=d_sf.reader, path_id=new_tex_pid,
                      type_id=tid, serialized_type=tst,
                      class_id=s_tex.class_id, type=s_tex.type,
                      byte_start=s_tex.byte_start, byte_size=0,
                      is_destroyed=s_tex.is_destroyed, is_stripped=s_tex.is_stripped)
    nt.save_typetree(tex)
    d_sf.objects[new_tex_pid] = nt
    notes.append("贴图：pid %s → %s（%sx%s 格式 %s，流式已清）"
                 % (s_tex.path_id, new_tex_pid, tex.m_Width, tex.m_Height, tex.m_TextureFormat))

    # ② Sprite：裸字节 + 重映射贴图 PPtr
    new_spr_pid = None
    if s_spr is not None:
        sid, sst, s_added = ensure_dst_type(d_sf, s_spr, log=log)
        notes.append("Sprite 类型：目标 types[%d] class_id=%s%s"
                     % (sid, int(getattr(sst, "class_id", -1)), "（新追加）" if s_added else ""))
        raw = bytearray(s_spr.get_raw_data())
        old, new = struct.pack("<q", s_tex.path_id), struct.pack("<q", new_tex_pid)
        if old not in raw:
            notes.append("⚠ Sprite 裸字节里没找到指向旧贴图的 pathID ⇒ 这个 Sprite 可能不是引用它")
        else:
            raw = bytearray(bytes(raw).replace(old, new))
        new_spr_pid = _alloc_pid(existing)
        ns = ObjectReader(assets_file=d_sf, reader=d_sf.reader, path_id=new_spr_pid,
                          type_id=sid, serialized_type=sst,
                          class_id=s_spr.class_id, type=s_spr.type,
                          byte_start=s_spr.byte_start, byte_size=len(raw),
                          is_destroyed=s_spr.is_destroyed, is_stripped=s_spr.is_stripped,
                          data=bytes(raw))
        d_sf.objects[new_spr_pid] = ns
        notes.append("Sprite：pid %s → %s（贴图 PPtr 已重映射，%d 字节）"
                     % (s_spr.path_id, new_spr_pid, len(raw)))
    else:
        notes.append("⚠ 源资产没有 Sprite，只搬了贴图（图标类正常应有 Sprite）")

    # ③ 容器两条 + preload（与 vanilla 一致）
    start = len(d.m_PreloadTable)
    d.m_PreloadTable.append(PPtr(m_FileID=0, m_PathID=new_tex_pid, assetsfile=d_sf))
    if new_spr_pid is not None:
        d.m_PreloadTable.append(PPtr(m_FileID=0, m_PathID=new_spr_pid, assetsfile=d_sf))
    d.m_Container.append((new_asset, AssetInfo(
        asset=PPtr(m_FileID=0, m_PathID=new_tex_pid, assetsfile=d_sf),
        preloadIndex=start, preloadSize=2 if new_spr_pid else 1)))
    if new_spr_pid is not None:
        d.m_Container.append((new_asset, AssetInfo(
            asset=PPtr(m_FileID=0, m_PathID=new_spr_pid, assetsfile=d_sf),
            preloadIndex=start, preloadSize=2)))
    d_ab.save_typetree(d)
    notes.append("容器：+%d 条（路径 %s）；preload 追加 %d 项"
                 % (2 if new_spr_pid else 1, new_asset, 2 if new_spr_pid else 1))

    # ⛔ **保存前先把源句柄放掉**（`release_env`）：不放开的话 `os.replace` 会退化成
    #   **原地覆盖**（`stream_save` 的兜底之路，非原子）—— 实测日志里就是那句
    #   `⚠ 目标文件仍被占用，无法原子替换 ⇒ 改为原地覆盖` ✗
    #   安全性依据：`_save_stream` 是**从内存对象表** `f.save()` 序列化的（不读原文件），
    #   第 3 步只读它自己写的临时文件 ⇒ 先关输入流**不影响**保存结果 ✓
    bf = d_env.file
    try:
        from stream_save import release_env
        release_env(s_env)
        release_env(d_env)
    except Exception:                                             # noqa: BLE001
        pass
    bf.save_stream(os.path.abspath(dst_bundle), "lz4")

    # ④ 写完**重新解析自证**（✗不证不算完）
    ok = verify_skeleton(dst_bundle, new_asset, log=log, notes=notes)
    return {"tex_pid": new_tex_pid, "spr_pid": new_spr_pid, "asset": new_asset,
            "verified": ok, "notes": notes}


def verify_skeleton(bundle, asset_path, log=print, notes=None):
    """只读自证：容器 2 条 / 贴图可解码 / **不是流式** / Sprite 指向新贴图 / `Sprite(213)` 在 types 里。"""
    def say(m):
        if notes is not None:
            notes.append(m)
        if log:
            try:
                log(m)
            except Exception:                                     # noqa: BLE001
                pass
    env, objs, sf, ab = load(bundle)
    d = ab.read()
    by = {o.path_id: o for o in objs}
    infos = entries_of(d, asset_path)
    ok = len(infos) >= 2
    say("自证：容器里 %r 有 %d 条（期望 ≥2）%s" % (asset_path, len(infos), "✓" if ok else "✗"))
    t2, s2 = pick_texture_sprite(by, infos)
    if t2 is None:
        say("  ✗ 找不到 Texture2D")
        ok = False
    else:
        tt = t2.read()
        sd = getattr(tt, "m_StreamData", None)
        streamed = bool(getattr(sd, "path", ""))
        say("  Texture2D pid=%s %sx%s 格式 %s；流式=%s（期望 False）"
            % (t2.path_id, tt.m_Width, tt.m_Height, tt.m_TextureFormat, streamed))
        try:
            im2 = t2.read().image
        except Exception as e:                                    # noqa: BLE001
            im2, say_ = None, say("  ✗ 解码抛异常：%s" % e)
        say("  解码：%s ⇒ %s" % (im2.size if im2 else None, "OK" if im2 else "失败"))
        ok = ok and (im2 is not None) and (not streamed)
    if s2 is None:
        say("  ⚠ 没有 Sprite（只搬了贴图）")
        ok = False
    else:
        raw2 = s2.get_raw_data()
        pts = struct.pack("<q", t2.path_id) in raw2 if t2 else False
        say("  Sprite pid=%s；裸字节指向新贴图（%s）= %s"
            % (s2.path_id, t2.path_id if t2 else "?", pts))
        ok = ok and bool(pts)
    spr_type = has_sprite_type(sf)
    say("  `Sprite(213)` 在 types 表里 = %s（★⑤ 的硬要求）" % spr_type)
    ok = ok and spr_type
    try:
        from stream_save import release_env
        release_env(env)
    except Exception:                                             # noqa: BLE001
        pass
    say("自证结果：%s" % ("✓ PASS" if ok else "✗ FAIL"))
    return ok


def _one_px_png(dest):
    """造一张 1×1 的占位 PNG（占位图标用它 —— 真图由用户在 ③b 里换）✓

    `dest` 可以是**路径**，也可以是 `BytesIO`（推荐：不落盘 ⇒ 无临时文件、无权限问题）✓
    """
    from PIL import Image
    Image.new("RGBA", (1, 1), (255, 255, 255, 0)).save(dest, "PNG")
    return dest


def make_skeleton_bundle(out, template=None, src=None, asset_path=None, log=print):
    r"""★⑤ **造一个"肖像/图标骨架包"**：小模板 + 一张 1×1 占位图标（Texture2D + Sprite + `Sprite(213)`）。

    默认：`template` = 产品自带 `units_warehouse_small.bundle`（149 KB、加载快）；
          `src` = 游戏里最小的图片包（`unitlabels_assets_all_*`，~16 MB）——
          ⛔ **别默认用肖像包**：那个 **304 MB**，只为了取一个骨架不值得 ✓
    返回 `{"out","bytes","verified","notes"}`。
    """
    _setup_paths()
    import shutil
    # ⛔ v1.11.0：这里原来 `import tempfile` 并把占位图往系统临时目录写 —— 已改成**内存里造**（见下）✓
    if not template:
        # ⛔ 用 `_find_product_file` 而不是硬拼"模块的上一级"：exe 布局里模板在 `_internal/`
        #   （`--add-data X;.` 的落点）⇒ 硬拼路径在**源码里能跑、交付件里找不到** ✗
        template = _find_product_file("units_warehouse_small.bundle")
    if not template or not os.path.isfile(template):
        raise FileNotFoundError("模板不存在：%s（产品自带的 units_warehouse_small.bundle 没找到）"
                                % template)
    if not src:
        from bundle_paths import find_bundle
        src = (find_bundle("unitlabels_assets_all") or find_bundle("unitportraits_assets_all")
               or find_bundle("ammoicons_assets_all"))
    if not src or not os.path.isfile(src):
        raise FileNotFoundError("找不到骨架来源图片包（游戏装了吗？也可以显式给 src=）")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    shutil.copy2(template, out)
    # ⛔ **不要**在这里先 save_stream 做一次"归一化"：`inject_skeleton` 结尾那次保存
    #   已经会做内部 CAB 名唯一化 + 清 `m_Dependencies`（`stream_save` 的保存路径自带）✓
    #   第一版多存了一次 ⇒ 目标包被上一次的 env 句柄占着，`os.replace` 退化成**原地覆盖**
    #   （非原子，日志里那句 `⚠ 无法原子替换 ⇒ 改为原地覆盖` 就是它）✗
    # ★★ 2026-09-16（★⑯ 收口时实测踩到）：这里原来是
    #   `png = os.path.join(tempfile.mkdtemp(prefix="skel_"), "placeholder_1x1.png")` —— 两个毛病：
    #     ① 临时目录落在**系统盘**（`%TEMP%`）而且**从不清理**（`ws_tmp` 卡里记着同类事故：
    #        136 个目录 / 116.6 GB ✗）；② 在**受限/沙箱环境**里 `%TEMP%` 下新建子目录写文件会被拒
    #        ⇒ `PermissionError` 直接把"造骨架包"这条功能打死（实测）✗✗
    #   ⇒ 占位图**根本不需要落盘**：`inject_skeleton` 的 `png=` 走的是 `Image.open()`，
    #     完全可以直接喂一个 `BytesIO` ✓（零临时文件、零权限问题、零垃圾）
    buf = io.BytesIO()
    _one_px_png(buf)
    buf.seek(0)
    r = inject_skeleton(out, src, asset_path=asset_path, new_asset=PLACEHOLDER_ASSET,
                        png=buf, log=log, force=True)
    return {"out": out, "bytes": os.path.getsize(out), "verified": r["verified"],
            "src": src, "template": template, "notes": r["notes"]}


# 占位图标的容器路径（**故意用显眼的名字**：用户在 ③d 里一眼认出该删掉它）
PLACEHOLDER_ASSET = "Assets/Mods/_MyMod/_placeholder_1x1.png"


def ensure_product_skeleton(log=None, force=False):
    r"""★ 2026-09-20 **单模板合并(甲) 后已改语义**：产品**只带一个模板**，本函数**默认不再现造**
    `portrait_skeleton.bundle`。

    为什么改（⛔ 不是洁癖）：合并后那张 1×1 占位图（`Texture2D`+`Sprite`+`Sprite(213)` 类型）已**烘进**
    `units_warehouse_small.bundle` 本体 ⇒ 产品**不再需要第二个模板**；
    若这里"找不到就现造"，就会在产品目录里**再长出一个模板** ⇒ 与「单模板」**自相矛盾**，
    且它与三处交付清单已经对齐（该件已从 `build_exe`／`package_zip`／`auto_build` 里删掉）。

    ⇒ 现在的行为：**找到就返回**；**找不到 ⇒ 返回 None 并出声说明**（⛔ 不写盘）；
      只有**显式 `force=True`**（人工/工具场景）才现造。
    `make_skeleton_bundle` 仍是"给**任意包**注入骨架"的**通用工具**（测试/人工复核在用），⛔ 它没被废。
    """
    # ⛔ 先在**产品自带的目录**里找（源码布局 / exe 的 `_internal/` 都算）
    found = _find_product_file("portrait_skeleton.bundle")
    if found:
        return found
    if not force:
        if log:
            log("（单模板合并(甲) 后**不再现造**骨架包：产品自带的是**唯一模板** "
                "`units_warehouse_small.bundle`，它已内含 1×1 占位图标 ⇒ 不需要 "
                "`portrait_skeleton.bundle`。确需现造请显式 `force=True`。）")
        return None
    # 要现造就落到**可写的那一层**（exe 里 `_internal/` 也可写，但优先产品根/工作目录）
    root = os.path.dirname(os.path.abspath(__file__))
    for cand in _product_dirs():
        if os.access(cand, os.W_OK):
            root = cand
            break
    out = os.path.join(root, "portrait_skeleton.bundle")
    try:
        r = make_skeleton_bundle(out, log=log or (lambda *_a: None))
        return r["out"] if r["verified"] else None
    except Exception as e:                                        # noqa: BLE001
        if log:
            log("⚠ 生成肖像骨架包失败（不影响其它功能）：%s: %s" % (type(e).__name__, e))
        return None


def template_tradeoff_text():
    """★⑤ 要求 UI 里给出的**取舍提示**（小模板 vs 主包模板）。"""
    return ("模板取舍：**小模板**（`units_warehouse_small.bundle`，149 KB）结构同源、加载快，"
            "**推荐** —— 它**已自带** 1×1 占位图标（`Sprite(213)` 类型在表里）⇒ 换图标开箱可用；"
            "用游戏主包当模板（3.15 GB）虽然结构完全同源，但包里带 2.77 GB `.resS` "
            "⇒ **游戏加载明显变慢**，只在排错时才用。")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="★⑤ 肖像/图标骨架：造骨架包 / 查骨架")
    ap.add_argument("--make", metavar="输出bundle", help="造一个骨架包到指定路径")
    ap.add_argument("--template", help="骨架用的模板（默认产品自带小模板）")
    ap.add_argument("--src", help="骨架来源图片包（默认自动找 unitlabels_assets_all）")
    ap.add_argument("--asset", help="骨架来源的容器路径（默认取源包里第一条 Texture2D）")
    ap.add_argument("--list", metavar="包", help="列出这个包里可当骨架的图片资产")
    ap.add_argument("--ensure-product", action="store_true", help="确保产品自带的骨架包存在")
    a = ap.parse_args()
    if a.list:
        for path, has_t, has_s in list_texture_assets(a.list):
            print("  %s  Texture2D=%s Sprite=%s" % (path, has_t, has_s))
    elif a.ensure_product:
        p = ensure_product_skeleton(log=print)
        print("产品骨架包：%s" % (p or "（没造出来）"))
    elif a.make:
        r = make_skeleton_bundle(a.make, template=a.template, src=a.src, asset_path=a.asset)
        print("✓ %s（%.1f KB，verified=%s）" % (r["out"], r["bytes"] / 1024.0, r["verified"]))
    else:
        print(__doc__)
        print(template_tradeoff_text())
