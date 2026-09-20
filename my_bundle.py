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
python my_bundle.py --restore-catalog --game-root <游戏目录> [--workdir <工作目录>]
# ★ 默认值安全化（2026-09-20，中枢裁）：`--install`／`--restore-catalog`／`--repoint`／`--sync-crc`
#   四处**都要求显式给 `--game-root`（或对应用 `--catalog`）**：不给 ⇒ **rc=2 拒绝**并打印探测到的
#   游戏根供复制。⛔ **不再静默回退 `find_game_root()`**（那会写用户正在玩的目录）。
#   `--restore-catalog` 现在**接受并透传 `--workdir`**（此前该参数对它无效）⇒ 测试可指向**假根**。
python my_bundle.py --list --workdir D:\mods
python my_bundle.py --self-test          # 全离线自测（用产品自带的小模板）
```
"""
import argparse
import glob
import hashlib
import json
import os
import re
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
def _default_workdir():
    """自建 bundle 的默认工作目录 —— **绝不落 C 盘**（2026-10 修；见 `mod_paths.py`）"""
    try:
        import mod_paths
        return mod_paths.workdir()
    except Exception:                                        # noqa: BLE001
        env = os.environ.get("BAMOD_HOME")
        if env:
            return os.path.join(env, "work")
        for d in ("D:", "E:"):
            if os.path.isdir(d + os.sep):
                return os.path.join(d + os.sep, "BrokenArrow_Mods")
        return os.path.join(os.path.expanduser("~"), "BrokenArrow_Mods")


DEFAULT_WORKDIR = _default_workdir()
# ── ★[体验-03]「记住上次工作目录」（2026-09-20，中枢裁）─────────────────────────
#   与 `ba_db_tool.py`／Blender 插件**共用同一份** `%APPDATA%\BA Mod Maker\settings.json`
#   （⛔ 不新建 .ini／注册表／第二份 json）⇒ 写一律**读—改—写**：
#   ⛔ 不许 `json.dump` 一个只有自己键的新 dict —— 那会**静默抹掉**别人的键
#   （实测现盘既有键：`game_data_dir`／`language`，插件侧另有 `auto_backup`）。
APP_NAME = "BA Mod Maker"
SETTINGS_KEY_WORKDIR = "last_workdir"


def settings_path():
    """共享设置文件（与 `ba_db_tool.py` 同路径同语义）"""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_NAME, "settings.json")


def load_settings():
    """读设置；缺件/半截 JSON 等任何异常 ⇒ `{}`（⇒ 调用方按默认值走）"""
    try:
        with open(settings_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:                                        # noqa: BLE001
        return {}


def save_settings(data):
    """写设置；失败**出声**（磁盘满/权限问题会让用户完全看不出为什么没记住）"""
    try:
        os.makedirs(os.path.dirname(settings_path()), exist_ok=True)
        with open(settings_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:                                   # noqa: BLE001
        print("[设置] 保存失败（%s: %s）—— 本次设置不会被记住" % (type(e).__name__, e))


def remembered_workdir():
    """上次用过的**且现仍存在**的工作目录；没有／已失效 ⇒ `None`（⛔ 不猜、不硬撑）"""
    p = load_settings().get(SETTINGS_KEY_WORKDIR)
    if isinstance(p, str) and p.strip() and os.path.isdir(p):
        return p
    return None


def remember_workdir(path):
    """记住用户选的工作目录（**读—改—写**，⛔ 不整份覆盖）"""
    if not isinstance(path, str) or not path.strip():
        return
    s = load_settings()
    s[SETTINGS_KEY_WORKDIR] = os.path.normpath(path)
    save_settings(s)



# ★[★28续] 2026-09-19：**工具侧"原版 catalog 留存"** —— 用户把游戏侧 `aa\catalog.json.bak_*`
#   清空后（实测现读 0 份），「还原 catalog」就无物可还原了。这里在**工作目录**（用户自己的 mods 目录，
#   Steam 验证与用户清理都不会动它）留一份"**本工具第一次安装之前**"的 catalog：
#   · ⛔ 已存在则**不覆盖**（那才代表"原版那一刻"；否则第二次安装会把上一版已改过的当"原版"）
#   · 还原优先级：游戏侧 `.bak_*`（原逻辑）→ 工具侧留存 → 都没有 ⇒ 如实拒绝（⛔ 不用别的东西顶替）
CATALOG_KEEP_SUBDIR = "_catalog_orig"


def catalog_keep_dir(workdir=None):
    r"""工具侧留存目录（默认 `<工作目录>\_catalog_orig`；测试可注入 `workdir`）。"""
    return os.path.join(workdir or DEFAULT_WORKDIR, CATALOG_KEEP_SUBDIR)


def keep_catalog_orig(src_catalog, workdir=None, log=print):
    """把 `src_catalog` 留一份到工具侧（**只在还没有时**写）。返回落点路径，未写则 None。"""
    import hashlib as _hl
    if not src_catalog or not os.path.isfile(src_catalog):
        return None
    keep = catalog_keep_dir(workdir)
    dst = os.path.join(keep, "catalog.json")
    if os.path.isfile(dst):
        return None                       # ⛔ 不覆盖：留着的那份才是"最早那一刻"
    try:
        os.makedirs(keep, exist_ok=True)
        shutil.copy2(src_catalog, dst)
        h = _hl.sha256(open(dst, "rb").read()).hexdigest()
        meta = {"src": src_catalog, "size": os.path.getsize(dst), "sha256": h,
                "stamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "note": "本工具**首次安装前**那份 catalog 的留存（⛔ 不随后续安装更新）"}
        with open(os.path.join(keep, "catalog.json.meta.json"), "w", encoding="utf-8") as f:
            import json as _js
            f.write(_js.dumps(meta, ensure_ascii=False, indent=1))
        log("✓ 已在工具侧留存原版 catalog → %s（%d B ／ %s…）"
            % (dst, meta["size"], h[:16].upper()))
        return dst
    except Exception as e:                                        # noqa: BLE001
        log("⚠ 工具侧留存原版 catalog 失败（不影响安装）：%s: %s" % (type(e).__name__, e))
        return None


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


def _require_explicit_game_root(explicit, action):
    r"""★ 默认值安全化（2026-09-20 裁）：⛔ CLI **不许静默回退真游戏根**。

    旧行为：不给 `--game-root` ⇒ `find_game_root(None)` 探到 Steam 里那个**真**游戏根
    ⇒ **直接写用户正在玩的目录**（P0：用户可能正在玩）。现在**拒绝**，并把探到的路径
    打印出来供**复制**（⛔ 不靠人记得传参 ⇒ 默认值本身必须是安全的）。

    返回可用的根；拒绝时返回 `None`（调用方据此给非零退出码）。
    """
    if explicit:
        return explicit
    guess = find_game_root(None)
    guess = os.path.normpath(guess) if guess else None      # ★ 打印成**本机分隔符**（便于复制）
    log = print
    log("⛔ %s 需要显式 --game-root：本工具**不静默回退真游戏根**（用户可能正在玩）。" % action)
    if guess:
        log("   探测到的游戏根：%s" % guess)
        log("   确认要写它，请显式加：--game-root \"%s\"" % guess)
    else:
        log("   也没探测到游戏根 ⇒ 请显式加 --game-root \"<...>\\broken_arrow\"")
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

def normalize_bundle(path, log=print):
    r"""把包"过一遍我们自己的保存器"：**内部 CAB 名唯一化** + **清空 `m_Dependencies`**。

    为什么要在**建包时**就做（2026-10-15，`17-软件改进待办` §一.5/§一.6）：
      · 模板包 `units_warehouse_small.bundle` 的内部名沿用了游戏 units 包的
        `CAB-1e53370fe5e57c593d6b445e251b8d1c` ⇒ 自建包**必然撞名** ⇒ 游戏报
        `can't be loaded because another AssetBundle with the same files is already loaded` ✗
        （`[发布-02]` 的根治在 `stream_save`，但那只在**保存时**生效；建包那一刻就已经是撞名状态）
      · 模板的 `m_Dependencies` 里有两条**小写 `cab-…`**，而**游戏 79 个包里一个都没有**
        ⇒ 是从模板抄来的垃圾，顺手清掉更干净 ✓
    ⇒ 建包时跑一次 `UnityPy.load` + `save_stream`，上面两件事由既有机制完成
      （顺带把包改成"我们自己的布局"：blocksInfo 在文件尾 ⇒ 以后能被 O4 字节手术复用压缩块 ✓）

    ⛔ 失败**不影响建包**（退回"只是复制模板"的老行为），只记一行日志。
    """
    try:
        from stream_save import ensure_stream_save
        ensure_stream_save()
        import UnityPy
        env = UnityPy.load(os.path.abspath(path))
        out = path + ".norm"
        env.file.save_stream(out, "lz4")
        del env
        import gc
        gc.collect()
        os.replace(out, path)
        return True
    except Exception as e:                                            # noqa: BLE001
        if log:
            log("⚠ 建包归一化失败（退回「只复制模板」）：%s: %s" % (type(e).__name__, e))
        try:
            if os.path.exists(path + ".norm"):
                os.remove(path + ".norm")
        except OSError:
            pass
        return False


def new_bundle(name, workdir, template=None, log=print, normalize=True, fix=True):
    """复制模板 → `<workdir>/<名字>_<32hex>.bundle`（名字里的 hex 是 Unity Addressables 的包名哈希）

    ★ `normalize=True`（默认）会把新包过一遍 `normalize_bundle()`：
      内部 CAB 名唯一化 + 清空 `m_Dependencies`（详见该函数的说明）。
      ⛔ 这会**改变文件字节**（不再与模板同大小）——这是**有意的**：
        不做的话自建包从出生那一刻就带着"必定撞名的内部名 + 不存在的依赖" ✗
    ★ `fix=True`（默认）顺手**补悬空脚本**（见 `fix_scripts`）：模板自带的 `_TEMPLATE_*` prefab
      引用了 16 个住在游戏主包里的 `m_Script`，不补的话新包天生"脚本悬空"（组件静默失效）✗
      实测 7 秒一次；失败只警告、不影响建包 ✓
    """
    if not name or any(ch in name for ch in '\\/:*?"<>|'):
        raise ValueError("bundle 名字不合法：%r" % name)
    if not template or not os.path.exists(template):
        raise FileNotFoundError("模板 bundle 不存在：%s" % template)
    os.makedirs(workdir, exist_ok=True)
    stage = os.path.join(workdir, name + ".bundle")
    shutil.copy2(template, stage)
    if normalize:
        normalize_bundle(stage, log=log)
    if fix:
        # ★ 2026-10-15（用户报的现场）：模板里的 `_TEMPLATE_*` prefab **本身就引用 16 个
        #   不在包里的 m_Script** ⇒ 不修的话**每个新建的包从出生就是"脚本悬空"** ✗
        #   （组件静默失效、而静态网格照样渲染 ⇒ 极易被当成成功）
        #   实测补一次 7 秒（含加载 3.15 GB 源包）⇒ 直接自动做，不必让用户记命令 ✓
        try:
            r = fix_scripts(stage, log=log)
            if not r["changed"]:
                log("（新建的包本来就没有悬空脚本）")
        except Exception as e:                                        # noqa: BLE001
            log("⚠ 补悬空脚本没成功（不影响建包；之后可在「④ 收尾」点『补悬空脚本』再补）："
                "%s: %s" % (type(e).__name__, e))
    with open(stage, "rb") as _fh:            # ★ 显式关闭（以前靠引用计数回收；句柄来源越少越好）
        hexv = hashlib.md5(_fh.read()).hexdigest()[:32]
    final = os.path.join(workdir, "%s_%s.bundle" % (name, hexv))
    if os.path.exists(final):
        os.remove(final)
    # ★ 收尾替换走既有助手：`gc.collect()` + 有界重试（杀软/编辑器**瞬时**占用也能过）。
    #   实测背景：`fix_scripts` 之后这一步踩过 WinError 32 —— 根因已在该函数里修掉（断名再 gc），
    #   这里再加一道保险，失败时**说清楚**而不是抛裸异常 ✓
    from stream_save import atomic_replace
    if not atomic_replace(stage, final, log=log):
        raise PermissionError("新建的包没法改名成最终文件名（文件被占用）：%s" % final)
    log("✓ 新建：%s（%d 字节，骨架来自 %s%s）"
        % (final, os.path.getsize(final), os.path.basename(template),
           "，已归一化：唯一 CAB 名 + 清空 m_Dependencies" if normalize else ""))
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
        # ★ 2026-10-15：素材包里的 prefab 也会引用 `m_Script`（组件的脚本住在游戏主包里）
        #   ⇒ 合并之后**再补一次**，否则新加进来的组件同样会静默失效 ✗
        #   ⛔ 必须放在下面的 `sync_crc` **之前**：补脚本会让包字节变，CRC 要按最终字节算 ✓
        try:
            r = fix_scripts(bundle, log=log)
            if not r["changed"]:
                log("（合并后包内脚本仍自包含）")
        except Exception as e:                                        # noqa: BLE001
            log("⚠ 合并后补悬空脚本没成功（不影响合并；可在「④ 收尾」点『补悬空脚本』再补）："
                "%s: %s" % (type(e).__name__, e))
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


def remove_asset(bundle, asset_path=None, log=print, all_matching=False, dry_run=False,
                 catalog=None):
    r"""★ 2026-10-15（用户提出）：把**包内已有的容器条目**删掉（"我的 bundle"以前没有任何删除入口）。

    真实诉求（飞行 ACV 会话实测的三条）：
      ① 换骨架命名时（`…/US/ACV/ACV.png` → `…/_TEMPLATE_PORTRAIT.png`）只能靠外部脚本清旧条目；
      ② 加错地址 / 加错图之后**无法撤销**；
      ③ 想给包"瘦身"（149 KB 模板建出来的包自带 9 个 `_TEMPLATE_*` 骨架 prefab）。

    ⛔ **只删容器条目**，三样东西一律不动（这是最安全的做法，实机验证过：容器 12→10 条、包正常、自检通过）：
        · 对象（被删掉的 Texture2D/Sprite 变成**无人引用的孤儿对象**，无害）
        · `m_PreloadTable`（不动它 ⇒ **不会**打乱其它条目的 `preloadIndex`）
        · 其它条目
    ⇒ 反过来说：**想"回收空间"要另外做**（那是重建包的活），这里只解决"条目还在、东西还看得见"的问题。

    `asset_path` 给完整容器路径；`all_matching=True` 时按"末段文件名相同"批量删
    （图标/肖像在容器里常是**两条同名条目**：Texture2D + Sprite ⇒ 默认就得一次删掉两条）。

    返回 {"removed": [(路径, 剩余同名条目数)], "kept": 剩余条目数, "dry_run": bool}

    ★③ 2026-10-16：新增 `catalog` —— 删完**自动同步 CRC**（以前只打一行"务必再点一次「同步 CRC」"，
      而忘了同步的后果是**静默**的：游戏取不到该包资产、还不报错）✗
    """
    _setup_paths()
    import UnityPy
    if not os.path.exists(bundle):
        raise FileNotFoundError("bundle 不存在：%s" % bundle)
    if not asset_path:
        raise ValueError("要删哪个条目？给我容器里的资产路径（用 --list-images / 自检里的清单看）")

    env = UnityPy.load(os.path.abspath(bundle))
    ab_obj = next((o for o in env.objects if o.type.name == "AssetBundle"), None)
    if ab_obj is None:
        raise ValueError("这个 bundle 里没有 AssetBundle 对象 ⇒ 不是能挂资产的那种包")
    d = ab_obj.read()
    cont = list(d.m_Container or [])
    want = str(asset_path).replace("/", "\\")
    want_alt = str(asset_path)

    def _match(name):
        s = str(name)
        if s == want_alt or s == want:
            return True
        if all_matching:
            return os.path.basename(s.replace("\\", "/")) == os.path.basename(want_alt.replace("\\", "/"))
        return False

    doomed = [(i, str(n)) for i, (n, _info) in enumerate(cont) if _match(n)]
    if not doomed:
        raise ValueError("容器里没有 %r 这条（用自检列出的清单核对；图标/肖像通常是两条同名条目）"
                         % asset_path)
    removed = [p for _i, p in doomed]
    kept = [t for i, t in enumerate(cont) if i not in {i for i, _ in doomed}]
    log("要删 %d 条容器条目：%s" % (len(removed), removed[:6]))
    log("   保留 %d 条（对象与 m_PreloadTable **都不动**）" % len(kept))
    if dry_run:
        log("（预演：没写文件）")
        return {"removed": removed, "kept": len(kept), "dry_run": True}

    d.m_Container = kept
    ab_obj.save_typetree(d)
    from stream_save import ensure_stream_save
    ensure_stream_save()
    out = bundle + ".tmp"
    env.file.save_stream(out, "lz4")
    del env
    import gc
    gc.collect()
    # ⛔ **不无条件留备份**：用户明确要求"备份文件不要一大堆"（`backup_policy` 默认关）。
    #    安全性靠 `save_stream` 自己的「写 .tmp → 原子替换」——中途失败原包**不会被截断** ✓
    #    想额外留回滚点就去『文件』菜单把自动备份打开（那时这里会留一份 .bak）。
    try:
        from backup_policy import maybe_backup
        maybe_backup(bundle, log=log, label=os.path.basename(bundle))
    except Exception:                                                 # noqa: BLE001
        pass
    os.replace(out, bundle)
    log("✓ 已删除 %d 条容器条目 → %s" % (len(removed), os.path.basename(bundle)))
    crc = auto_sync_crc(bundle, catalog, log=log, why="删条目后")
    return {"removed": removed, "kept": len(kept), "dry_run": False, "crc": crc}


def missing_scripts(bundle, log=None):
    r"""→ `{"need","missing","by_file","mb","external","unreadable"}`（**只读**）。

    ⛔ **先查再修**：查一遍目标包只要零点几秒，而加载源包（3 GB 主包）要几秒
      ⇒ 大多数情况（包本来就是好的）根本不必碰源包 ✓

    ★ 2026-10-15：改用 `fix_dangling_scripts.script_stats` 做**唯一口径**，
      顺便把 `mb`（MonoBehaviour 个数）/`external`（`m_FileID != 0` 的跨文件引用）/
      `unreadable`（读不出 typetree）一起带出来 —— 这样自检能分清
      "查了 0 条引用" 和 "查了 134 条全过"（以前两者输出一模一样，是假绿的温床）✗
    """
    _setup_paths()
    import UnityPy
    from fix_dangling_scripts import script_stats
    from stream_save import release_env
    env = UnityPy.load(os.path.abspath(bundle))
    by_file, miss = {}, []
    agg = {"mb": 0, "external": 0, "unreadable": 0, "internal": 0}
    for name, f in (getattr(env.file, "files", None) or {}).items():
        sf = f if hasattr(f, "objects") else None
        if sf is None or not getattr(sf, "objects", None):
            continue
        st = script_stats(sf)
        for k in agg:
            agg[k] += st[k]
        by_file[name] = {"need": len(st["need"]), "missing": st["missing"],
                         "mb": st["mb"], "external": st["external"],
                         "unreadable": st["unreadable"]}
        miss += st["missing"]
    # ⛔ 必须**确定性**放句柄：这里查的常常就是"下一步要 os.replace 替换"的那个文件，
    #   只 `del env` 的话句柄要等下一次 gc 才关 ⇒ 后面 replace 报 WinError 32 ✗
    #   （本次实测踩到，见 `stream_save.release_env` 的说明）
    release_env(env)
    return {"need": sum(v["need"] for v in by_file.values()), "missing": miss,
            "by_file": by_file, "mb": agg["mb"], "external": agg["external"],
            "unreadable": agg["unreadable"], "internal": agg["internal"]}


def fix_scripts(bundle, source=None, catalog=None, log=print):
    r"""★ **补悬空脚本**：把 MonoBehaviour 引用、但包内没有的 `MonoScript` 从源包复制进来。

    为什么这是"根治"而不是"提示"（2026-10-15 用户报的现场）
    ======================================================
    自建 bundle 是从 149 KB 模板建出来的，而模板里的 `_TEMPLATE_*` prefab **本身就引用了
    16 个不在包里的 `m_Script`**（它们住在游戏的 units 主包里）⇒ **每一个自建包从出生那一刻
    就是"脚本悬空"的** ✗。后果：游戏里刷 `The referenced script … is missing!`、编辑器抛
    `No UnitPrefabRoot script found`，**而静态网格照样渲染** ⇒ 极易被当成"成功"，
    但 `AnimationHub`（旋翼自转）、`DriverMarker`、`ContainerSeatInitializer` 这些**组件全部静默失效**。
    实测（用户那个 26 MB 的 `acvfly_*` 包）：缺 16 个，复制后 **16/16**，复制进来的类名是
    `AnimationHub` / `AnimationManager` / `AnimationManagerBridge` / `ContainerSeatInitializer` /
    `DecalProjector` / `DriverMarker` …（正是模型能用起来必需的那几个）✓

    做法（**保持原 pathID**，因为 `m_FileID=0` 的引用靠 pathID 解析）
    ---------------------------------------------------------------
      ① 先查（`missing_scripts`，不碰源包）；
      ② 有缺的才加载源包（默认用 `bundle_paths.units_bundle()` **自动找游戏主包**）；
      ③ `fix_dangling_scripts.copy_referenced_monoscripts` 复制；
      ④ 写 `.tmp` → **重新解析自证"缺 0"** → 才原子替换（自证不过就删 tmp、原包一字不动）；
      ⑤ 给了 `catalog` 就顺手**同步 CRC**（包字节变了，不重算 catalog 里还是旧 `m_Crc` ⇒ 游戏静默取不到）。

    实测耗时：**7 秒**（含加载 3.15 GB 源包）⇒ 完全可以自动跑，不必让用户记命令 ✓
    返回 `{"need", "missing_before", "missing_after", "source", "changed", "crc"}`。
    """
    _setup_paths()
    if not os.path.exists(bundle):
        raise FileNotFoundError("bundle 不存在：%s" % bundle)
    before = missing_scripts(bundle)
    if not before["missing"]:
        if log:
            log("✓ 包内脚本自包含（查了 %d 个 MonoBehaviour 的 %d 条引用，全在包里）—— 不用修"
                % (before["mb"], before["need"]))
        return {"need": before["need"], "missing_before": 0, "missing_after": 0,
                "source": None, "changed": False, "crc": None,
                "mb": before["mb"], "external": before["external"],
                "unreadable": before["unreadable"]}

    src = source
    if not src:
        try:
            from bundle_paths import units_bundle
            src = units_bundle()
        except Exception as e:                                        # noqa: BLE001
            if log:
                log("⚠ 自动找源包失败（%s）" % e)
    if not src or not os.path.isfile(src):
        raise FileNotFoundError(
            "缺 %d 个脚本，但**找不到源包** —— 源包 = 这些 prefab 原来所在的包（通常是游戏 units 主包）。\n"
            "  ⛔ 不给源包就修不了（那几个 MonoScript 只住在源包里）\n"
            "  · 游戏装了的话会自动找到；没找到就显式给一个：--source <路径>"
            % len(before["missing"]))
    if log:
        log("发现 %d 个悬空脚本 ⇒ 从源包复制（保持原 pathID）：%s"
            % (len(before["missing"]), os.path.basename(src)))

    import UnityPy
    from fix_dangling_scripts import copy_referenced_monoscripts
    from stream_save import ensure_stream_save, release_env, atomic_replace
    env = UnityPy.load(os.path.abspath(bundle))
    senv = UnityPy.load(os.path.abspath(src))
    ssf = next(f for f in senv.file.files.values() if hasattr(f, "objects"))
    copied = 0
    try:
        for name, f in (getattr(env.file, "files", None) or {}).items():
            sf = f if hasattr(f, "objects") else None
            if sf is None or not getattr(sf, "objects", None):
                continue
            n, _still = copy_referenced_monoscripts(sf, ssf, log=log)
            copied += n
        ensure_stream_save()
        tmp = bundle + ".fixscripts.tmp"
        env.file.save_stream(tmp, "lz4")
    finally:
        # ⛔ 用 `release_env` 而不是 `del env`：确定性关句柄，否则后面 os.replace
        #   报 WinError 32（**实测踩到**，见 `stream_save.release_env` 的说明）
        release_env(env)
        release_env(senv)
        # ★★ 只 `release_env` **还不够**（2026-09-16 实测：`new_bundle` 收尾仍然报 WinError 32）：
        #   此时 `env`/`senv`/`ssf` 这些**局部名还活着** ⇒ `release_env` 里那次 `gc.collect()`
        #   **根本收不掉** reader 的引用环；等函数返回、局部名消失，环还在 ⇒ 只能等"下一次碰巧发生的 gc"。
        #   实测（`_rev_tools\out\tmp\diag_holder.py`，工具输出不是推断）：
        #     失败瞬间 gc 里**仍有 4 个**对象攥着这个包
        #     （`UnityPy…EndianBinaryReader_Streamable` / `fsspec…LocalFileOpener` /
        #       `_io.BufferedReader` / `_io.FileIO`）；`gc.collect()` 之后变 **0** ⇒ 就是引用环 ✓
        #   对照实验：失败后**只等 2s / 空转 3s**（不 gc）仍然失败；**只 gc**（0.03s）立刻成功
        #     ⇒ 不是杀软/索引器那种"瞬时外部占用"，就是**本进程**句柄 ✗
        #   ⇒ 先**断名**、再 collect（顺序不能反），句柄才是确定性释放的 ✓
        env = senv = ssf = None
        import gc
        gc.collect()

    # ④ 写完**必须重新解析自证**（沿用 patch_multi(verify_parse=True) 那套纪律）✗不证不替换
    after = missing_scripts(tmp)                 # 内部已确定性放句柄 ✓
    if after["missing"]:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise RuntimeError("修完复查仍有 %d 个悬空脚本 ⇒ 已放弃、原包一字未动：%s"
                           % (len(after["missing"]), after["missing"][:5]))
    if log:
        log("✓ 复查通过：%d 个 MonoBehaviour 的 %d 条引用全部自包含（复制了 %d 个 MonoScript）"
            % (after["mb"], after["need"], copied))
    try:
        from backup_policy import maybe_backup
        maybe_backup(bundle, log=log, label=os.path.basename(bundle))
    except Exception:                                                 # noqa: BLE001
        pass
    if not atomic_replace(tmp, bundle, log=log):
        # ⛔ 不退回"原地覆盖"：那会把用户的包截断成半截 ✗
        #   修好的包是**完整且已验证**的 ⇒ 留在 .tmp 里，原包一字未动，让用户自己决定 ✓
        raise PermissionError(
            "目标 bundle 被别的程序占用，没法原子替换 ⇒ **原包一字未动** ✓\n"
            "  · 修好的包已经是完整、已验证的：%s\n"
            "  · 关掉占用它的程序（游戏 / 编辑器 / 资源管理器预览）后，"
            "把这个 .tmp 改名覆盖过去，或者再点一次「🔧 补悬空脚本」即可" % tmp)
    crc = None
    if catalog and os.path.isfile(catalog):
        # ⛔ 包字节变了 ⇒ catalog 里的 m_Crc 必须重算，否则游戏**静默取不到**这个包的资产 ✗
        crc = sync_crc(bundle, catalog, log=log)
    if log:
        log("✅ 已补齐悬空脚本并就地替换：%s" % os.path.basename(bundle))
    return {"need": after["need"], "missing_before": len(before["missing"]),
            "missing_after": 0, "source": src, "changed": True, "crc": crc,
            "mb": after["mb"], "external": after["external"], "copied": copied,
            "unreadable": after["unreadable"]}


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
    dirty = False
    for eidx in c.buckets[hit]["entries"]:
        e = c.entries[eidx]
        if e[4] == 0xFFFFFFFF or not c.extra or e[4] >= len(c.extra):
            continue
        obj, old_end = read_object(c.extra, e[4])
        if obj[0] != 7:
            continue
        jt = obj[1][2]
        new_jt = re.sub(r'"m_Crc"\s*:\s*\d+', '"m_Crc":%d' % crc, jt)
        new_jt = re.sub(r'"m_BundleSize"\s*:\s*\d+', '"m_BundleSize":%d' % os.path.getsize(bundle), new_jt)
        new_jt = re.sub(r'"m_Hash"\s*:\s*"[0-9a-fA-F]*"', '"m_Hash":"%s"' % md5hex, new_jt)
        # ★ 幂等（2026-09-17 修）：值没变 ⇒ **一个字都不写**。
        #   以前这里无论如何都会往下走 ⇒ 每次都往 extra 末尾**追加**一份新对象 ⇒ 连点「同步 CRC」副本持续膨胀。
        if new_jt == jt:
            continue
        # 把新对象**序列化到临时缓冲**里量长度，再跟**旧对象的真实字节数**（`old_end - e[4]`）比。
        #   ⛔ 以前是拿「整个对象的长度」去比「JSON 的 UTF-8 长度 + 4」——量纲不同 ⇒ 判据恒 False
        #   ⇒ 那条「等长就地覆盖」分支**从来没进过**（每次必走追加）。
        probe = bytearray()
        write_object(probe, 7, (obj[1][0], obj[1][1], new_jt))
        probe = bytes(probe)
        if len(probe) == old_end - e[4]:
            # 等长 ⇒ 在 e[4] 处**原地覆盖**这一段（不增长、不产生孤儿）
            c.extra = c.extra[:e[4]] + probe + c.extra[old_end:]
        else:
            # 长度变了（数字位数变了）⇒ 追加一份新 extra 并让条目指向它（旧的那份留成孤儿；有意的最省事做法）
            off = len(c.extra)
            c.extra = c.extra + probe
            e = list(c.entries[eidx])
            e[4] = off
            c.entries[eidx] = tuple(e)
        dirty = True
        changed += 1
    if dirty:
        c.save(catalog)
    log("✓ CRC 同步：%s → m_Crc=%d（0x%08X）m_BundleSize=%d m_Hash=%s（改了 %d 条%s）"
        % (name, crc, crc, os.path.getsize(bundle), md5hex, changed,
           "" if dirty else "，值已是最新 ⇒ 未写盘"))
    return crc


def workdir_catalog(bundle):
    r"""约定：自建包和它的 catalog 副本**放在同一个目录**（`catalog_with_mybundle.json`）。

    ⛔ 绝不直接写游戏目录 —— 所有改动都落在工作目录的副本上，最后「安装到游戏」才拷过去 ✓
    """
    return os.path.join(os.path.dirname(os.path.abspath(bundle)), "catalog_with_mybundle.json")


def auto_sync_crc(bundle, catalog, log=print, why=""):
    r"""★③ 改了包之后**自动**同步 CRC（把"务必再点一次同步 CRC"这条提醒变成自动执行）。

    为什么必须自动（T 清单 §一.3 + ★③）
    ====================================
    忘了同步的后果是**静默**的：包里的字节变了、catalog 里还是旧 `m_Crc`
    ⇒ 游戏**取不到**这个包的资产（表现成"图标/立绘/模型不出来"，而**没有任何报错**）✗
    v1.8.117 已自动的四条路：新建 / 加资产 / 加图片 / 补悬空脚本。
    ★③ 补上**还能漏的两条**：③d 删除包内条目、③c 就地替换图标（不重指向时）。

    设计取舍：**没 catalog 副本时不报错、不打断**，只说明一句
    —— 包本身是好的，只是还没注册过（没注册过就没有 CRC 可同步）✓
    返回新 CRC；没同步成返回 None。
    """
    if not catalog or not os.path.isfile(catalog):
        if log:
            log("（%s包已改动；没有 catalog 副本 ⇒ 没有 CRC 要同步 —— 注册一次地址后就会自动同步）"
                % (why or ""))
        return None
    try:
        crc = sync_crc(bundle, catalog, log=log)
        if log:
            log("✓ 已自动同步 CRC（%s不用再手点「同步 CRC」了）" % (why or ""))
        return crc
    except Exception as e:                                            # noqa: BLE001
        if log:
            log("⚠ 自动同步 CRC 失败（%s: %s）⇒ 请手动点一次「同步 CRC」，"
                "否则游戏里这个包的资产会静默取不到" % (type(e).__name__, e))
        return None


# ------------------------------------------------- ★②/★⑪ 肖像·图标原键替换

def repoint_entry_to_bundle(bundle, catalog, asset_internal_id, log=print, dry_run=False,
                            only_key=None):
    r"""★⑪ **条目级重指向**：把 catalog 里"这一条资产"改指到**我的小包**，地址键一个都不动。

    为什么这条很重要（T 清单 §一.2 + ★⑪）
    ======================================
    ① **肖像/图标的"新地址"不会被游戏消费**（实机：`Units.PortraitFileName` 指向新地址 ⇒
       立绘**回退显示成步兵立绘**）⇒ 肖像类只能走"**原键不变**"这条路 ✓
    ② 而原键替换的**老做法是整包重指向** ⇒ 自建肖像包必须顶替整个游戏肖像包
       ⇒ 必须含全部 **1153 张**（实测 304 MB）⇒ 换一张立绘要复制 304 MB ✗
    ③ 条目级只改**一条资产的依赖键** ⇒ 小包里只要有那张图（几十 KB）就够 ✓

    前提：`我的小包` 里**已经有** `asset_internal_id` 这条容器路径
    （用 ③b「把图片加进这个 bundle」并把"bundle 内路径"填成**游戏里那条原始路径**即可）✓
    """
    _setup_paths()
    import my_bundle_catalog as MC
    if not os.path.isfile(catalog):
        raise FileNotFoundError("catalog 不存在：%s" % catalog)
    cat = MC.Catalog(catalog)
    # 我们的小包必须先有 bundle 条目（只有条目，不加地址）
    be = MC.ensure_bundle_entry(catalog, bundle, in_place=True, log=log)
    plan = MC.entry_repoint_plan(catalog, asset_internal_id)
    if not plan:
        raise ValueError("catalog 里没有任何键挂着 %r —— 先用「查一下这条资产」核对路径"
                         "（要用**游戏里那条原始容器路径**，不是随便起的名字）" % asset_internal_id)
    if log:
        log("这条资产在 catalog 里被 %d 个键挂着，现在依赖：%s"
            % (len(plan), sorted({(p[5] or "（无依赖）") for p in plan})))
        for ki, kv, _pos, _ei, dep, dep_name in plan[:5]:
            log("   · 键 #%d %r → 依赖 %r（%s）" % (ki, kv, dep, dep_name or "无"))
    r = MC.repoint_asset_entry(catalog, asset_internal_id, be["bundle_key"], only_key=only_key,
                               in_place=True, dry_run=dry_run, log=log)
    if not dry_run:
        auto_sync_crc(bundle, catalog, log=log, why="条目级重指向后")
        if log:
            log("✓ 完成：地址键名一个没动 ⇒ **不用改 DB**；点「安装到游戏」把 catalog 和小包装上去即可")
    return r


def list_image_assets_in_bundle(path, log=None):
    """列出某个包里可当图片骨架/目标的容器条目（按名字排序）。"""
    _setup_paths()
    import UnityPy
    from stream_save import release_env
    env = UnityPy.load(os.path.abspath(path))
    names = []
    for o in env.objects:
        if o.type.name == "AssetBundle":
            for nm, _info in (o.read().m_Container or []):
                names.append(str(nm))
            break
    release_env(env)
    return sorted(set(names))


def one_click_replace_original_key(png, game_pack, asset_path, workdir, log=print,
                                   new_name=None, catalog=None):
    r"""★② **一键"原键替换"**：把用户原来手工做的 3 步合成一步 ✓

    老流程（教学里记的就是这 3 步，容易漏第 3 步 ⇒ 立绘不变）：
      ① 复制原包（肖像包 304 MB）到工作目录
      ② 就地换图（`replace_image`）
      ③ 把原包键**重指向**我的副本（`repoint_bundle`）—— ⛔ 漏了这步的话：图换了、
         **游戏还是读原包** ⇒ "改了没反应" ✗

    本函数按 ①②③ 顺序自动做，并**自动同步 CRC**（★③）。
    返回 `{"bundle","repointed","crc"}`。
    """
    _setup_paths()
    if not png or not os.path.isfile(png):
        raise FileNotFoundError("找不到你的 PNG：%s" % png)
    if not game_pack or not os.path.isfile(game_pack):
        raise FileNotFoundError("找不到要替换的**游戏原包**：%s" % game_pack)
    os.makedirs(workdir, exist_ok=True)
    base = new_name or ("My" + os.path.splitext(os.path.basename(game_pack))[0])
    hexes = re.findall(r"[0-9a-fA-F]{32}", os.path.basename(game_pack))
    # ⛔ 目录名里**已经带** 32hex（`IconTest_b20509ab….bundle`）⇒ 先把它去掉再加，
    #   否则会拼出 `MyIconTest_b20509ab…_b20509ab….bundle` 这种双哈希丑名字 ✗
    base = re.sub(r"_[0-9a-fA-F]{32}$", "", base)
    suffix = hexes[-1].lower() if hexes else hashlib.md5(
        open(game_pack, "rb").read(1 << 20)).hexdigest()
    dst = os.path.join(workdir, "%s_%s.bundle" % (base, suffix))
    if log:
        log("① 复制原包（%.1f MB）→ %s" % (os.path.getsize(game_pack) / 1048576.0,
                                          os.path.basename(dst)))
    shutil.copy2(game_pack, dst)
    # ⛔ 副本必须做**内部 CAB 名唯一化**：沿用原包的内部名 ⇒ 游戏报
    #   `The AssetBundle '…' can't be loaded because another AssetBundle with the same files
    #    is already loaded.`（[发布-02] 实机撞到过）✗
    normalize_bundle(dst, log=log)
    if log:
        log("② 就地换图：%s → %s" % (os.path.basename(png), asset_path))
    replace_image(dst, png, asset_path, log=log)
    if not catalog or not os.path.isfile(catalog):
        if log:
            log("⚠ 没有 catalog ⇒ 只做到「换了图的副本」，第③步（重指向）跳过了。"
                "请在「加资产/加图片」里注册一次地址生成 catalog 副本后再来")
        return {"bundle": dst, "repointed": False, "crc": None}
    if log:
        log("③ 把原包键重指向我的副本：%s" % os.path.basename(game_pack))
    r = repoint_bundle(catalog, os.path.basename(game_pack), dst, catalog, log=log)
    crc = auto_sync_crc(dst, catalog, log=log, why="原键替换后")
    if log:
        log("✅ 一键原键替换完成：地址键没变 ⇒ **不用改 DB**；点「安装到游戏」即可")
    return {"bundle": dst, "repointed": True, "crc": crc, "repoint": r}


def cab_selfcheck(bundle, before=None, pc_dir=None, log=print, is_ours=None):
    r"""★ B 包（2026-10-16）：**改包后自检** —— 内部名/大小 + "谁依赖我动的那个 CAB"。

    为什么在产品里（深挖第 183 轮实测）：UnityFS 内部名（`CAB-<32hex>`）**会被别的包引用**
    （实测 `vfx_assets_all` 的内部 CAB 名被 **31 个包**引用）⇒ 你重建/改名了那个包 ⇒
    **那些包一起断**，而游戏里往往**不报错**、只是内容不见 ✗
    判据来自 `技术资料\scripts\cab_name_census.py --compare-nodes`（深挖已验证），
    ⛔ 那个脚本不进交付件 ⇒ **判据在本产品里自带实现**（`_rev_tools\cab_check.py`，
    节点表用 `inplace_surgery.read_layout`：纯 struct + lz4，**不加载整包**）✓

    `pc_dir` 不给就自动找游戏 `aa\PC`（找不到就跳过"谁引用"那半，并说明 ✓）。
    """
    _setup_paths()
    import cab_check as CC
    if not pc_dir:
        try:
            gr = find_game_root()
            pc_dir = game_paths(gr)["pc"] if gr else None
        except Exception:                                             # noqa: BLE001
            pc_dir = None
    if log and not pc_dir:
        log("（没找到游戏 aa\\PC 目录 ⇒ 只做内部名/大小对比，「谁引用我动的 CAB」那半跳过）")
    r = CC.post_mod_check(bundle, before=before, pc_dir=pc_dir, is_ours=is_ours, log=log)
    if log:
        log("⇒ 改包后自检：%s" % ("✓ 通过" if r["ok"] else "✗ 有问题（看上面）"))
    return r


# ------------------------------------------------------------------ ⑤ 安装 / 还原

def install(bundle, catalog, game_root, keep_backup=True, log=print, autofix=True):
    """把"我的 bundle"装进游戏：bundle → aa\\PC\\，catalog → aa\\catalog.json（先备份 .bak_<时间>）

    ★ 2026-10-15：装之前**先补悬空脚本**（`autofix=True`）——
      这是最后一道闸：装进去了才发现组件静默失效，代价比在这里多花 7 秒大得多 ✓
      补完会**同步 CRC 到要装的那份 catalog**，再一起拷进游戏（顺序不能反：包变了 CRC 就得重算）。

    ★ 2026-10-15（用户反馈「不希望备份文件一大堆，**最多五个**」）：
      每装一次就留一份 `catalog.json.bak_<时间戳>` ⇒ 用几次就攒一堆。
      现在**装完立刻滚动保留最近 5 份**（旧的按 mtime 删），实现见
      `backup_policy.rotate_backups()`（⛔ 只认我们自己生成的时间戳格式，用户手动改名的备份不碰）。
    """
    if autofix:
        try:
            fix_scripts(bundle, catalog=catalog, log=log)
        except Exception as e:                                        # noqa: BLE001
            log("⚠ 安装前补悬空脚本没成功（继续安装；装进去后组件可能静默失效）：%s: %s"
                % (type(e).__name__, e))
    gp = game_paths(game_root)
    os.makedirs(gp["pc"], exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dst_bundle = os.path.join(gp["pc"], os.path.basename(bundle))
    if keep_backup and os.path.exists(gp["catalog"]):
        shutil.copy2(gp["catalog"], gp["catalog"] + ".bak_" + stamp)
        log("已备份 catalog → %s" % (os.path.basename(gp["catalog"]) + ".bak_" + stamp))
        try:
            from backup_policy import rotate_backups
            rotate_backups(gp["catalog"], log=log)
        except Exception as e:                                       # noqa: BLE001
            log("⚠ 滚动清理旧备份失败（不影响安装）：%s: %s" % (type(e).__name__, e))
    keep_catalog_orig(gp["catalog"], log=log)     # ★[★28续]：装之前先留一份"原版那一刻"
    shutil.copy2(bundle, dst_bundle)
    shutil.copy2(catalog, gp["catalog"])
    log("✓ 已安装：%s → %s" % (os.path.basename(bundle), gp["pc"]))
    log("✓ 已安装：%s → catalog.json" % os.path.basename(catalog))
    return dst_bundle


def restore_catalog(game_root, log=print, workdir=None):
    r"""最近一次备份还原 catalog（bundle 文件不动，避免误删）

    ★ 「不透传」时的行为（2026-09-20 写明，中枢裁 (丙)）：
      · **写目标**永远是 `game_root` 指向的那份 `aa\catalog.json`（⛔ 与 `workdir` 无关）；
      · `workdir=None`（不给）⇒ 工具侧留存目录取 **`DEFAULT_WORKDIR\_catalog_orig`**
        （即"没透传"时用的是**用户默认工作目录**里的留存，⛔ 不是别的东西顶替）；
      · ★ CLI 侧已**显式要求** `--game-root` 并**透传** `--workdir` ⇒ ⛔ 不存在"看着没变、其实落了真根"。

    ⛔ 排序用 **mtime**（不是文件名）：滚动保留会把旧的删掉、用户也可能手工复制过备份，
       文件名里的时间戳与"真实的新旧"可能不一致。
    """
    gp = game_paths(game_root)
    baks = [p for p in glob.glob(gp["catalog"] + ".bak_*") if os.path.isfile(p)]
    if not baks:
        # ★[★28续] 2026-09-19：**先试工具侧留存**（`<工作目录>\_catalog_orig\catalog.json`）
        #   —— 用户把游戏侧 `.bak_*` 清空后，这是唯一还能"自动还原"的源 ✓
        _kp = os.path.join(catalog_keep_dir(workdir), "catalog.json")
        if os.path.isfile(_kp):
            try:
                shutil.copy2(_kp, gp["catalog"])
                _sz = os.path.getsize(_kp)
                _h = hashlib.sha256(open(_kp, "rb").read()).hexdigest()[:16].upper()
                _meta = os.path.join(catalog_keep_dir(workdir), "catalog.json.meta.json")
                _when = ""
                try:
                    import json as _js
                    _m = _js.load(open(_meta, encoding="utf-8"))
                    _when = "（留存于 %s，取自 %s）" % (_m.get("stamp", "?"), _m.get("src", "?"))
                except Exception:                                     # noqa: BLE001
                    pass
                log("✓ 已还原 catalog（游戏侧无备份 ⇒ 用**工具侧留存**那份：%s，%d B ／ %s…）%s"
                    % (_kp, _sz, _h, _when))
                return _kp
            except Exception as e:                                    # noqa: BLE001
                log("⚠ 工具侧留存那份还原失败（%s: %s）⇒ 继续走下面的拒绝分支"
                    % (type(e).__name__, e))
        # ★ [★28] 2026-09-18：用户已把游戏侧 `catalog.json.bak_*` **清成 0 份** ⇒ 这个按钮不能再"假装能还原"。
        #   ⛔ 不抛裸异常（GUI 里只会看到一行 FileNotFoundError、且没有任何退路）；改成**明确拒绝 ＋ 退路指引**，
        #   并返回 None 让调用方把「没做」与「做了」分开（CLI 侧据此给非零退出）。
        log("✗ **找不到可还原的备份**（%s 下没有 `catalog.json.bak_*`）⇒ **本次什么都没做**。"
            % os.path.dirname(gp["catalog"]))
        log("  退路①（最省事）：Steam → 该游戏「属性 → 已安装文件 → 验证游戏文件完整性」"
            "会把 catalog 还原成原版（实测有效）；")
        log("  退路②：以后**自己留副本** —— 装之前先把 `aa/catalog.json` 复制一份到别处"
            "（或保持「安装到游戏（先备份）」的勾选）；")
        log("  ⓘ 另：本工具**安装时**会把一份留在 `%s`；那里也没有 ⇒ 只能走上面两条退路。"
            % catalog_keep_dir(workdir))
        log("  ⛔ 本按钮**不会**用别的东西顶替、也不会假装还原成功。")
        return None
    try:
        baks.sort(key=lambda p: (os.stat(p).st_mtime, p))
    except OSError:
        baks.sort()
    shutil.copy2(baks[-1], gp["catalog"])
    log("✓ 已还原 catalog（来自 %s）" % os.path.basename(baks[-1]))
    return baks[-1]


# ------------------------------------------------------------------ ⑥ 自检

def container_asset_mismatch(env):
    r"""→ [(容器路径, pathID)]：**容器自称有、包内其实没有**的条目。

    实机症状：`Addressable prefab 'X' was not loaded!`（**Unity 侧不报错**）。
        ⛔ 两种 `not loaded` 要分开（飞行 ACV 会话 §一.1）：
          · **有** Unity 报错（`can't be loaded because another AssetBundle with the same files…`）
            ⇒ 内部 CAB 名撞车（`[发布-02]`，已根治）
          · **没有** Unity 报错 ⇒ 就是这个函数查的东西（容器条目指向不存在的资产）

    ★ 抽成独立函数是为了**能被负向对照测到**：`selfcheck()` 里原来内联，
      而"造一条坏容器条目"很难在真包里稳定复现（`save_typetree` 往返后
      容器 AssetInfo 会被 UnityPy 重建 ⇒ 改了不生效）⇒ 单元级拿**假 env** 测更可靠 ✓
    """
    out = []
    try:
        cont = {}
        for _fname, _f in (getattr(getattr(env, "file", None), "files", None) or {}).items():
            cont.update(getattr(_f, "container", None) or {})
        by_pid = {}
        for _o in env.objects:
            by_pid[_o.path_id] = _o
        for key, info in cont.items():
            try:
                asset = getattr(info, "asset", None)
                pid_ = getattr(asset, "m_PathID", None)
            except Exception:                                         # noqa: BLE001
                pid_ = None
            if pid_ and pid_ not in by_pid:
                out.append((str(key), pid_))
    except Exception:                                                 # noqa: BLE001
        return out
    return out


def selfcheck(bundle, catalog=None, address=None, log=print):
    """重开 bundle（UnityPy）列容器条目 + 复算 CRC + （有 catalog/address 时）走一遍注册链"""
    rev = _setup_paths()
    ok = True
    import UnityPy
    env = UnityPy.load(os.path.abspath(bundle))
    names = []
    for o in env.objects:
        try:
            # ⛔ `ObjectReader.container` 是**属性**，返回的是"这个对象在容器里的路径"——
            #   单个字符串（不是列表）⇒ 直接 `for c in ...` 会把字符串**按字符拆开** ✗
            #   （第 74 轮暴露：`names` 变成 ['.', '/', 'A', …] ⇒ 后面的"容器里有 X"判据假红）
            c = getattr(o, "container", None)
            if isinstance(c, str):
                names.append(c)
            elif c:
                names.extend(c)
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

    # ══════════════════════════════════════════════════════════════════════════
    # ★ 2026-10-15 新增（`17-软件改进待办` T 批 ③）：自检以前只走
    #   「地址 → 条目 → 依赖键 → bundle → CRC」，**看不见**下面两类"静默失效"：
    #     · **包内脚本悬空**：MonoBehaviour 引用的 `m_Script` 不在本包内 ⇒
    #       游戏刷 `The referenced script … is missing!`、编辑器抛 `No UnitPrefabRoot script found`，
    #       而**静态网格照样渲染** ⇒ 极易被当成"成功"✗（`[发布-03]` 那个坑）
    #     · **容器条目指向的资产其实不在包里** ⇒ 加载期报 `Addressable prefab 'X' was not loaded!`
    #   ★ 2026-10-15 再改（用户现场）：③ 以前只数 `m_FileID == 0` 的引用，
    #     **跨文件引用被静默丢掉** ⇒ 输出"✓ 全在本包内"可能其实是"一条都没查"（**假绿**）✗；
    #     而且报出问题后只说"跑某条 python 命令"，用户得自己开终端 ⛔
    #     ⇒ 现在：① 用 `script_stats` 单一口径，把 **查了几条** 明明白白打出来；
    #             ② `unreadable > 0`（读不出 typetree）**不当成没问题**，明确提示；
    #             ③ 修法指向 GUI 里的 **「🔧 补悬空脚本」** 按钮（一键修 + 自动同步 CRC）✓
    # ══════════════════════════════════════════════════════════════════════════
    dangling = {}
    _st_all = {"mb": 0, "internal": 0, "external": 0, "unreadable": 0}
    _ext_targets = {}
    try:
        from fix_dangling_scripts import script_stats
        for _fname, _f in (getattr(env.file, "files", None) or {}).items():
            sf_ = _f if hasattr(_f, "objects") else None
            if sf_ is None or not getattr(sf_, "objects", None):
                continue
            _st = script_stats(sf_)
            for _k in _st_all:
                _st_all[_k] += _st[_k]
            for _fid, _n in _st["external_targets"].items():
                _ext_targets[_fid] = _ext_targets.get(_fid, 0) + _n
            if _st["missing"]:
                dangling[_fname] = {p: _st["need"][p] for p in _st["missing"]}
    except Exception as e:                                            # noqa: BLE001
        log("③ ⚠ 悬空脚本检查没跑成（%s: %s）—— **不能当成通过** ⚠" % (type(e).__name__, e))
        ok = False
    n_mb = _st_all["mb"] or sum(
        1 for o in env.objects if getattr(getattr(o, "type", None), "name", "") == "MonoBehaviour")
    _scope = ("查了 %d 个 MonoBehaviour 的 **%d 条**本包内引用（另有 %d 条跨文件引用，"
              "本工具不判断）" % (n_mb, _st_all["internal"], _st_all["external"]))
    if _st_all["unreadable"]:
        log("③ ⚠ 有 %d 个 MonoBehaviour 读不出 typetree ⇒ **这部分没查**（不能当成通过）⚠"
            % _st_all["unreadable"])
        ok = False
    if dangling:
        total = sum(len(v) for v in dangling.values())
        ok = False
        log("③ ⛔ **包内脚本悬空 %d 个**（%s）" % (total, _scope))
        log("   后果：游戏里刷 `The referenced script … is missing!`、编辑器抛 "
            "`No UnitPrefabRoot script found`；而且**静态网格照样渲染** ⇒ 会被误当成「成功」⚠")
        for fname, miss in list(dangling.items())[:3]:
            log("   · 内部文件 %s：缺 %s" % (fname, sorted(miss)[:6]))
        # ★ 一键修：以前这里只给 python 命令，用户得自己开终端、自己找源包、自己搬文件 ⛔
        log("   🔧 **修法**：点「④ 收尾」里的 **『🔧 补悬空脚本』** —— "
            "它自动找游戏主包、保持原 pathID 复制、写完重新解析自证、再自动同步 CRC ✓")
        try:
            from bundle_paths import units_bundle
            _u = units_bundle()
            if _u:
                log("   （源包 = 这些 prefab 原来所在的包；本机自动找到：%s）" % _u)
        except Exception:                                             # noqa: BLE001
            pass
    else:
        log("③ ✓ 包内脚本自包含：%s" % _scope)
    if _ext_targets:
        # 只报数不判对错：跨文件引用是否合法取决于游戏那边的文件表，静态查不出 ⇒ 不臆测 ✓
        log("   · 跨文件引用指向的文件（只列数，不判定）：%s"
            % ", ".join("fileID %s × %d" % (k, v) for k, v in sorted(_ext_targets.items())[:5]))

    # ④ 容器条目 ↔ 包内资产：自称有的资产是不是真的在包里
    #    （`Addressable prefab 'X' was not loaded!` 就是这条的实机症状）
    dangling_container = []
    try:
        dangling_container = container_asset_mismatch(env)
    except Exception as e:                                            # noqa: BLE001
        log("④ ⚠ 容器↔资产检查没跑成（%s: %s）" % (type(e).__name__, e))
    if dangling_container:
        ok = False
        log("④ ⛔ **容器条目指向的资产不在包里** %d 条（加载期会报 "
            "`Addressable prefab 'X' was not loaded!`）" % len(dangling_container))
        for k, pid_ in dangling_container[:5]:
            log("   · %s → pathID %s（包里没有）" % (k, pid_))
    else:
        log("④ ✓ 容器条目全部指向包内真实存在的资产")

    if catalog and address:
        import my_bundle_catalog as MC
        # ★ 2026-10-15 修（用户现场）：以前直接拿"地址"那格去 verify_chain，而那一格**可能还是
        #   默认值**（`MyMod/Asset`）⇒ 报 `✗ catalog 里找不到地址 'MyMod/Asset'`、整条自检判红 ✗
        #   —— 那是**输入框的锅，不是包的锅**。现在：找不到就先**按名字反查本包实际注册了哪些地址**，
        #   有就改验那些（并说明），一个都没有才判红 ✓
        targets, why = [address], ""
        try:
            found = MC.addresses_for_bundle(catalog, os.path.basename(bundle))
        except Exception as e:                                        # noqa: BLE001
            found, why = [], "反查地址失败：%s" % e
        if not found and not why:
            why = "catalog 里没有任何地址指向本包"
        if found and address not in found:
            log("⑤ ⚠ 你填的地址 %r 不在 catalog 里（那一格可能还是默认值）；"
                "本包实际注册了 %d 个地址，改验它们：" % (address, len(found)))
            targets = found
        if not found:
            log("⑤ ✗ %s —— 这个包还没被注册进 catalog（点「加资产/加图片」时会自动注册）" % why)
            ok = False
        else:
            chain_ok = True
            for i, tgt in enumerate(targets):
                chain_ok = MC.verify_chain(catalog, tgt, os.path.dirname(os.path.abspath(bundle)),
                                           os.path.basename(bundle), log=log) and chain_ok
                if i >= 4 and len(targets) > 5:
                    log("   …（还有 %d 个地址，略）" % (len(targets) - 5))
                    break
            log("⑤ 注册链复核：%s" % ("✓ 通过" if chain_ok else "✗ 有问题"))
            ok = ok and chain_ok
        # ⛔ **不要**再拿 catalog 的"地址"去和容器路径逐字比：地址是 Addressables 的**键**
        #    （如 `MyBundleTest/Asset1`），容器里放的是**资产路径**（如 `Assets/Mods/…prefab`），
        #    两者本来就不相等 ⇒ 那样比会**永远判红** ✗（第一版就这么错过）。
        #    "地址 → 条目 → internalId 是否真在包里" 这条链由 `verify_chain` 覆盖 ✓

    # ══════════════════════════════════════════════════════════════════════════
    # ⑥ ★⑩ **整份 catalog 的全量自洽校验**（不再只看"本包那一条链"）
    #   事故背景（★⑩）：把工具写出的 catalog 装进游戏 ⇒ **UI 全没了**（换回原版立刻正常），
    #   而事后静态检查查不出问题：数组长度"正好"、索引越界全 0、extra 字段齐全合法 ✗
    #   ⇒ 现在把"引用完整性 + 字段风格普查"整份过一遍；**判据都是在游戏原生 catalog 上实测过的**
    #     （见 `my_bundle_catalog.verify_catalog` 的 docstring），避免拿"我们的约定"当"游戏的不变量"✗
    # ══════════════════════════════════════════════════════════════════════════
    catv = None
    if catalog and os.path.isfile(catalog):
        import my_bundle_catalog as MC
        try:
            from catalog_mod import Catalog as _Cat
            _ref = MC.find_reference_bundle_options(_Cat(catalog), log=lambda *_a: None)
            catv = MC.verify_catalog(catalog, ref_style=(_ref[2] if _ref else None),
                                     bundle_dir=os.path.dirname(os.path.abspath(bundle)))
        except Exception as e:                                        # noqa: BLE001
            log("⑥ ⚠ catalog 全量自洽校验没跑成（%s: %s）—— **不能当成通过** ⚠"
                % (type(e).__name__, e))
            catv = None
        if catv is not None:
            st = catv["stats"]
            log("⑥ catalog 全量自洽：internalIds %d · keys %d · buckets %d · entries %d · "
                "bundle 条目 %d · extra %d B"
                % (st["internalIds"], st["keys"], st["buckets"], st["entries"],
                   st["bundles"], st["extra_bytes"]))
            if catv["ok"]:
                log("⑥ ✓ 引用完整性 / 孤儿条目 / 重复键 / 字段风格 **全部通过**%s"
                    % ("（" + catv["style_note"] + "）" if catv["style_note"] else ""))
            else:
                ok = False
                log("⑥ ⛔ **catalog 全量自洽校验不通过**（%d 条问题）—— 装进游戏可能整个 UI 消失 ⚠"
                    % len(catv["problems"]))
                for p in catv["problems"][:6]:
                    log("   · %s" % p)
                log("   🔧 修法：① 「字段风格不对」这类 ⇒ 点**「🔧 修 catalog 字段风格」一键修**"
                    "（只重写我们自己那条 bundle 条目的 extra ⇒ ⛔ 不需要重新注册、⛔ 不会动别人的条目）；"
                    "② 其它问题 ⇒ 用游戏原版 catalog 覆盖回去再重来")
            for eidx, nm, bn, _keys in (catv.get("style_entries") or [])[:3]:
                log("   · 风格异常条目[%d] %s → m_BundleName=%r（应当是不带 .bundle 的纯 32 位哈希）"
                    % (eidx, nm, bn))
    elif catalog:
        log("⑥ ⚠ catalog 不存在（%s）⇒ 跳过全量自洽校验 ⚠" % catalog)

    return {"ok": ok, "objects": len(env.objects), "container": names, "crc": crc,
            "dangling_scripts": dangling, "dangling_container": dangling_container,
            "catalog_verify": catv}


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

    def __init__(self, master, app=None):
        r"""`app`（可选）= 主程序 `EditorApp` ⇒ ★⑥ 地址命名辅助要靠它读**已加载的数据库**
        （`app.tables`）。不给也不影响用：那时只提示"没有 DB 参照"✓
        """
        import tkinter as tk
        from tkinter import ttk
        import queue
        self.tk = tk
        self.ttk = ttk
        self.queue = queue
        self.app = app
        self._db_tables = None            # 懒加载：第一次点「📏 查 DB 原值」时才去取
        self._addr_limit = None           # 当前地址的字符数上限（来自 DB 原值）
        # ★[体验-03] 优先用**上次用过且还存在**的工作目录；没有/已失效 ⇒ 默认值
        self.workdir = remembered_workdir() or DEFAULT_WORKDIR
        self.game_root = find_game_root()
        self._setup_paths()

        self.win = tk.Toplevel(master)
        self.win.title("我的 bundle —— 自建独立资源包（游戏按 catalog 地址读取）")
        # ★ 2026-10-15（用户："信息太多了，做些折叠栏吧"）：面板改成**可折叠分区** ⇒
        #   默认只露出「① 工作区 / ② bundle 列表 / ④ 收尾 / 日志」，
        #   加资产 / 图片 / 替换 / 删除 四块**默认收起**（点标题展开）⇒ 窗口也不必那么高 ✓
        self.win.geometry("1000x820")
        self.win.transient(master)
        # ★ 2026-10-15：`transient()` 会清掉 Win32 的 `WS_MAXIMIZEBOX` ⇒ **最大化按钮变灰**。
        #   生产路径靠 `ui_fit.install_autofit` 的 `<Map>` 钩子补回来，但那个钩子只在
        #   "程序装了钩子"时存在 ⇒ 这里**再显式补一次**，单独起对话框（自测/别的入口）也能最大化 ✓
        try:
            import ui_fit as _uf
            _uf.allow_maximize(self.win)
        except Exception:                                             # noqa: BLE001
            pass
        # ★ [界面-05 2026-09-19 用户亲令] **系统标题栏也切暗色**：ttk 主题只管客户区，
        #   标题栏由 DWM 画 ⇒ 浅色系统主题下这里是一条白条（用户截图两条之一）。
        #   ★ 2026-09-20（[界面-11] 收口）：**这里只做创建时首设，⛔ 不再「显示之后再补」** ——
        #     本类 `self.win` 是 **Toplevel** ⇒ 已在 `ui_fit.install_autofit` 的 `<Map>` 钩子**覆盖面内**，
        #     再补一次＝**重复施加**（`[界面-08]`「打地鼠」体感来源之一）⇒ 那两行已删（差 165 B＝2 行含 CRLF）。
        try:
            import ui_fit as _uf_tb
            _uf_tb.apply_dark_title_bar(self.win)
        except Exception:                                             # noqa: BLE001
            pass
        # ⛔ 折叠**不销毁**内容：变量与回调都还在，「全部展开」一按就回来 ✓
        #   布局回归见 `测试\test_my_bundle_dialog.py` ③（先全部展开再量"按钮有没有被挤出窗口"）

        top = ttk.Frame(self.win, padding=10)
        top.pack(fill="both", expand=True)
        import ui_fit as _ufit

        # ── 顶部：全部展开 / 全部折叠（折叠栏多了以后必须有一个一键开关）──
        bar = ttk.Frame(top)
        bar.pack(fill="x")
        ttk.Label(bar, text="分区：", foreground="#888").pack(side="left")
        ttk.Button(bar, text="全部展开", command=lambda: self._all_sections(True)).pack(side="left", padx=4)
        ttk.Button(bar, text="全部折叠", command=lambda: self._all_sections(False)).pack(side="left")
        ttk.Label(bar, text="（收起的分区点标题即可展开）", foreground="#888").pack(side="left", padx=8)

        self._sections = []

        def _mk(title, expanded, summary=""):
            sec = _ufit.Collapsible(top, title, expanded=expanded, summary=summary, ttk=ttk,
                                    on_toggle=lambda _s: self._fit())
            sec.pack(fill="x", pady=(8, 0))
            self._sections.append(sec)
            return sec

        # ── ① 工作区（默认展开：一进来就要用它选目录）──
        self.sec_work = _mk("① 工作区", True)
        frm = self.sec_work.body
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

        # ── ② 我的 bundle（默认展开：它是"选一个继续操作"的入口）──
        self.sec_list = _mk("② 我的 bundle（选一个继续加资产）", True)
        frm2 = self.sec_list.body
        self.listbox = tk.Listbox(frm2, height=6)
        self.listbox.grid(row=0, column=0, columnspan=4, sticky="we")
        self.listbox.bind("<<ListboxSelect>>", lambda _e: self._on_pick())
        frm2.columnconfigure(0, weight=1)

        # ── ③ 新建 / 加资产（默认收起）──
        self.sec_assets = _mk("③ 新建 bundle / 加资产(.bamod)", False)
        frm3 = self.sec_assets.body
        self.name_var = tk.StringVar(value="MyMod")
        self.tmpl_var = tk.StringVar(value="")
        self.pack_var = tk.StringVar(value="")
        # ★★ 2026-09-17 缺陷修复（「默认值被静默注册」，中枢 P1）：
        #   ⛔ 这里**不许**再预填任何具体地址！旧值是硬编码 `"MyMod/Asset"` ⇒ **"用户不填" ≠ "空"**
        #   （`addr_var.get()` 恒为 `"MyMod/Asset"`）⇒ `_run()`（L2192）把它当**用户输入的地址**
        #   传进 `add_assets()` ⇒ L271 `if catalog and address:` 判定为真 ⇒ **静默往 catalog 注册
        #   一个用户从没要求过的地址**（用户实跑日志 + 产物复算双证据）。
        #   ⇒ 默认留空 = 与下方提示文案「地址留空 ⇒ 不注册进 DB」（L1693 区）**一致**；
        #     想让用户照抄示例时，示例写在**提示文案**里（见 addr_hint），⛔ 不写进输入框的值。
        self.addr_var = tk.StringVar(value="")
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
        # ★★ 单模板合并(甲)（2026-09-20，中枢采纳）：**产品只带一个模板** —— 原独立的
        #   「肖像/图标骨架包」已把 1×1 占位图标（Texture2D + Sprite + `Sprite(213)` 类型）
        #   **烘进** `units_warehouse_small.bundle` 本体 ⇒ 那个"切换模板"按钮**没有存在必要**
        #   ⇒ 改成一行提示（⛔ 不再现造第二个包）✓
        ttk.Label(frm3, text="（单模板：小模板 149 KB、**加载快** 且**已自带** 1×1 占位图标 ⇒ ③b 的"
                             "「骨架图标」下拉天然可用；游戏主包 3.15 GB 结构同源但**游戏加载明显变慢**）",
                  foreground="#888888").grid(row=2, column=1, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(frm3, text="素材包(.bamod)：").grid(row=3, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(frm3, textvariable=self.pack_var, width=64).grid(row=3, column=1, columnspan=2, sticky="we",
                                                                  pady=(6, 0))
        ttk.Button(frm3, text="选素材包…", command=self._browse_pack).grid(row=3, column=3, padx=4, pady=(6, 0))
        ttk.Label(frm3, text="地址：").grid(row=4, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(frm3, textvariable=self.addr_var, width=30).grid(row=4, column=1, sticky="w", pady=(6, 0))
        # ★⑥ 地址命名辅助：DB 里这个字段的**原值与字符数**，并校验"新值 ≤ 原值"
        #   （实机踩过：`FLYACV2`(7) 写不进 DB，`FLACV2`(6) 才行 —— 定长回填）
        ttk.Button(frm3, text="📏 查 DB 原值", command=self._addr_lookup).grid(row=4, column=2, padx=4,
                                                                             pady=(6, 0))
        # ★[界面-01] 文案必须写清「留空 = 不注册」——逻辑本来就是 `if catalog and address:`（见 L271），
        #   但界面从没告诉用户 ⇒ 用户填不出地址时只能瞎猜（本次修复把它写进可见提示）。
        self.addr_hint = ttk.Label(frm3, text="（地址留空 ⇒ 不注册进 DB；点「📏 查 DB 原值」看这个字段原值有几个字符）",
                                   foreground="#888888")
        self.addr_hint.grid(row=4, column=3, sticky="w", pady=(6, 0))
        # 边打字边校验：超长立刻变红（不必等到注册失败才发现）
        try:
            self.addr_var.trace_add("write", lambda *_a: self._addr_check())
        except Exception:                                             # noqa: BLE001
            pass
        # ★[界面-01] 修：下面这两行原来与「地址 / 📏 查 DB 原值」**同占 row=4**
        #   ⇒ 同格后 `grid()` 盖住先 `grid()` 的控件 ⇒ 地址框与查 DB 按钮**完全不可见**（不是宽度问题，
        #   拉多宽都治不好）；资产类型下拉同样盖住 addr_hint。⇒ 本组整体下移到 row=5，按钮移到 row=6。
        #   判据：`python _rev_tools\out\probe_gui_grid_collisions.py 工具制作资源\BA_Mod_Maker\my_bundle.py`
        #   （改前 4 处 / FAIL / exit 1 ⇒ 改后必须 0 处 / exit 0）
        ttk.Label(frm3, text="bundle 内路径：").grid(row=5, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(frm3, textvariable=self.asset_var, width=30).grid(row=5, column=1, sticky="w", pady=(6, 0))
        self.types_box = ttk.Combobox(frm3, textvariable=self.type_var, width=34, values=[
            "UnityEngine.GameObject", "UnityEngine.Texture2D", "UnityEngine.Sprite",
            "UnityEngine.Material", "UnityEngine.Mesh", "UnityEngine.AudioClip",
            "UnityEngine.TextAsset", "UnityEngine.AnimationClip"])
        ttk.Label(frm3, text="资产类型：").grid(row=5, column=2, sticky="e", pady=(6, 0))
        self.types_box.grid(row=5, column=3, sticky="we", pady=(6, 0))
        ttk.Button(frm3, text="把素材加进这个 bundle", command=lambda: self._run("add")).grid(
            row=6, column=1, sticky="w", pady=(8, 0))
        frm3.columnconfigure(1, weight=1)

        # ── ③b 图片（默认收起）──
        self.sec_image = _mk("③b 图片（图标 / 头像 / 标签图）", False)
        frm3b = self.sec_image.body
        self.png_var = tk.StringVar(value="")
        self.src_var = tk.StringVar(value="")
        ttk.Label(frm3b, text="PNG 图片：").grid(row=0, column=0, sticky="w")
        ttk.Entry(frm3b, textvariable=self.png_var, width=64).grid(row=0, column=1, columnspan=2, sticky="we")
        ttk.Button(frm3b, text="选 PNG…", command=self._browse_png).grid(row=0, column=3, padx=4)
        ttk.Label(frm3b, text="骨架图标：").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.src_box = ttk.Combobox(frm3b, textvariable=self.src_var, width=60, values=[])
        self.src_box.grid(row=1, column=1, columnspan=2, sticky="we", pady=(6, 0))
        ttk.Button(frm3b, text="刷新骨架列表", command=self._fill_source_paths).grid(row=1, column=3, padx=4,
                                                                                 pady=(6, 0))
        ttk.Button(frm3b, text="★ 把图片加进这个 bundle（自动注册新地址 + 同步 CRC）",
                   command=lambda: self._run("addimg")).grid(row=2, column=1, sticky="w", pady=(8, 0))
        frm3b.columnconfigure(1, weight=1)

        # ── ③c 替换现有图标（默认收起）──
        self.sec_replace = _mk("③c 替换现有图标（就地换图 + 可选重指向原包键）", False)
        frm3c = self.sec_replace.body
        self.repl_var = tk.StringVar(value="")
        self.oldkey_var = tk.StringVar(value="")
        self.batch_var = tk.BooleanVar(value=False)
        ttk.Label(frm3c, text="替换：").grid(row=0, column=0, sticky="w")
        ttk.Entry(frm3c, textvariable=self.repl_var, width=64).grid(row=0, column=1, columnspan=2, sticky="we")
        ttk.Button(frm3c, text="选 PNG/文件夹…", command=self._browse_replace).grid(row=0, column=3, padx=4)
        ttk.Checkbutton(frm3c, text="批量（文件夹里按**原图标名**匹配，勾上时下面那格不用填）",
                        variable=self.batch_var).grid(row=1, column=1, sticky="w", pady=(6, 0))
        ttk.Label(frm3c, text="重指向原包键：").grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(frm3c, textvariable=self.oldkey_var, width=60).grid(row=2, column=1, columnspan=2,
                                                                     sticky="we", pady=(6, 0))
        ttk.Button(frm3c, text="= 骨架包名", command=self._fill_oldkey).grid(row=2, column=3, padx=4, pady=(6, 0))
        ttk.Button(frm3c, text="🔁 ★ 替换现有图标（填了原包键就一并重指向）",
                   command=lambda: self._run("replace")).grid(row=3, column=1, sticky="w", pady=(8, 0))
        frm3c.columnconfigure(1, weight=1)

        # ── ③e ★②/★⑪ 肖像 / 图标的「原键替换」（默认收起）──
        # 为什么单开一块（T 清单 §一.2 实机定案）：
        #   ⛔ **单位肖像/图标不要指望新建地址键 —— 实测游戏不消费它**
        #      （指向新地址 ⇒ 立绘**回退显示成一个步兵立绘**）
        #   ⇒ 肖像类只能"**原键不变**"：把游戏原来那条键/catalog 条目的内容换成我们的图 ✓
        self.sec_portrait = _mk("③e 肖像 / 图标：原键替换（★新建地址不生效，必须走这里）", False)
        frm3e = self.sec_portrait.body
        self.op_png_var = tk.StringVar(value="")
        self.op_pack_var = tk.StringVar(value="")
        self.op_asset_var = tk.StringVar(value="")
        ttk.Label(frm3e, text="⛔ 单位**肖像/图标**必须用「原键替换」：实测新建地址键**不会被游戏消费**"
                              "（立绘会回退成步兵图）", foreground="#d0a020").grid(
            row=0, column=0, columnspan=4, sticky="w")
        ttk.Label(frm3e, text="我的 PNG：").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(frm3e, textvariable=self.op_png_var, width=58).grid(row=1, column=1, columnspan=2,
                                                                     sticky="we", pady=(6, 0))
        ttk.Button(frm3e, text="选 PNG…", command=lambda: self._browse_into(self.op_png_var, True)).grid(
            row=1, column=3, padx=4, pady=(6, 0))
        ttk.Label(frm3e, text="游戏原包：").grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(frm3e, textvariable=self.op_pack_var, width=58).grid(row=2, column=1, columnspan=2,
                                                                      sticky="we", pady=(6, 0))
        ttk.Button(frm3e, text="选原包…", command=lambda: self._browse_into(self.op_pack_var, False)).grid(
            row=2, column=3, padx=4, pady=(6, 0))
        ttk.Label(frm3e, text="原包内资产路径：").grid(row=3, column=0, sticky="w", pady=(6, 0))
        self.op_asset_box = ttk.Combobox(frm3e, textvariable=self.op_asset_var, width=58, values=[])
        self.op_asset_box.grid(row=3, column=1, columnspan=2, sticky="we", pady=(6, 0))
        ttk.Button(frm3e, text="列出原包里的图", command=self._fill_op_assets).grid(row=3, column=3, padx=4,
                                                                                   pady=(6, 0))
        ttk.Button(frm3e, text="🔁 一键原键替换（复制原包 → 换图 → 重指向，3 步合成 1 步）",
                   command=lambda: self._run("oneclick")).grid(row=4, column=1, sticky="w", pady=(8, 0))
        ttk.Button(frm3e, text="✂ 只把这条资产改指到我这个小包（条目级，不用复制整个 304 MB 包）",
                   command=lambda: self._run("entryrepoint")).grid(row=5, column=1, sticky="w", pady=(6, 0))
        ttk.Label(frm3e, text="（条目级那条要求：**我这个小包的大路径就是上面那条原始路径** —— "
                              "用 ③b「把图片加进这个 bundle」并把 bundle 内路径填成它）",
                  foreground="#888888").grid(row=6, column=1, columnspan=3, sticky="w")
        frm3e.columnconfigure(1, weight=1)

        # ── ③d ★ 删除包内已有条目（默认收起）──
        # 真实诉求：① 换骨架命名时要清旧条目；② 加错地址/图后要能撤销；③ 给包"瘦身"
        #   （149 KB 模板建出来的包自带 9 个 `_TEMPLATE_*` 骨架 prefab）。
        # ⛔ 只删**容器条目**：对象与 m_PreloadTable 都不动 ⇒ 被删的资产成了孤儿对象（无害）✓
        self.sec_remove = _mk("③d 删除包内条目（只删容器条目；对象与 preload 都不动）", False)
        frm5 = self.sec_remove.body
        self.del_var = tk.StringVar(value="")
        ttk.Label(frm5, text="包内条目：").grid(row=0, column=0, sticky="w")
        self.del_box = ttk.Combobox(frm5, textvariable=self.del_var, width=70, values=[])
        self.del_box.grid(row=0, column=1, sticky="we", padx=(4, 4))
        ttk.Button(frm5, text="刷新条目列表", command=self._fill_container_paths).grid(row=0, column=2, padx=4)
        self.del_fuzzy_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(frm5, text="按末段文件名批量删（图标/肖像常是 Texture2D+Sprite 两条同名条目）",
                        variable=self.del_fuzzy_var).grid(row=1, column=1, sticky="w", pady=(6, 0))
        ttk.Button(frm5, text="🗑 删除选中条目", command=lambda: self._run("remove")).grid(
            row=1, column=2, padx=4, pady=(6, 0))
        frm5.columnconfigure(1, weight=1)

        # ── [界面-04] prefab ↔ 映射路径（双向检索）──
        #   ★ 复用**产品树内唯一正本** `_rev_tools\bundle_asset_lookup_core.py`（`技术资料\scripts\bundle_asset_lookup.py`
        #     那个 CLI 薄壳也 import 同一件）⇒ ⛔ 不复制逻辑、两处同源。
        #   默认包 = 「② 我的 bundle」列表里**当前选中**那个（用既有 `self._cur()`）⇒ ⛔ 不新造选择器。
        #   ⚠ 本分区必须建在 `ui_fit.theme_from_master` **之前**（那条调用只给"已存在"的经典控件上色；
        #      [界面-03] 实测踩过：晚建 ⇒ 空跑、读数仍是系统白底）。
        #   三条风险由核心统一处理：巨型 CAB 显式报大小/阈值、同资产两键归并成一行并注明、limit＋"共 N 条已显示前 M 条"。
        self.sec_lookup = _mk("③f prefab ↔ 映射路径（双向检索）", False,
                              summary="与命令行同一实现：路径→prefab ／ prefab名→路径")
        lk = self.sec_lookup.body
        self.lk_grep_var = tk.StringVar(value="")
        self.lk_by_var = tk.StringVar(value="key")
        self.lk_limit_var = tk.StringVar(value="200")
        ttk.Label(lk, text="片段：").grid(row=0, column=0, sticky="w")
        ttk.Entry(lk, textvariable=self.lk_grep_var, width=36).grid(row=0, column=1, sticky="we", padx=4)
        ttk.Radiobutton(lk, text="按路径（路径 → prefab）", value="key",
                        variable=self.lk_by_var).grid(row=0, column=2, sticky="w", padx=(6, 0))
        ttk.Radiobutton(lk, text="按对象名（prefab → 路径）", value="name",
                        variable=self.lk_by_var).grid(row=0, column=3, sticky="w", padx=(6, 0))
        ttk.Label(lk, text="限条：").grid(row=0, column=4, sticky="e", padx=(8, 0))
        ttk.Entry(lk, textvariable=self.lk_limit_var, width=6).grid(row=0, column=5, sticky="w")
        ttk.Button(lk, text="🔎 检索", command=lambda: self._run("lookup")).grid(row=0, column=6, padx=6)
        self.lk_cur_lbl = ttk.Label(lk, text="当前包：（未选；默认用「② 我的 bundle」里选中的那个）",
                                    foreground="#888888")
        self.lk_cur_lbl.grid(row=1, column=0, columnspan=7, sticky="w", pady=(6, 0))
        self.lk_txt = tk.Text(lk, height=10, wrap="none")
        self.lk_txt.grid(row=2, column=0, columnspan=7, sticky="we", pady=(4, 0))
        lk.columnconfigure(1, weight=1)
        self._lk_last = None
        self._lk_update_cur()

        # ── ④ 收尾（**不折叠**：这是主操作栏，每次都点）──
        frm4 = ttk.LabelFrame(top, text="④ 收尾", padding=8)
        frm4.pack(fill="x", pady=(8, 0))
        # ★ v1.8.117：新增 **「🔧 补悬空脚本」**（用户现场：自检报 `③ ⛔ 包内脚本悬空 16 个`，
        #   而当时唯一的修法是"去开终端跑一条 python" ⇒ 用户直接问"你能不能直接修一下"）✗
        #   ⇒ 把它做成**一键**：自动找游戏主包、保持原 pathID 复制、写完重新解析自证、
        #     再自动同步 CRC 到当前 catalog ✓（`fix_scripts()`）
        #   ⛔ 顺序上它放在「自检」旁边：自检报红 → 点它 → 再自检（自检输出里也这么指路）✓
        # ★㉑（[工具-10]）：表里新增 **「🔧 修 catalog 字段风格」**（用户现场：⑥ 自检报
        #   `我们注册的 bundle 条目的字段风格不对`，而当时唯一的修法是"重新注册一遍"——
        #   要地址、还要走一遍打包流程 ⇒ 用户直接问"你能不能直接修一下"）✗
        #   ⇒ 一键：**只重写我们自己那条 bundle 条目的 extra**（判定键 = internalId 以本包文件名结尾
        #     ⇒ 游戏原生条目 / 别家的包**不可能命中**）· ⛔ 不需要重新注册 · ⛔ 不动别人的条目 ✓
        #   ⛔ 修的是**工作目录里那份 catalog 副本**（`workdir_catalog`），绝不直接写游戏目录 ✓
        # ⛔ 第 8 枚起**换行**：上面那排已有 7 枚（含两枚长标签），再往同一行塞会把窗口撑宽 ——
        #    本文件就有"按钮被挤出窗口/点不到"的前科（见下面日志框那段注释）✗
        _WRAP_AT = 7
        for i, (txt, act) in enumerate([("同步 CRC", "crc"), ("自检", "check"),
                                        ("🔧 补悬空脚本", "fixscripts"),
                                        ("🔍 改包后自检（内部名/CAB 依赖）", "cabcheck"),
                                        ("安装到游戏（先备份）", "install"), ("还原 catalog", "restore"),
                                        ("全流程（新建→加资产→CRC→自检）", "all"),
                                        ("🔧 修 catalog 字段风格", "fixstyle")]):
            _r, _c = (0, i) if i < _WRAP_AT else (i - _WRAP_AT + 1, i - _WRAP_AT)
            # ⛔ 换行那枚必须 `columnspan` 横跨整行：否则它落在第 0 列、**把第 0 列撑宽**
            #    ⇒ 上面一整行被整体推右 ⇒ 最后一枚（全流程）**被挤出窗口右边** ✗
            #    （GUI 冒烟实测：不加 columnspan 时既有 7 枚坐标整体右移 27 px、末枚超出 28 px）
            _kw = {} if _r == 0 else {"columnspan": _WRAP_AT, "sticky": "w", "pady": (6, 0)}
            ttk.Button(frm4, text=txt, command=lambda a=act: self._run(a)).grid(
                row=_r, column=_c, padx=4, **_kw)

        # ⛔ 2026-10 第 62 轮修：以前 log 先 `pack(expand=True)`、关闭按钮后 pack ⇒
        #    日志框吃掉全部剩余空间，**关闭按钮被挤出窗口底部**（点不到）✗
        #    ⇒ 先 pack 一条 `side="bottom"` 的底栏，再 pack 日志（顺序决定分配）✓
        bottom = ttk.Frame(top)
        bottom.pack(side="bottom", fill="x", pady=(6, 0))
        ttk.Button(bottom, text="关闭", command=self.win.destroy).pack(anchor="e")
        # ⛔ 日志框：`height` 只是**请求**高度（`expand=True` 会把它撑满剩余空间）⇒
        #   把它调小可以让"全部展开"时少要一点高度，屏幕小时少截一点 ✓
        self.log = tk.Text(top, height=8, wrap="none")
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

    # ---- ★⑥ 地址命名辅助（DB 原值 + 字符数 + 长度校验）----
    def _db(self):
        """懒取数据库表：优先用主程序**已加载**的（`app.tables`），否则找一份导出目录读进来。"""
        if self._db_tables is not None:
            return self._db_tables
        import addr_limits as AL
        t = {}
        app = getattr(self, "app", None)
        cand = getattr(app, "tables", None)
        if isinstance(cand, dict) and cand:
            t = cand
            self.say("（用主程序里已加载的数据库做参照：%d 张表）" % len(cand))
        else:
            for folder in (os.path.join(self.workdir, "db_export"),
                           os.path.join(self._setup_paths() or "", "db_export"),
                           os.path.join(self.workdir, "db")):
                if folder and os.path.isdir(folder):
                    AL.load_db_folder(folder, t)
                    if t:
                        self.say("（用 %s 里的数据库导出做参照：%d 张表）" % (folder, len(t)))
                        break
        self._db_tables = t
        return t

    def _addr_lookup(self):
        r"""★⑥ 点「📏 查 DB 原值」：算出**这个地址的字符数上限**。

        三种情况，按"最有用"排序：
          ① 地址框里的值**就是 DB 原值**（用户把 `US_ACV` 填进来问长度）⇒ 直接用它 ✓
          ② 地址框里是新起的名（DB 里当然还没有）⇒ **用 catalog 反查本包注册过的地址**，
             再去 DB 里找"哪个字段 == 那个地址" ⇒ 拿到的就是**这条要替换的**原值长度 ✓
             （正好复用自检 ⑤ 那套 `addresses_for_bundle`；实机里这个问题就是
              "我刚给这个包注册了 `FLYACV`，DB 里指过去的 `ModelFileName` 原来多长？"）
          ③ 都查不到 ⇒ 只报字段现有长度的统计，**不给数字上限**（不臆测）✓
        """
        import addr_limits as AL
        tables = self._db()
        addr = self.addr_var.get().strip()
        limit, hint = None, ""
        if addr:
            hits = AL.find_db_usage(tables, addr)
            if hits:
                _t, _i, _rid, _f, val = hits[0]
                limit = len(val)
                hint, limit = AL.lookup(tables, addr)
        if limit is None:
            # ② 反查本包注册过的地址 ⇒ 再回 DB 找原值
            b = self._cur()
            cat = workdir_catalog(b) if b else None
            if b and cat and os.path.isfile(cat):
                try:
                    import my_bundle_catalog as MC
                    for a in MC.addresses_for_bundle(cat, os.path.basename(b))[:5]:
                        h2 = AL.find_db_usage(tables, a)
                        if h2:
                            _t, _i, _rid, _f, val = h2[0]
                            limit = len(val)
                            hint = ("📏 本包注册的地址 %r 在 DB 里是 %s #%s 的 %s = %r "
                                    "⇒ **%d 字符**（新地址必须 ≤ %d）"
                                    % (a, _t, _rid, _f, val, len(val), len(val)))
                            break
                except Exception as e:                                # noqa: BLE001
                    self.say("（反查本包地址失败：%s）" % e)
        if not hint:
            hint, limit = AL.lookup(tables, None)
        self._addr_limit = limit
        self.say(hint)
        self._addr_check(quiet=False)

    def _addr_check(self, quiet=True):
        """实时校验：地址超过 DB 原值长度就**变红**（定长回填放不下 —— 实机踩过）✗

        ★ 2026-09-17：**空**地址是**合法状态**（= 不注册，见 L271 `if catalog and address:`）
          ⇒ 空时只显示中性提示，⛔ 不再走 `check_len`（否则会显示「地址 '' 共 0 字符」那种
          让人以为"必须填"的文案 —— 那正是「默认值被静默注册」缺陷的**同源诱导**）。
        """
        try:
            import addr_limits as AL
            if not self.addr_var.get().strip():
                if hasattr(self, "addr_hint"):
                    self.addr_hint.configure(text="\u2009（留空 ⇒ 不注册进 DB；要注册就填一个地址，"
                                                  "示例 `MyMod/Camo`）", foreground="#888888")
                return True
            ok, msg = AL.check_len(self.addr_var.get().strip(), self._addr_limit)
            if hasattr(self, "addr_hint"):
                self.addr_hint.configure(text=("\u2009" + msg.replace("\n", " "))[:150],
                                         foreground=("#3ba55d" if ok else "#e05555"))
            if not ok and not quiet:
                self.say(msg)
            return ok
        except Exception:                                             # noqa: BLE001
            return True
        self.win.update_idletasks()

    def _browse_workdir(self):
        from tkinter import filedialog
        p = filedialog.askdirectory(title="选择工作目录")
        if p:
            self.workdir_var.set(p)
            self.workdir = p
            remember_workdir(p)          # ★[体验-03] 选定即记住（共享 settings.json）
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

    # ★★ 2026-09-20 单模板合并(甲)：原 `_use_skeleton_template()`（把模板切到单独造的
    #   `portrait_skeleton.bundle`）**已删** —— 骨架已烘进唯一模板 ⇒ 没有"可切换的第二个模板"了。
    #   造骨架的能力仍在 `_rev_tools\image_skeleton.py`（工具/回归用），⛔ 产品 GUI 不再需要它。

    def _browse_into(self, var, is_png=False):
        """★② 通用"选文件填进某个变量"（③e 那块用；省得为每个框写一份 browse）"""
        from tkinter import filedialog
        if is_png:
            p = filedialog.askopenfilename(title="选择图片（PNG/JPG）",
                                           filetypes=[("图片", "*.png *.jpg *.jpeg"), ("全部", "*.*")])
        else:
            p = filedialog.askopenfilename(title="选择游戏里的原包（.bundle）",
                                           filetypes=[("bundle", "*.bundle"), ("全部", "*.*")])
        if p:
            var.set(p)

    def _fill_op_assets(self):
        """★② 列出"游戏原包"里的容器条目（图片类排前面）—— 用户不用手抄那串长路径 ✓"""
        p = self.op_pack_var.get().strip()
        if not p or not os.path.isfile(p):
            self.say("✗ 先在「游戏原包」里选一个 .bundle（比如游戏自己的 `unitportraits_assets_all_*.bundle`）")
            return []
        try:
            names = list_image_assets_in_bundle(p)
        except Exception as e:                                        # noqa: BLE001
            self.say("✗ 读不出这个包的容器条目：%s: %s" % (type(e).__name__, e))
            return []
        imgs = [n for n in names if n.lower().endswith((".png", ".jpg", ".jpeg", ".tga", ".dds"))]
        vals = imgs + [n for n in names if n not in imgs]
        try:
            if hasattr(self, "op_asset_box"):
                self.op_asset_box.configure(values=vals[:2000])
        except Exception:                                             # noqa: BLE001
            pass
        if vals and not self.op_asset_var.get().strip():
            self.op_asset_var.set(vals[0])
        self.say("原包里有 %d 条容器条目（图片 %d 条）⇒ 已填进下拉框%s"
                 % (len(names), len(imgs), ("，默认选了第一条") if vals else ""))
        return vals

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

    def _fill_container_paths(self):
        """★ 把当前 bundle 的**容器条目**填进删除下拉框（③d 用）。

        为什么要有这个按钮：用户不知道容器里到底有什么（图标/肖像在容器里是
        `Texture2D` + `Sprite` **两条同名条目**）⇒ 先列出来再选，别让人猜路径。
        """
        b = self._cur()
        if not b:
            self.say("（先在上面列表里选一个 bundle，再刷新条目列表）")
            return []
        try:
            res = selfcheck(b, log=lambda *_a: None)
            paths = list(res.get("container") or [])
        except Exception as e:                                    # noqa: BLE001
            self.say("✗ 读容器条目失败：%s: %s" % (type(e).__name__, e))
            return []
        self.del_box["values"] = paths
        if paths and not self.del_var.get():
            self.del_var.set(paths[0])
        try:
            self.sec_remove.summary = "%d 条" % len(paths)
        except Exception:                                             # noqa: BLE001
            pass
        self.say("包内容器条目 %d 条（选一条点「🗑 删除选中条目」；勾了批量就按末段文件名删）" % len(paths))
        return paths

    def _fit(self):
        """把窗口重新长到"当前展开状态放得下"（折叠栏展开/收起后调）。

        ⛔ 不 `shrink`：`ui_fit.fit_window` 只增不减（用户手动放大的窗口不会被缩回去）✓
        """
        try:
            self.win.update_idletasks()
            import ui_fit
            ui_fit.fit_window(self.win)
        except Exception:                                             # noqa: BLE001
            pass

    def _all_sections(self, expand):
        """「全部展开 / 全部折叠」——折叠栏多了以后必须有这个一键开关。"""
        secs = getattr(self, "_sections", None) or []
        try:
            import ui_fit
            ui_fit.expand_all(secs, expand)
        except Exception:                                             # noqa: BLE001
            for s in secs:
                try:
                    s.expand() if expand else s.collapse()
                except Exception:                                     # noqa: BLE001
                    pass
        # 展开后窗口内容变高 ⇒ 交给全局自适应（`ui_fit.install_autofit` 的 <Map> 钩子只跑一次）
        try:
            self.win.update_idletasks()
            import ui_fit
            ui_fit.fit_window(self.win)
        except Exception:                                             # noqa: BLE001
            pass
        self.say("已%s全部折叠分区（点分区标题可单独展开/收起）" % ("展开" if expand else "折叠"))
        # ⛔ 诚实交代物理限制（2026-10-15 实测）：**全部展开**需要 ~1300 px 高，
        #   而本机屏幕可用高度只有 ~880 px ⇒ 窗口会被屏幕截断、底部按钮看不到 ✗
        #   ⇒ 别让用户以为"按钮没了/坏了"：直接告诉他**一次只展开要用的那一块** ✓
        if expand:
            try:
                self.win.update_idletasks()
                need = self.win.winfo_reqheight()
                usable = int(self.win.winfo_screenheight() * 0.92)
                if need > usable:
                    self.say("⚠ 全部展开需要约 %d px，而屏幕可用约 %d px ⇒ 底部会被截断；"
                             "**建议一次只展开你要用的那一块**（收起其余分区即可）" % (need, usable))
            except Exception:                                         # noqa: BLE001
                pass

    def _refresh(self):
        self.listbox.delete(0, "end")
        self._bundles = list_my_bundles(self.workdir_var.get())
        for f, sz, _mt in self._bundles:
            self.listbox.insert("end", "%8.1f KB  %s" % (sz / 1024, f))
        # ★ 折叠栏标题上的摘要：收起时也看得见"有几个 bundle"（用户嫌信息太多 ⇒ 折叠但不能变瞎）✓
        try:
            self.sec_list.summary = "%d 个" % len(self._bundles) if self._bundles else "还没有"
        except Exception:                                             # noqa: BLE001
            pass
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
            try:
                self._lk_update_cur()                  # ★ [界面-04] 当前包提示跟着选中的包走
            except Exception:                                              # noqa: BLE001
                pass

    def _cur(self):
        return getattr(self, "_cur_bundle", None)

    # ---- [界面-04] prefab ↔ 映射路径（双向检索）----
    def _lk_update_cur(self):
        r"""把"当前包"读进只读提示行（默认包 = 「② 我的 bundle」列表里选中的那个）"""
        b = self._cur()
        try:
            self.lk_cur_lbl.configure(
                text=("当前包：%s" % b) if b else "当前包：（未选；默认用「② 我的 bundle」里选中的那个）")
        except Exception as exc:                                               # noqa: BLE001
            self.say("⚠ 当前包提示刷新失败：%s: %s" % (type(exc).__name__, exc))

    def _lk_show(self, res):
        r"""把检索结果写进**只读结果框**（★ 必须主线程调用；worker 用 `self.win.after(0, …)` 调度）。

        同时把原始结果挂到 `self._lk_last`（回归用：GUI 与 CLI **逐行一致**时以它为准）。
        """
        from bundle_asset_lookup_core import format_rows
        self._lk_last = res
        txt = "\n".join(format_rows(res))
        try:
            self.lk_txt.delete("1.0", "end")
            self.lk_txt.insert("end", txt + "\n")
        except Exception as exc:                                               # noqa: BLE001
            self.say("⚠ 结果框写入失败：%s: %s" % (type(exc).__name__, exc))
        self._lk_update_cur()

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
                    delpath=self.del_var.get().strip() or None,
                    delfuzzy=bool(self.del_fuzzy_var.get()),
                    oppng=self.op_png_var.get().strip() or None,
                    oppack=self.op_pack_var.get().strip() or None,
                    opasset=self.op_asset_var.get().strip() or None,
                    lk_grep=self.lk_grep_var.get().strip(),
                    lk_by=self.lk_by_var.get().strip() or "key",
                    lk_limit=self.lk_limit_var.get().strip() or "200",
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
        if act == "lookup":
            # ★ [界面-04] prefab ↔ 映射路径：GUI 侧**只调核心**（⛔ 不复制逻辑）
            bundle = a["bundle"] or self._cur()
            if not bundle:
                log("✗ 先在「② 我的 bundle」列表里选一个包（或先新建一个）")
                return
            try:
                from bundle_asset_lookup_core import lookup, format_rows
            except Exception as e:                                            # noqa: BLE001
                log("✗ 载入检索核心失败（_rev_tools\\bundle_asset_lookup_core.py）：%s: %s" % (type(e).__name__, e))
                return
            try:
                lim = int(a.get("lk_limit") or 200)
            except Exception:                                                  # noqa: BLE001
                lim = 200
            res = lookup(bundle, a.get("lk_grep") or "", by=a.get("lk_by") or "key", limit=lim)
            for line in format_rows(res):
                log(line)
            # 结果框只在**主线程**写（worker 里用 after 调度 ⇒ 不跨线程碰 Tk 控件）
            try:
                self.win.after(0, lambda r=res: self._lk_show(r))
            except Exception as e:                                             # noqa: BLE001
                log("⚠ 结果框调度失败（日志里已打印全文）：%s: %s" % (type(e).__name__, e))
            return
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
            # ★③：就地替换**也改了包的字节** ⇒ 无论有没有做重指向，都自动同步 CRC
            #   （以前只有"给了原包键"那条路会顺手更新 extra，不走重指向时 CRC 就悄悄过期了 ✗）
            auto_sync_crc(bundle, workdir_catalog(bundle), log=log, why="替换图标后")
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
        if act == "remove":
            # ★ 2026-10-15：删除包内已有的容器条目（只删条目，对象与 preload 都不动）
            if not bundle:
                log("✗ 先「新建 bundle」或在列表里选一个")
                return
            tgt = (a.get("delpath") or "").strip()
            if not tgt:
                log("✗ 先在「包内条目」下拉里选一条（点「刷新条目列表」拉出来）")
                return
            # ★③：删完**自动同步 CRC**（不再只提醒 —— 忘了同步的后果是静默失效 ✗）
            r = remove_asset(bundle, tgt, log=log, all_matching=bool(a.get("delfuzzy")),
                             catalog=workdir_catalog(bundle))
            log("✓ 删掉 %d 条，包内还剩 %d 条容器条目" % (len(r["removed"]), r["kept"]))
            self._refresh_safe()
            self._fill_container_paths()
            return
        if act == "addimg":            # ★ 图片（图标/头像/标签图）：深拷贝骨架 → 换图 → 注册新地址（Texture2D + Sprite 两条）→ CRC
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
        if act == "fixscripts":        # ★ v1.8.117：一键「补悬空脚本」（用户现场："你能不能直接修一下"）
            if not bundle:
                log("✗ 先「新建 bundle」或在列表里选一个")
                return
            # 源包候选：① 用户填的 pack（很可能就是那些 prefab 的来源）② 自动找游戏 units 主包
            # ⛔ 源包猜错的代价**几乎为零**：`fix_scripts` 是 fail-closed 的 ——
            #   源包里没有对应 pathID ⇒ 复查不过 ⇒ 抛错、**原包一字不动** ⇒ 换个候选继续试 ✓
            cands = []
            if a.get("pack") and os.path.isfile(a["pack"]):
                cands.append(a["pack"])
            try:
                from bundle_paths import units_bundle
                u = units_bundle()
                if u and u not in cands:
                    cands.append(u)
            except Exception as e:                                    # noqa: BLE001
                log("（自动找游戏主包失败：%s）" % e)
            if not cands:
                log("✗ 找不到源包 —— 源包 = 那些 prefab 原来所在的包（通常游戏 units 主包）。"
                    "命令行可显式指定：--fix-scripts --source <路径>")
                return
            last = None
            for i, s in enumerate(cands):
                log("—— 试第 %d/%d 个源包：%s" % (i + 1, len(cands), os.path.basename(s)))
                try:
                    r = fix_scripts(bundle, source=s,
                                    catalog=os.path.join(os.path.dirname(os.path.abspath(bundle)),
                                                         "catalog_with_mybundle.json"),
                                    log=log)
                    log("✓ 补悬空脚本完成：缺 %d → 0，复制 %s 个 MonoScript，CRC %s"
                        % (r["missing_before"], r.get("copied", 0),
                           ("已同步" if r["crc"] is not None else "未同步（没有 catalog 副本）")))
                    if not r["changed"] and not r["missing_before"]:
                        log("（本来就没有悬空脚本，什么都没改）")
                    last = None
                    break
                except PermissionError as e:
                    # ⛔ 这不是"源包不对"，是目标包被占用 ⇒ 换源包也没用，直接停 ✗
                    log("✗ 修是修好了，但目标包被占用、没法替换（原包一字未动）：%s" % e)
                    last = None
                    break
                except Exception as e:                                # noqa: BLE001
                    last = e
                    log("  ✗ 这个源包不行：%s" % e)
            if last is not None:
                log("✗ 换遍了 %d 个源包都没修成（最后一次：%s）—— 原包**一字未动** ✓" % (len(cands), last))
                log("  提示：源包要选**这些 prefab 原来所在的那个包**，不是随便一个包")
            self._refresh_safe()
            return
        if act == "oneclick":          # ★② 一键"原键替换"（复制原包 → 换图 → 重指向）
            png = (a.get("oppng") or "").strip()
            pack = (a.get("oppack") or "").strip()
            asset = (a.get("opasset") or "").strip()
            if not png or not pack or not asset:
                log("✗ ③e 那三格要填齐：我的 PNG / 游戏原包 / 原包内资产路径"
                    "（路径用「列出原包里的图」拉出来，别手抄）")
                return
            catcopy = workdir_catalog(pack if pack else a["workdir"])
            if not os.path.exists(catcopy) and cat:
                shutil.copy2(cat, catcopy)
            try:
                r = one_click_replace_original_key(png, pack, asset, a["workdir"], log=log,
                                                   catalog=catcopy if os.path.exists(catcopy) else None)
            except Exception as e:                                    # noqa: BLE001
                log("✗ %s: %s" % (type(e).__name__, e))
                return
            self._refresh_safe()
            self._cur_bundle = r["bundle"]
            return
        if act == "entryrepoint":      # ★⑪ 条目级重指向（不用复制整个 304 MB 肖像包）
            if not bundle:
                log("✗ 先「新建 bundle」或在列表里选一个（= 你那个装着这张图的小包）")
                return
            asset = (a.get("opasset") or "").strip()
            if not asset:
                log("✗ 先填「原包内资产路径」（= 你要替换的那条资产的**原始容器路径**）")
                return
            catcopy = workdir_catalog(bundle)
            if not os.path.exists(catcopy):
                log("✗ 没有 catalog 副本（%s）—— 先在「加资产/加图片」里注册一次地址生成副本" % catcopy)
                return
            try:
                repoint_entry_to_bundle(bundle, catcopy, asset, log=log)
            except Exception as e:                                    # noqa: BLE001
                log("✗ %s: %s" % (type(e).__name__, e))
                return
            return
        if act == "fixstyle":          # ★㉑（[工具-10]）：一键「修 catalog 字段风格」
            if not bundle:
                log("✗ 先「新建 bundle」或在列表里选一个")
                return
            catcopy = workdir_catalog(bundle)
            if not os.path.exists(catcopy):
                log("✗ 工作目录里没有 catalog 副本（%s）—— 先「加资产」注册一次地址，"
                    "或把那要修的 catalog 放到包旁边" % catcopy)
                return
            import my_bundle_catalog as MC
            from catalog_mod import Catalog as _Cat
            # ★ **点击时自门控**（比"按钮置灰"更稳：不依赖"用户先点过自检"这个状态）：
            #   先按 **⑥ 的同一口径**（ref_style ＋ bundle_dir=我们的工作目录）查一遍；
            #   ⛔ 没有风格问题 ⇒ 打印 noop 并**不写盘**（连点两次文件哈希逐字节不变）✓
            #   ⛔ 不许裸调 `verify_catalog(cat)`—— 那样 problems 恒为 0 = **假绿**（★㉑ 本线踩过）✗
            try:
                _ref = MC.find_reference_bundle_options(_Cat(catcopy), log=lambda *_a: None)
                pv = MC.verify_catalog(catcopy, ref_style=(_ref[2] if _ref else None),
                                       bundle_dir=os.path.dirname(os.path.abspath(bundle)))
            except Exception as e:                                    # noqa: BLE001
                log("✗ 先查一遍没查成（%s: %s）⇒ 什么都没改" % (type(e).__name__, e))
                return
            probs = list(pv.get("problems") or [])
            if not probs:
                log("✓ 没有需要修的（⑥ catalog 全量自洽 problems=0）⇒ 什么都没改 ✓")
                return
            log("—— 发现 %d 条问题（字段风格相关 %d 条）："
                % (len(probs), sum(1 for p in probs if "字段风格" in p)))
            for p in probs[:4]:
                log("   · %s" % p)
            # ★★ [工具-10] ②③：先预演、再确认（⛔ 不许静默改）
            r0 = MC.refresh_bundle_entry_style(catcopy, bundle, dry_run=True, log=log)
            if not r0.get("entries") or not r0.get("changed"):
                log("✗ 没改：%s" % r0.get("reason"))
                return
            ok_go = _confirm_style_fix("修 catalog 字段风格",
                                       "将改条目 %s（已逐条列出 旧→新）。确定要改吗？"
                                       % (r0["entries"],))
            if not ok_go:
                log("—— 取消/没确认（含弹不出确认框）⇒ 什么都没改 ✓")
                return
            try:
                r = MC.refresh_bundle_entry_style(catcopy, bundle, in_place=True, log=log)
            except Exception as e:                                    # noqa: BLE001
                log("✗ 修 catalog 字段风格没成（%s: %s）" % (type(e).__name__, e))
                return
            if not r.get("ok"):
                log("✗ 没改：%s" % r.get("reason"))
                return
            self._refresh_safe()          # 副本的大小/mtime 变了 ⇒ 列表刷一下
            log("⚠ 提醒：这份 catalog 副本已改 ⇒ 若之前装过游戏，请再点「安装到游戏（先备份）」同步过去")
            # ⛔ 改完**立刻自证**（与「🔧 补悬空脚本」同一条纪律）：自动复跑 ⑥ ✓
            self._do(dict(a, action="check"), log)
            return
        if act == "cabcheck":          # ★ B 包：改包后自检（内部名/大小 + 谁依赖我动的 CAB）
            if not bundle:
                log("✗ 先「新建 bundle」或在列表里选一个")
                return
            cab_selfcheck(bundle, before=(a.get("cabbefore") or None), log=log)
            log("   （提示：想比「改动前」，可以在命令行用 `--cab-check --before <游戏原包>`；"
                "GUI 里这枚按钮只做「当前内部名 + 谁依赖我动的 CAB」两件事）")
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
            _r = restore_catalog(a["game_root"], log=log)
            if not _r:
                log("（本次**未改动任何文件** ⇒ 想回到原版请走上面的退路①/②）")

    def _refresh_safe(self):
        try:
            self.win.after(0, self._refresh)
        except Exception:                                        # noqa: BLE001
            pass


# ------------------------------------------------------------------ CLI

def _confirm_style_fix(title, prompt):
    r"""确认步的**可注入钩子**（默认弹 Tk 模态框；测试用 monkeypatch 注入 True/False）。

    为什么要留钩子：⛔ 测试里**不许出现"人肉点对话框"**（中枢 2026-09-18 令：GUI 测试一律走非交互路径）。
    约定：**任何异常/弹不出框 ⇒ 返回 False**（fail-closed：宁可不改，⛔ 不静默改）。
    """
    try:
        from tkinter import messagebox as _mb
        return bool(_mb.askyesno(title, prompt))
    except Exception as e:                                              # noqa: BLE001
        # ★ §1 第 11 条项下 (d) 反面：**兜底必打异常并让它可见**（⛔ 不许吞成"看起来正常"）
        print("⚠ 确认框弹不出来（%s: %s）⇒ 按「未确认」处理（fail-closed，不改）" % (type(e).__name__, e))
        return False


def fix_catalog_style_cli(a):
    r"""★㉑ `[工具-10]` 命令行入口：修 catalog 字段风格 —— **默认只预演，`--yes` 才写**。

    ⛔ 不新造检测逻辑：要不要修、修完干不干净，一律复用 ★⑩ 的 `verify_catalog(...)`（同 ⑥ 口径，
       必须带 `ref_style` ＋ `bundle_dir`，否则 problems 恒为 0 = 假绿）。

    退出码：**0** = 已改（`--yes`）或预演显示有可修项 ｜ **1** = 修后自证不过（红）
            ｜ **2** = 拒改（判定键不命中 / 文件不存在 / 缺参） ｜ **3** = 无需改动（本来就合规）
    落盘：每次运行都写 `fix_catalog_style_<时刻>.json`（含 exit_code、逐条 preview、前后三元组、自检结果）
          ＋ `.log`（全部输出行）⇒ 「非 0 退出码」也**落盘留痕** ✓
    """
    import datetime
    import json as _json
    import my_bundle_catalog as MC
    from catalog_mod import Catalog as _Cat

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    lines = []

    def log(m):
        s = str(m)
        lines.append(s)
        print(s)

    def sha16(p):
        import hashlib
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for b in iter(lambda: f.read(1 << 20), b""):
                h.update(b)
        return h.hexdigest()[:16].upper()

    def trio(p):
        if not p or not os.path.isfile(p):
            return None
        return {"path": p, "size": os.path.getsize(p), "sha16": sha16(p),
                "mtime": datetime.datetime.fromtimestamp(os.path.getmtime(p)
                                                        ).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]}

    rdir = a.result_dir or os.path.dirname(os.path.abspath(a.bundle or a.catalog or "."))
    res = {"tool": "my_bundle --fix-catalog-style", "stamp": stamp, "dry_run": not a.yes,
           "in_place": bool(a.in_place), "bundle": a.bundle, "catalog": a.catalog,
           "result_dir": rdir, "exit_code": None, "preview": [], "reason": None,
           "before": None, "after": None, "selfcheck": None}

    def finish(code, reason=None):
        res["exit_code"] = code
        if reason:
            res["reason"] = reason
        try:
            os.makedirs(rdir, exist_ok=True)
            jp = os.path.join(rdir, "fix_catalog_style_%s.json" % stamp)
            lp = os.path.join(rdir, "fix_catalog_style_%s.log" % stamp)
            with open(jp, "w", encoding="utf-8") as f:
                _json.dump(res, f, ensure_ascii=False, indent=1)
            with open(lp, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            print("—— 落盘：%s ｜ %s（exit_code=%d）" % (jp, lp, code))
        except Exception as e:                                          # noqa: BLE001
            print("⚠ 落盘失败（不影响结论）：%s: %s" % (type(e).__name__, e))
        print("—— 退出码 %d%s" % (code, ("（%s）" % reason) if reason else ""))
        return code

    if not a.bundle or not a.catalog:
        return finish(2, "缺 --bundle / --catalog")
    if not os.path.isfile(a.catalog):
        return finish(2, "catalog 不存在：%s" % a.catalog)
    res["before"] = trio(a.catalog)
    ref = None
    try:
        ref = MC.find_reference_bundle_options(_Cat(a.catalog), log=lambda *_x: None)
    except Exception as e:                                              # noqa: BLE001
        log("⚠ 取原生参考条目失败（继续，但 ⑥ 口径会缺 ref_style）：%s" % e)
    # ① 预演（dry-run）：逐条列出将改什么 —— ⛔ 一个字都不写
    r0 = MC.refresh_bundle_entry_style(a.catalog, a.bundle, dry_run=True, log=log)
    res["preview"] = r0.get("preview") or []
    if not r0.get("entries"):
        return finish(2, r0.get("reason") or "判定键不命中 ⇒ 拒改")
    if not r0.get("changed"):
        return finish(3, r0.get("reason") or "条目本来就是原生风格 ⇒ 无需改动（拒绝空改）")
    if not a.yes:
        log("⛔ 未给 `--yes` ⇒ **只预演、没写盘**（确认步：看清楚上面的逐条 diff 再决定）")
        return finish(0, "预演：有 %d 条可修，未写盘" % r0["changed"])
    # ② 确认后执行
    out = None if a.in_place else os.path.join(rdir, "catalog_with_mybundle.json")
    r = MC.refresh_bundle_entry_style(a.catalog, a.bundle, out=out,
                                     in_place=bool(a.in_place), log=log)
    if not r.get("ok"):
        return finish(2, r.get("reason") or "修没成")
    res["after"] = trio(r.get("out"))
    # ③ 判据：修后 ★⑩ 自检必须转干净（同 ⑥ 口径：ref_style ＋ bundle_dir 都要给）
    c1 = MC.verify_catalog(r["out"], ref_style=(ref[2] if ref else None),
                           bundle_dir=os.path.dirname(os.path.abspath(a.bundle)))
    res["selfcheck"] = {"problems": len(c1.get("problems") or []),
                        "problems_text": (c1.get("problems") or [])[:5],
                        "style_note": c1.get("style_note")}
    if c1.get("problems"):
        return finish(1, "修后 ★⑩ 自检仍有 %d 条问题" % len(c1["problems"]))
    log("✓ 修后 ★⑩ 自检（同 ⑥ 口径）problems=0 ⇒ 转干净 ✓")
    return finish(0, "已修 %d 条；自检干净" % r["changed"])


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
    ap.add_argument("--replace-by-name", metavar="PNG目录",                    help="★ **批量就地替换**：目录里每个 PNG 按**文件名**匹配包里的资产名")
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
    # ★ 2026-10-15：删除包内容器条目（以前"我的 bundle"**没有任何删除入口**）
    ap.add_argument("--remove-asset", metavar="容器路径",
                    help="★ 删掉包内**这条容器条目**（对象与 m_PreloadTable 都不动；最安全的瘦身/撤销做法）")
    ap.add_argument("--remove-asset-all", metavar="容器路径",
                    help="★ 同上，但按**末段文件名**批量删（图标/肖像通常是 Texture2D+Sprite 两条同名条目）")
    ap.add_argument("--dry-run", action="store_true", help="配 --remove-asset*：只列要删什么，不写文件")
    ap.add_argument("--bundle")
    ap.add_argument("--pack", help=".bamod 素材包")
    ap.add_argument("--catalog")
    ap.add_argument("--address")
    ap.add_argument("--asset", help="bundle 内资产路径（默认 Assets/Mods/<名字>.prefab）")
    ap.add_argument("--type", default="UnityEngine.GameObject", help="资产类型（用 catalog 那套类名，默认 %(default)s）")
    ap.add_argument("--sync-crc", action="store_true")
    ap.add_argument("--selfcheck", action="store_true")
    ap.add_argument("--fix-catalog-style", action="store_true", dest="fix_catalog_style",
                    help="★㉑：修 catalog 字段风格（默认**只预演**：逐条列出 条目[号] 字段：旧→新；"
                         "⛔ 加 --yes 才写盘）。退出码 0=改了/预演有可修项 · 1=修后自证不过 · "
                         "2=拒改 · 3=无需改动")
    ap.add_argument("--yes", action="store_true",
                    help="配 --fix-catalog-style：确认执行（⛔ 没有它就绝不写盘）")
    ap.add_argument("--in-place", action="store_true", dest="in_place",
                    help="配 --fix-catalog-style：原地改这份 catalog（默认写到结果目录的 "
                         "catalog_with_mybundle.json）")
    ap.add_argument("--result-dir", help="配 --fix-catalog-style：预演/结果 JSON＋日志的落盘目录"
                                        "（默认：包所在目录）")
    ap.add_argument("--fix-scripts", action="store_true",
                    help="★ 补悬空脚本：把 MonoBehaviour 引用、包里却没有的 MonoScript 从源包复制进来"
                         "（等价于 GUI「④ 收尾 → 🔧 补悬空脚本」）")
    ap.add_argument("--check-scripts", action="store_true",
                    help="只查悬空脚本（不改文件）；有悬空返回码 1")
    ap.add_argument("--source-pack", metavar="路径",
                    help="配 --fix-scripts：那些 MonoScript 所在的源包（默认自动找游戏 units 主包）")
    ap.add_argument("--cab-check", action="store_true",
                    help="★ 改包后自检：内部名（CAB）与大小 + 「谁依赖我动的那个 CAB」")
    ap.add_argument("--before", metavar="路径",
                    help="配 --cab-check：**改动前**的同一份包（给就做内部名对比；游戏原包最常见）")
    ap.add_argument("--pc-dir", metavar="路径", help="配 --cab-check：游戏 aa\\PC 目录（默认自动找）")
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
        _root = _require_explicit_game_root(a.game_root, "--restore-catalog")
        if not _root:
            return 2
        _r = restore_catalog(_root, log=print, workdir=a.workdir)
        if not _r:
            return 3          # ★ [★28] 无备份可还原 ⇒ 非零（⛔ 不静默成功）
        return 0
    if a.list_images:
        for p in list_image_assets(a.bundle):
            print(p)
        return 0
    if a.remove_asset or a.remove_asset_all:
        if not a.bundle:
            print("✗ 要删包内条目得给 --bundle <你的包>")
            return 2
        try:
            r = remove_asset(a.bundle, a.remove_asset or a.remove_asset_all,
                             log=print, all_matching=bool(a.remove_asset_all),
                             dry_run=bool(a.dry_run), catalog=a.catalog)
        except (FileNotFoundError, ValueError) as e:
            print("✗ %s: %s" % (type(e).__name__, e))
            return 2
        print("删除 %d 条，保留 %d 条%s"
              % (len(r["removed"]), r["kept"], "（预演，未写文件）" if r["dry_run"] else ""))
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
        cat = a.catalog
        if not cat:
            _gr = _require_explicit_game_root(a.game_root, "--repoint")
            if not _gr:
                return 2
            cat = game_paths(_gr)["catalog"]
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
        cat = a.catalog
        if not cat:
            _gr = _require_explicit_game_root(a.game_root, "--sync-crc")
            if not _gr:
                return 2
            cat = game_paths(_gr)["catalog"]
        sync_crc(a.bundle, cat, log=print)
        return 0
    if a.fix_scripts:
        # ★ v1.8.117：把 GUI 那个「🔧 补悬空脚本」也开成命令行（CI/脚本里能自动跑）✓
        if not a.bundle:
            print("✗ --fix-scripts 要配 --bundle <你的包>")
            return 2
        try:
            r = fix_scripts(a.bundle, source=a.source_pack, catalog=a.catalog, log=print)
        except (FileNotFoundError, RuntimeError, PermissionError) as e:
            print("✗ %s: %s" % (type(e).__name__, e))
            return 2
        print("悬空脚本 %d → %d（复制 %s 个）%s"
              % (r["missing_before"], r["missing_after"], r.get("copied", 0),
                 "，CRC 已同步" if r["crc"] is not None else ""))
        return 0
    if a.cab_check:
        # ★ B 包：改包后自检（内部名/大小 + 谁依赖我动的 CAB）
        if not a.bundle:
            print("✗ --cab-check 要配 --bundle <你的包>")
            return 2
        try:
            r = cab_selfcheck(a.bundle, before=a.before, pc_dir=a.pc_dir, log=print)
        except Exception as e:                                        # noqa: BLE001
            print("✗ %s: %s" % (type(e).__name__, e))
            return 2
        return 0 if r["ok"] else 1
    if a.check_scripts:                # 只查不修（和自检 ③ 同口径，但只打这一条）
        if not a.bundle:
            print("✗ --check-scripts 要配 --bundle <你的包>")
            return 2
        r = missing_scripts(a.bundle)
        print("MonoBehaviour %d 个 · 本包内引用 %d 条 · 跨文件引用 %d 条 · 读不出 %d 个"
              % (r["mb"], r["internal"], r["external"], r["unreadable"]))
        for fname, v in r["by_file"].items():
            print("  内部文件 %s：%d 个引用 · 缺 %d" % (fname, v["need"], len(v["missing"])))
        if r["missing"]:
            print("⛔ 悬空 %d 个：%s" % (len(r["missing"]), sorted(r["missing"])[:8]))
            return 1
        print("✓ 包内脚本自包含")
        return 0
    if a.selfcheck:
        r = selfcheck(a.bundle, a.catalog, a.address, log=print)
        return 0 if r["ok"] else 1
    if a.fix_catalog_style:
        return fix_catalog_style_cli(a)
    if a.install:
        _root = _require_explicit_game_root(a.game_root, "--install")
        if not _root:
            return 2
        install(a.bundle, a.catalog, _root, log=print)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
