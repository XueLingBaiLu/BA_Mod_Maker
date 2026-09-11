# -*- coding: utf-8 -*-
r"""载具皮肤数据模块：SkinStorageBridge 解析/重打包 + 皮肤索引（纯 Python + UnityPy）。

已破解布局（来自 bundle 内 typetree，实测 322 个桥实例）：
  SkinStorageBridge (MonoBehaviour) 原始字节：
    0..11   {i32 m_FileID, i64 go_pid}          m_GameObject
    12      UInt8 m_Enabled (+3 填充)
    16..27  {i32 m_FileID, i64 script_pid}      m_Script
    28..31  m_Name 空串（4 字节 0）
    32..    Data: List<SkinStorageData>
        i32 count
        per entry:
            i32 Id
            i32 keyCount,  keyCount  × {i32 fid, i64 mat_pid}         （材质）
            i32 valCount,  valCount  × { i32 rCount, rCount × {i32 fid, i64 renderer_pid} }
  皮肤应用方式（游戏端）：SetSkin 把每个材质绑定到对应 Renderer。

设计：全部返回纯 Python 数据；Blender operator / BA_Mod_Maker 各自负责 UI 与写盘。
"""
import os
import struct

try:
    import UnityPy  # noqa: F401
except ImportError:
    HERE = os.path.dirname(os.path.abspath(__file__))
    import sys
    sys.path.insert(0, os.path.join(HERE, "..", "..", "_unitypy"))
    import UnityPy  # noqa: F401


# ---------------------------------------------------------------- bridge 编解码

def parse_bridge_items(raw, off=32):
    """从字节偏移 off 解析 List<SkinStorageData>，返回 (items, 结束偏移)。

    items: [{"id": int, "mats": [{"pid": mat_pid, "renderers": [r_pid...]}...]}...]
    """
    n = struct.unpack_from("<i", raw, off)[0]
    off += 4
    items = []
    for _ in range(n):
        sid = struct.unpack_from("<i", raw, off)[0]
        off += 4
        kn = struct.unpack_from("<i", raw, off)[0]
        off += 4
        keys = []
        for _k in range(kn):
            _fid, mp = struct.unpack_from("<iq", raw, off)
            off += 12
            keys.append(mp)
        vn = struct.unpack_from("<i", raw, off)[0]
        off += 4
        vals = []
        for _v in range(vn):
            rn = struct.unpack_from("<i", raw, off)[0]
            off += 4
            rs = []
            for _r in range(rn):
                _fid, rp = struct.unpack_from("<iq", raw, off)
                off += 12
                rs.append(rp)
            vals.append(rs)
        items.append({"id": sid, "mats": [{"pid": k, "renderers": r}
                                          for k, r in zip(keys, vals)]})
    return items, off


def parse_bridge_full(raw):
    """解析完整 MonoBehaviour 原始字节：返回 {go_pid, script_pid, items}。"""
    if len(raw) < 32:
        raise ValueError("bridge 字节过短: %d" % len(raw))
    _gof, go_pid = struct.unpack_from("<iq", raw, 0)
    _sf, script_pid = struct.unpack_from("<iq", raw, 16)
    items, _end = parse_bridge_items(raw, 32)
    return {"go_pid": go_pid, "script_pid": script_pid, "items": items}


def emit_bridge_full(go_pid, script_pid, items, enabled=1):
    """把桥数据序列化为完整 MonoBehaviour 原始字节（m_Name 空）。"""
    buf = bytearray()
    buf += struct.pack("<iq", 0, go_pid)          # m_GameObject
    buf += struct.pack("<B", enabled) + b"\x00" * 3
    buf += struct.pack("<iq", 0, script_pid)      # m_Script
    buf += struct.pack("<i", 0)                   # m_Name 空
    buf += struct.pack("<i", len(items))          # Data.size
    for it in items:
        buf += struct.pack("<i", it["id"])
        mats = it["mats"]
        buf += struct.pack("<i", len(mats))
        for m in mats:
            buf += struct.pack("<iq", 0, m["pid"])
        buf += struct.pack("<i", len(mats))
        for m in mats:
            rs = m["renderers"]
            buf += struct.pack("<i", len(rs))
            for r in rs:
                buf += struct.pack("<iq", 0, r)
    return bytes(buf)


def replace_skin_mats(raw, target_id, mat_map, append_new_id=0):
    """修改桥原始字节：
    - 对每个 id==target_id 的条目：把材质 pid 按 mat_map 替换（旧 pid -> 新 pid）。
    - 若 append_new_id>0 且不存在该 id，则复制 target_id 条目追加为新 id 条目
      （材质全部替换后；用于「新增皮肤槽」——菜单显示需另改 UserItemsConfig）。
    返回新字节；无变化时返回 None。
    """
    d = parse_bridge_full(raw)
    changed = False
    for it in d["items"]:
        if it["id"] != target_id:
            continue
        for m in it["mats"]:
            if m["pid"] in mat_map:
                m["pid"] = mat_map[m["pid"]]
                changed = True
    if append_new_id > 0 and append_new_id != target_id \
            and not any(it["id"] == append_new_id for it in d["items"]):
        src = next((it for it in d["items"] if it["id"] == target_id), None)
        if src:
            import copy
            new_it = copy.deepcopy(src)
            new_it["id"] = append_new_id
            d["items"].append(new_it)
            changed = True
    if not changed:
        return None
    return emit_bridge_full(d["go_pid"], d["script_pid"], d["items"])


# ---------------------------------------------------------------- bundle 索引

_ENV_CACHE = {}


def _load_env(bundle):
    from extract_model import _load
    return _load(bundle)


def _renderer_go_map(objs, by_pid):
    """renderer 组件 pid -> GO pid（用于显示名）。"""
    out = {}
    for o in objs:
        if o.type.name in ("MeshRenderer", "SkinnedMeshRenderer"):
            try:
                g = o.read().m_GameObject
                if g:
                    out[o.path_id] = g.m_PathID
            except Exception:
                pass
    return out


def _go_name(by_pid, gpid):
    o = by_pid.get(gpid)
    if not o or o.type.name != "GameObject":
        return "?"
    try:
        return o.read().m_Name
    except Exception:
        return "?"


def _mat_info(by_pid, pid, cache):
    if pid in cache:
        return cache[pid]
    info = {"pid": pid, "name": "?", "tex": []}
    o = by_pid.get(pid)
    if o and o.type.name == "Material":
        try:
            m = o.read()
            info["name"] = m.m_Name or "?"
            for te in (m.m_SavedProperties.m_TexEnvs or []):
                slot = te[0]
                tp = te[1].m_Texture.m_PathID if te[1].m_Texture else 0
                if tp:
                    to = by_pid.get(tp)
                    tn = "?"
                    if to:
                        try:
                            tn = to.read().m_Name
                        except Exception:
                            pass
                    info["tex"].append({"slot": slot, "pid": tp, "name": tn})
        except Exception:
            pass
    cache[pid] = info
    return info


def build_skin_index(bundle):
    """解析 bundle 内全部 SkinStorageBridge，返回:
    [{"prefab": 容器路径, "go": GO 名, "bridge_pid": pid,
      "items": [{"id": 235, "mats": [{"pid", "name", "tex": [{slot,pid,name}],
                  "renderers": [{"pid", "go"}]}]}]}]
    缓存：皮肤索引只建一次（每 bundle）。
    """
    _, objs, by_pid = _load_env(bundle)
    cache_key = ("skin_index", bundle)
    if cache_key in _ENV_CACHE:
        return _ENV_CACHE[cache_key]
    sbb_pid = 0
    for o in objs:
        if o.type.name == "MonoScript":
            try:
                if o.read().m_Name == "SkinStorageBridge":
                    sbb_pid = o.path_id
            except Exception:
                pass
    if not sbb_pid:
        _ENV_CACHE[cache_key] = []
        return []
    rg_map = _renderer_go_map(objs, by_pid)
    mat_cache = {}
    out = []
    for o in objs:
        if o.type.name != "MonoBehaviour":
            continue
        raw = o.get_raw_data()
        if len(raw) < 32 or struct.unpack_from("<q", raw, 20)[0] != sbb_pid:
            continue
        try:
            d = parse_bridge_full(raw)
        except Exception:
            continue
        items = []
        for it in d["items"]:
            mats = []
            for m in it["mats"]:
                mi = _mat_info(by_pid, m["pid"], mat_cache)
                mats.append({
                    "pid": m["pid"], "name": mi["name"], "tex": mi["tex"],
                    "renderers": [{"pid": r, "go": _go_name(by_pid, rg_map.get(r, 0))}
                                  for r in m["renderers"]],
                })
            items.append({"id": it["id"], "mats": mats})
        out.append({"prefab": o.container or "(none)", "go": _go_name(by_pid, d["go_pid"]),
                    "bridge_pid": o.path_id, "items": items})
    _ENV_CACHE[cache_key] = out
    return out


def skins_for_renderers(index, renderer_pids):
    """按导入场景的 renderer pid 集合匹配皮肤（合并多个桥的同一 id）。

    返回: {skin_id: {"mats": [{"pid", "name", "tex", "renderers"}...]}}
    只保留与该模型相关的材质（其 renderers 与 renderer_pids 有交集）。
    """
    rset = set(renderer_pids)
    merged = {}
    for entry in index:
        for it in entry["items"]:
            mats = [m for m in it["mats"]
                    if any(r["pid"] in rset for r in m["renderers"])]
            if not mats:
                continue
            bag = merged.setdefault(it["id"], {})
            for m in mats:
                bag.setdefault(m["pid"], m)
    return merged


def texture_png_bytes(by_pid, tex_pid):
    """把 Texture2D 解码为 PNG 字节；失败返回 None。"""
    o = by_pid.get(tex_pid)
    if not o or o.type.name != "Texture2D":
        return None
    try:
        t = o.read()
        img = t.image
        if img is None:
            return None
        import io
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()
    except Exception:
        return None


def renderer_materials(by_pid, renderer_pid):
    """renderer 组件的材质 pid 列表（默认外观）。"""
    o = by_pid.get(renderer_pid)
    if not o or o.type.name not in ("MeshRenderer", "SkinnedMeshRenderer"):
        return []
    try:
        r = o.read()
        return [m.m_PathID for m in (r.m_Materials or []) if m and m.m_PathID]
    except Exception:
        return []
