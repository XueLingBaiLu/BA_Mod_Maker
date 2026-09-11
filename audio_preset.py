# -*- coding: utf-8 -*-
"""新增武器音效预设：克隆 WeaponSoundsPreset + WeaponAudioCollection 注入 sfx bundle，
注册 catalog 地址并更新 CRC。配套「把现有预设的 Shot/Impact 指向新事件」的就地改引用。

设计（2026-09，依据 .re-kb/data-structures/broken-arrow-audio-refs.md 与 import_pack 机制）：
- 预设资产在 `aa/PC/sfx_assets_all_<hash>.bundle`（约 85KB，含 622 个预设）；
  容器条目 = 4 项 preload：{WeaponSoundsPreset MonoScript, WeaponAudioCollection MonoScript,
  预设 MB, 集合 MB}，容器 asset = 预设 MB。
- DB 里 Weapons.AudioPreset = catalog 地址（纯名字）；武器开火/命中音效经
  WeaponAudioCollection.Shot/Impact（event:/ 字符串）解析。
- 克隆用 UnityPy typetree 读改写（sfx bundle 带 typetree，字符串长度由 save_typetree 重算）。
- 修改前自动备份 bundle 与 catalog（.bak，只做一次）。
"""
import os
import sys
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))


def _setup_paths():
    """打包版 exe：_rev_tools/_unitypy 在 _internal 下；开发态在工作区根目录。"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        internal = os.path.join(os.path.dirname(sys.executable), "_internal")
        rev = os.path.join(internal, "_rev_tools")
        unity = os.path.join(internal, "_unitypy")
    else:
        rev = os.path.join(HERE, "_rev_tools")
        unity = os.path.join(HERE, "_unitypy")
    for p in (rev, unity):
        if os.path.isdir(p) and p not in sys.path:
            sys.path.insert(0, p)
    return rev, unity


_setup_paths()


def _unitypy():
    import UnityPy
    return UnityPy


def detect_game_dirs():
    """返回 (aa_dir, catalog_path) 或 (None, None)。"""
    roots = [
        "D:/Steam/steamapps/common",
        "C:/Program Files (x86)/Steam/steamapps/common",
        "C:/Program Files/Steam/steamapps/common",
        "E:/Steam/steamapps/common",
        "F:/Steam/steamapps/common",
    ]
    import glob
    for root in roots:
        for g in glob.glob(os.path.join(root, "*")):
            base = os.path.basename(g).lower()
            if not (base.startswith("broken") or base.startswith("arrow")):
                continue
            aa = os.path.join(g, "BrokenArrow_Data", "StreamingAssets", "aa")
            cat = os.path.join(aa, "catalog.json")
            if os.path.isdir(aa) and os.path.isfile(cat):
                return aa, cat
    return None, None


def find_sfx_bundle(aa_dir):
    """sfx_assets_all_*.bundle 路径。"""
    pc = os.path.join(aa_dir, "PC")
    for fn in sorted(os.listdir(pc)):
        if fn.startswith("sfx_assets_all_") and fn.endswith(".bundle"):
            return os.path.join(pc, fn)
    return None


def _backup_once(path):
    bak = path + ".bak"
    if not os.path.exists(bak):
        shutil.copy2(path, bak)
    return bak


def patch_preset_events(address, shot_event=None, impact_event=None,
                        aa_dir=None, progress=None):
    """把现有武器预设的 Shot/Impact 事件名改成新值（等长或更短；更长自动报错）。

    返回 (bundle, 改前值, 改后值)。
    """
    def step(msg):
        if progress:
            progress(msg)
    if aa_dir is None:
        aa_dir, _ = detect_game_dirs()
    bundle = find_sfx_bundle(aa_dir)
    if bundle is None:
        raise FileNotFoundError("找不到 sfx bundle")
    cat = os.path.join(aa_dir, "catalog.json")
    _backup_once(bundle)
    _backup_once(cat)

    import import_pack  # noqa: F401  # 供 stream_save/finalize_crc 同目录导入
    UnityPy = _unitypy()
    step("加载 sfx bundle ...")
    env = UnityPy.load(bundle)
    sf = list(env.objects)[0].assets_file
    target = "Assets/Resources_moved/SFX/Weapons/%s.asset" % address
    try:
        obj = sf.objects[env.container[target].m_PathID]
    except KeyError:
        raise ValueError("预设地址不存在：%s（sfx bundle 容器里没有 %s）" % (address, target))
    preset = obj.read_typetree()
    col_pid = (preset.get("WeaponCollection") or {}).get("m_PathID")
    if not col_pid or col_pid not in sf.objects:
        raise ValueError("预设 %s 没有 WeaponCollection 引用" % address)
    col_obj = sf.objects[col_pid]
    col = col_obj.read_typetree()
    before = (col.get("Shot", ""), col.get("Impact", ""))

    def patch_field(d, field, value):
        if value is None:
            return
        if len(value) > len(d.get(field) or ""):
            raise ValueError(
                "新事件名比原值长，不能就地改（%s: %d > %d）。"
                "请用「新增预设」功能。" % (field, len(value), len(d.get(field) or "")))
        d[field] = value

    patch_field(col, "Shot", shot_event)
    patch_field(col, "Impact", impact_event)
    col_obj.save_typetree(col)
    step("保存 bundle ...")
    from stream_save import ensure_stream_save
    ensure_stream_save()
    env.file.save_stream(bundle, "lz4")
    from finalize_crc import run
    step("更新 CRC ...")
    crc, _ = run(bundle, cat)
    return bundle, before, (col.get("Shot", ""), col.get("Impact", ""))


def create_weapon_sound_preset(address, shot_event, impact_event,
                               template=None, aa_dir=None, progress=None):
    """克隆模板武器预设 → 新预设（含开火集合），注册 catalog 地址 + 更新 CRC。

    address：新预设名（catalog 键 = DB AudioPreset 字段值），如 WEAPON_SOUND_PRESET_MY_GUN。
    template：模板预设名（缺省取第一个非 DLC 预设）。
    返回 (bundle, 新 preset pid, crc, 注册结果)。
    """
    def step(msg):
        if progress:
            progress(msg)
    if aa_dir is None:
        aa_dir, _ = detect_game_dirs()
    bundle = find_sfx_bundle(aa_dir)
    if bundle is None:
        raise FileNotFoundError("找不到 sfx bundle")
    cat = os.path.join(aa_dir, "catalog.json")
    _backup_once(bundle)
    _backup_once(cat)

    import import_pack  # noqa: F401
    from import_pack import _clean_target, _alloc_pids
    UnityPy = _unitypy()
    step("加载 sfx bundle ...")
    env = UnityPy.load(bundle)
    sf = list(env.objects)[0].assets_file

    new_path = "Assets/Resources_moved/SFX/Weapons/%s.asset" % address

    # 模板：容器里第一个 Weapons 预设（可指定）
    tpl_path = None
    if template:
        tpl_path = "Assets/Resources_moved/SFX/Weapons/%s.asset" % template
    else:
        for p in env.container:
            if "/SFX/Weapons/" in p:
                tpl_path = p
                break
    if tpl_path is None or tpl_path not in env.container:
        raise ValueError("找不到模板预设 %s" % (template or "(任意 Weapons 预设)"))
    _tpl_pp = env.container[tpl_path]
    tpl_obj = sf.objects[_tpl_pp.m_PathID]
    tpl = tpl_obj.read_typetree()
    col_pid = (tpl.get("WeaponCollection") or {}).get("m_PathID")
    if not col_pid or col_pid not in sf.objects:
        raise ValueError("模板预设 %s 没有 WeaponCollection" % tpl_path)
    col_tpl = sf.objects[col_pid].read_typetree()

    # 清理同地址旧 mod 对象
    removed, ab, ab_obj = _clean_target(sf, new_path)
    step("清理旧对象 %d 个 ..." % removed)

    pids = _alloc_pids(sf, 2)
    new_col_pid, new_preset_pid = pids[0], pids[1]

    # 集合克隆
    col = dict(col_tpl)
    col["m_Name"] = address + "_Collection"
    col["Shot"] = shot_event
    col["Impact"] = impact_event
    from UnityPy.files.ObjectReader import ObjectReader  # noqa: E402
    r_col = ObjectReader(
        assets_file=sf, reader=sf.reader, path_id=new_col_pid,
        type_id=sf.objects[col_pid].type_id, serialized_type=sf.objects[col_pid].serialized_type,
        class_id=sf.objects[col_pid].class_id, type=sf.objects[col_pid].type,
        byte_start=0, byte_size=0, is_destroyed=False, is_stripped=False)
    r_col.save_typetree(col)
    sf.objects[new_col_pid] = r_col

    # 预设克隆（WeaponCollection 指向新集合）
    preset = dict(tpl)
    preset["m_Name"] = address
    preset["WeaponCollection"] = {"m_FileID": 0, "m_PathID": new_col_pid}
    r_pre = ObjectReader(
        assets_file=sf, reader=sf.reader, path_id=new_preset_pid,
        type_id=tpl_obj.type_id, serialized_type=tpl_obj.serialized_type,
        class_id=tpl_obj.class_id, type=tpl_obj.type,
        byte_start=0, byte_size=0, is_destroyed=False, is_stripped=False)
    r_pre.save_typetree(preset)
    sf.objects[new_preset_pid] = r_pre

    # MonoScript pid（WeaponSoundsPreset / WeaponAudioCollection，与原条目一致）
    from UnityPy.files.ObjectReader import ObjectReader  # noqa: E402
    from UnityPy.classes.PPtr import PPtr  # noqa: E402
    from UnityPy.classes.generated import AssetInfo  # noqa: E402
    ms_preset = ms_col = None
    for pid, o in sf.objects.items():
        if o.type.name != "MonoScript":
            continue
        t = o.read_typetree()
        if t.get("m_ClassName") == "WeaponSoundsPreset":
            ms_preset = pid
        elif t.get("m_ClassName") == "WeaponAudioCollection":
            ms_col = pid
    if ms_preset is None or ms_col is None:
        raise ValueError("sfx bundle 里找不到 MonoScript（WeaponSoundsPreset/WeaponAudioCollection）")

    # preload + 容器条目（复用 _clean_target 返回的实例）
    preload = [ms_preset, ms_col, new_preset_pid, new_col_pid]
    start = len(ab.m_PreloadTable)
    ab.m_PreloadTable.extend([PPtr(m_FileID=0, m_PathID=p, assetsfile=sf) for p in preload])
    ab.m_Container.append((new_path, AssetInfo(
        asset=PPtr(m_FileID=0, m_PathID=new_preset_pid, assetsfile=sf),
        preloadIndex=start, preloadSize=len(preload))))
    ab_obj.save_typetree(ab)

    step("保存 bundle ...")
    from stream_save import ensure_stream_save
    ensure_stream_save()
    env.file.save_stream(bundle, "lz4")

    from finalize_crc import run
    step("更新 CRC ...")
    crc, _ = run(bundle, cat)

    from import_pack import register_address
    step("注册地址 %s ..." % address)
    reg = register_address(cat, address, new_path, ref_substr="SFX/Weapons")
    return bundle, new_preset_pid, crc, reg


if __name__ == "__main__":
    # 自测：用测试目录里的副本跑（不碰游戏文件）
    import tempfile
    aa, cat = detect_game_dirs()
    print("game aa:", aa)
    b = find_sfx_bundle(aa)
    print("sfx bundle:", b)
