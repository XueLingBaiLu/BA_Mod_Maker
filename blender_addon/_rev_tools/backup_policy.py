# -*- coding: utf-8 -*-
r"""自动备份开关 —— BA_Mod_Maker 与 Blender 工具**共用同一处配置**。

⛔ 为什么要有这个模块（v1.8.75）：
   以前导入 .bamod / 写回 bundle / 导入数据库时**无条件**把目标文件复制一份
   `<文件>.bak`，而这些目标全是 GB 级的大文件，实测用户机器上被白占 6.95 GB：

       data.unity3d                  13.67 GB  -> .bak 6.63 GB（旧版残留）
       units_assets_all_*.bundle      3.42 GB  -> .bak 3.42 GB
       unitportraits_assets_*.bundle  0.30 GB
       unitlabels_assets_*.bundle     0.02 GB
       catalog.json                   4.1 MB

   用户的诉求很直接：**别自动备份，占空间**。
   所以现在默认**不生成任何备份**，只保留那条零成本的安全线 —— 先把新内容写到
   `.new` / `.tmp`，全部成功后再 `os.replace` **原子替换**（中途异常/断电只会留下
   临时文件，**原文件绝不会被截断**）。想额外留回滚点的人自己勾开关。

配置来源（优先级从高到低）：
   1. 环境变量 ``BA_AUTO_BACKUP=1|0``（临时覆盖，测试/命令行用）
   2. ``%APPDATA%\\BA Mod Maker\\settings.json`` 的 ``auto_backup`` 键（默认 False）

用法（各写入点统一这么调）::

    from backup_policy import maybe_backup
    bak = maybe_backup(bundle_path, log=step)      # 关着就什么都不做，返回 None

⛔ 不做备份之后谁保证安全：`import_pack` / `stream_save` / `ba_db_tool` 的写入
   路径全都是「临时文件 + os.replace 原子替换」，这条线**不受本开关影响**。
   本开关只决定「要不要额外多留一份完整副本」。
"""
import os

APP_NAME = "BA Mod Maker"
SETTINGS_KEY = "auto_backup"
ENV_VAR = "BA_AUTO_BACKUP"

_OVERRIDE = None        # 进程内缓存（UI 勾选后立刻生效，不必重启）


def settings_path():
    """BA_Mod_Maker 的设置文件（与 ba_db_tool.settings_path 保持一致）。"""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_NAME, "settings.json")


def _env_value():
    v = os.environ.get(ENV_VAR)
    if v is None:
        return None
    v = v.strip().lower()
    if v == "":
        return None
    return v in ("1", "true", "yes", "on", "y")


def backup_enabled():
    """是否要额外留 `.bak` 副本。默认 **False**（不备份）。"""
    if _OVERRIDE is not None:
        return bool(_OVERRIDE)
    env = _env_value()
    if env is not None:
        return env
    try:
        import json
        with open(settings_path(), "r", encoding="utf-8") as f:
            return bool(json.load(f).get(SETTINGS_KEY, False))
    except Exception:
        return False


def set_backup_enabled(on, persist=True):
    """打开/关闭自动备份。persist=True 时写进 settings.json（两个工具共享）。"""
    global _OVERRIDE
    _OVERRIDE = bool(on)
    if not persist:
        return _OVERRIDE
    try:
        import json
        p = settings_path()
        data = {}
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f) or {}
            except Exception:
                data = {}
        data[SETTINGS_KEY] = bool(on)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, p)
    except Exception as e:  # noqa: BLE001
        print("[设置] 自动备份开关保存失败（%s: %s）—— 本次设置不会被记住" % (type(e).__name__, e))
    return _OVERRIDE


def state_text():
    return "开（会留 .bak 副本）" if backup_enabled() else "关（不留 .bak 副本）"


def maybe_backup(path, log=None, label=None):
    """按开关决定要不要把 `path` 复制成 `path + ".bak"`。

    返回备份文件路径；没备份（开关关着 / 源不存在 / 已有备份）时返回 None。
    绝不抛异常 —— 备份失败不该让整个导入流程挂掉。
    """
    def say(msg):
        if log:
            try:
                log(msg)
            except Exception:
                pass
    if not path or not os.path.isfile(path):
        return None
    name = label or os.path.basename(path)
    if not backup_enabled():
        say("未备份 %s（自动备份已关闭 ·『文件』菜单可开启）" % name)
        return None
    bak = path + ".bak"
    if os.path.exists(bak):
        say("已有备份 %s，跳过" % os.path.basename(bak))
        return bak
    try:
        import shutil
        say("备份 %s -> %s" % (name, os.path.basename(bak)))
        shutil.copy2(path, bak)
        return bak
    except OSError as e:
        say("⚠ 备份失败（继续，不影响写入）：%s" % e)
        return None


# ---------------------------------------------------------------------------
# 清理已有备份（把历史上自动生成的 .bak 收回来）
# ---------------------------------------------------------------------------

# 只认这些后缀，避免误删用户自己起名的备份
_BAK_SUFFIXES = (".bak", ".bak_crc", ".bak_icons")


def _bak_of(path):
    for suf in _BAK_SUFFIXES:
        p = path + suf
        if os.path.isfile(p):
            return p
    return None


def find_backups(paths):
    """在给定文件旁边找自动生成的备份。返回 [(bak_path, size), ...]。"""
    found, seen = [], set()
    for p in paths:
        if not p:
            continue
        b = _bak_of(p)
        if b and os.path.normcase(b) not in seen:
            seen.add(os.path.normcase(b))
            try:
                found.append((b, os.path.getsize(b)))
            except OSError:
                pass
    return found


def purge_backups(paths, log=None):
    """删除自动生成的备份。返回 (删除个数, 释放字节数)。

    ⛔ 两种入参**都收**（v1.8.75 实测踩到：只收原文件路径的话，
       `scan_tree_backups` 的返回值直接喂进来会「报删除 0 个、文件却还在」✗）：
         · 原文件路径   `.../units.bundle`      ⇒ 删旁边的 `.../units.bundle.bak`
         · 备份文件路径 `.../units.bundle.bak`  ⇒ 直接删它自己

    ⛔ 只删名字**落在白名单后缀**上的文件 —— 不递归、不按通配符扫全盘，
       避免把用户手动另存的东西一起删掉。
    """
    def say(msg):
        if log:
            try:
                log(msg)
            except Exception:
                pass
    targets, seen = [], set()
    for p in paths:
        if not p:
            continue
        cands = []
        if p.lower().endswith(_BAK_SUFFIXES):
            cands.append(p)              # 入参本身就是备份文件
        else:
            b = _bak_of(p)               # 入参是原文件，找它旁边的备份
            if b:
                cands.append(b)
        for c in cands:
            k = os.path.normcase(c)
            if k in seen or not os.path.isfile(c):
                continue
            seen.add(k)
            targets.append(c)
    n, freed = 0, 0
    for b in targets:
        try:
            size = os.path.getsize(b)
            os.remove(b)
            n += 1
            freed += size
            say("已删除 %s（%.2f GB）" % (b, size / (1024.0 ** 3)))
        except OSError as e:
            say("⚠ 删除失败 %s：%s" % (b, e))
    return n, freed


def scan_tree_backups(root, max_files=4000):
    """兜底扫描：在 root 下找自动生成的备份文件（返回的**就是备份文件本身**的路径）。

    给『清理自动备份』菜单用（用户可能连游戏目录都换了，靠记录找不到）。

    ⛔ 白名单里**故意没有 `.bank.bak`**：音库那个 `.bak` 不是备份，是
       「重复添加音效时的重建基底」（见 audio_bank.py）—— 删了下次加音效就废 ✗
    """
    out, seen = [], set()
    hits = (".bundle.bak", ".unity3d.bak", ".json.bak",
            ".bak_crc", ".bak_icons")
    cnt = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__")]
        for fn in filenames:
            if not fn.lower().endswith(hits):
                continue
            cnt += 1
            if cnt > max_files:
                return out
            p = os.path.join(dirpath, fn)
            k = os.path.normcase(p)
            if k in seen:
                continue
            seen.add(k)
            try:
                out.append((p, os.path.getsize(p)))
            except OSError:
                pass
    return out
