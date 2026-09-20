# -*- coding: utf-8 -*-
r"""★⑮ **流式贴图**：把一个包里的 `Texture2D` 读出来（含 `.resS` 侧载），并生成"流式引用"条目。

为什么需要它（★⑮ 的两条贴图来源之一）
======================================
"给移植过来的零件用它**原机**的贴图"有两条路：
  ① **PNG 内嵌**：把贴图导成 PNG → 编辑 → 打进包（简单，但**占体积**：一张 2048² 就是几 MB）
  ② **流式引用**：在自己包里只建一个 `Texture2D`，把 `m_StreamData` 指到**游戏那份 `.resS`** 里的
     那一段 —— **不搬像素、不解码**，实测包只 **+27 KB** ✓（`技术资料/17` ★⑮ + KB 卡
     `.re-kb\tools\rotor-texture-graft.md`）

⛔ 三个**实测**坑（别重踩）
==========================
1. **`_unpacked` 目录名可能带旧哈希**：现盘实测游戏目录里只有
   `units_assets_all_3cc1eb58…bundle_unpacked`，而当前包是 `…1e6c04ce…bundle`
   ⇒ 按当前包名去拼 `<bundle>_unpacked` **永远找不到** ✗ ⇒ **必须按前缀 glob** ✓
2. **offset/size 必须从"当前"包读**（旧副本里的偏移与新包对不上）✓
3. **解码失败 ≠ 死数据**：先看**有没有材质引用它**；名字最像的那张可能是死数据（KB 卡坑 3/4）
"""
import glob
import os
import struct

TEX_CLASS = "Texture2D"


def _setup_paths():
    import sys
    here = os.path.dirname(os.path.abspath(__file__))
    prod = os.path.dirname(here)
    for p in (here, prod, os.path.join(prod, "_unitypy")):
        if os.path.isdir(p) and p not in sys.path:
            sys.path.insert(0, p)


def obj_name(o):
    r"""`Texture2D` 的名字（**优先 `peek_name()`**：只解 m_Name，不解析整个对象）。

    ⛔ 为什么在意：units 包有 **2725 张**流式贴图、16 万对象 ⇒ 逐个 `o.read()` 再取名字
      实测要几十秒（GUI 里点一下「列出候选贴图」就得等那么久）✗
      UnityPy 的 `ObjectReader.peek_name()` 只读类型树里的名字字段 ⇒ 快得多 ✓
    """
    try:
        nm = o.peek_name()
        if nm:
            return nm
    except Exception:                                             # noqa: BLE001
        pass
    try:
        return getattr(o.read(), "m_Name", "") or ""
    except Exception:                                             # noqa: BLE001
        return ""


def find_unpacked_dir(bundle):
    r"""→ 与 `bundle` 同级的 `*_unpacked` 目录（**按前缀 glob**，别按当前包名拼）。

    实测：游戏里那个目录用的还是**旧哈希名**（`…3cc1eb58…bundle_unpacked`），
    而当前包已经叫 `…1e6c04ce…bundle` ⇒ 字符串拼接必定落空 ✗
    挑法：同前缀优先、其次同目录唯一的一个；多个候选取**最近修改**的那个（更可能是当前那份）✓
    """
    d = os.path.dirname(os.path.abspath(bundle))
    stem = os.path.basename(bundle).split("_assets_all_")[0]
    cands = sorted(glob.glob(os.path.join(d, stem + "*_unpacked")))
    if not cands:
        cands = sorted(glob.glob(os.path.join(d, "*_unpacked")))
    if not cands:
        return None
    cands.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return cands[0]


def resolve_ress(bundle, asset_path, want_size=None):
    r"""`archive:/CAB-x/CAB-x.resS` → 磁盘上的 `.resS` 文件路径（找不到返回 None）。

    ⛔ 先看**包里**有没有同名节点（.resS 常常就在 bundle 内部）—— 那种情况**根本不需要外部文件** ✓
    """
    if not asset_path:
        return None
    cab = asset_path.replace("\\", "/").rstrip("/").split("/")[-1]
    if not cab.endswith(".resS"):
        return None
    d = find_unpacked_dir(bundle)
    cands = []
    if d:
        cands.append(os.path.join(d, cab))
        cands += sorted(glob.glob(os.path.join(d, "*.resS")))
    for p in cands:
        if os.path.isfile(p) and (want_size is None or os.path.getsize(p) >= want_size):
            return p
    return None


def load(bundle):
    """→ `(env, sf, objs)`：UnityPy 打开一个包（调用方负责 release）。"""
    _setup_paths()
    import UnityPy
    env = UnityPy.load(os.path.abspath(bundle))
    objs = list(env.objects)
    if not objs:
        raise ValueError("读不出对象：%s" % bundle)
    return env, objs[0].assets_file, objs


def release(env):
    try:
        from stream_save import release_env
        release_env(env)
    except Exception:                                             # noqa: BLE001
        pass


def stream_info(tex_obj):
    """→ `{"name","w","h","fmt","mip","img_size","path","offset","size"}`（含流位置）。"""
    t = tex_obj.read()
    sd = getattr(t, "m_StreamData", None)
    return {"pid": tex_obj.path_id,
            "name": getattr(t, "m_Name", "") or "",
            "w": int(getattr(t, "m_Width", 0) or 0),
            "h": int(getattr(t, "m_Height", 0) or 0),
            "fmt": int(getattr(t, "m_TextureFormat", 0) or 0),
            "mip": int(getattr(t, "m_MipCount", 1) or 1),
            "img_size": int(getattr(t, "m_CompleteImageSize", 0) or 0),
            "path": (getattr(sd, "path", "") or "") if sd else "",
            "offset": int(getattr(sd, "offset", 0) or 0) if sd else 0,
            "size": int(getattr(sd, "size", 0) or 0) if sd else 0}


def list_textures(bundle, grep=None, streamed_only=False, limit=200, log=None,
                  only_alive=False, with_refs=True):
    r"""→ `[stream_info, …]`：包里可当"原机贴图"的 `Texture2D` 清单（名字子串过滤）。

    ★ 这张表就是 ★⑮ 第 3 步 GUI 里的**候选池**（用户从里面挑"零件原来那台机器的贴图"）✓
    ★ v1.10.0：每条多带 **`refs`（被几个材质引用）/ `alive`** —— KB 卡坑 3 明确纠正过
      "解码失败 ≠ 死数据，判死活看**有没有材质引用**" ⇒ 候选表必须把这个数字给用户看 ✓
      `only_alive=True` 可只留有引用的（默认 False：**给数字，不替用户砍掉**）
    """
    env, sf, objs = load(bundle)
    out = []
    try:
        refs = material_refs(objs, log=log) if with_refs else {}
        for o in objs:
            if getattr(getattr(o, "type", None), "name", "") != TEX_CLASS:
                continue
            # 先用**便宜**的名字过滤，再做 `stream_info`（后者要解析整个对象）✓
            nm = obj_name(o)
            if grep and grep.lower() not in (nm or "").lower():
                continue
            try:
                info = stream_info(o)
            except Exception:                                     # noqa: BLE001
                continue
            if grep and grep.lower() not in info["name"].lower():
                continue
            if streamed_only and not info["path"]:
                continue
            if with_refs:
                info["refs"] = int(refs.get(info["pid"], 0))
                info["alive"] = info["refs"] > 0
                if only_alive and not info["alive"]:
                    continue
            out.append(info)
            if len(out) >= limit:
                break
    finally:
        release(env)
    if log:
        n_dead = sum(1 for i in out if with_refs and not i.get("alive"))
        log("  候选贴图 %d 张%s%s" % (len(out), ("（过滤 %r）" % grep) if grep else "",
                                     ("，其中 **%d 张没有任何材质引用**（可能是死数据 ⇒ 慎重）" % n_dead)
                                     if n_dead else ""))
        for i in out[:12]:
            log("   · %-42s %sx%s fmt=%-3s %-14s %s"
                % (i["name"][:42], i["w"], i["h"], i["fmt"],
                   ("流式 %d B" % i["size"]) if i["path"] else "内嵌",
                   ("材质引用 %d" % i["refs"]) if with_refs else ""))
    return out


def find_by_name(bundle, name):
    """按**精确名字**找一张贴图 → stream_info（找不到再试大小写不敏感/包含匹配）→ None。"""
    env, _sf, objs = load(bundle)
    try:
        best = None
        for o in objs:
            if getattr(getattr(o, "type", None), "name", "") != TEX_CLASS:
                continue
            nm = obj_name(o)                     # ★ 先用便宜的名字比，命中才解析整个对象 ✓
            if nm == name:
                return stream_info(o)
            if best is None and name.lower() in (nm or "").lower():
                best = o
        return stream_info(best) if best is not None else None
    finally:
        release(env)


def read_stream_bytes(bundle, info, sf=None, log=None):
    r"""按 `info` 里的 `path/offset/size` 读出**原始像素字节**（内嵌返回 None）。

    ★★ 规矩（KB 卡 `.re-kb\tools\rotor-texture-graft.md` 坑 3/4，**实测踩过**）：
      **offset 与解码必须用同一份数据**。offset 是从**当前**包读的，所以数据也必须从
      **当前包里那个 `.resS` 节点**读（UnityPy 环境注册的 cab）✓
      ⇒ 只有当前包里找不到这个节点时，才退回磁盘上的 `*_unpacked/*.resS`（那是**旧副本**），
        并且**明显报警**：两边不是同一份数据时，解码失败**不能**据此判这张"是死数据" ✗
        （教学线就这么误判过 `US_AH-1Z_3_BaseMap` —— 它其实是冬季迷彩、有材质正常引用它，
         失败的真因是拿旧副本解了当前包的 offset）
    ⛔ 判"死没死"要看**有没有材质引用它**（`material_refs()`），不是看解码成不成功 ✓
    """
    if not info.get("path"):
        return None
    off = int(info.get("offset", 0) or 0)
    size = int(info.get("size", 0) or 0)
    if sf is not None:
        try:
            from UnityPy.helpers.ResourceReader import get_resource_data
            return get_resource_data(info["path"], sf, off, size)
        except Exception as e:                                    # noqa: BLE001
            if log:
                log("   ⚠ 当前包里读不出这段流数据（%s: %s）⇒ 退回磁盘 `*_unpacked` 旧副本；"
                    "⚠ 数据来源与 offset 不一致时，解码失败**不能**判它死数据"
                    % (type(e).__name__, str(e)[:60]))
    p = resolve_ress(bundle, info["path"], want_size=off + size)
    if not p:
        raise FileNotFoundError("找不到 .resS（%s）—— 试过 `*_unpacked` 前缀 glob" % info["path"])
    with open(p, "rb") as f:
        f.seek(off)
        return f.read(size)


def material_refs(objs, log=None):
    r"""→ `{贴图 pid: 被几个材质引用}`（**判"死数据"的正确尺子**，不是"解码成不成功"）。

    ⛔ KB 卡坑 3 明确纠正过：解码失败只能说明"你取到的那段 `.resS` 不是这个包的数据"，
      **不能**说明贴图是死数据（实测 `US_AH-1Z_3_BaseMap` 被误判过 —— 它其实是冬季迷彩）。
      真正该看的是**有没有材质引用它** ✓
    """
    out = {}
    n_mat = 0
    for o in objs:
        if getattr(getattr(o, "type", None), "name", "") != "Material":
            continue
        n_mat += 1
        try:
            m = o.read()
        except Exception:                                         # noqa: BLE001
            continue
        for te in (m.m_SavedProperties.m_TexEnvs or []):
            try:
                tp = te[1].m_Texture.m_PathID if te[1].m_Texture else 0
            except Exception:                                     # noqa: BLE001
                tp = 0
            if tp:
                out[tp] = out.get(tp, 0) + 1
    if log:
        log("  材质 %d 个，引用到的贴图 %d 张" % (n_mat, len(out)))
    return out


def png_from_object(tex_obj, bundle=None, log=None):
    r"""**手上已经有一个 `Texture2D` 对象**时用它 → PNG 字节（省掉重新 load 整个包）。

    ★ 这是 ★⑮ 第 1/2 步（`导出贴图`）能读**流式贴图**的关键：先走 UnityPy 的 `t.image`
      （`.resS` 在包里时这条就够），失败再**自己读流 + 用 UnityPy 的解码器解** ✓
    ⛔ 每张图都去 `load(bundle)` 一次会**慢到不可用**（units 包 3.2 GB / 1.6 s 一次）——
      所以导出循环必须走这条"对象已在手上"的路 ✓
    """
    import io
    try:
        t = tex_obj.read()
        img = t.image
        if img is not None:
            b = io.BytesIO()
            img.save(b, "PNG")
            return b.getvalue()
    except Exception as e:                                        # noqa: BLE001
        if log:
            log("   （UnityPy 解码失败，改走「自己读流」：%s: %s）" % (type(e).__name__, str(e)[:70]))
    if not bundle:
        return None
    info = stream_info(tex_obj)
    if not info.get("path"):
        return None
    # ★ offset 是当前包的 ⇒ 数据也从**当前包**读（同一份数据），见 `read_stream_bytes` 的规矩
    raw = read_stream_bytes(bundle, info, sf=getattr(tex_obj, "assets_file", None), log=log)
    if raw is None:
        return None
    return _decode_raw(info, raw, log=log, ref_obj=tex_obj)


def texture_png_bytes(bundle, pid, log=None):
    r"""按 pid 取 `Texture2D` → PNG 字节（**内嵌与流式都试**）；失败返回 None。

    ⛔ 会**重新 load 整个包** ⇒ 只适合单张抽查；批量导出请用 `png_from_object` ✓
    """
    env, sf, objs = load(bundle)
    try:
        o = next((x for x in objs if x.path_id == pid), None)
        if o is None or getattr(getattr(o, "type", None), "name", "") != TEX_CLASS:
            return None
        return png_from_object(o, bundle=bundle, log=log)
    finally:
        release(env)


# 基准色贴图的**名字家族**（不写死任何具体资源名 —— `测试\test_no_hardcoded_game_ids.py` 守着）
BASE_TEX_HINTS = ("basemap", "base_map", "basecolor", "base_color", "albedo", "diffuse", "_d.")


def pick_base_slots(tex_envs):
    r"""从材质的纹理槽里挑出**基准色槽**（★⑮ 流式引用要换的就是这些槽）。

    `tex_envs`: `[(槽名, 当前贴图名), …]` → `[槽名, …]`（挑不出来返回空表）
    规则（按优先级，实测 ★⑮ 的旋翼材质走第 2 条）：
      ① 槽名正好是 `_BaseMap`（Unity 标准名）
      ② **当前贴图名**里带 `BaseMap`/`BaseColor`/`Albedo`… ⇒ 实测 ACV 旋翼是
         槽 `Layer_A97CDC25` → 贴图 `US_ACV_1_BaseMap`，走这条 ✓
      ③ 槽名里带 `basemap`（有些材质槽名才是标准名、贴图名反而乱）
    ⛔ **不返回全部槽**：法线/粗糙度槽换了会一起坏（只有基准色槽需要"零件原机的图"）✓
    """
    def norm(s):
        return (s or "").lower()

    out = []
    for slot, tex in (tex_envs or []):
        if norm(slot) == "_basemap":
            out.append(slot)
    if out:
        return out
    for slot, tex in (tex_envs or []):
        tn = norm(tex)
        if tn and any(h in tn for h in BASE_TEX_HINTS):
            out.append(slot)
    if out:
        return out
    for slot, tex in (tex_envs or []):
        if "basemap" in norm(slot) or "basecolor" in norm(slot):
            out.append(slot)
    return out


def _decode_raw(info, raw, log=None, ref_obj=None):
    r"""用 UnityPy 的解码器把**原始字节**解成 PNG（绕过它的 `.resS` 解析）。

    ⛔⛔ v1.10.0 实测踩到两个"看着对、其实全错"的写法（都会**静默**产出垃圾图）：
      1. 自己造一个 shim 对象喂给 `get_image_from_texture2d` 时，**少 `get_image_data()` 方法**
         ⇒ `AttributeError`，所有 fmt 必然失败，而日志还写着"可能是死数据"（**误导性极强** ✗）
      2. 就算补上方法，shim 的 `object_reader` 是 `None` ⇒ 解码器读到的
         `version=(0,0,0,0)` / `platform=0`。**crunch 格式**（本包实测 fmt **29 = DXT5Crunched**）
         正是靠 `version[0] > 2017` 选 `unpack_unity_crunch` 还是 `unpack_crunch`
         ⇒ 选错就解出**一张 11 MB 的垃圾图**（真值只有 2.1 MB）✗✗
      ⇒ 正解：**不造 shim**，直接把 `parse_image_data` 需要的 7 个参数喂进去，
        其中 version/platform/PlatformBlob **从同包里那个真对象上取**（`ref_obj`）✓
    ⛔ 解码失败**不能**当"死数据"的判据（KB 卡坑 3）：判死活看**材质引用**（`material_refs`）✓
    """
    import io
    from UnityPy.export import Texture2DConverter

    version, platform, blob = (0, 0, 0, 0), 0, None
    if ref_obj is not None:
        try:                                                       # 真 Unity 版本（crunch 分支靠它）
            version = tuple(getattr(ref_obj, "version", None) or (0, 0, 0, 0))
        except Exception:                                          # noqa: BLE001
            pass
        try:
            platform = getattr(ref_obj, "platform", 0) or 0
        except Exception:                                          # noqa: BLE001
            pass
        try:
            blob = ref_obj.read().m_PlatformBlob
        except Exception:                                          # noqa: BLE001
            blob = None
    try:
        img = Texture2DConverter.parse_image_data(
            raw, info["w"], info["h"], info["fmt"], version, platform, blob, True)
    except Exception as e:                                        # noqa: BLE001
        if log:
            log("   ⚠ 原始字节解码失败（%s: %s）—— ⛔ 这**不能**判它是死数据；"
                "判死活看有没有材质引用它" % (type(e).__name__, str(e)[:70]))
        return None
    if img is None:
        return None
    b = io.BytesIO()
    img.save(b, "PNG")
    return b.getvalue()


def make_stream_item(src_bundle, name, info=None, log=None):
    r"""★ 生成"流式引用"贴图条目（给 `pack_model.create_matswap_pack` 用）。

    返回 `{"name","stream":{path,offset,size,w,h,fmt,mip,img_size}}`；找不到就抛错（不静默）✓
    """
    info = info or find_by_name(src_bundle, name)
    if not info:
        raise ValueError("在 %s 里找不到贴图 %r（先用 list_textures 看候选）"
                         % (os.path.basename(src_bundle), name))
    if not info.get("path"):
        raise ValueError("%r 是**内嵌**贴图（没有 m_StreamData）⇒ 只能走 PNG 内嵌那条路" % name)
    if info["size"] != info["img_size"]:
        # KB 卡的自检条件；不等就说明这张的流信息可疑 ⇒ 报警但仍返回（由调用方决定）
        if log:
            log("   ⚠ %s 的 m_StreamData.size(%d) ≠ m_CompleteImageSize(%d) —— 这张可疑"
                % (name, info["size"], info["img_size"]))
    return {"name": info["name"],
            "stream": {"path": info["path"], "offset": info["offset"], "size": info["size"],
                       "w": info["w"], "h": info["h"], "fmt": info["fmt"],
                       "mip": info["mip"], "img_size": info["img_size"]}}


if __name__ == "__main__":
    import argparse
    import sys
    ap = argparse.ArgumentParser(description="流式贴图：列候选 / 导出 PNG / 生成流式引用条目")
    ap.add_argument("bundle")
    ap.add_argument("--grep")
    ap.add_argument("--streamed-only", action="store_true")
    ap.add_argument("--only-alive", action="store_true",
                    help="只列**有材质引用**的（默认全列，但会把引用数打出来）")
    ap.add_argument("--png", metavar="贴图名", help="导出这张贴图为 PNG（写到当前目录）")
    ap.add_argument("--ref", metavar="贴图名", help="打印它的流式引用条目（JSON）")
    a = ap.parse_args()
    if a.png:
        info = find_by_name(a.bundle, a.png)
        if not info:
            print("✗ 没找到 %r" % a.png)
            sys.exit(2)
        b = texture_png_bytes(a.bundle, info["pid"], log=print)
        if not b:
            print("✗ 解码失败")
            sys.exit(3)
        out = os.path.join(os.getcwd(), info["name"] + ".png")
        open(out, "wb").write(b)
        print("✓ %s（%d 字节）" % (out, len(b)))
    elif a.ref:
        import json
        print(json.dumps(make_stream_item(a.bundle, a.ref, log=print), ensure_ascii=False, indent=1))
    else:
        list_textures(a.bundle, grep=a.grep, streamed_only=a.streamed_only,
                      only_alive=a.only_alive, log=print)
