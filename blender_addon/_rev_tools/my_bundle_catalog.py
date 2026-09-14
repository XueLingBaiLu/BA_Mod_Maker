# -*- coding: utf-8 -*-
r"""**给 catalog 注册"我自己的 bundle"**：新 bundle 条目 + extra(CRC/Hash/Size) + 资产条目 + 地址。

为什么必须单独一份（不能复用 `import_pack.register_address`）：
  `import_pack.register_address()` 是**抄游戏里已有参考条目的 7 元组** ⇒ 它的 `[2]`（依赖键下标）
  指向**参考资产所在的 bundle**。那只适合"往游戏已有 bundle 里加东西"。
  要让游戏加载一个**全新的 bundle 文件**，必须补齐五步：
```
① 新 bundle 的 internalId：{Addressables.RuntimePath}\PC\<文件名>
② 新 bundle 的条目：provider=0(AssetBundleProvider)、[2]=无依赖(0xFFFFFFFF)、[3]=17(空依赖 hash)、
   [4]=★新写的 extra（AssetBundleRequestOptions：m_Hash/m_Crc/m_BundleSize/m_BundleName…）、[6]=0
③ 新 bundle 的键（= 文件名，和游戏现有 bundle 的键法一致）+ 桶
④ 资产的 internalId（Assets/… 路径）+ 条目：[2]=★新 bundle 的键下标、[6]=该资产的 resourceType 下标
⑤ 地址键 → 指向资产条目
```
★ **增量友好**：如果 catalog 里**已经有**这个 bundle 的键（说明是你上次建的"我的 bundle"），
  本函数**不会**再加一份 bundle 条目/extra，只追加资产条目 + 地址 ⇒ 同一个自建 bundle 可以反复加东西 ✓

出处：`技术资料/scripts/add_bundle_and_asset.py`（离线版，实机验证过 T2：新 bundle 被游戏加载）+
`测试/test_make_mod_bundle.py` 的判据。CRC 语义统一委托 `compute_bundle_crc.compute_bundle_crc`。
"""
import hashlib
import json
import os
import re

RUNTIME = "{UnityEngine.AddressableAssets.Addressables.RuntimePath}"
BUNDLE_KEY_PREFIX = RUNTIME + "\\PC\\"


def _log(log):
    return log if callable(log) else (lambda *_a: None)


def find_resource_type(cat, want):
    """在 m_resourceTypes 里按类名找下标 → (index, dict)"""
    for i, t in enumerate(cat.get("m_resourceTypes") or []):
        if isinstance(t, dict) and t.get("m_ClassName") == want:
            return i, t
    return None, None


def list_resource_types(catalog_path):
    """可用资产类型（给 UI 下拉框用）"""
    from catalog_mod import Catalog
    c = Catalog(catalog_path)
    return [t.get("m_ClassName") for t in (c.cat.get("m_resourceTypes") or [])
            if isinstance(t, dict) and t.get("m_ClassName")]


def uncompressed_crc(path):
    """bundle 的 CRC（= catalog 的 m_Crc 语义：各数据块解压后按顺序拼接的 zlib.crc32）"""
    from compute_bundle_crc import compute_bundle_crc
    return compute_bundle_crc(path)


def _keyval(k):
    return k[1] if isinstance(k, tuple) and len(k) > 1 else k


def find_key_index(c, value):
    for i, k in enumerate(c.keys):
        if _keyval(k) == value:
            return i
    return None


def add_bundle_and_asset(catalog_path, bundle_path, address, asset_path,
                         asset_type="UnityEngine.GameObject", out=None, in_place=False,
                         log=None, asset_types=None):
    """★ 主入口：把"我的 bundle"注册进 catalog（多次调用 = 往同一个 bundle 里继续加资产）

    `asset_types`（列表，可选）：**同一个地址键挂多条不同资源类型的条目**。
    图标类资产**必须**这么注册 —— 实测 vanilla 的
    `AMMO_40MM_GRENADE` 桶里就是**两条条目**（同一 PNG 路径）：`[6]=3 UnityEngine.Texture2D`
    与 `[6]=4 UnityEngine.Sprite`（`catalog_entry_query.py --resolve AMMO_40MM_GRENADE` 实证）
    ⇒ 只注册一条的后果是"游戏按另一类型去取就取不到" ✓

    返回 dict(ok, catalog=<写出的文件>, bundle_name, bundle_key, reused_bundle, crc, md5hex…)
    """
    say = _log(log)
    from catalog_mod import Catalog, write_object
    if not os.path.exists(catalog_path):
        raise FileNotFoundError("catalog 不存在：%s" % catalog_path)
    if not os.path.exists(bundle_path):
        raise FileNotFoundError("bundle 不存在：%s" % bundle_path)
    bundle_name = os.path.basename(bundle_path)
    c = Catalog(catalog_path)
    ids = c.cat["m_InternalIds"]
    rt = c.cat.get("m_resourceTypes") or []

    if find_key_index(c, address) is not None:
        raise ValueError("地址 %r 已存在（换个地址，或先用 register 的更新路径删除旧条目）" % address)
    want_types = list(asset_types) if asset_types else [asset_type]
    rtis = []
    for tn in want_types:
        rti, rtd = find_resource_type(c.cat, tn)
        if rti is None:
            raise ValueError("m_resourceTypes 里没有类 %r（可用的有：%s）"
                             % (tn, [t.get("m_ClassName") for t in rt[:8]]))
        rtis.append((rti, rtd))

    crc = uncompressed_crc(bundle_path)
    hexes = re.findall(r"[0-9a-fA-F]{32}", bundle_name)
    if hexes:
        md5hex = hexes[-1].lower()
    else:
        md5hex = hashlib.md5(open(bundle_path, "rb").read()).hexdigest()

    # ---- ①-③ bundle 本身：已有键就复用（增量加资产） ----
    b_key = find_key_index(c, bundle_name)
    reused = b_key is not None
    if reused:
        say("bundle 键已存在（#%d，= 你上次建的），本次只追加资产" % b_key)
    else:
        jtext = json.dumps({
            "m_Hash": md5hex, "m_Crc": crc, "m_Timeout": 0, "m_ChunkedTransfer": False,
            "m_RedirectLimit": -1, "m_RetryCount": 0, "m_BundleName": bundle_name,
            "m_AssetLoadMode": 0, "m_BundleSize": os.path.getsize(bundle_path),
            "m_UseCrcForCachedBundles": True, "m_UseUWRForLocalBundles": False,
            "m_ClearOtherCachedVersionsWhenLoaded": False,
        }, separators=(",", ":"))       # ★ 紧凑格式：与游戏自己序列化的一致（带空格会让逐字节对照对不上）
        buf = bytearray(c.extra or b"")
        extra_off = len(buf)
        write_object(buf, 7, ("Unity.ResourceManager, Version=0.0.0.0, Culture=neutral, PublicKeyToken=null",
                              "UnityEngine.ResourceManagement.ResourceProviders.AssetBundleRequestOptions",
                              jtext))
        c.extra = bytes(buf)
        b_iid = len(ids)
        ids.append(BUNDLE_KEY_PREFIX + bundle_name)
        b_key = len(c.keys)
        c.keys.append((0, bundle_name))
        c.entries.append((b_iid, 0, 0xFFFFFFFF, 17, extra_off, b_key, 0))
        b_eidx = len(c.entries) - 1
        c.buckets.append({"dataOffset": -1, "entries": [b_eidx]})
        say("新 bundle 条目：internalId[%d]=%s | 键 #%d | 条目[%d] | extra@%d | CRC=%d m_Hash=%s"
            % (b_iid, BUNDLE_KEY_PREFIX + bundle_name, b_key, b_eidx, extra_off, crc, md5hex))

    # ---- ④ 资产条目 ----
    if asset_path in ids:
        ai = ids.index(asset_path)
    else:
        ai = len(ids)
        ids.append(asset_path)
    a_key = len(c.keys)
    c.keys.append((0, address))
    a_eidxs = []
    for rti, rtd in rtis:
        c.entries.append((ai, 2, b_key, 3006669565, 0xFFFFFFFF, a_key, rti))
        a_eidxs.append(len(c.entries) - 1)
        say("资产条目：internalId[%d]=%s | 地址 %r 键 #%d | 条目[%d]（[2]→bundle 键 #%d，[6]=%d %s）"
            % (ai, asset_path, address, a_key, a_eidxs[-1], b_key, rti, rtd.get("m_ClassName")))
    c.buckets.append({"dataOffset": -1, "entries": a_eidxs})
    a_eidx = a_eidxs[0]

    out_path = catalog_path if in_place else (out or os.path.join(
        os.path.dirname(os.path.abspath(bundle_path)), "catalog_with_mybundle.json"))
    c.save(out_path)
    say("✓ catalog 写出：%s（键 %d / 条目 %d）" % (out_path, len(c.keys), len(c.entries)))
    return {"ok": True, "catalog": out_path, "bundle_name": bundle_name, "bundle_key": b_key,
            "reused_bundle": reused, "crc": crc, "md5hex": md5hex,
            "address": address, "asset": asset_path, "type": asset_type,
            "asset_types": [rtd.get("m_ClassName") for _rti, rtd in rtis]}


def verify_chain(catalog_path, address, bundle_dir=None, bundle_name=None, log=None):
    """独立复核：地址 → 键 → 条目 → 依赖桶 → bundle → extra(m_Crc) →（可选）磁盘 CRC 复算"""
    say = _log(log)
    from catalog_mod import Catalog, read_object
    c = Catalog(catalog_path)
    ids = c.cat.get("m_InternalIds") or []
    rt = c.cat.get("m_resourceTypes") or []
    hit = find_key_index(c, address)
    if hit is None:
        say("✗ catalog 里找不到地址 %r" % address)
        return False
    ok = True
    say("✓ 地址键 #%d = %r → 条目 %s" % (hit, address, c.buckets[hit]["entries"]))
    for eidx in c.buckets[hit]["entries"]:
        e = c.entries[eidx]
        say("   条目[%d] internalId=%s provider=%s type=%s"
            % (eidx, ids[e[0]] if e[0] < len(ids) else "?",
               c.cat["m_ProviderIds"][e[1]] if e[1] < len(c.cat["m_ProviderIds"]) else "?",
               rt[e[6]].get("m_ClassName") if e[6] < len(rt) else "?"))
        if e[2] == 0xFFFFFFFF:
            say("      ⚠ [2] 依赖 = 无（资产不在任何 bundle 里？）")
            ok = False
            continue
        bk = _keyval(c.keys[e[2]])
        say("      [2] 依赖键 #%d = %r" % (e[2], bk))
        for beidx in c.buckets[e[2]]["entries"]:
            be = c.entries[beidx]
            biid = ids[be[0]] if be[0] < len(ids) else "?"
            crc_in_cat = None
            info = ""
            if be[4] != 0xFFFFFFFF:
                obj, _ = read_object(c.extra, be[4])
                if obj[0] == 7:
                    jt = obj[1][2]
                    h = re.search(r'"m_Hash"\s*:\s*"([0-9a-fA-F]+)"', jt)
                    cr = re.search(r'"m_Crc"\s*:\s*(\d+)', jt)
                    sz = re.search(r'"m_BundleSize"\s*:\s*(\d+)', jt)
                    info = "m_Hash=%s m_Crc=%s m_BundleSize=%s" % (
                        h.group(1) if h else "-", cr.group(1) if cr else "-", sz.group(1) if sz else "-")
                    crc_in_cat = int(cr.group(1)) if cr else None
            say("         bundle 条目[%d] → %s   %s" % (beidx, biid, info))
            if bundle_dir and bundle_name and biid.endswith(bundle_name):
                p = os.path.join(bundle_dir, bundle_name)
                if os.path.exists(p):
                    calc = uncompressed_crc(p)
                    same = (crc_in_cat == calc)
                    say("         %s 磁盘 CRC 复算 = %d（catalog 里 %s）"
                        % ("✓" if same else "✗", calc, crc_in_cat))
                    ok = ok and same
                else:
                    say("         ⚠ 磁盘上没有 %s" % p)
    return ok
