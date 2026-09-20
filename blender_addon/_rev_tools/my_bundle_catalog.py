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


_BUNDLE_OPTIONS_CLASS = "UnityEngine.ResourceManagement.ResourceProviders.AssetBundleRequestOptions"


def find_reference_bundle_options(c, log=None):
    r"""★ 找一个**游戏原生**的 bundle extra 当"字段风格参考" ⇒ `(条目下标, extra 偏移, dict)`。

    为什么要有它（2026-10-16 ★⑨）
    ==============================
    我们以前是**自己拼 JSON 字面量**写 `AssetBundleRequestOptions`，字段名/顺序/取值全靠手抄
    ⇒ 结果只有一处风格不对：`m_BundleName` 写成了**文件名**（`acvfly_aaba….bundle`），
    而游戏原生条目里它是**纯 32 位哈希**：
      · 原生：`{"m_Hash":"b54a3ecae2424f30d00f4dbb79449716", … "m_BundleName":"b54a3ecae2424f30d00f4dbb79449716"}`
      · 我们新注册的：`"m_BundleName":"acvfly_aaba….bundle"` ← **带后缀** ✗
      · 我们**重指向**来的（沿用原值）：纯哈希 ⇒ **这条路从没出过问题** ✓
    ⇒ 修法：**从参考条目深拷贝字段风格**（键集 + 顺序 + 非数值字段），只覆盖
      `m_Hash`/`m_Crc`/`m_BundleSize`/`m_BundleName` 这四个随文件变的 ✓

    挑选判据（都要求"看起来像游戏自己的"）：
      · extra 对象类型 = 7（`AssetBundleRequestOptions`）
      · `m_BundleName` **是 32 位十六进制**（不带 `.bundle`）
      · 该条目所属 internalId 以 `.bundle` 结尾（确实是个 bundle 条目）
    ⛔ 找不到就返回 None，调用方**退回老的内联字面量**并打一行警告 —— 不因为"参考缺失"就拒绝注册 ✓
    """
    say = _log(log)
    from catalog_mod import read_object
    ids = c.cat.get("m_InternalIds") or []
    for eidx, e in enumerate(c.entries):
        if len(e) < 6 or e[4] == 0xFFFFFFFF or e[0] >= len(ids):
            continue
        iid = ids[e[0]]
        if not iid.endswith(".bundle"):
            continue
        try:
            obj, _ = read_object(c.extra, e[4])
        except Exception:                                             # noqa: BLE001
            continue
        if obj[0] != 7 or not isinstance(obj[1], tuple) or len(obj[1]) < 3:
            continue
        if obj[1][1] != _BUNDLE_OPTIONS_CLASS:
            continue
        try:
            j = json.loads(obj[1][2])
        except Exception:                                             # noqa: BLE001
            continue
        bn = j.get("m_BundleName")
        if isinstance(bn, str) and re.fullmatch(r"[0-9a-fA-F]{32}", bn or ""):
            say("字段风格参考：条目[%d] %s（m_BundleName=%s 纯哈希 ✓）" % (eidx, os.path.basename(iid), bn))
            return eidx, e[4], j
    say("⚠ catalog 里找不到「游戏原生」的 AssetBundleRequestOptions 参考条目 ⇒ 退回内置字段模板")
    return None


def bundle_extra_json(ref_dict, md5hex, crc, size):
    r"""按参考条目的**字段风格**生成 extra 的 JSON 文本（深拷贝后只覆盖 4 个随文件变的值）。

    `ref_dict` 为 None 时退回内置模板（老行为，但 `m_BundleName` 也写成**纯哈希**）。
    ⛔ `ensure_ascii=False` + 紧凑分隔符：与游戏自己序列化的风格一致（带空格会让逐字节对照对不上）。
    """
    if isinstance(ref_dict, dict):
        j = json.loads(json.dumps(ref_dict))           # 深拷贝：不动调用方那份
    else:
        j = {"m_Hash": "", "m_Crc": 0, "m_Timeout": 0, "m_ChunkedTransfer": False,
             "m_RedirectLimit": -1, "m_RetryCount": 0, "m_BundleName": "",
             "m_AssetLoadMode": 0, "m_BundleSize": 0,
             "m_UseCrcForCachedBundles": True, "m_UseUWRForLocalBundles": False,
             "m_ClearOtherCachedVersionsWhenLoaded": False}
    j["m_Hash"] = md5hex
    j["m_Crc"] = crc
    j["m_BundleSize"] = size
    # ★ 纯 32 位哈希、**不带 `.bundle`**（★⑨ 的核心）
    j["m_BundleName"] = md5hex
    return json.dumps(j, separators=(",", ":"), ensure_ascii=False)


def bundle_hash_of(bundle_name):
    """bundle 文件名里的 32 位哈希（Addressables 的内容哈希）；没有就退回文件 md5。"""
    hexes = re.findall(r"[0-9a-fA-F]{32}", bundle_name or "")
    return hexes[-1].lower() if hexes else None


def find_key_index(c, value):
    for i, k in enumerate(c.keys):
        if _keyval(k) == value:
            return i
    return None


def extra_keys(jtext):
    """extra JSON 里的顶层键（保持顺序）—— 用来做"逐字段风格比对"。"""
    try:
        return list(json.loads(jtext).keys())
    except Exception:                                                 # noqa: BLE001
        return []


def compare_extra_style(ref_dict, ours_jtext):
    r"""→ None（风格一致）或一句差异描述。**参考缺失时返回 None**（不误报）。

    ★⑩ 的"逐字段风格比对"用的就是它：键集 + 顺序必须与游戏原生条目一致
    （值只有 `m_Hash`/`m_Crc`/`m_BundleSize`/`m_BundleName` 允许不同）。
    """
    if not isinstance(ref_dict, dict):
        return None
    a, b = list(ref_dict.keys()), extra_keys(ours_jtext)
    if a == b:
        return None
    miss = [k for k in a if k not in b]
    extra = [k for k in b if k not in a]
    bits = []
    if miss:
        bits.append("缺 %s" % miss)
    if extra:
        bits.append("多 %s" % extra)
    if not bits and a != b:
        bits.append("顺序不同：参考 %s / 我们 %s" % (a, b))
    return "；".join(bits)


def _rewrite_extra(c, eidx, new_jtext):
    """把条目 `eidx` 的 extra 换成 `new_jtext`（内容相同则不动）。返回是否真的改了。**

    ⛔ 与 `my_bundle.sync_crc` 同一套做法：extra 是**共享的字节池**，别的条目可能也指着
      同一段 ⇒ **一律追加新段 + 把本条目的 [4] 指过去**（绝不原地伸缩，那会踩坏相邻条目）。
    """
    from catalog_mod import read_object, write_object
    e = c.entries[eidx]
    if e[4] == 0xFFFFFFFF:
        return False
    obj, _ = read_object(c.extra, e[4])
    if obj[0] != 7:
        return False
    if obj[1][2] == new_jtext:
        return False
    buf = bytearray(c.extra or b"")
    off = len(buf)
    write_object(buf, 7, (obj[1][0], obj[1][1], new_jtext))
    c.extra = bytes(buf)
    e = list(c.entries[eidx])
    e[4] = off
    c.entries[eidx] = tuple(e)
    return True


def _update_existing(c, existing, address, bundle_path, bundle_name, md5hex, crc, size,
                     asset_path, want_types, catalog_path, out, in_place, say):
    r"""★⑭ 地址已存在 ⇒ **只重写 extra**，沿用原条目的 internalId/key/providerId ✓

    判据：这个地址的依赖链最终指向的 bundle 条目里，**有没有 internalId 以我们的文件名结尾**的。
      · 有 ⇒ 那是我们自己的包 ⇒ 刷新 `m_Hash`/`m_Crc`/`m_BundleSize`/`m_BundleName` 就完事
      · 没有 ⇒ 这个地址属于**别人的包**（游戏原生 / 另一个 mod）。⛔ 不硬抢 —— 抢了就是顶掉
        游戏自己的资产（那次"装进游戏 UI 全没了"的教训就在这一类上）⇒ 报错并**说清三条出路**。
    """
    from catalog_mod import read_object
    ids = c.cat.get("m_InternalIds") or []
    mine, theirs = [], []
    for eidx in c.buckets[existing]["entries"]:
        e = c.entries[eidx]
        if e[2] == 0xFFFFFFFF:
            theirs.append("条目[%d] 依赖 = 无（资产不在任何 bundle 里）" % eidx)
            continue
        for beidx in c.buckets[e[2]]["entries"]:
            be = c.entries[beidx]
            biid = ids[be[0]] if be[0] < len(ids) else "?"
            (mine if biid.endswith(bundle_name) else theirs).append(
                (beidx, biid) if biid.endswith(bundle_name) else "条目[%d] %s" % (beidx, biid))
    if not mine:
        raise ValueError(
            "地址 %r 已存在，但它指向**别的包**（不是 %s）⇒ 不能直接覆盖，否则会顶掉别人的资产。\n"
            "  现状：%s\n"
            "  出路：① 换一个新地址（地址长度要 ≤ DB 字段原值，见「地址命名辅助」）；\n"
            "        ② 想**替换**那个资产 ⇒ 用「肖像/图标：一键原键替换」或条目级重指向；\n"
            "        ③ 确实要删掉这条注册 ⇒ 用显式的「删除已注册地址」入口，别让工具替你猜。"
            % (address, bundle_name, "；".join(theirs[:3]) or "（空）"))
    changed, crc_before = 0, None
    for beidx, biid in mine:
        e = c.entries[beidx]
        if e[4] == 0xFFFFFFFF:
            continue
        obj, _ = read_object(c.extra, e[4])
        if obj[0] != 7:
            continue
        try:
            old = json.loads(obj[1][2])
        except Exception:                                             # noqa: BLE001
            old = {}
        if crc_before is None:
            crc_before = old.get("m_Crc")
        # ★⑨：沿用刚刚读出来的那份**字段风格**（键集+顺序），只换随文件变的四个值
        if _rewrite_extra(c, beidx, bundle_extra_json(old or None, md5hex, crc, size)):
            changed += 1
    out_path = catalog_path if in_place else (out or os.path.join(
        os.path.dirname(os.path.abspath(bundle_path)), "catalog_with_mybundle.json"))
    c.save(out_path)
    if changed:
        say("✓ 已更新现有条目（地址 %r · 改了 %d 条 extra · 沿用原 internalId/key/providerId）："
            "m_Crc %s → %d · m_BundleSize %d · m_BundleName=%s（纯哈希 ✓）"
            % (address, changed, crc_before, crc, size, md5hex))
    else:
        say("✓ 地址 %r 已存在且就是本包 ⇒ extra 已经是最新的（无需改动）" % address)
    return {"ok": True, "updated": True, "changed": changed, "catalog": out_path,
            "bundle_name": bundle_name, "bundle_key": find_key_index(c, bundle_name),
            "reused_bundle": True, "crc": crc, "crc_before": crc_before, "md5hex": md5hex,
            "address": address, "asset": asset_path, "asset_types": [],
            "updated_entry": "现有条目（沿用 internalId/key/providerId）",
            "noop": changed == 0}


def refresh_bundle_entry_style(catalog_path, bundle_path, out=None, in_place=False, log=None,
                               dry_run=False):
    r"""★㉑：把**我们自己的** bundle 条目的 extra 刷成"像游戏自己写的"（★⑨ 口径）。

    为什么需要（★㉑/[工具-10] 的用户现场）：catalog 里那条 bundle 条目的 `m_BundleName` 带
    `.bundle` ⇒ ⑥ 自检报「我们注册的 bundle 条目的字段风格不对」，而当时**唯一的修法是
    "重新注册一遍"**（要地址、要走一遍打包流程）⇒ 用户直接问"你能不能直接修一下" ✗
    ⇒ 这里给一个**只修这一条 extra、别的一字不动**的入口 ✓

    · 判定键 = `ids[e[0]].endswith(<我们的 bundle 文件名>)`（与 `verify_bundle_entry` **同一个键**）
      ⇒ 游戏原生条目 / 别家的包**不可能命中** ✓
    · 只重写 `m_Hash`/`m_Crc`/`m_BundleSize`/`m_BundleName`（`_rewrite_extra` + `bundle_extra_json`）
      ⇒ `internalId`/`key`/`providerId`/地址键**逐条不变** ✓
    · ⛔ **0 命中 ⇒ `ok=False` 且一字不写**（防"没改也算成功"）；⛔ **changed==0（本来就是好风格）
      ⇒ `noop=True` 且不写盘**（连点两次 ⇒ 文件哈希逐字节不变）✓
    · ⛔ 默认**不 in_place**、落点 = 我们的工作目录（`catalog_with_mybundle.json` 口径，见 L251-L252）
      ⇒ **绝不直接写游戏目录** ✓

    ⇒ `{ok, changed, noop, entries[条目号], out, reason, bundle_name, md5hex, crc, size, verify}`
    ★★ [工具-10] 补缺：`dry_run=True` ⇒ **只预演**：返回 `preview=[{entry, diffs:[{field, old, new}]}]`、
      逐条打印"条目[号] 字段：旧 → 新"，**⛔ 一个字都不写盘**（`applied=False`；⛔ 连 `out` 都不生成）
      ⇒ 满足硬要求「dry-run 能看出将改哪些条目／把什么改成什么（逐条，⛔ 只给计数不算）」✓
    """
    say = _log(log)
    r = {"ok": False, "changed": 0, "noop": False, "entries": [], "out": None, "reason": None,
         "bundle_name": None, "md5hex": None, "crc": None, "size": None, "verify": None}
    if not os.path.isfile(catalog_path):
        r["reason"] = "catalog 不存在：%s" % catalog_path
        say("✗ %s" % r["reason"])
        return r
    if not os.path.isfile(bundle_path):
        r["reason"] = "bundle 不存在：%s" % bundle_path
        say("✗ %s" % r["reason"])
        return r
    from catalog_mod import Catalog, read_object
    bundle_name = os.path.basename(bundle_path)
    r["bundle_name"] = bundle_name
    c = Catalog(catalog_path)
    ids = c.cat.get("m_InternalIds") or []
    # ⛔ 先判"有没有我们自己的条目"、**再**去算 CRC：算 CRC 要读整包（本包 43 MB），
    #    而"判定键不命中"（喂了原生包名）的情形应当**既快又保证一字不写** ✓
    mine = []
    for eidx, e in enumerate(c.entries):
        if len(e) < 7 or e[4] == 0xFFFFFFFF or e[0] >= len(ids):
            continue
        if not ids[e[0]].endswith(bundle_name):
            continue
        obj, _ = read_object(c.extra, e[4])
        if obj[0] != 7 or not isinstance(obj[1], tuple) or len(obj[1]) < 3:
            continue
        mine.append((eidx, obj[1][2]))
    if not mine:
        r["reason"] = ("catalog 里找不到 internalId 以 %s 结尾的 bundle 条目"
                       "（判定键不命中 ⇒ 这不是我们注册的包）⇒ ⛔ 拒改、一字不写" % bundle_name)
        say("✗ %s" % r["reason"])
        return r
    crc = uncompressed_crc(bundle_path)
    md5hex = bundle_hash_of(bundle_name) or hashlib.md5(open(bundle_path, "rb").read()).hexdigest()
    size = os.path.getsize(bundle_path)
    ref = find_reference_bundle_options(c, log=log)
    r.update({"md5hex": md5hex, "crc": crc, "size": size})
    # ★★ [工具-10] 补缺 ③：**dry-run 逐条**（⛔ 不新造检测逻辑：要改什么仍由 `_rewrite_extra` 比对决定；
    #    这里只是把"旧 extra vs 新 extra"按字段列出来，供人**先看清楚再决定改不改**）。
    preview = []
    for eidx, jtext in mine:
        try:
            old = json.loads(jtext)
        except Exception as _e:                                       # noqa: BLE001
            say("⚠ 条目[%d] 旧 extra 解析失败（%s: %s）⇒ 按空字典比对（差异会整体列出）"
                % (eidx, type(_e).__name__, _e))
            old = None
        new_jtext = bundle_extra_json(ref[2] if ref else old, md5hex, crc, size)
        try:
            new_d = json.loads(new_jtext)
        except Exception as _e:                                       # noqa: BLE001
            say("⚠ 条目[%d] 新 extra 解析失败（%s: %s）⇒ 记为整体差异"
                % (eidx, type(_e).__name__, _e))
            new_d = {}
        old_d = old if isinstance(old, dict) else {}
        diffs = []
        for k in list(new_d.keys()) + [k for k in old_d if k not in new_d]:
            ov, nv = old_d.get(k, None), new_d.get(k, None)
            if ov != nv:
                diffs.append({"field": k, "old": ov, "new": nv})
        if not diffs and old_d != new_d:
            diffs.append({"field": "<整个 extra>", "old": jtext, "new": new_jtext})
        # ★⑨：字段风格**从原生参考条目深拷贝**，只换随文件变的四个值（`m_BundleName` 写纯哈希）
        if _rewrite_extra(c, eidx, new_jtext):
            r["changed"] += 1
        r["entries"].append(eidx)
        preview.append({"entry": eidx, "diffs": diffs})
    r["preview"] = preview
    if dry_run:
        r.update({"dry_run": True, "applied": False, "ok": False})
        say("—— 预演（dry-run）：**未写盘**。将改 %d 条、条目 %s："
            % (r["changed"], r["entries"]))
        for pv in preview:
            for d in pv["diffs"]:
                say("   · 条目[%d] %s：旧 %r → 新 %r"
                    % (pv["entry"], d["field"], d["old"], d["new"]))
        if r["changed"] == 0:
            r["noop"] = True
            r["reason"] = "条目 %s 的 extra 已经是原生风格（无需改动）" % r["entries"]
            say("✓ %s ⇒ 预演结论 = 无需改动（⛔ 也不写盘）" % r["reason"])
        return r
    if r["changed"] == 0:
        r["noop"] = True
        r["reason"] = "条目 %s 的 extra 已经是原生风格（无需改动）" % r["entries"]
        say("✓ %s ⇒ noop：⛔ 不写盘（文件哈希不变）" % r["reason"])
        return r
    out_path = catalog_path if in_place else (out or os.path.join(
        os.path.dirname(os.path.abspath(bundle_path)), "catalog_with_mybundle.json"))
    c.save(out_path)
    r.update({"ok": True, "out": out_path, "dry_run": False, "applied": True})
    say("✓ 已修 catalog 字段风格：条目 %s 的 extra 重写（改了 %d 条）· m_Hash=%s · m_Crc=%d · "
        "m_BundleSize=%d · m_BundleName=%s（纯哈希 ✓）· internalId/key/providerId/地址键逐条不变"
        % (r["entries"], r["changed"], md5hex, crc, size, md5hex))
    v = verify_bundle_entry(out_path, bundle_name, ref_style=ref[2] if ref else None)
    r["verify"] = v
    if v["ok"]:
        say("✓ 自证：%s 的 extra 现在与原生参考同风格（%d 个字段）" % (bundle_name, len(v["keys"])))
    else:
        say("⚠ 修后自证仍有问题：%s" % "；".join(v["problems"]))
    return r


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

    ★ 2026-10-16（★⑭）**地址已存在时不再抛异常**：自动走「更新现有条目」分支 ——
      重建模型时高频走到这里，以前直接 `ValueError: 地址 'FLYACV' 已存在` 把整步打断，
      而**包其实已经成功写好了**，只有 catalog 这一步没做（用户以为整个失败）✗
      ⇒ 现在：地址已存在**且指向我们自己的包** ⇒ 只重写 extra（hash/CRC/size/BundleName），
      沿用原条目的 internalId/key/providerId，输出 `✓ 已更新现有条目（CRC 旧 → 新）` ✓
      ⛔ 地址指向**别人的包**时不硬抢（那会顶掉游戏原生资产）⇒ 报一条**说清怎么办**的错。
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

    crc = uncompressed_crc(bundle_path)
    md5hex = bundle_hash_of(bundle_name) or hashlib.md5(open(bundle_path, "rb").read()).hexdigest()
    size = os.path.getsize(bundle_path)

    # ★⑭ 地址已存在 ⇒ 走"更新现有条目"，不再中断整步
    existing = find_key_index(c, address)
    if existing is not None:
        return _update_existing(c, existing, address, bundle_path, bundle_name, md5hex, crc, size,
                                asset_path, asset_types or ([asset_type] if asset_type else []),
                                catalog_path, out, in_place, say)

    want_types = list(asset_types) if asset_types else [asset_type]
    rtis = []
    for tn in want_types:
        rti, rtd = find_resource_type(c.cat, tn)
        if rti is None:
            raise ValueError("m_resourceTypes 里没有类 %r（可用的有：%s）"
                             % (tn, [t.get("m_ClassName") for t in rt[:8]]))
        rtis.append((rti, rtd))

    ref = find_reference_bundle_options(c, log=log)

    # ---- ①-③ bundle 本身：已有键就复用（增量加资产） ----
    b_key = find_key_index(c, bundle_name)
    reused = b_key is not None
    if reused:
        say("bundle 键已存在（#%d，= 你上次建的），本次只追加资产" % b_key)
    else:
        # ★⑨：字段风格**从参考条目深拷贝**，`m_BundleName` 写**纯 32 位哈希**（不带 `.bundle`）
        jtext = bundle_extra_json(ref[2] if ref else None, md5hex, crc, size)
        ref_mismatch = compare_extra_style(ref[2], jtext) if ref else None
        buf = bytearray(c.extra or b"")
        extra_off = len(buf)
        write_object(buf, 7, ("Unity.ResourceManager, Version=0.0.0.0, Culture=neutral, PublicKeyToken=null",
                              _BUNDLE_OPTIONS_CLASS, jtext))
        c.extra = bytes(buf)
        b_iid = len(ids)
        ids.append(BUNDLE_KEY_PREFIX + bundle_name)
        b_key = len(c.keys)
        c.keys.append((0, bundle_name))
        c.entries.append((b_iid, 0, 0xFFFFFFFF, 17, extra_off, b_key, 0))
        b_eidx = len(c.entries) - 1
        c.buckets.append({"dataOffset": -1, "entries": [b_eidx]})
        say("新 bundle 条目：internalId[%d]=%s | 键 #%d | 条目[%d] | extra@%d | CRC=%d m_Hash=%s m_BundleName=%s"
            % (b_iid, BUNDLE_KEY_PREFIX + bundle_name, b_key, b_eidx, extra_off, crc, md5hex, md5hex))
        if ref_mismatch:
            say("   ⚠ 与参考条目的字段差异（应当只有随文件变的四个值）：%s" % ref_mismatch)

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
    # ★ **写入后重新解析自证**（沿用项目纪律）：新条目的 extra 必须
    #   ① 是纯 32 位哈希的 m_BundleName（不带 .bundle）② 与参考条目**字段风格一致**
    verify = verify_bundle_entry(out_path, bundle_name, ref_style=ref[2] if ref else None)
    if verify["ok"]:
        say("✓ 自证：新条目 extra 字段风格与原生一致（%d 个字段，m_BundleName=%s 纯哈希）"
            % (len(verify["keys"]), verify["m_BundleName"]))
    else:
        say("⚠ 自证发现问题：%s" % "；".join(verify["problems"]))
    return {"ok": True, "catalog": out_path, "bundle_name": bundle_name, "bundle_key": b_key,
            "reused_bundle": reused, "crc": crc, "md5hex": md5hex,
            "address": address, "asset": asset_path, "type": asset_type,
            "updated": False, "verify": verify,
            "asset_types": [rtd.get("m_ClassName") for _rti, rtd in rtis]}


def verify_bundle_entry(catalog_path, bundle_name, ref_style=None):
    r"""★ **写入后自证**：这个 bundle 在 catalog 里的条目 extra 是否"像游戏自己写的"。

    检查项（★⑨ 的验收口径，返回 `{ok, problems[], keys[], m_BundleName, extras[]}`）：
      ① `m_BundleName` 是**纯 32 位十六进制**（⛔ 不带 `.bundle`）
      ② `m_Hash` 与文件名里的 32hex 一致（游戏就是用这个找文件的）
      ③ extra 的键集与 `ref_style` 一致（给了参考才有这条）
      ④ `m_BundleSize` 是正整数
    ⛔ 只读，不改文件 ⇒ 可以被自检（★⑩）直接调用 ✓
    """
    from catalog_mod import Catalog, read_object
    ref = ref_style if isinstance(ref_style, dict) else None
    out = {"ok": True, "problems": [], "keys": [], "m_BundleName": None, "extras": []}
    if not os.path.isfile(catalog_path):
        out["ok"] = False
        out["problems"].append("catalog 不存在：%s" % catalog_path)
        return out
    c = Catalog(catalog_path)
    ids = c.cat.get("m_InternalIds") or []
    want_hash = bundle_hash_of(bundle_name)
    found = False
    for eidx, e in enumerate(c.entries):
        if len(e) < 7 or e[4] == 0xFFFFFFFF or e[0] >= len(ids):
            continue
        if not ids[e[0]].endswith(bundle_name):
            continue
        obj, _ = read_object(c.extra, e[4])
        if obj[0] != 7:
            continue
        found = True
        try:
            j = json.loads(obj[1][2])
        except Exception as ex:                                       # noqa: BLE001
            out["problems"].append("条目[%d] extra 不是合法 JSON：%s" % (eidx, ex))
            continue
        out["extras"].append(j)
        out["keys"] = list(j.keys())
        bn = j.get("m_BundleName")
        out["m_BundleName"] = bn
        if not (isinstance(bn, str) and re.fullmatch(r"[0-9a-fA-F]{32}", bn)):
            out["problems"].append("条目[%d] m_BundleName=%r 不是纯 32 位哈希（★⑨：不能带 .bundle）"
                                   % (eidx, bn))
        if want_hash and isinstance(j.get("m_Hash"), str) and j["m_Hash"].lower() != want_hash:
            out["problems"].append("条目[%d] m_Hash=%s 与文件名里的 %s 不一致"
                                   % (eidx, j.get("m_Hash"), want_hash))
        if not isinstance(j.get("m_BundleSize"), int) or j.get("m_BundleSize", 0) <= 0:
            out["problems"].append("条目[%d] m_BundleSize=%r 不是正整数" % (eidx, j.get("m_BundleSize")))
        if ref is not None:
            diff = compare_extra_style(ref, obj[1][2])
            if diff:
                out["problems"].append("条目[%d] 字段风格与原生参考不同：%s" % (eidx, diff))
    if not found:
        out["problems"].append("catalog 里找不到 %s 的 bundle 条目" % bundle_name)
    out["ok"] = not out["problems"]
    return out


def verify_catalog(catalog_path, ref_style=None, bundle_dir=None, log=None):
    r"""★⑩ **全量自洽校验**：整份 catalog 的引用完整性 + 字段风格普查。

    为什么需要（★⑩ 那次事故）
    ==========================
    把优化版工具写出的 catalog 装进游戏 ⇒ **UI 全没了**（换回原版立刻正常）。
    事后静态检查**查不出问题**：四个数组长度"正好"、索引越界 **全 0**、extra 字段齐全合法。
    而旧自检只验"地址→条目→依赖键→bundle→CRC"**这一条链** ⇒ 整份 catalog 坏没坏它一概不看 ✗

    判据来源（**2026-10-16 在游戏原生 catalog 上实测过**，`_rev_tools\out\probe_catalog_invariants.py`）
    ================================================================================================
    原生 catalog 数据：internalIds 14456 / providerIds 4 / resourceTypes 86 / keys 28939 /
    entries 21276 / buckets 28939 / extra 67832 B。实测结论：
      · `len(keys) == len(buckets)` ✓（28939）
      · 越界（e[0]/e[1]/e[6]/依赖键/extra 偏移）**全 0** ✓
      · **孤儿条目 0**（每个条目至少被一个键的桶引用）✓
      · **重复键 0** ✓
      · ⛔ **`e[5] == 当前键下标` 是 FALSE（30059 处）** —— 一个条目**可以被多个键的桶共享**
        （实测反例：条目[77] 同时出现在键 #77 `3dArrow` 与键 #78 `f46069d0…` 的桶里）
        ⇒ **别把"e[5] 必须等于当前键"写进自检**，那是假判据（第一版探针就在这里误报了 3 万条）✗
      · ⛔ **`m_BundleName == m_Hash` 也是 FALSE（0/78）** —— 所以"m_BundleName 写哈希"只是
        **风格要求**，值的语义**未定**（未验证推测；见返回值里的 `style_note`）
      · 文件名里的 32hex **== m_Hash**（78/78）✓
      · bundle 条目里 `m_Crc != 0`（78/78）、`m_BundleName` 带 `.bundle` 后缀的 **只有 1 个**
        —— 那一个正是**我们自己注册的**（`FLYACV_…bundle`，★⑨ 的现场）✓

    返回 `{"ok", "problems": [...], "stats": {...}, "style_entries": [...], "style_note": str}`
    ⛔ **只读**，不写文件 ✓
    """
    from catalog_mod import Catalog, read_object
    out = {"ok": True, "problems": [], "stats": {}, "style_entries": [], "style_note": ""}
    if not os.path.isfile(catalog_path):
        out["ok"] = False
        out["problems"].append("catalog 不存在：%s" % catalog_path)
        return out
    c = Catalog(catalog_path)
    ids = c.cat.get("m_InternalIds") or []
    provs = c.cat.get("m_ProviderIds") or []
    rts = c.cat.get("m_resourceTypes") or []
    extra_len = len(c.extra or b"")
    st = {"internalIds": len(ids), "providerIds": len(provs), "resourceTypes": len(rts),
          "keys": len(c.keys), "entries": len(c.entries), "buckets": len(c.buckets),
          "extra_bytes": extra_len, "bundles": 0, "orphan_entries": 0, "dup_keys": 0}

    # ---- ① 长度关系 ----
    if len(c.keys) != len(c.buckets):
        out["problems"].append("keys(%d) 与 buckets(%d) 长度不等（原生里必然相等）"
                               % (len(c.keys), len(c.buckets)))

    # ---- ② 全量引用完整性 ----
    referenced = set()
    oor = []
    for ki, k in enumerate(c.keys):
        bk = c.buckets[ki] if ki < len(c.buckets) else None
        if bk is None:
            out["problems"].append("键 #%d 没有对应的桶" % ki)
            continue
        for ei in bk.get("entries") or []:
            if not isinstance(ei, int) or ei >= len(c.entries):
                oor.append("桶 #%d 里的条目下标 %r 越界" % (ki, ei))
                continue
            referenced.add(ei)
            e = c.entries[ei]
            if e[0] >= len(ids):
                oor.append("条目[%d] e[0]=%d 超出 internalIds" % (ei, e[0]))
            if e[1] >= len(provs):
                oor.append("条目[%d] e[1]=%d 超出 providerIds" % (ei, e[1]))
            if e[6] >= len(rts):
                oor.append("条目[%d] e[6]=%d 超出 resourceTypes" % (ei, e[6]))
            if e[2] != 0xFFFFFFFF and e[2] >= len(c.keys):
                oor.append("条目[%d] e[2]=%d 依赖键越界" % (ei, e[2]))
            if e[4] != 0xFFFFFFFF and e[4] >= extra_len:
                oor.append("条目[%d] e[4]=%d 超出 extra(%d)" % (ei, e[4], extra_len))
            if e[5] >= len(c.keys):
                oor.append("条目[%d] e[5]=%d 超出 keys" % (ei, e[5]))
    if oor:
        out["problems"].append("引用越界 %d 处（例：%s）" % (len(oor), "；".join(oor[:3])))

    # ---- ③ 孤儿条目 + 重复键 ----
    orphans = [i for i in range(len(c.entries)) if i not in referenced]
    st["orphan_entries"] = len(orphans)
    if orphans:
        out["problems"].append("**孤儿条目 %d 个**（没被任何键的桶引用，例：%s）"
                               % (len(orphans), orphans[:5]))
    seen, dups = {}, []
    for ki, k in enumerate(c.keys):
        kv = _keyval(k)
        if kv in seen:
            dups.append((kv, seen[kv], ki))
        else:
            seen[kv] = ki
    st["dup_keys"] = len(dups)
    if dups:
        out["problems"].append("**重复键 %d 个**（例：%r 同时是 #%d 与 #%d）"
                               % (len(dups), dups[0][0], dups[0][1], dups[0][2]))

    # ---- ④ bundle 条目：字段风格（★⑨ 的长期防线） ----
    #   ⛔ 判据不能是"必须等于某个值"，也不能是"必须与多数派一致" ——
    #     实测：游戏原生 catalog 78 个 bundle 条目里，**本来就有 1 个例外**
    #     （条目[0] `156861a1…_unitybuiltinshaders`，m_BundleName = 去掉 `_<hash>.bundle` 的前缀，
    #      既不是纯哈希也不带 `.bundle`）⇒ "多数派"判法会**永久红**在游戏自己的条目上，
    #      用户很快就学会无视这个检查 ✗
    #   ⇒ 正确判据 = **按归属分开**：
    #       · 文件就在我们的工作目录里（`bundle_dir`）⇒ **我们的包**，严格按我们的约定查（纯 32hex）
    #       · 不在 ⇒ 游戏自己的包，只做"崩不了"的通用检查（m_Crc≠0 / m_BundleSize>0），风格只统计不判红
    #     `bundle_dir` 没给时退回"多数派普查"，并在 note 里说明精度较低 ✓
    ours = set()
    if bundle_dir and os.path.isdir(bundle_dir):
        try:
            ours = {n for n in os.listdir(bundle_dir) if n.endswith(".bundle")}
        except OSError:
            ours = set()
    styles = {}
    ours_style_problems = []
    ours_bad_entries = []
    for eidx, e in enumerate(c.entries):
        if len(e) < 7 or e[4] == 0xFFFFFFFF or e[0] >= len(ids):
            continue
        iid = ids[e[0]]
        if not iid.endswith(".bundle"):
            continue
        base = os.path.basename(iid)
        try:
            obj, _ = read_object(c.extra, e[4])
        except Exception:                                             # noqa: BLE001
            out["problems"].append("条目[%d] 的 extra 读不出来（偏移 %d）" % (eidx, e[4]))
            continue
        if obj[0] != 7:
            continue
        try:
            j = json.loads(obj[1][2])
        except Exception as ex:                                       # noqa: BLE001
            out["problems"].append("条目[%d] extra 不是合法 JSON：%s" % (eidx, ex))
            continue
        st["bundles"] += 1
        mine = (base in ours) if ours else None
        bn = j.get("m_BundleName")
        pure = isinstance(bn, str) and bool(re.fullmatch(r"[0-9a-fA-F]{32}", bn))
        kind = "32hex" if pure else ("后缀" if isinstance(bn, str) and bn.endswith(".bundle") else "其它")
        styles.setdefault((kind, mine), []).append((eidx, base, bn))
        # 通用检查（原生 78/78 都成立 ⇒ 零误报）
        if j.get("m_Crc") == 0:
            out["problems"].append("条目[%d] %s 的 m_Crc = 0（没算过 CRC ⇒ 游戏会校验失败）" % (eidx, base))
        if not isinstance(j.get("m_BundleSize"), int) or j.get("m_BundleSize", 0) <= 0:
            out["problems"].append("条目[%d] %s 的 m_BundleSize=%r 不是正整数"
                                   % (eidx, base, j.get("m_BundleSize")))
        fh = bundle_hash_of(base)
        if fh and isinstance(j.get("m_Hash"), str) and j["m_Hash"].lower() != fh:
            out["problems"].append("条目[%d] %s 的 m_Hash=%s ≠ 文件名里的 %s（游戏按它找文件）"
                                   % (eidx, base, j.get("m_Hash"), fh))
        # 严格检查：**只对我们的包**（★⑨ 的约定）
        if mine:
            if not pure:
                ours_style_problems.append("条目[%d] %s → m_BundleName=%r 不是纯 32 位哈希（★⑨：不能带 .bundle）"
                                           % (eidx, base, bn))
                ours_bad_entries.append((eidx, base, bn, tuple(j.keys())))
            if ref_style is not None:
                diff = compare_extra_style(ref_style, obj[1][2])
                if diff:
                    ours_style_problems.append("条目[%d] %s 字段风格与原生参考不同：%s" % (eidx, base, diff))
        if bundle_dir:
            p = os.path.join(bundle_dir, base)
            if os.path.isfile(p) and os.path.getsize(p) != j.get("m_BundleSize"):
                out["problems"].append("条目[%d] %s 的 m_BundleSize=%s ≠ 磁盘 %d"
                                       % (eidx, base, j.get("m_BundleSize"), os.path.getsize(p)))
    out["style_entries"] = ours_bad_entries
    if ours_style_problems:
        out["problems"].append(
            "**我们注册的 bundle 条目字段风格不对**（%d 条）：%s"
            % (len(ours_style_problems), "；".join(ours_style_problems[:5])))
    elif not ours:
        # 没给 bundle_dir ⇒ 退回"多数派普查"（精度较低，只当提示）
        native_kinds = {k: v for k, v in styles.items() if k[0] != "32hex"}
        tot = sum(len(v) for v in styles.values())
        out["style_note"] = ("bundle 条目 %d 个，其中 %d 个不是「纯 32hex」风格"
                             "（未给工作目录 ⇒ 无法区分是不是我们的包，仅作提示）"
                             % (tot, sum(len(v) for v in native_kinds.values())))
    if not out["style_note"]:
        tot = sum(len(v) for v in styles.values())
        ours_n = sum(len(v) for (k, m), v in styles.items() if m)
        out["style_note"] = ("bundle 条目 %d 个（我们的 %d 个）字段风格全部合规"
                             % (tot, ours_n))
    out["stats"] = st
    out["ok"] = not out["problems"]
    return out


def ensure_bundle_entry(catalog_path, bundle_path, out=None, in_place=False, log=None):
    r"""★⑪ **只注册 bundle 本身**（bundle 条目 + extra），**不加资产条目、不加地址**。

    为什么单独要有它：★⑪ 的"条目级重指向"只需要我们的小包在 catalog 里**有个 bundle 条目**
    （好让别的资产条目把依赖键指过来），**不需要**新地址 —— 地址键要保持游戏原来那个 ✓
    幂等：已经有这个文件名/哈希的键就直接复用，不重复加 ✓
    返回 `{"bundle_key", "created", "crc", "md5hex", "entry"}`。
    """
    say = _log(log)
    from catalog_mod import Catalog, write_object
    if not os.path.isfile(catalog_path):
        raise FileNotFoundError("catalog 不存在：%s" % catalog_path)
    if not os.path.isfile(bundle_path):
        raise FileNotFoundError("bundle 不存在：%s" % bundle_path)
    bundle_name = os.path.basename(bundle_path)
    c = Catalog(catalog_path)
    ids = c.cat["m_InternalIds"]
    md5hex = bundle_hash_of(bundle_name) or hashlib.md5(open(bundle_path, "rb").read()).hexdigest()
    crc = uncompressed_crc(bundle_path)
    size = os.path.getsize(bundle_path)
    hit = find_key_index(c, bundle_name)
    if hit is not None:
        say("bundle 条目已存在（键 #%d = %s），复用" % (hit, bundle_name))
        return {"bundle_key": hit, "created": False, "crc": crc, "md5hex": md5hex,
                "catalog": catalog_path, "entry": None}
    ref = find_reference_bundle_options(c, log=log)
    jtext = bundle_extra_json(ref[2] if ref else None, md5hex, crc, size)
    buf = bytearray(c.extra or b"")
    off = len(buf)
    write_object(buf, 7, ("Unity.ResourceManager, Version=0.0.0.0, Culture=neutral, PublicKeyToken=null",
                          _BUNDLE_OPTIONS_CLASS, jtext))
    c.extra = bytes(buf)
    b_iid = len(ids)
    ids.append(BUNDLE_KEY_PREFIX + bundle_name)
    b_key = len(c.keys)
    c.keys.append((0, bundle_name))
    c.entries.append((b_iid, 0, 0xFFFFFFFF, 17, off, b_key, 0))
    b_eidx = len(c.entries) - 1
    c.buckets.append({"dataOffset": -1, "entries": [b_eidx]})
    out_path = catalog_path if in_place else (out or catalog_path)
    c.save(out_path)
    say("✓ 新 bundle 条目：键 #%d | 条目[%d] | CRC=%d m_Hash=%s（m_BundleName 写纯哈希）"
        % (b_key, b_eidx, crc, md5hex))
    return {"bundle_key": b_key, "created": True, "crc": crc, "md5hex": md5hex,
            "catalog": out_path, "entry": b_eidx}


def entry_repoint_plan(catalog_path, asset_internal_id):
    r"""★⑪ → `[(键下标, 键值, 桶内位置, 条目下标, 当前依赖键, 当前依赖的 bundle 名), …]`（**只读**）。

    找出"哪些键的桶里挂着这条资产（按 internalId 精确定位）"，以及它现在依赖哪个 bundle。
    ⛔ 判据是 **internalId 精确相等**（不是 `in` / 模糊匹配）—— 肖像路径长得很像，
      模糊匹配会一次改掉一片（实机里 1153 张肖像同前缀）✗
    """
    from catalog_mod import Catalog
    c = Catalog(catalog_path)
    ids = c.cat.get("m_InternalIds") or []
    out = []
    for ki, k in enumerate(c.keys):
        for pos, ei in enumerate((c.buckets[ki].get("entries") or []) if ki < len(c.buckets) else []):
            if ei >= len(c.entries):
                continue
            e = c.entries[ei]
            if e[0] >= len(ids) or ids[e[0]] != asset_internal_id:
                continue
            dep = "（无依赖）"
            dep_name = None
            if e[2] != 0xFFFFFFFF and e[2] < len(c.keys):
                dep = _keyval(c.keys[e[2]])
                for beidx in c.buckets[e[2]]["entries"]:
                    be = c.entries[beidx]
                    if be[0] < len(ids) and ids[be[0]].endswith(".bundle"):
                        dep_name = os.path.basename(ids[be[0]])
                        break
            out.append((ki, _keyval(k), pos, ei, dep, dep_name))
    return out


def repoint_asset_entry(catalog_path, asset_internal_id, bundle_key, only_key=None,
                        out=None, in_place=False, dry_run=False, log=None):
    r"""★⑪ **条目级重指向**：只把"这一条资产"的依赖键指到我们的包，**地址键一个都不动** ✓

    与"整包重指向"（`my_bundle.repoint_bundle`）的区别
    --------------------------------------------------
    整包重指向要**顶替整个游戏包** ⇒ 自建肖像包必须包含全部 **1153 张**肖像
    （实测 304 MB）⇒ 换一张立绘要复制 304 MB，分包时等于重发一份游戏资源 ✗
    条目级只改**一条资产**的依赖 ⇒ 小包里有 1 张图就够（几十 KB）✓

    做法（**不动共享条目**）
    ------------------------
      · 条目是**可被多个键的桶共享**的（★⑩ 实测：原生里 30059 处共享）
        ⇒ ⛔ **绝不能原地改 `e[2]`**（那会连带改掉别的键看到的资产）✗
      · 正确做法：**复制一条新条目** `e' = e` 且 `e'[2] = 我们的 bundle 键`，
        然后**只把目标键的桶里那个下标**换成 `e'` ⇒ 其它键完全不受影响 ✓

    `only_key`：只改这个键（默认改**所有**挂着这条资产的键 —— 那才是"替换这个图标"的语义）
    返回 `{"changed": n, "keys": [...], "dry_run": bool}`
    """
    say = _log(log)
    from catalog_mod import Catalog
    c = Catalog(catalog_path)
    ids = c.cat.get("m_InternalIds") or []
    hits = entry_repoint_plan(catalog_path, asset_internal_id)
    if only_key is not None:
        hits = [h for h in hits if _keyval(h[1]) == only_key or h[0] == only_key]
    if not hits:
        raise ValueError("catalog 里没有任何键挂着这条资产（internalId=%r）—— "
                         "先用「查一下」看看它在不在，或者是不是复制错了路径" % asset_internal_id)
    # 已经指着我们的包就不用改
    done, todo = [], []
    for ki, kv, pos, ei, dep, dep_name in hits:
        e = c.entries[ei]
        if e[2] == bundle_key:
            done.append(kv)
        else:
            todo.append((ki, kv, pos, ei, dep))
    if dry_run or not todo:
        say("（预演）要把 %d 个键的这条资产改指到 bundle 键 #%d：%s"
            % (len(todo), bundle_key, [t[1] for t in todo][:5]))
        return {"changed": 0, "keys": [t[1] for t in todo], "already": done, "dry_run": True}
    for ki, kv, pos, ei, dep in todo:
        e = list(c.entries[ei])
        e[2] = bundle_key                              # ★ 只改这一份副本的依赖
        c.entries.append(tuple(e))
        new_ei = len(c.entries) - 1
        b = c.buckets[ki]
        ents = list(b.get("entries") or [])
        ents[pos] = new_ei                             # ★ 只换这个键的桶里那个下标
        b["entries"] = ents
        say("  · 键 %r：条目[%d]（原依赖 %r）→ 新条目[%d] 依赖我们的包（键 #%d）"
            % (kv, ei, dep, new_ei, bundle_key))
    out_path = catalog_path if in_place else (out or catalog_path)
    c.save(out_path)
    say("✓ 条目级重指向完成：改了 %d 个键（地址键名一个没动 ⇒ 不用改 DB）" % len(todo))
    return {"changed": len(todo), "keys": [t[1] for t in todo], "already": done, "dry_run": False}


def addresses_for_bundle(catalog_path, bundle_name):
    r"""→ [地址, ...]：catalog 里**指向这个 bundle 文件**的所有地址。

    为什么需要（2026-10-15 用户现场）：自检时"地址"那格可能还留着**默认值**（`MyMod/Asset`），
    于是自检报 `✗ catalog 里找不到地址 'MyMod/Asset'` —— 那是**输入框的锅，不是包的锅** ✗
    ⇒ 先按名字反查"本包实际注册了哪些地址"，用它来复核真正的注册链 ✓
    """
    from catalog_mod import Catalog
    c = Catalog(catalog_path)
    ids = c.cat.get("m_InternalIds") or []
    out = []
    for i in range(len(c.keys)):
        addr = _keyval(c.keys[i])
        try:
            entries = c.buckets[i]["entries"]
        except Exception:                                             # noqa: BLE001
            continue
        hit = False
        for eidx in entries:
            e = c.entries[eidx]
            if e[2] == 0xFFFFFFFF:
                continue
            try:
                bents = c.buckets[e[2]]["entries"]
            except Exception:                                         # noqa: BLE001
                continue
            for beidx in bents:
                be = c.entries[beidx]
                iid = ids[be[0]] if be[0] < len(ids) else ""
                if bundle_name and iid.endswith(bundle_name):
                    hit = True
                    break
            if hit:
                break
        if hit:
            out.append(addr)
    return sorted(set(out))


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
