# -*- coding: utf-8 -*-
r"""**「我的 bundle」管理器**：自己建一个独立 bundle 装自己的资源/素材，游戏按 catalog 地址读取。

为什么要它（用户需求）：现在导入素材是**往游戏自带的 bundle 里塞**（units / unitportraits /
unitlabels / unitweaponicons…）⇒ 改乱了不好回退、多个 mod 互相踩、也没法整体搬迁。
本模块让你**自建一个 bundle**（可以反复往里加资产），catalog 只多出"你的 bundle + 你的地址"，
游戏照常加载 ✓（实机验证过：T2 新包体被游戏加载）。

完整链（每一步都可单独跑，也可在 UI 里按顺序点）：
```
① 新建：复制一个模板 bundle 当骨架 →  我的mod/<名字>/<名字>_<32hex>.bundle
② 加资产：.bamod（Blender 导出）/ 图片 → 合并进 ② 那个 bundle（可反复加）
③ 注册：catalog 里加"你的 bundle 条目 + extra(CRC/Hash/Size) + 资产条目 + 地址"
        （第一次调用会建 bundle 条目；以后只追加资产 ⇒ 增量友好）
④ 同步 CRC：资产变了就重算 m_Crc/m_BundleSize（忘了这步游戏会报 CRC Mismatch）
⑤ 安装：bundle → aa\PC\ ，catalog → aa\catalog.json（都会先备份）
⑥ 自检：重开 bundle 列容器条目 + 复算 CRC + 走一遍"地址→条目→依赖键→bundle→CRC"
```

CLI（也能被 UI 调用；`--self-test` 是不依赖 Blender 的全链自测）：
```powershell
python my_bundle.py --new MyMod --workdir D:\mods\MyMod --template <某游戏 bundle>
python my_bundle.py --add --bundle D:\mods\MyMod\MyMod_<hex>.bundle --pack x.bamod `
    --catalog <catalog.json> --address MyMod/Camo --type UnityEngine.GameObject
python my_bundle.py --sync-crc --bundle <bundle> --catalog <catalog.json>
python my_bundle.py --selfcheck --bundle <bundle> --catalog <catalog.json> --address MyMod/Camo
python my_bundle.py --install --bundle <bundle> --catalog <catalog.json> --game-root <游戏目录>
python my_bundle.py --list --workdir D:\mods
python my_bundle.py --self-test          # 全离线自测（用产品自带的小模板）
```
"""
import argparse
import glob
import hashlib
import os
import shutil
import sys
import time

_GAME_ROOTS = [
    "D:/Steam/steamapps/common/broken_arrow",
    "C:/Program Files (x86)/Steam/steamapps/common/broken_arrow",
    "C:/Program Files/Steam/steamapps/common/broken_arrow",
    "E:/Steam/steamapps/common/broken_arrow",
    "F:/Steam/steamapps/common/broken_arrow",
]
DEFAULT_WORKDIR = os.path.join(os.path.expanduser("~"), "BrokenArrow_Mods")


def _setup_paths():
    """把产品的 _rev_tools / _unitypy 加进 sys.path（打包 exe 与开发运行都适用）"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        internal = os.path.join(os.path.dirname(sys.executable), "_internal")
        rev = os.path.join(internal, "_rev_tools")
        unity = os.path.join(internal, "_unitypy")
    else:
        here = os.path.dirname(os.path.abspath(__file__))
        rev = os.path.join(here, "_rev_tools")
        unity = os.path.join(here, "_unitypy")
    for p in (rev, unity):
        if os.path.isdir(p) and p not in sys.path:
            sys.path.insert(0, p)
    try:
        import unitypy_path
        unitypy_path.ensure()
    except Exception:                                            # noqa: BLE001
        pass
    return rev


def find_game_root(explicit=None):
    """→ 游戏根目录（有 aa\\PC 的那个）"""
    for d in ([explicit] if explicit else []) + _GAME_ROOTS:
        if d and os.path.isdir(os.path.join(d, "BrokenArrow_Data", "StreamingAssets", "aa", "PC")):
            return d
    return None


def game_paths(game_root):
    aa = os.path.join(game_root, "BrokenArrow_Data", "StreamingAssets", "aa")
    return {"catalog": os.path.join(aa, "catalog.json"), "pc": os.path.join(aa, "PC")}


def list_templates(game_root):
    """可作为骨架的 bundle（小一点的更好：units 之外的图片包都很小）"""
    if not game_root:
        return []
    pc = game_paths(game_root)["pc"]
    out = []
    for f in sorted(glob.glob(os.path.join(pc, "*.bundle"))):
        out.append((os.path.getsize(f), f))
    return [f for _s, f in sorted(out)]                      # 按大小升序（小包当模板最省事）


def _bundled_dirs():
    """打包数据可能所在的目录：PyInstaller onedir 是 `sys._MEIPASS`（= `_internal`），源码运行是产品目录。

    （与 `ba_db_tool._bundled_data_dir()` 同一约定 —— `--add-data x;.` 的东西就落在这里）
    """
    out = []
    for d in (getattr(sys, "_MEIPASS", None), os.path.dirname(os.path.abspath(__file__))):
        if d and os.path.isdir(d) and d not in out:
            out.append(d)
    return out


def default_template(rev_dir=None):
    """没给模板时用产品自带的小 bundle（149 KB，纯骨架，内含 `_MountPointWarehouse` 等模板 prefab）"""
    cands = []
    if rev_dir:
        cands.append(os.path.join(os.path.dirname(rev_dir), "units_warehouse_small.bundle"))
    for d in _bundled_dirs():
        cands.append(os.path.join(d, "units_warehouse_small.bundle"))
        cands.append(os.path.join(d, "_rev_tools", "..", "units_warehouse_small.bundle"))
    for p in cands:
        p = os.path.abspath(p)
        if os.path.exists(p):
            return p
    return None


# ------------------------------------------------------------------ ① 新建

def new_bundle(name, workdir, template=None, log=print):
    """复制模板 → `<workdir>/<名字>_<32hex>.bundle`（名字里的 hex 是 Unity Addressables 的包名哈希）"""
    if not name or any(ch in name for ch in '\\/:*?"<>|'):
        raise ValueError("bundle 名字不合法：%r" % name)
    if not template or not os.path.exists(template):
        raise FileNotFoundError("模板 bundle 不存在：%s" % template)
    os.makedirs(workdir, exist_ok=True)
    stage = os.path.join(workdir, name + ".bundle")
    shutil.copy2(template, stage)
    hexv = hashlib.md5(open(stage, "rb").read()).hexdigest()[:32]
    final = os.path.join(workdir, "%s_%s.bundle" % (name, hexv))
    if os.path.exists(final):
        os.remove(final)
    os.replace(stage, final)
    log("✓ 新建：%s（%d 字节，骨架来自 %s）" % (final, os.path.getsize(final), os.path.basename(template)))
    return final


def list_my_bundles(workdir):
    """→ [(路径, 大小, mtime)]（工作目录里所有自建 bundle）"""
    out = []
    for f in glob.glob(os.path.join(workdir, "**", "*.bundle"), recursive=True):
        out.append((f, os.path.getsize(f), os.path.getmtime(f)))
    return sorted(out, key=lambda t: -t[2])


# ------------------------------------------------------------------ ② 加资产

def add_assets(bundle, pack=None, catalog=None, address=None,
               asset_path=None, asset_type="UnityEngine.GameObject",
               do_crc=True, log=print):
    """把资产加进"我的 bundle"：

    * `pack`（.bamod，Blender 导出）⇒ 走产品已有的 `import_pack.import_pack`（类型匹配/pid 重映射/容器条目）
    * 注册地址：catalog 里**还没有**这个 bundle 的键 ⇒ 走 `my_bundle_catalog.add_bundle_and_asset`
      （新建 bundle 条目 + extra）；已经有了 ⇒ 也走它（它会复用 bundle 条目，只追加资产）✓
    """
    if not os.path.exists(bundle):
        raise FileNotFoundError("bundle 不存在：%s" % bundle)
    rev = _setup_paths()
    n = 0
    if pack:
        if not os.path.exists(pack):
            raise FileNotFoundError("素材包不存在：%s" % pack)
        import import_pack
        # 有 catalog + address 时，import_pack 自己会"抄参考条目"注册地址 —— 那只适合
        # **游戏已有 bundle**。自建 bundle 必须由 my_bundle_catalog 建条目 ⇒ 这里分开做：
        n, new_root, crc, removed = import_pack.import_pack(
            pack, bundle, None, address=None, progress=(lambda m: log("  " + str(m))), do_crc=False)
        log("✓ 合并素材包：新增/更新对象 %d 个（清理旧对象 %d 个）" % (n, removed))
    if catalog and address:
        import my_bundle_catalog as MC
        # ⛔ 绝不往游戏目录写临时文件：catalog 副本写到"我的 bundle"旁边的工作目录里，
        #    只有用户点"安装"时才拷进游戏（install() 会先备份）✓
        out_cat = os.path.join(os.path.dirname(os.path.abspath(bundle)), "catalog_with_mybundle.json")
        r = MC.add_bundle_and_asset(catalog, bundle, address,
                                    asset_path or ("Assets/Mods/" + os.path.basename(bundle).split("_")[0] + ".prefab"),
                                    asset_type, out=out_cat, log=log)
        log("✓ 已注册（bundle 条目%s）→ %s" % ("复用" if r["reused_bundle"] else "新建", r["catalog"]))
        if do_crc:
            sync_crc(bundle, r["catalog"], log=log)
        return {"objects": n, "register": r}
    if do_crc and catalog and os.path.exists(catalog):
        sync_crc(bundle, catalog, log=log)
    return {"objects": n, "register": None}


# ------------------------------------------------------------------ ②b 加图片（图标/头像/标签图）
ICON_PID_BASE = 0x69636F6E00000000          # = b"icon"（与 `_rev_tools/make_icons.py` 同一段，便于辨认）


def list_image_assets(bundle, grep=None, limit=40):
    """列出 bundle 里可当"骨架"的图片资产路径（容器里以 .png/.jpg 结尾的条目，去重）"""
    _setup_paths()
    import UnityPy
    env = UnityPy.load(os.path.abspath(bundle))
    ab = next((o for o in env.objects if o.type.name == "AssetBundle"), None)
    if ab is None:
        return []
    d = ab.read()
    out = []
    for name, _info in d.m_Container:
        s = str(name)
        if s.lower().endswith((".png", ".jpg", ".jpeg", ".tga")) and s not in out:
            if not grep or grep.lower() in s.lower():
                out.append(s)
    return out[:limit]


def add_image(bundle, png, source_path=None, asset_path=None, log=print,
              tex_name=None, spr_name=None):
    r"""把一张 PNG 做成**新图标**加进 bundle：深拷贝源 `Texture2D` + `Sprite` → 换图 → 新容器条目 + preload 重映射。

    为什么必须**两个对象一起**深拷贝（配方来自 `_rev_tools/make_icons.py`，实测可用）：
      Unity 的图标是 **`Texture2D` + `Sprite` 两个对象**，容器里是**两条同名条目**；
      只换贴图、Sprite 仍指向旧贴图 = **没换** ✗ ⇒ Sprite 深拷贝时必须把 texture 的 PPtr 重映射到新贴图 ✓
      （另一个易漏点：**`m_PreloadTable` 要同步追加并重映射** —— 漏了会"资产在包里但加载不出来"）

    尺寸策略（v1）：新图会**缩放到源贴图的尺寸** ⇒ Sprite 的 rect/几何保持与源一致，最稳 ✓
    （想改尺寸要另外改 Sprite 的 `m_Rect` 等字段，属于后续版本）

    返回 dict(texture_pid, sprite_pid, asset_path, size, scaled)
    """
    import shutil
    import struct
    import UnityPy
    from UnityPy.files.ObjectReader import ObjectReader
    from UnityPy.classes.PPtr import PPtr
    from UnityPy.classes.generated import AssetInfo
    from PIL import Image
    _setup_paths()

    if not os.path.exists(bundle):
        raise FileNotFoundError("bundle 不存在：%s" % bundle)
    if not os.path.exists(png):
        raise FileNotFoundError("PNG 不存在：%s" % png)

    env = UnityPy.load(os.path.abspath(bundle))
    objs = list(env.objects)
    ab = next((o for o in objs if o.type.name == "AssetBundle"), None)
    if ab is None:
        raise ValueError("这个 bundle 里没有 AssetBundle 对象 ⇒ 不是能挂资产的那种包")
    d = ab.read()
    by_pid = {o.path_id: o for o in objs}
    sf = ab.assets_file
    existing = set(sf.objects.keys())

    # ---- 找源：容器里同名条目下的 Texture2D 与 Sprite ----
    if not source_path:
        cands = list_image_assets(bundle, limit=1)
        if not cands:
            raise ValueError("bundle 里没有可当骨架的图片资产（容器里没有 .png 条目）")
        source_path = cands[0]
        log("  （未给 --source-path，用第一个图片资产当骨架：%s）" % source_path)
    src_pids = [info.asset.m_PathID for name, info in d.m_Container if str(name) == source_path]
    if not src_pids:
        raise ValueError("容器里没有资产路径 %r（可用 `my_bundle.py --list-images` 看）" % source_path)
    tex_obj = next((by_pid[p] for p in src_pids if p in by_pid and by_pid[p].type.name == "Texture2D"), None)
    spr_obj = next((by_pid[p] for p in src_pids if p in by_pid and by_pid[p].type.name == "Sprite"), None)
    if tex_obj is None:
        raise ValueError("资产路径 %r 下没找到 Texture2D（这一条不能当图标骨架）" % source_path)

    ctr = 0

    def alloc():
        nonlocal ctr
        while True:
            c = ICON_PID_BASE + ctr
            ctr += 1
            if c not in existing:
                existing.add(c)
                return c

    base_name = (asset_path or source_path).rsplit("/", 1)[-1].rsplit(".", 1)[0]
    # ---- ① 贴图：换图 → 新 pid ----
    tex = tex_obj.read()
    base = tex.image.convert("RGBA")
    src_size = base.size
    rect = _sprite_texture_rect(env, spr_obj.path_id) if spr_obj is not None else None
    img = Image.open(png).convert("RGBA")
    # ★ 第 67 轮修正：按 **Sprite 内容区** 保比例居中放（以前缩放到整张贴图 ⇒ 会被 Sprite 裁成一条 ⇒ 变形 ✗）
    final = _paste_into_rect(base, img, rect)
    if rect:
        log("  尺寸：按 Sprite 内容区 %dx%d 保比例居中（原贴图 %dx%d，其余区域保持原样）"
            % (int(round(rect[2])), int(round(rect[3])), src_size[0], src_size[1]))
    else:
        log("  尺寸：没有 Sprite 内容区信息 ⇒ 直接铺满整张贴图 %dx%d" % (src_size[0], src_size[1]))
    tex.set_image(final, target_format=tex.m_TextureFormat)
    tex.m_Name = tex_name or base_name
    new_tex_pid = alloc()
    nt = ObjectReader(assets_file=sf, reader=tex_obj.reader, path_id=new_tex_pid,
                      type_id=tex_obj.type_id, serialized_type=tex_obj.serialized_type,
                      class_id=tex_obj.class_id, type=tex_obj.type, byte_start=tex_obj.byte_start,
                      byte_size=0, is_destroyed=tex_obj.is_destroyed, is_stripped=tex_obj.is_stripped)
    nt.save_typetree(tex)
    sf.objects[new_tex_pid] = nt
    log("  ✓ 贴图：%s → 新 pid %d（%sx%s，格式 %s）"
        % (tex_obj.path_id, new_tex_pid, src_size[0], src_size[1], tex.m_TextureFormat))
    new_spr_pid, spr_preload = None, None
    # ---- ② Sprite：深拷贝裸字节 + 重映射 texture PPtr ----
    if spr_obj is not None:
        raw = bytearray(spr_obj.get_raw_data())
        old = struct.pack("<q", tex_obj.path_id)
        new = struct.pack("<q", new_tex_pid)
        if old not in raw:
            log("  ⚠ Sprite 裸字节里没找到指向旧贴图的 pathID ⇒ 这个 Sprite 可能不是引用它（仍按原样拷贝）")
        raw = raw.replace(old, new)
        new_spr_pid = alloc()
        ns = ObjectReader(assets_file=sf, reader=spr_obj.reader, path_id=new_spr_pid,
                          type_id=spr_obj.type_id, serialized_type=spr_obj.serialized_type,
                          class_id=spr_obj.class_id, type=spr_obj.type, byte_start=spr_obj.byte_start,
                          byte_size=len(raw), is_destroyed=spr_obj.is_destroyed,
                          is_stripped=spr_obj.is_stripped, data=bytes(raw))
        sf.objects[new_spr_pid] = ns
        log("  ✓ Sprite：%s → 新 pid %d（texture 已重映射）" % (spr_obj.path_id, new_spr_pid))
    else:
        log("  ⚠ 源资产里没有 Sprite（只拷了贴图）；图标类资产正常应有 Sprite，请确认骨架选对了")

    # ---- ③ 容器条目（两条同名：贴图 + Sprite）+ preload 重映射 ----
    new_path = asset_path or source_path
    tex_info = next(info for name, info in d.m_Container if str(name) == source_path
                    and info.asset.m_PathID == tex_obj.path_id)
    d.m_Container.append((new_path, AssetInfo(
        asset=PPtr(m_FileID=0, m_PathID=new_tex_pid, assetsfile=sf),
        preloadIndex=tex_info.preloadIndex, preloadSize=tex_info.preloadSize)))
    if spr_obj is not None:
        spr_info = next(info for name, info in d.m_Container if str(name) == source_path
                        and info.asset.m_PathID == spr_obj.path_id)
        preload_map = {tex_obj.path_id: new_tex_pid, spr_obj.path_id: new_spr_pid}
        idx, sz = spr_info.preloadIndex, spr_info.preloadSize
        new_preload = []
        for pptr in d.m_PreloadTable[idx:idx + sz]:
            tgt = preload_map.get(pptr.m_PathID, pptr.m_PathID)
            new_preload.append(PPtr(m_FileID=pptr.m_FileID, m_PathID=tgt, assetsfile=sf))
        new_start = len(d.m_PreloadTable)
        d.m_PreloadTable.extend(new_preload)
        d.m_Container.append((new_path, AssetInfo(
            asset=PPtr(m_FileID=0, m_PathID=new_spr_pid, assetsfile=sf),
            preloadIndex=new_start, preloadSize=sz)))
        log("  ✓ 容器：+2 条条目（路径 %s）；preload 追加 %d 项并重映射" % (new_path, sz))
    ab.save_typetree(d)

    # ---- ④ 原子写回 ----
    data = env.file.save(packer="lz4")
    tmp = bundle + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    shutil.move(tmp, bundle)
    log("  ✓ bundle 已写回：%s（%.2f MB）" % (os.path.basename(bundle), os.path.getsize(bundle) / 1048576))
    return {"texture_pid": new_tex_pid, "sprite_pid": new_spr_pid, "asset_path": new_path,
            "size": list(final.size), "rect": list(rect) if rect else None}


# ------------------------------------------------------------------ ②c 替换现有图标（整包替换法）
def _tex_entries(bundle):
    """→ [(容器路径, Texture2D pid, Sprite pid|None)]（同名条目配对）"""
    _setup_paths()
    import UnityPy
    env = UnityPy.load(os.path.abspath(bundle))
    ab = next((o for o in env.objects if o.type.name == "AssetBundle"), None)
    if ab is None:
        return {}, env
    by_pid = {o.path_id: o for o in env.objects}
    d = ab.read()
    groups = {}
    for name, info in d.m_Container:
        pid = info.asset.m_PathID
        o = by_pid.get(pid)
        if o is None:
            continue
        g = groups.setdefault(str(name), {"tex": None, "spr": None})
        if o.type.name == "Texture2D" and g["tex"] is None:
            g["tex"] = pid
        elif o.type.name == "Sprite" and g["spr"] is None:
            g["spr"] = pid
    return groups, env


def list_replaceable(bundle, grep=None, limit=0):
    """列出"可以就地替换贴图"的资产路径（有 Texture2D 的那些）"""
    groups, _env = _tex_entries(bundle)
    out = [k for k, g in sorted(groups.items()) if g["tex"] and (not grep or grep.lower() in k.lower())]
    return out[:limit] if limit else out


def _sprite_texture_rect(env, spr_pid):
    """Sprite 实际采样的贴图子区域（`m_RD.textureRect`，Unity 原点在**左下**）。

    ⛔ 为什么必须用它（2026-10 第 67 轮实测）：图标贴图往往比可见内容大
    （实测 ammoicons：贴图 288×56，而 Sprite 只采样 x=4.1..228、高 55 的区域，左右边距全透明）。
    以前把用户的图**缩放到整张贴图尺寸** ⇒ 图先被压成宽条、再被 Sprite 裁出一个子条 ⇒ **画面变形** ✗
    正确做法：**按这个内容区保比例居中放**，其余区域保持原样 ✓
    """
    by = {o.path_id: o for o in env.objects}
    o = by.get(spr_pid)
    if o is None:
        return None
    try:
        s = o.read()
        tr = s.m_RD.textureRect
        return (float(tr.x), float(tr.y), float(tr.width), float(tr.height))
    except Exception:                                            # noqa: BLE001
        return None


def _paste_into_rect(base_img, new_img, rect, mode="fit"):
    """把 `new_img` 放进 `base_img` 的 `rect` 区域（Unity 左下原点 ⇒ 换算成 PIL 顶边）。

    * `mode="fit"`（默认）：**保持比例**、居中、四周补透明 ✓（不变形）
    * `mode="stretch"`：拉伸铺满该区域（想填满整格时用）
    返回新图；`rect=None` 时退化成"整张替换"（旧行为）。
    """
    from PIL import Image, ImageOps
    canvas = base_img.convert("RGBA").copy()
    if rect is None:
        return new_img.convert("RGBA").resize(canvas.size, Image.LANCZOS)
    rx, ry, rw, rh = rect
    W, H = canvas.size
    w, h = max(1, int(round(rw))), max(1, int(round(rh)))
    x = max(0, min(W - 1, int(round(rx))))
    y = max(0, min(H - 1, int(round(H - (ry + rh)))))       # Unity 左下 → PIL 左上
    w = min(w, W - x)
    h = min(h, H - y)
    fitted = (new_img.convert("RGBA").resize((w, h), Image.LANCZOS) if mode == "stretch"
              else ImageOps.contain(new_img.convert("RGBA"), (w, h), Image.LANCZOS))
    canvas.paste((0, 0, 0, 0), (x, y, x + w, y + h))         # 先清空该区域（别让旧图标透出来）
    canvas.alpha_composite(fitted, (x + (w - fitted.size[0]) // 2,
                                    y + (h - fitted.size[1]) // 2))
    return canvas


def replace_image(bundle, png, target_path, log=print, scale=True, mode="fit"):
    r"""**就地替换** bundle 里某个图标的贴图（同一个 pid ⇒ 所有引用它的 Sprite/地址**自动跟着变**）。

    与 `add_image()` 的区别（两条路各有用途）：
      * `add_image` = **加一个新图标 + 新地址**（要再去改 DB 指过去）
      * `replace_image` = **把现有图标换成你的图**（地址不变 ⇒ **不用改 DB** ✓）
        —— 配合 `repoint_bundle()`（把 catalog 里那个包的条目指向你的副本），
        游戏里所有引用这些图标的界面**直接变成你的图** ✓（整包替换法，T2 已实机验证过机制）

    ★ 尺寸策略（第 67 轮修正）：按 **Sprite 的内容区**（`m_RD.textureRect`）**保比例居中**放
    （`mode="stretch"` 可改成拉伸铺满）；`scale=False` 时**不做任何缩放**（图大于内容区会被居中裁掉）
    ⚠ 只能改贴图：Sprite 的几何（烘在 `m_RD.m_VertexData` 里的四边形）保持不变
    """
    import shutil
    import UnityPy
    from PIL import Image
    _setup_paths()
    groups, env = _tex_entries(bundle)
    if target_path not in groups or not groups[target_path]["tex"]:
        raise ValueError("这个包里没有可替换的贴图资产 %r（用 --list-images 看有哪些）" % target_path)
    by_pid = {o.path_id: o for o in env.objects}
    tex_obj = by_pid[groups[target_path]["tex"]]
    tex = tex_obj.read()
    base = tex.image.convert("RGBA")
    rect = _sprite_texture_rect(env, groups[target_path]["spr"]) if groups[target_path]["spr"] else None
    img = Image.open(png).convert("RGBA")
    if not scale and rect:
        # 不缩放：把图居中贴进内容区（超出部分被裁）
        canvas = base.copy()
        rx, ry, rw, rh = rect
        W, H = canvas.size
        w, h = int(round(rw)), int(round(rh))
        x = int(round(rx))
        y = int(round(H - (ry + rh)))
        canvas.paste((0, 0, 0, 0), (x, y, x + w, y + h))
        canvas.alpha_composite(img, (x + (w - img.size[0]) // 2, y + (h - img.size[1]) // 2))
        final = canvas
    else:
        final = _paste_into_rect(base, img, rect, mode=mode)
        if rect:
            log("  尺寸：按 Sprite 内容区 %dx%d %s（原贴图 %dx%d，其余区域保持原样）"
                % (int(round(rect[2])), int(round(rect[3])),
                   "拉伸铺满" if mode == "stretch" else "保比例居中", base.size[0], base.size[1]))
    tex.set_image(final, target_format=tex.m_TextureFormat)
    tex_obj.save_typetree(tex)                       # ★ 同一个 pid：容器/preload 都不用动 ✓
    data = env.file.save(packer="lz4")
    tmp = bundle + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    shutil.move(tmp, bundle)
    log("  ✓ 已就地替换：%s（贴图 pid %d，%dx%d）"
        % (target_path, tex_obj.path_id, final.size[0], final.size[1]))
    return target_path


def replace_by_name(bundle, png_dir, grep=None, log=print, pick_first=False, mode="fit", scale=True):
    """**批量就地替换**：目录里每个 PNG 按**文件名**（不带扩展名、大小写不敏感）匹配包里的资产名 ✓

    这是 mod 作者最常用的姿势：把要换的图标按**原图标名**存进一个文件夹，一条命令全换掉 ✓
    * 默认：**多个候选匹配同一个 PNG 时跳过并报出来**（不猜）—— 游戏里同名变体很常见
      （如 `Icons/AMMO_x.png` 与 `Icons/outline/AMMO_x.png` 并存）；想全换就用 `--grep` 过滤，
      或 `pick_first=True` 只换第一个候选（在批量日志里会逐条列出来）
    """
    names = list_replaceable(bundle, grep)
    by_base = {}
    for n in names:
        base = n.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
        by_base.setdefault(base, []).append(n)
    pngs = sorted([p for p in os.listdir(png_dir)
                   if p.lower().endswith((".png", ".jpg", ".jpeg"))])
    done, miss, amb = [], [], []
    for p in pngs:
        base = p.rsplit(".", 1)[-1].rsplit(".", 1)[0].lower() if "." in p else p.lower()
        base = p.rsplit(".", 1)[0].lower()
        cand = by_base.get(base) or []
        if not cand:
            miss.append(p)
            continue
        if len(cand) > 1 and not pick_first:
            amb.append((p, cand))
            continue
        targets = cand if (pick_first and len(cand) > 1) else cand[:1]
        for t in targets:
            replace_image(bundle, os.path.join(png_dir, p), t, log=log, mode=mode, scale=scale)
            done.append((p, t))
    log("批量替换：成功 %d / 没找到同名资产 %d / 有歧义跳过 %d%s"
        % (len(done), len(miss), len(amb), "（--pick-first：歧义取第一个）" if pick_first else ""))
    for p in miss[:8]:
        log("  ⚠ 包里没有同名资产：%s" % p)
    for p, cand in amb[:8]:
        log("  ⚠ 有歧义（多个候选），已跳过：%s → %s" % (p, cand[:3]))
    return {"done": done, "missing": miss, "ambiguous": amb}


def repoint_bundle(catalog_in, old_key, bundle, catalog_out=None, log=print):
    r"""把 catalog 里**已有 bundle 条目**改指向我的文件（**地址键不变** ⇒ 不用改 DB）✓

    这是 T2 实机验证过的机制（"新包体被游戏加载" ✓）：
      ① 找到键 == `old_key` 的那条 bundle 条目（形如 `ammoicons_assets_all_<32hex>.bundle`）
      ② 把它的 `internalId` 改成 `{RuntimePath}\\PC\\<我的文件名>`
      ③ 重写它的 extra（`m_Hash`/`m_Crc`/`m_BundleSize` 换成我的文件的）
    ⇒ 游戏按原键加载时，读到的就是**我的包** ⇒ 那个包里所有原地址都变成我的内容 ✓
    """
    import json as _json
    import re
    from compute_bundle_crc import compute_bundle_crc
    from catalog_mod import Catalog, read_object, write_object
    name = os.path.basename(bundle)
    if not os.path.exists(bundle):
        raise FileNotFoundError("bundle 不存在：%s" % bundle)
    crc = compute_bundle_crc(bundle)
    hexes = re.findall(r"[0-9a-fA-F]{32}", name)
    md5hex = hexes[-1].lower() if hexes else hashlib.md5(open(bundle, "rb").read()).hexdigest()
    c = Catalog(catalog_in)
    ids = c.cat["m_InternalIds"]
    want = old_key or ""
    hit = next((i for i, k in enumerate(c.keys)
                if (k[1] if isinstance(k, tuple) and len(k) > 1 else k) == want), None)
    if hit is None:
        raise ValueError("catalog 里没有键 %r（原包名要对得上，如 ammoicons_assets_all_<hex>.bundle）" % old_key)
    for eidx in c.buckets[hit]["entries"]:
        e = list(c.entries[eidx])
        old_iid = ids[e[0]] if e[0] < len(ids) else "?"
        ids[e[0]] = "{UnityEngine.AddressableAssets.Addressables.RuntimePath}\\PC\\" + name
        if e[4] != 0xFFFFFFFF:
            obj, _ = read_object(c.extra, e[4])
            if obj[0] == 7:
                jt = obj[1][2]
                jt = re.sub(r'"m_Crc"\s*:\s*\d+', '"m_Crc":%d' % crc, jt)
                jt = re.sub(r'"m_BundleSize"\s*:\s*\d+', '"m_BundleSize":%d' % os.path.getsize(bundle), jt)
                jt = re.sub(r'"m_Hash"\s*:\s*"[0-9a-fA-F]*"', '"m_Hash":"%s"' % md5hex, jt)
                jt = re.sub(r'"m_BundleName"\s*:\s*"[^"]*"', '"m_BundleName":"%s"' % name, jt)
                tmp = bytearray(c.extra or b"")
                off = len(tmp)
                write_object(tmp, 7, (obj[1][0], obj[1][1], jt))
                c.extra = bytes(tmp)
                e[4] = off
        c.entries[eidx] = tuple(e)
        log("  ✓ 条目[%d]：internalId %s → %s（CRC=%d m_Hash=%s）"
            % (eidx, old_iid, ids[e[0]], crc, md5hex))
    out = catalog_out or catalog_in
    c.save(out)
    log("✓ catalog 已重指向：键 %r 现在指向 %s → %s" % (old_key, name, out))
    return {"catalog": out, "crc": crc, "md5hex": md5hex, "bundle": name}


# ------------------------------------------------------------------ ④ 同步 CRC

def sync_crc(bundle, catalog, log=print):
    """重算 bundle 的 m_Crc/m_BundleSize/m_Hash 并写回 catalog（**资产变了就必须跑**）"""
    from compute_bundle_crc import compute_bundle_crc
    from catalog_mod import Catalog, read_object, write_object
    import json as _json
    import re
    name = os.path.basename(bundle)
    crc = compute_bundle_crc(bundle)
    hexes = re.findall(r"[0-9a-fA-F]{32}", name)
    md5hex = hexes[-1].lower() if hexes else hashlib.md5(open(bundle, "rb").read()).hexdigest()
    c = Catalog(catalog)
    ids = c.cat.get("m_InternalIds") or []
    hit = None
    for i, k in enumerate(c.keys):
        if (k[1] if isinstance(k, tuple) and len(k) > 1 else k) == name:
            hit = i
            break
    if hit is None:
        raise ValueError("catalog 里没有 %s 的键 ⇒ 先注册（--add 带地址）再同步 CRC" % name)
    changed = 0
    for eidx in c.buckets[hit]["entries"]:
        e = c.entries[eidx]
        if e[4] == 0xFFFFFFFF:
            continue
        obj, _ = read_object(c.extra, e[4])
        if obj[0] != 7:
            continue
        jt = obj[1][2]
        new_jt = re.sub(r'"m_Crc"\s*:\s*\d+', '"m_Crc":%d' % crc, jt)
        new_jt = re.sub(r'"m_BundleSize"\s*:\s*\d+', '"m_BundleSize":%d' % os.path.getsize(bundle), new_jt)
        new_jt = re.sub(r'"m_Hash"\s*:\s*"[0-9a-fA-F]*"', '"m_Hash":"%s"' % md5hex, new_jt)
        buf = bytearray(c.extra)
        # 原地等长改写（JSON 里数字位数可能变，所以整段重写：先记旧长度，不足补 0 再改长度前缀）
        old_len = len(jt.encode("utf-8"))
        re_write = None
        tmp = bytearray(c.extra or b"")
        off = len(tmp)
        write_object(tmp, 7, (obj[1][0], obj[1][1], new_jt))
        if len(tmp) - off == old_len + 4:
            # 长度没变 ⇒ 直接覆盖这段
            c.extra = bytes(tmp)
        else:
            # 长度变了 ⇒ 追加一份新 extra，并让条目指向它（旧的不动，最省事也最安全）
            c.extra = bytes(tmp)
            e = list(c.entries[eidx])
            e[4] = off
            c.entries[eidx] = tuple(e)
        changed += 1
    c.save(catalog)
    log("✓ CRC 同步：%s → m_Crc=%d（0x%08X）m_BundleSize=%d m_Hash=%s（改了 %d 条）"
        % (name, crc, crc, os.path.getsize(bundle), md5hex, changed))
    return crc


# ------------------------------------------------------------------ ⑤ 安装 / 还原

def install(bundle, catalog, game_root, keep_backup=True, log=print):
    """把"我的 bundle"装进游戏：bundle → aa\\PC\\，catalog → aa\\catalog.json（先备份 .bak_<时间>）"""
    gp = game_paths(game_root)
    os.makedirs(gp["pc"], exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dst_bundle = os.path.join(gp["pc"], os.path.basename(bundle))
    if keep_backup and os.path.exists(gp["catalog"]):
        shutil.copy2(gp["catalog"], gp["catalog"] + ".bak_" + stamp)
        log("已备份 catalog → %s" % (os.path.basename(gp["catalog"]) + ".bak_" + stamp))
    shutil.copy2(bundle, dst_bundle)
    shutil.copy2(catalog, gp["catalog"])
    log("✓ 已安装：%s → %s" % (os.path.basename(bundle), gp["pc"]))
    log("✓ 已安装：%s → catalog.json" % os.path.basename(catalog))
    return dst_bundle


def restore_catalog(game_root, log=print):
    """最近一次备份还原 catalog（bundle 文件不动，避免误删）"""
    gp = game_paths(game_root)
    baks = sorted(glob.glob(gp["catalog"] + ".bak_*"))
    if not baks:
        raise FileNotFoundError("没有备份可还原：%s" % gp["catalog"])
    shutil.copy2(baks[-1], gp["catalog"])
    log("✓ 已还原 catalog（来自 %s）" % os.path.basename(baks[-1]))
    return baks[-1]


# ------------------------------------------------------------------ ⑥ 自检

def selfcheck(bundle, catalog=None, address=None, log=print):
    """重开 bundle（UnityPy）列容器条目 + 复算 CRC + （有 catalog/address 时）走一遍注册链"""
    rev = _setup_paths()
    ok = True
    import UnityPy
    env = UnityPy.load(os.path.abspath(bundle))
    names = []
    for o in env.objects:
        try:
            for c in getattr(o, "container", None) or []:
                names.append(c)
        except Exception:                                        # noqa: BLE001
            pass
    try:
        for f in env.files.values():
            for k in getattr(f, "container", {}) or {}:
                names.append(k)
    except Exception:                                            # noqa: BLE001
        pass
    names = sorted(set(names))
    log("① bundle 可打开：%d 个对象，容器条目 %d 个" % (len(env.objects), len(names)))
    for n in names[:12]:
        log("   · %s" % n)
    from compute_bundle_crc import compute_bundle_crc
    crc = compute_bundle_crc(bundle)
    log("② CRC 复算 = %d（0x%08X），文件 %d 字节" % (crc, crc, os.path.getsize(bundle)))
    if catalog and address:
        import my_bundle_catalog as MC
        chain_ok = MC.verify_chain(catalog, address, os.path.dirname(os.path.abspath(bundle)),
                                   os.path.basename(bundle), log=log)
        log("③ 注册链复核：%s" % ("✓ 通过" if chain_ok else "✗ 有问题"))
        ok = ok and chain_ok
    return {"ok": ok, "objects": len(env.objects), "container": names, "crc": crc}


# ------------------------------------------------------------------ 全离线自测

def make_pack(bundle_path, pack_path, prefab_path, root_pid=None):
    r"""从 bundle 里挑一个 GameObject **原样取出**，打成最小 `.bamod`（不需要 Blender）。

    格式与 Blender 插件导出的一致（见 `_rev_tools/import_pack.py` 的读取约定）：
    `manifest.json = {format, prefab_path, root_pid, objects[{pid,class_id,script_id,tree_hash,raw}], preload}`
    ⛔ 别自己发明字段名 —— import_pack 认的是这一套（踩过：字段名不对会静默少对象）。
    """
    import base64
    import json as _json
    import zipfile
    import UnityPy
    env = UnityPy.load(os.path.abspath(bundle_path))
    gobj = None
    for o in env.objects:
        if o.type.name == "GameObject" and (root_pid is None or o.path_id == root_pid):
            gobj = o
            break
    if gobj is None:
        return None
    manifest = {
        "format": "bamod-assets",
        "prefab_path": prefab_path,
        "root_pid": gobj.path_id,
        "objects": [{"pid": gobj.path_id, "class_id": 1, "script_id": None,
                     "tree_hash": None, "raw": base64.b64encode(gobj.get_raw_data()).decode()}],
        "preload": [],
    }
    with zipfile.ZipFile(pack_path, "w") as z:
        z.writestr("manifest.json", _json.dumps(manifest))
    return gobj.path_id


def pick_template(game_root=None, rev_dir=None, max_mb=40, log=print):
    """挑一个"有 GameObject + 有容器条目"的小 bundle 当骨架（自测/新建都靠它）

    小包优先：产品自带的小包 → 游戏 aa\\PC 里 < max_mb 的最小合格包。
    """
    rev_dir = rev_dir or _setup_paths()
    import UnityPy
    cands = []
    own = default_template(rev_dir)
    if own:
        cands.append(own)
    root = find_game_root(game_root)
    if root:
        pc = game_paths(root)["pc"]
        fs = [os.path.join(pc, f) for f in os.listdir(pc) if f.endswith(".bundle")]
        fs.sort(key=os.path.getsize)
        cands += [f for f in fs if os.path.getsize(f) < max_mb * 1048576]
    for p in cands:
        try:
            env = UnityPy.load(os.path.abspath(p))
        except Exception:                                        # noqa: BLE001
            continue
        if any(o.type.name == "GameObject" for o in env.objects) and len(env.container or {}) > 0:
            return p
    return None


def self_test(workdir=None, log=print):
    """不依赖 Blender 的全链自测：模板 → 合成 .bamod → 新建我的 bundle → 合并 → 注册 → CRC → 自检

    判据（与 `测试/test_make_mod_bundle.py` 同源）：
      ① 合成的最小 .bamod 能被 import_pack 接受（对象数正确）
      ② 合并后 bundle 能被 UnityPy 打开，**容器条目里出现新资产路径**
      ③ CRC 能复算出来
      ④ 注册链（地址→条目→依赖键→bundle→m_Crc）全部对得上，且磁盘 CRC == catalog 里的
    """
    rev = _setup_paths()
    import tempfile
    workdir = workdir or tempfile.mkdtemp(prefix="mybundle_selftest_")
    os.makedirs(workdir, exist_ok=True)
    state = {"ok": True}

    def chk(name, cond, detail=""):
        state["ok"] = state["ok"] and bool(cond)
        log("   %s %s%s" % ("✓" if cond else "✗", name, ("  —— " + str(detail)) if detail else ""))

    tmpl = pick_template(rev_dir=rev, log=log)
    if not tmpl:
        log("✗ 找不到可用骨架（要有 GameObject + 容器条目的小 bundle）")
        return {"ok": False, "workdir": workdir}
    log("骨架：%s（%.1f MB）" % (os.path.basename(tmpl), os.path.getsize(tmpl) / 1048576))

    prefab = "Assets/Mods/SelfTest.prefab"
    pack = os.path.join(workdir, "selftest.bamod")
    pid = make_pack(tmpl, pack, prefab)
    chk("① 合成最小 .bamod", pid is not None and os.path.exists(pack), "root_pid=%s" % pid)
    if pid is None:
        return {"ok": False, "workdir": workdir}

    root = find_game_root()
    cat = game_paths(root)["catalog"] if root else None
    if not cat or not os.path.exists(cat):
        log("⚠ 没找到游戏 catalog ⇒ 只测「建包+合并」，跳过 catalog 注册（判据 ④）")
    try:
        bundle = new_bundle("SelfTest", workdir, tmpl, log=log)
        addr = "MyBundleSelfTest/Asset"
        r = add_assets(bundle, pack=pack, catalog=cat, address=addr,
                       asset_path=prefab, asset_type="UnityEngine.GameObject",
                       do_crc=True, log=log)
        chk("② 合并素材包", r["objects"] >= 1, "对象 %d" % r["objects"])
        res = selfcheck(bundle, r["register"]["catalog"] if r["register"] else None,
                        addr if r["register"] else None, log=log)
        chk("③ bundle 可打开 + CRC 可复算", res["crc"] > 0, "CRC=%d 对象=%d" % (res["crc"], res["objects"]))
        chk("④ 容器条目里出现新资产路径", any("SelfTest" in n or "Mods" in n for n in res["container"]),
            res["container"][:3])
        if r["register"]:
            chk("④b 注册链复核通过（含 CRC 对账）", res["ok"])
    except Exception as e:                                        # noqa: BLE001
        chk("②/③/④ 全链跑通", False, "%s: %s" % (type(e).__name__, e))
    log("自测工作目录：%s" % workdir)
    return {"ok": state["ok"], "workdir": workdir}


# ------------------------------------------------------------------ UI（tkinter 对话框）

class MyBundleDialog:
    """"我的 bundle"管理器（tkinter Toplevel）。

    设计要点（跟 `mod_assets.py` 的对话框保持一致）：
      * 所有重活放**后台线程**，用 queue 回传日志，避免卡 UI
      * 每一步都能单独点（新建 / 加资产 / 同步 CRC / 自检 / 安装）+ 一键"全流程"
      * **不往游戏目录写任何临时文件**：catalog 副本先落在工作目录，点"安装"才拷进去（带备份）
    """

    def __init__(self, master):
        import tkinter as tk
        from tkinter import ttk
        import queue
        self.tk = tk
        self.ttk = ttk
        self.queue = queue
        self.workdir = DEFAULT_WORKDIR
        self.game_root = find_game_root()
        self._setup_paths()

        self.win = tk.Toplevel(master)
        self.win.title("我的 bundle —— 自建独立资源包（游戏按 catalog 地址读取）")
        # ⚠ 尺寸别乱调小：面板③ 的控件已经排到第 12 行，窗口太矮会把「④ 收尾」整排挤出可视区 ✗
        #    （`测试\test_my_bundle_dialog.py` 的判据 ③ 就是量这个：所有按钮必须 mapped 且在窗口内）
        self.win.geometry("1000x900")
        self.win.transient(master)

        top = ttk.Frame(self.win, padding=10)
        top.pack(fill="both", expand=True)
        frm = ttk.LabelFrame(top, text="① 工作区", padding=8)
        frm.pack(fill="x")
        self.workdir_var = tk.StringVar(value=self.workdir)
        ttk.Label(frm, text="工作目录：").grid(row=0, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.workdir_var, width=70).grid(row=0, column=1, sticky="we")
        ttk.Button(frm, text="浏览…", command=self._browse_workdir).grid(row=0, column=2, padx=4)
        ttk.Button(frm, text="刷新列表", command=self._refresh).grid(row=0, column=3)
        self.game_var = tk.StringVar(value=self.game_root or "（没找到游戏目录）")
        ttk.Label(frm, text="游戏目录：").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Label(frm, textvariable=self.game_var, foreground="#555").grid(
            row=1, column=1, columnspan=3, sticky="w", pady=(6, 0))
        frm.columnconfigure(1, weight=1)

        frm2 = ttk.LabelFrame(top, text="② 我的 bundle（选一个继续加资产）", padding=8)
        frm2.pack(fill="x", pady=(8, 0))
        self.listbox = tk.Listbox(frm2, height=6)
        self.listbox.grid(row=0, column=0, columnspan=4, sticky="we")
        self.listbox.bind("<<ListboxSelect>>", lambda _e: self._on_pick())
        frm2.columnconfigure(0, weight=1)

        frm3 = ttk.LabelFrame(top, text="③ 新建 / 加资产", padding=8)
        frm3.pack(fill="x", pady=(8, 0))
        self.name_var = tk.StringVar(value="MyMod")
        self.tmpl_var = tk.StringVar(value="")
        self.pack_var = tk.StringVar(value="")
        self.addr_var = tk.StringVar(value="MyMod/Asset")
        self.asset_var = tk.StringVar(value="Assets/Mods/MyMod.prefab")
        self.type_var = tk.StringVar(value="UnityEngine.GameObject")
        ttk.Label(frm3, text="名字：").grid(row=0, column=0, sticky="w")
        ttk.Entry(frm3, textvariable=self.name_var, width=24).grid(row=0, column=1, sticky="w")
        ttk.Button(frm3, text="新建 bundle", command=lambda: self._run("new")).grid(row=0, column=2, padx=4)
        ttk.Label(frm3, text="骨架模板：").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(frm3, textvariable=self.tmpl_var, width=64).grid(row=1, column=1, columnspan=2, sticky="we",
                                                                 pady=(6, 0))
        ttk.Button(frm3, text="选模板…", command=self._browse_template).grid(row=1, column=3, padx=4,
                                                                            pady=(6, 0))
        ttk.Label(frm3, text="素材包(.bamod)：").grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(frm3, textvariable=self.pack_var, width=64).grid(row=2, column=1, columnspan=2, sticky="we",
                                                                  pady=(6, 0))
        ttk.Button(frm3, text="选素材包…", command=self._browse_pack).grid(row=2, column=3, padx=4, pady=(6, 0))
        ttk.Label(frm3, text="地址：").grid(row=3, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(frm3, textvariable=self.addr_var, width=30).grid(row=3, column=1, sticky="w", pady=(6, 0))
        ttk.Label(frm3, text="bundle 内路径：").grid(row=4, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(frm3, textvariable=self.asset_var, width=30).grid(row=4, column=1, sticky="w", pady=(6, 0))
        self.types_box = ttk.Combobox(frm3, textvariable=self.type_var, width=34, values=[
            "UnityEngine.GameObject", "UnityEngine.Texture2D", "UnityEngine.Sprite",
            "UnityEngine.Material", "UnityEngine.Mesh", "UnityEngine.AudioClip",
            "UnityEngine.TextAsset", "UnityEngine.AnimationClip"])
        ttk.Label(frm3, text="资产类型：").grid(row=4, column=2, sticky="e", pady=(6, 0))
        self.types_box.grid(row=4, column=3, sticky="we", pady=(6, 0))
        ttk.Button(frm3, text="把素材加进这个 bundle", command=lambda: self._run("add")).grid(
            row=5, column=1, sticky="w", pady=(8, 0))
        # --- ③b 图片（图标/头像/标签图）：深拷贝骨架 → 换图 → 新地址（v1.8.100 起）---
        self.png_var = tk.StringVar(value="")
        self.src_var = tk.StringVar(value="")
        ttk.Label(frm3, text="PNG 图片：").grid(row=6, column=0, sticky="w", pady=(12, 0))
        ttk.Entry(frm3, textvariable=self.png_var, width=64).grid(row=6, column=1, columnspan=2,
                                                                 sticky="we", pady=(12, 0))
        ttk.Button(frm3, text="选 PNG…", command=self._browse_png).grid(row=6, column=3, padx=4, pady=(12, 0))
        ttk.Label(frm3, text="骨架图标：").grid(row=7, column=0, sticky="w", pady=(6, 0))
        self.src_box = ttk.Combobox(frm3, textvariable=self.src_var, width=60, values=[])
        self.src_box.grid(row=7, column=1, columnspan=2, sticky="we", pady=(6, 0))
        ttk.Button(frm3, text="刷新骨架列表", command=self._fill_source_paths).grid(row=7, column=3, padx=4,
                                                                                pady=(6, 0))
        ttk.Button(frm3, text="★ 把图片加进这个 bundle（自动注册新地址 + 同步 CRC）",
                   command=lambda: self._run("addimg")).grid(row=8, column=1, sticky="w", pady=(8, 0))
        # --- ③c 替换现有图标（就地换图 + 整包替换；v1.8.103）---
        self.repl_var = tk.StringVar(value="")
        self.oldkey_var = tk.StringVar(value="")
        self.batch_var = tk.BooleanVar(value=False)
        ttk.Label(frm3, text="替换：").grid(row=9, column=0, sticky="w", pady=(12, 0))
        ttk.Entry(frm3, textvariable=self.repl_var, width=64).grid(row=9, column=1, columnspan=2,
                                                                 sticky="we", pady=(12, 0))
        ttk.Button(frm3, text="选 PNG/文件夹…", command=self._browse_replace).grid(row=9, column=3, padx=4,
                                                                                pady=(12, 0))
        ttk.Checkbutton(frm3, text="批量（文件夹里按**原图标名**匹配，勾上时下面那格不用填）",
                        variable=self.batch_var).grid(row=10, column=1, sticky="w", pady=(6, 0))
        ttk.Label(frm3, text="重指向原包键：").grid(row=11, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(frm3, textvariable=self.oldkey_var, width=60).grid(row=11, column=1, columnspan=2,
                                                                   sticky="we", pady=(6, 0))
        ttk.Button(frm3, text="= 骨架包名", command=self._fill_oldkey).grid(row=11, column=3, padx=4,
                                                                         pady=(6, 0))
        ttk.Button(frm3, text="🔁 ★ 替换现有图标（填了原包键就一并重指向）",
                   command=lambda: self._run("replace")).grid(row=12, column=1, sticky="w", pady=(8, 0))
        frm3.columnconfigure(1, weight=1)

        frm4 = ttk.LabelFrame(top, text="④ 收尾", padding=8)
        frm4.pack(fill="x", pady=(8, 0))
        for i, (txt, act) in enumerate([("同步 CRC", "crc"), ("自检", "check"),
                                        ("安装到游戏（先备份）", "install"), ("还原 catalog", "restore"),
                                        ("全流程（新建→加资产→CRC→自检）", "all")]):
            ttk.Button(frm4, text=txt, command=lambda a=act: self._run(a)).grid(row=0, column=i, padx=4)

        # ⛔ 2026-10 第 62 轮修：以前 log 先 `pack(expand=True)`、关闭按钮后 pack ⇒
        #    日志框吃掉全部剩余空间，**关闭按钮被挤出窗口底部**（点不到）✗
        #    ⇒ 先 pack 一条 `side="bottom"` 的底栏，再 pack 日志（顺序决定分配）✓
        bottom = ttk.Frame(top)
        bottom.pack(side="bottom", fill="x", pady=(6, 0))
        ttk.Button(bottom, text="关闭", command=self.win.destroy).pack(anchor="e")
        self.log = tk.Text(top, height=14, wrap="none")
        self.log.pack(fill="both", expand=True, pady=(8, 0))

        self._refresh()
        self.say("提示：自建 bundle 可以反复「加资产」——每次都点一下「同步 CRC」，再「安装到游戏」。")

        # ★ v1.8.112：**深色主题**（用户实测反馈："② 和 ④ 的显示栏白底太刺眼"）。
        #   ② 的 bundle 列表是 `tk.Listbox`、底部日志是 `tk.Text` —— 这两类是**经典 tk 控件，
        #   不跟随 ttk 主题**，默认 `SystemWindow`（白）⇒ 必须显式上色。
        #   ⛔ 以前这个对话框**没调** `theme_children`（其他对话框都调了）⇒ 只有它两块白底 ✗
        #   ⛔ 别在这里硬编码颜色：`ui_fit.theme_from_master` 会去主程序调色板取色，
        #      取不到时用 `ui_fit.DARK_FALLBACK` 兜底（保证永远不回到白底）。
        #   回归：`测试\test_dialog_dark_theme.py`（含"没上色就应该是亮色"的负向对照）✓
        try:
            import ui_fit
            ui_fit.theme_from_master(self.win, master)
        except Exception:  # noqa: BLE001 - 主题失败不影响功能
            pass

    # ---- 小工具 ----
    def _setup_paths(self):
        """（类内转发）模块级 `_setup_paths()`。

        ⛔ 2026-10 第 62 轮修的真 bug：这里以前直接调 `self._setup_paths()`，而它**只有模块级定义**
        ⇒ `AttributeError: 'MyBundleDialog' object has no attribute '_setup_paths'`
        ⇒ **整个「我的 bundle」对话框一打开就崩** ✗（v1.8.99 就这么发出去了）。
        由新建的 `测试\\test_my_bundle_dialog.py` 抓到（真把窗口建出来才跑得到这一行）。

        方法内部的裸名 `_setup_paths` 解析到的是**模块级函数**（类属性不在方法作用域里）⇒ 不会递归 ✓
        """
        return _setup_paths()

    def say(self, msg):
        self.log.insert("end", str(msg) + "\n")
        self.log.see("end")
        self.win.update_idletasks()

    def _browse_workdir(self):
        from tkinter import filedialog
        p = filedialog.askdirectory(title="选择工作目录")
        if p:
            self.workdir_var.set(p)
            self.workdir = p
            self._refresh()

    def _browse_template(self):
        from tkinter import filedialog
        p = filedialog.askopenfilename(title="选择骨架 bundle", filetypes=[("bundle", "*.bundle")])
        if p:
            self.tmpl_var.set(p)

    def _browse_pack(self):
        from tkinter import filedialog
        p = filedialog.askopenfilename(title="选择 .bamod 素材包", filetypes=[("bamod", "*.bamod")])
        if p:
            self.pack_var.set(p)

    def _browse_png(self):
        from tkinter import filedialog
        p = filedialog.askopenfilename(title="选择图片（PNG/JPG）",
                                       filetypes=[("图片", "*.png *.jpg *.jpeg"), ("全部", "*.*")])
        if p:
            self.png_var.set(p)

    def _browse_replace(self):
        """替换用：可选**一张 PNG**（单个替换）或**一个文件夹**（批量按文件名替换）"""
        from tkinter import filedialog, messagebox
        if self.batch_var.get():
            p = filedialog.askdirectory(title="选择放图片的文件夹（文件名 = 原图标名）")
        else:
            p = filedialog.askopenfilename(title="选择要替换成的图片",
                                           filetypes=[("图片", "*.png *.jpg *.jpeg"), ("全部", "*.*")])
            if not p:
                d = filedialog.askdirectory(title="（或选一个文件夹做批量替换）")
                if d:
                    p = d
                    self.batch_var.set(True)
        if p:
            self.repl_var.set(p)

    def _fill_oldkey(self):
        """把「重指向原包键」自动填成当前骨架包的名字（最常用：把原图标包指向我的副本）"""
        t = os.path.basename(self.tmpl_var.get().strip() or "")
        if t.endswith(".bundle"):
            self.oldkey_var.set(t)
            self.say("原包键已填：%s（安装后会把这个包的加载目标改成你的 bundle）" % t)
        else:
            self.say("（先在「骨架模板」里选一个 .bundle，再点这个按钮）")

    def _fill_source_paths(self):
        """把当前 bundle 里**可当骨架的图片资产**填进下拉框（决定新图的尺寸与格式）"""
        b = self._cur()
        if not b:
            self.say("（先在上面列表里选一个 bundle，再刷新骨架列表）")
            return []
        try:
            paths = list_image_assets(b)
        except Exception as e:                                    # noqa: BLE001
            self.say("✗ 读图片列表失败：%s: %s" % (type(e).__name__, e))
            return []
        self.src_box["values"] = paths
        if paths and not self.src_var.get():
            self.src_var.set(paths[0])
        if paths:
            self.say("可当骨架的图片资产 %d 个（新图会缩放到它的尺寸）" % len(paths))
            # 路径里带 .png，bundle 内新路径默认换成 Assets/Mods/<名字>.png，避免覆盖原资产
            if not self.asset_var.get().lower().endswith((".png", ".jpg")):
                self.asset_var.set("Assets/Mods/%s/New.png" % (self.name_var.get() or "MyMod"))
        return paths

    def _refresh(self):
        self.listbox.delete(0, "end")
        self._bundles = list_my_bundles(self.workdir_var.get())
        for f, sz, _mt in self._bundles:
            self.listbox.insert("end", "%8.1f KB  %s" % (sz / 1024, f))
        if not self.tmpl_var.get():
            t = pick_template(self.game_root, self._setup_paths())
            if t:
                self.tmpl_var.set(t)

    def _on_pick(self):
        sel = self.listbox.curselection()
        if sel:
            self._cur_bundle = self._bundles[sel[0]][0]
            self.say("选中：%s" % self._cur_bundle)
            self._fill_source_paths()

    def _cur(self):
        return getattr(self, "_cur_bundle", None)

    # ---- 动作（后台线程 + 队列）----
    def _run(self, action):
        import threading
        args = dict(workdir=self.workdir_var.get(), name=self.name_var.get().strip(),
                    template=self.tmpl_var.get().strip() or None,
                    pack=self.pack_var.get().strip() or None,
                    png=self.png_var.get().strip() or None,
                    repl=self.repl_var.get().strip() or None,
                    batch=bool(self.batch_var.get()),
                    oldkey=self.oldkey_var.get().strip() or None,
                    source=self.src_var.get().strip() or None,
                    address=self.addr_var.get().strip() or None,
                    asset=self.asset_var.get().strip() or None,
                    type=self.type_var.get().strip() or "UnityEngine.GameObject",
                    bundle=self._cur(), game_root=self.game_root, action=action)
        self.say("—— %s ——" % action)

        def work():
            try:
                self._do(args, self.say)
            except Exception as e:                                # noqa: BLE001
                self.say("✗ %s: %s" % (type(e).__name__, e))
        threading.Thread(target=work, daemon=True).start()

    def _do(self, a, log):
        act = a["action"]
        if act in ("new", "all"):
            if not a["template"]:
                a["template"] = pick_template(a["game_root"], self._setup_paths(), log=log)
            b = new_bundle(a["name"] or "MyMod", a["workdir"], a["template"], log=log)
            self._cur_bundle = b
            log("（已选中新建的 bundle，后续动作都用它）")
            self._refresh_safe()
            if act == "new":
                return
        bundle = a["bundle"] or self._cur()
        cat = game_paths(a["game_root"])["catalog"] if a["game_root"] else None
        if act in ("add", "all"):
            if not bundle:
                log("✗ 先「新建 bundle」或在列表里选一个")
                return
            add_assets(bundle, a["pack"], cat, a["address"], a["asset"], a["type"], do_crc=True, log=log)
            self._refresh_safe()
            bundle = bundle
        if act == "replace":
            # 🔁 就地替换现有图标（+ 可选：把原包键重指向我的副本 ⇒ 零 DB 改动）
            if not bundle:
                log("✗ 先「新建 bundle」或在列表里选一个")
                return
            p = a.get("repl")
            if not p or not os.path.exists(p):
                log("✗ 先选要替换成的 PNG（或批量用的文件夹）")
                return
            if a.get("batch") or os.path.isdir(p):
                replace_by_name(bundle, p, log=log)
            else:
                tgt = a.get("source")
                if not tgt:
                    log("✗ 单个替换要在「骨架图标」下拉里选一个**目标资产**（= 要换掉的那个图标）")
                    return
                replace_image(bundle, p, tgt, log=log)
            self._refresh_safe()
            if a.get("oldkey"):
                # catalog 副本落在工作目录里（⛔ 绝不直接写游戏目录）；没有就先把游戏那份拷过来
                catcopy = os.path.join(os.path.dirname(os.path.abspath(bundle)),
                                       "catalog_with_mybundle.json")
                if not os.path.exists(catcopy) and cat:
                    shutil.copy2(cat, catcopy)
                if not os.path.exists(catcopy):
                    log("⚠ 没有 catalog 副本 ⇒ 跳过重指向（先在「加资产/加图片」里注册一次会生成副本）")
                else:
                    repoint_bundle(catcopy, a["oldkey"], bundle, catcopy, log=log)
                    log("✓ 重指向完成：游戏加载 %s 时读到的将是你的 bundle"
                        "（点「安装到游戏」把它装上去即可；地址键没变 ⇒ 不用改 DB）" % a["oldkey"])
            return
        if act == "addimg":
            # ★ 图片（图标/头像/标签图）：深拷贝骨架 → 换图 → 注册新地址（Texture2D + Sprite 两条）→ CRC
            if not bundle:
                log("✗ 先「新建 bundle」或在列表里选一个")
                return
            if not a["png"] or not os.path.exists(a["png"]):
                log("✗ 先选一张 PNG（「选 PNG…」按钮）")
                return
            info = add_image(bundle, a["png"], a["source"], a["asset"], log=log)
            self._refresh_safe()
            if cat and a["address"]:
                import my_bundle_catalog as MC
                out_cat = os.path.join(os.path.dirname(os.path.abspath(bundle)),
                                       "catalog_with_mybundle.json")
                r = MC.add_bundle_and_asset(cat, bundle, a["address"], info["asset_path"],
                                            out=out_cat, log=log,
                                            asset_types=["UnityEngine.Texture2D", "UnityEngine.Sprite"])
                sync_crc(bundle, r["catalog"], log=log)
                log("✓ 完成：图片已加进 bundle、新地址已注册（Texture2D + Sprite 两条）、CRC 已同步")
                log("   接下来：点「安装到游戏」，再把 DB 指到该地址"
                    "（Units.PortraitFileName / ThumbnailFileName、Weapons.HUDIcon…）")
            else:
                log("（未填地址或没找游戏目录 ⇒ 只加进了 bundle；填上地址再点一次可注册）")
            return
        if act in ("crc",):
            sync_crc(bundle, os.path.join(os.path.dirname(bundle), "catalog_with_mybundle.json"), log=log)
        if act in ("check", "all"):
            c = os.path.join(os.path.dirname(bundle), "catalog_with_mybundle.json")
            selfcheck(bundle, c if os.path.exists(c) else None, a["address"], log=log)
        if act == "install":
            c = os.path.join(os.path.dirname(bundle), "catalog_with_mybundle.json")
            if not os.path.exists(c):
                log("✗ 没有 catalog 副本（先「加资产」注册地址）")
                return
            install(bundle, c, a["game_root"], log=log)
        if act == "restore":
            restore_catalog(a["game_root"], log=log)

    def _refresh_safe(self):
        try:
            self.win.after(0, self._refresh)
        except Exception:                                        # noqa: BLE001
            pass


# ------------------------------------------------------------------ CLI

def main():
    ap = argparse.ArgumentParser(description="「我的 bundle」管理器（自建 bundle + catalog 注册 + CRC + 安装）")
    ap.add_argument("--new", metavar="名字", help="新建一个自建 bundle")
    ap.add_argument("--workdir", default=DEFAULT_WORKDIR, help="自建 bundle 的工作目录（默认 %(default)s）")
    ap.add_argument("--template", help="骨架 bundle（默认用产品自带的小包）")
    ap.add_argument("--add", action="store_true", help="往 bundle 加资产（配 --bundle/--pack/--catalog/--address）")
    ap.add_argument("--add-image", action="store_true",
                    help="★ 把一张 PNG 当**新图标**加进 bundle（配 --png/--bundle；可再配 --catalog/--address 一次注册完）")
    ap.add_argument("--png", help="要加进去的 PNG")
    ap.add_argument("--source-path", help="骨架：bundle 里现成图片资产的**容器路径**（不给我就用第一个）")
    ap.add_argument("--asset-name", help="bundle 内新资产路径（默认沿用骨架路径）")
    ap.add_argument("--list-images", action="store_true", help="列出这个 bundle 里可当骨架的图片资产路径")
    ap.add_argument("--replace-image", action="store_true",
                    help="★ **就地替换**现有图标的贴图（配 --png/--target；地址不变 ⇒ 不用改 DB）")
    ap.add_argument("--target", help="要替换的资产路径（容器里的路径，用 --list-images 查）")
    ap.add_argument("--replace-by-name", metavar="PNG目录",
                    help="★ **批量就地替换**：目录里每个 PNG 按**文件名**匹配包里的资产名")
    ap.add_argument("--pick-first", action="store_true",
                    help="批量替换遇到同名多候选时取第一个（默认跳过并报出来，不猜）")
    ap.add_argument("--stretch", action="store_true",
                    help="替换时**拉伸铺满**内容区（默认：保比例居中，四周补透明 ⇒ 不变形）")
    ap.add_argument("--no-scale", action="store_true",
                    help="替换时**完全不缩放**（图比内容区大就居中裁掉）")
    ap.add_argument("--grep", help="批量替换只处理路径含该子串的候选（如 outline 变体）")
    ap.add_argument("--repoint", action="store_true",
                    help="★ 把 catalog 里**已有 bundle 条目**改指向我的文件（原地址键不变；T2 已验证的机制）")
    ap.add_argument("--old-key", help="要重指向的原 bundle 键（形如 xxx_assets_all_<32hex>.bundle）")
    ap.add_argument("--bundle")
    ap.add_argument("--pack", help=".bamod 素材包")
    ap.add_argument("--catalog")
    ap.add_argument("--address")
    ap.add_argument("--asset", help="bundle 内资产路径（默认 Assets/Mods/<名字>.prefab）")
    ap.add_argument("--type", default="UnityEngine.GameObject", help="资产类型（用 catalog 那套类名，默认 %(default)s）")
    ap.add_argument("--sync-crc", action="store_true")
    ap.add_argument("--selfcheck", action="store_true")
    ap.add_argument("--install", action="store_true")
    ap.add_argument("--game-root")
    ap.add_argument("--restore-catalog", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--list-templates", action="store_true")
    ap.add_argument("--self-test", action="store_true", help="全离线自测（不依赖 Blender）")
    a = ap.parse_args()

    if a.self_test:
        r = self_test(log=print)
        print("\n自测结果：%s" % ("✓ 通过" if r["ok"] else "✗ 失败"))
        return 0 if r["ok"] else 1
    rev = _setup_paths()
    if a.list_templates:
        root = find_game_root(a.game_root)
        for f in list_templates(root):
            print("%10d  %s" % (os.path.getsize(f), f))
        return 0
    if a.list:
        for f, sz, mt in list_my_bundles(a.workdir):
            print("%10d  %s  %s" % (sz, time.strftime("%m-%d %H:%M", time.localtime(mt)), f))
        return 0
    if a.new:
        tmpl = a.template or default_template(rev)
        print(new_bundle(a.new, a.workdir, tmpl))
        return 0
    if a.restore_catalog:
        restore_catalog(find_game_root(a.game_root) or a.game_root, log=print)
        return 0
    if a.list_images:
        for p in list_image_assets(a.bundle):
            print(p)
        return 0
    if a.replace_image or a.replace_by_name:
        if a.bundle and a.bundle.lower().endswith(".bundle"):
            b = a.bundle
        else:
            print("✗ --replace-* 要配 --bundle <你的包（=原图标包的副本）>")
            return 2
        try:
            if a.replace_by_name:
                replace_by_name(b, a.replace_by_name, a.grep, log=print, pick_first=a.pick_first,
                                mode="stretch" if a.stretch else "fit", scale=not a.no_scale)
            else:
                if not a.png or not a.target:
                    print("✗ --replace-image 要配 --png <图片> --target <资产路径>")
                    return 2
                replace_image(b, a.png, a.target, log=print,
                              mode="stretch" if a.stretch else "fit", scale=not a.no_scale)
        except Exception as e:                                    # noqa: BLE001
            print("✗ %s: %s" % (type(e).__name__, e))
            return 1
        if a.repoint:
            if not (a.catalog and a.old_key):
                print("⚠ 要重指向请再给 --catalog <副本> --old-key <原包名>")
                return 1
            repoint_bundle(a.catalog, a.old_key, b,
                           a.catalog.replace(".json", "_repointed.json") if a.catalog else None, log=print)
        return 0
    if a.repoint:
        cat = a.catalog or game_paths(find_game_root(a.game_root))["catalog"]
        repoint_bundle(cat, a.old_key, a.bundle,
                       cat.replace(".json", "_repointed.json"), log=print)
        return 0
    if a.add_image:
        if not a.png:
            print("✗ --add-image 要配 --png <图片>")
            return 2
        info = add_image(a.bundle, a.png, a.source_path, a.asset_name or a.asset, log=print)
        if a.catalog and a.address:
            import my_bundle_catalog as MC
            out_cat = os.path.join(os.path.dirname(os.path.abspath(a.bundle)),
                                   "catalog_with_mybundle.json")
            # ★ 图标必须**同时**注册 Texture2D 与 Sprite 两条（与 vanilla 一致；见 my_bundle_catalog 注释）
            r = MC.add_bundle_and_asset(a.catalog, a.bundle, a.address, info["asset_path"],
                                        out=out_cat, log=print,
                                        asset_types=["UnityEngine.Texture2D", "UnityEngine.Sprite"])
            sync_crc(a.bundle, r["catalog"], log=print)
        return 0
    if a.add:
        add_assets(a.bundle, a.pack, a.catalog, a.address, a.asset, a.type, log=print)
        return 0
    if a.sync_crc:
        sync_crc(a.bundle, a.catalog or game_paths(find_game_root(a.game_root))["catalog"], log=print)
        return 0
    if a.selfcheck:
        r = selfcheck(a.bundle, a.catalog, a.address, log=print)
        return 0 if r["ok"] else 1
    if a.install:
        install(a.bundle, a.catalog, find_game_root(a.game_root) or a.game_root, log=print)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
