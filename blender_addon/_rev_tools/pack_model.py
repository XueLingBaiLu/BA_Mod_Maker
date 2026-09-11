# -*- coding: utf-8 -*-
r""".bamod 素材包导出：把构建出的对象打包成小 zip（仅含改动对象 + 元数据）。

格式（manifest.json）：
{
  "format": "bamod-assets", "version": 1,
  "bundle": "units_assets_all",            # 目标 bundle 类别（导入时解析具体文件）
  "prefab_path": "Assets/.../MY_TURRET.prefab",   # 容器条目路径
  "root_pid": <容器资产对象 pid>,
  "preload": [<pid>...],                    # 导入时的 preload 顺序：
                                            #   MonoScript 常量 pid 原样使用 + 我的对象 pid（会被重映射）
  "objects": [                              # 仅我的对象（顺序与 preload 中一致）
     {"pid": ..., "class_id": ..., "script_id": "hex" 或 null,
      "type_name": ..., "raw": base64}
  ]
}

导入端（BA_Mod_Maker）按 script_id（MB）/class_id（引擎类型）匹配目标 bundle 的类型，
分配新 pid 后逐对象字节替换引用，再补容器条目 + preload + CRC。
"""
import os
import zipfile, json, base64

FORMAT = "bamod-assets"
VERSION = 1


def _type_identity(o):
    st = getattr(o, "serialized_type", None)
    sid = None
    th = None
    if st is not None:
        sid = st.script_id.hex() if isinstance(getattr(st, "script_id", None), bytes) else None
        th = st.old_type_hash.hex() if isinstance(getattr(st, "old_type_hash", None), bytes) else None
    return {"class_id": o.class_id, "type_name": o.type.name,
            "script_id": sid, "tree_hash": th}


def create_texture_pack(out_zip, textures, bundle_kind="unitportraits_assets_all"):
    r"""把图标/肖像 PNG 打成 .bamod 包（仅纹理）。

    textures: [(容器路径, png 文件路径), ...] 或 [(容器路径, png 路径, 映射地址), ...]
      容器路径形如 "Assets/Resources_moved/Images/UnitPortraits/RU/MY_UNIT/MY_ICON.png"
      （与原版一致：每个图标 = Texture2D + Sprite 两条容器条目）。
      映射地址可选：导入时注册进 catalog（像模型导入一样）。
    """
    items = []
    for entry in textures:
        path, png_path = entry[0], entry[1]
        addr = entry[2] if len(entry) > 2 else None
        with open(png_path, "rb") as f:
            data = f.read()
        item = {"path": path, "png": base64.b64encode(data).decode("ascii")}
        if addr:
            item["address"] = addr
        items.append(item)
    manifest = {
        "format": FORMAT,
        "version": VERSION,
        "bundle": bundle_kind,
        "textures": items,
    }
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
    total = sum(len(i["png"]) * 3 // 4 for i in items)
    return out_zip, len(items), total


def export_pack(sf, prefab_path, root_pid, preload_pids, out_zip, bundle_kind="units_assets_all"):
    r"""导出 .bamod 包。

    sf: 构建完成的 SerializedFile（对象已在内存中）
    preload_pids: 导入时的 preload 顺序（MonoScript 常量 + 我的对象 pid 混合）
    """
    objects = []
    for pid in preload_pids:
        o = sf.objects.get(pid)
        if o is None or o.type.name == "MonoScript":
            continue  # MonoScript 目标 bundle 里已有，preload 列表里保留其 pid 即可
        raw = o.data if getattr(o, "data", None) is not None else o.get_raw_data()
        if not raw:
            continue
        obj = _type_identity(o)
        obj["pid"] = pid
        obj["raw"] = base64.b64encode(raw).decode("ascii")
        objects.append(obj)
    manifest = {
        "format": FORMAT,
        "version": VERSION,
        "bundle": bundle_kind,
        "prefab_path": prefab_path,
        "root_pid": root_pid,
        "preload": list(preload_pids),
        "objects": objects,
    }
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
    total = sum(len(o["raw"]) * 3 // 4 for o in objects)
    return out_zip, len(objects), total


def create_skin_pack(out_zip, replace_mats, textures, target_id, prefab_path,
                     bundle_kind="units_assets_all", new_id=0):
    r"""把皮肤改动打成 .bamod 包（皮肤重涂 / 新增皮肤槽）。

    replace_mats: [{"orig": 材质 pid, "texs": [{"slot": 纹理槽名, "png": png文件路径}...]}...]
      只替换给出 png 的槽；其余槽保持原纹理。
    textures: [{"name": 纹理名, "png": png文件路径}]（自动去重，按路径）
    target_id: 目标皮肤 id（游戏皮肤菜单里的 id；repaint 该槽）
    new_id: 0=重涂原槽；>0=复制该槽为新 id 条目（菜单显示需另改 UserItemsConfig）
    prefab_path: 相关 prefab 容器路径（仅记录用；导入端自动扫描全部桥）

    导入端（import_pack._import_skin）：
      1. 新建 Texture2D（RGBA32 内嵌）；
      2. 克隆原材质并替换对应纹理槽引用；
      3. 扫描 bundle 内全部 SkinStorageBridge，把 target_id 条目的原材质 pid 换成新 pid
         （new_id>0 时额外追加一条新 id 条目）。
    """
    mats, tex_items = _pack_tex_slots(replace_mats, textures)
    manifest = {
        "format": FORMAT,
        "version": VERSION,
        "bundle": bundle_kind,
        "prefab_path": prefab_path,
        "skin": {
            "target_id": target_id,
            "new_id": new_id,
            "mats": mats,
            "textures": tex_items,
        },
    }
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
    return out_zip, len(mats), len(tex_items)


def _pack_tex_slots(replace_mats, textures):
    """共用：把 {orig, texs:[{slot,png}]} + [{name,png}] 归一化成 manifest 结构。

    返回 (mats, tex_items)：mats=[{orig, texs:[{slot, tex索引}]}]，tex_items=[{name,png(b64)}]。
    """
    tex_index = {}
    tex_items = []
    for t in textures:
        key = os.path.abspath(t["png"])
        if key in tex_index:
            continue
        tex_index[key] = len(tex_items)
        with open(t["png"], "rb") as f:
            tex_items.append({"name": t["name"],
                              "png": base64.b64encode(f.read()).decode("ascii")})
    mats = []
    for m in replace_mats:
        slots = []
        for s in m["texs"]:
            key = os.path.abspath(s["png"])
            if key in tex_index:
                slots.append({"slot": s["slot"], "tex": tex_index[key]})
        if not slots:
            continue
        mats.append({"orig": m["orig"], "texs": slots})
    if not mats:
        raise ValueError("没有找到可替换的纹理（贴图文件名需与材质纹理名一致）")
    return mats, tex_items


def create_matswap_pack(out_zip, replace_mats, textures, renderer_pids, prefab_path,
                        bundle_kind="units_assets_all"):
    r"""把模型材质贴图替换打成 .bamod 包（枪械等无皮肤桥的模型）。

    replace_mats / textures 同 create_skin_pack（只替换给出 png 的纹理槽）。
    renderer_pids: 要更新材质引用的渲染器 pid 列表（当前导入模型的所有渲染器）。

    导入端（import_pack._import_matswap）：
      1. 新建 Texture2D（RGBA32 内嵌）；
      2. 克隆原材质并替换对应纹理槽引用；
      3. 把这些渲染器的 m_Materials 里匹配的原材质 pid 换成新 pid（原地更新）。
    与皮肤包的区别：不经过 SkinStorageBridge——任何模型（武器/炸弹/挂架）都能换贴图。
    """
    mats, tex_items = _pack_tex_slots(replace_mats, textures)
    manifest = {
        "format": FORMAT,
        "version": VERSION,
        "bundle": bundle_kind,
        "prefab_path": prefab_path,
        "matswap": {
            "mats": mats,
            "textures": tex_items,
            "renderers": [int(r) for r in renderer_pids],
        },
    }
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
    return out_zip, len(mats), len(tex_items)
