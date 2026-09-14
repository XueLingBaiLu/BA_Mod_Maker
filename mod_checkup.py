# -*- coding: utf-8 -*-
r"""**Mod 自查**：改完 DB 之后、进游戏之前，先在这里把"会看不见/游戏会报 invalid"的问题挑出来。

为什么做它（2026-10 第 71 轮）
=============================
这三轮把游戏自己的把关逻辑挖出来了，但结论散在文档里，模组作者改完数据仍然要进游戏撞一遍 ✗：

* **游戏自己会报的错**（日志原文，`.re-kb` 有出处）：
  `[SquadMember] ID={0} PrimaryWeaponId is invalid`、`[SquadWeapon] Duplicate found, firstID={0} otherID={1}`、
  `[WeaponAmmo] ID={0} WeaponId is invalid`… ⇒ 每一条都对应"某个关联字段指向了不存在的 Id"
* **不会报错、但东西就是不出现**（第 70 轮反汇编坐实）：
  * 武器若**没有任何单位引用**（`SquadMembers` 主/副武器、`TurretWeapons`）⇒ 军械库/战场都看不到
    （军械库的武器列表就是 `SquadMembers.SelectMany(→Weapons) ∪ Turrets.SelectMany(→Weapons)`）
  * 单位若**没有 `SpecAvails` 行** ⇒ 不在任何专精的可用列表里、卡组带不进来
* **名字写成了本地化键**（`Weapons.HUDName` 这类字段要写**字面量**）⇒ 界面显示键名

⇒ 本模块把这些检查一次做完，并**给出对应的游戏日志原文**，让作者能对号入座。

怎么用
======
    # ① 先把 DB 导成 JSON（每张表一个文件）
    python 技术资料\scripts\db_table_census.py --data _rev_tools\out\pristine\data.unity3d \
        --pid 52084 --export-dir _rev_tools\out\db_live
    # ② 自查（产品里也有菜单：工具 → Mod 自查…）
    python mod_checkup.py --db-dir _rev_tools\out\db_live
    python mod_checkup.py --data <包> --pid 52084            # 顺手导出再查
    python mod_checkup.py --db-dir <目录> --json out.json    # 机器可读

输出怎么读
==========
* `✗ 错误` = **游戏一定会报 invalid / 该内容一定不出现**，必须改
* `⚠ 提示` = 不一定错，但按第 70/71 轮的定案很可能"看不见"
* 每条都带 **`日志原文`**（游戏里真会打这句话）⇒ 拿它去 `GameLogs` 里对号
"""
import argparse
import glob
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
WS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TABLES_SUBDIR = os.path.join("技术资料", "scripts")

# 关联表 → [(字段, 目标表, 中文说明)]；字段值为 0/None/"" 一律当"无"跳过
RELATIONS = [
    ("SquadMembers", [("UnitId", "Units", "所属单位"),
                      ("PrimaryWeaponId", "Weapons", "主武器"),
                      ("SpecialWeaponId", "Weapons", "副武器")]),
    ("SquadWeapons", [("UnitId", "Units", "所属单位"), ("WeaponId", "Weapons", "武器")]),
    ("WeaponAmmunitions", [("UnitId", "Units", "所属单位"), ("WeaponId", "Weapons", "武器"),
                           ("AmmunitionId", "Ammunitions", "弹药")]),
    ("UnitArmors", [("UnitId", "Units", "所属单位"), ("ArmorId", "Armors", "装甲")]),
    ("UnitPropulsions", [("UnitId", "Units", "所属单位"), ("MobilityId", "Mobility", "机动")]),
    ("SensorUnits", [("UnitId", "Units", "所属单位"), ("SensorId", "Sensors", "传感器")]),
    ("UnitAbilities", [("UnitId", "Units", "所属单位"), ("AbilityId", "Abilities", "能力")]),
    ("Modifications", [("UnitId", "Units", "所属单位")]),
    ("TurretUnits", [("UnitId", "Units", "所属单位"), ("TurretId", "Turrets", "炮塔")]),
    ("TurretWeapons", [("WeaponId", "Weapons", "武器"), ("TurretId", "Turrets", "炮塔")]),
    ("SpecializationAvailabilities", [("UnitId", "Units", "单位"), ("SpecializationId", "Specializations", "专精")]),
    ("TransportAvailabilities", [("UnitId", "Units", "单位"),
                                 ("SpecializationAvailabilityId", "SpecializationAvailabilities", "可用性行")]),
    ("Turrets", [("ParentTurretId", "Turrets", "父炮塔")]),
    ("Mobility", [("FlyPresetId", "FlyPresets", "飞行预设")]),
]
# 游戏**确实**会查重的表（日志里有 `Duplicate found, firstID=… otherID=…` 这句话）
UNIQUE_KEYS = [
    ("SquadWeapons", ("UnitId", "WeaponId")),
    ("UnitPropulsions", ("UnitId", "MobilityId")),
    ("SensorUnits", ("UnitId", "SensorId")),
    ("SpecializationAvailabilities", ("SpecializationId", "UnitId")),
]
NAME_FIELDS = [("Weapons", ("Name", "HUDName")), ("Ammunitions", ("Name", "HUDName")),
               ("Units", ("Name", "HUDName")), ("Modifications", ("Name", "UIName")),
               ("Turrets", ("Name",)), ("Mobility", ("Name",))]


# 游戏日志里的**逐字**模板（来自字面量表实测，见 .re-kb/tools/annotate-asm-and-log-diagnostics.md）
#   table → (日志标签, {DB 字段名: 日志里的字段名}, 去重消息用的标签)
# ⚠ 标签并不总和表名相同：`SquadMembers` 在日志里是 `[SquadMember]`（单数）、
#   `WeaponAmmunitions` 是 `[WeaponAmmo]` 且字段叫 `AmmoId`（不是 AmmunitionId）、
#   `SpecializationAvailabilities` 无效消息是 `[SpecAvail]` 而**去重**消息是 `[SpecAvails]`（复数）✓
LOG = {
    "SquadMembers": ("SquadMember", {}, "SquadMember"),
    "SquadWeapons": ("SquadWeapon", {}, "SquadWeapon"),
    "WeaponAmmunitions": ("WeaponAmmo", {"AmmunitionId": "AmmoId"}, "WeaponAmmo"),
    "UnitArmors": ("UnitArmors", {}, "UnitArmors"),
    "UnitPropulsions": ("UnitPropulsions", {}, "UnitPropulsions"),
    "SensorUnits": ("SensorUnits", {}, "SensorUnits"),
    "UnitAbilities": ("UnitAbility", {}, "UnitAbility"),
    "Modifications": ("Modification", {}, "Modification"),
    "Mobility": ("Mobility", {}, "Mobility"),
    "Turrets": ("Turrets", {}, "Turrets"),
    "TurretUnits": ("TurretUnits", {}, "TurretUnits"),
    "TurretWeapons": ("TurretWeapons", {}, "TurretWeapons"),
    "SpecializationAvailabilities": ("SpecAvail", {"SpecializationId": "SpecId"}, "SpecAvails"),
    "TransportAvailabilities": ("TransportAvail", {"SpecializationAvailabilityId": "SpecAvailId"},
                                "TransportAvail"),
}


def log_invalid(table, rid, field):
    """→ 游戏日志里那句「invalid」的**逐字原文**（代入真实值，方便直接去日志里搜）"""
    tag, fmap, _ = LOG.get(table, (table, {}, table))
    return "[%s] ID=%s %s is invalid" % (tag, rid, fmap.get(field, field))


def log_duplicate(table, first, other):
    tag, _fmap, dup_tag = LOG.get(table, (table, {}, table))
    return "[%s] Duplicate found, firstID=%s otherID=%s" % (dup_tag, first, other)


def load_dir(db_dir):
    """→ {表名: [行 dict]}（缺哪张表就不查哪张，不报错）"""
    out = {}
    for p in glob.glob(os.path.join(db_dir, "*.json")):
        name = os.path.basename(p)[:-5]
        try:
            rows = json.load(open(p, encoding="utf-8"))
        except Exception:                                            # noqa: BLE001
            continue
        if isinstance(rows, dict):                                   # 兼容 {"rows": [...]} 形式
            rows = rows.get("rows", [])
        out[name] = rows
    return out


def _ids(db, table):
    return {r.get("Id") for r in db.get(table, []) if isinstance(r, dict)}


def _empty(v):
    return v in (None, "", 0, "0")


def looks_like_loc_key(v):
    """★ 启发式（配合 `loc_lookup.py --show <值>` 确认）：
    `ui_spec_usmc_name` 这种键 = 小写+下划线且有 ≥3 段；或全大写下划线（`INF_RPG27` 这种**图标名**也是全大写 ⇒ 只提示）
    ⛔ 这不是判据本身，只是一个"值得看一眼"的提示；真判据是 loc_lookup 查得到/查不到 ✓"""
    if not isinstance(v, str) or not v:
        return False
    if v.startswith(("ui_", "loc_", "text_", "desc_")):
        return True
    return v.count("_") >= 3 and v.islower()


def scope_vs_baseline(db, base):
    """→ (scope, stats)：`scope[表] = set(Id)`，只含**新增或改动过**的行。

    ★ 为什么要它：原版 DB 里本来就有 100+ 件"没人用的武器"、20+ 个"没有可用性行的单位"（制作残留），
      全报出来会把真正的问题淹掉 ✗ ⇒ 给一份**原始导出**当基线，只报"你改动的那些行" ✓
    行级判据 = 该行 JSON 序列化后与基线不同（够用且简单；不看字段级 diff）。"""
    scope, stats = {}, {"added": 0, "changed": 0, "gone": 0}
    for table, rows in db.items():
        by_id = {}
        for r in rows:
            if isinstance(r, dict):
                by_id[r.get("Id")] = json.dumps(r, sort_keys=True, ensure_ascii=False)
        old = {}
        for r in base.get(table, []):
            if isinstance(r, dict):
                old[r.get("Id")] = json.dumps(r, sort_keys=True, ensure_ascii=False)
        touched = set()
        for i, blob in by_id.items():
            if i not in old:
                touched.add(i)
                stats["added"] += 1
            elif old[i] != blob:
                touched.add(i)
                stats["changed"] += 1
        for i in old:
            if i not in by_id:
                stats["gone"] += 1
        if touched:
            scope[table] = touched
    return scope, stats


def check(db, log=print, baseline=None):
    """核心检查：→ (issues, stats)

    ⛔ 重要：**导出可能是不全的**（走变长重切改过的包，偏移式读取器只能读到编辑点之前的表）
    ⇒ 缺表时对应检查会被跳过 ⇒ 必须把"跳过了哪些"讲清楚，否则"0 问题"会被误读成"没问题" ✗
    """
    issues = []
    scope, stats = (None, None)
    if baseline:
        scope, stats = scope_vs_baseline(db, baseline)

    def in_scope(table, rid):
        return scope is None or rid in scope.get(table, set())

    def add(sev, table, rid, field, msg, hint="", force=False):
        if sev != "error" and not force and not in_scope(table, rid):
            return
        issues.append({"severity": sev, "table": table, "id": rid, "field": field,
                       "msg": msg, "log_hint": hint})

    # ① 关联存在性（对应游戏自己的 invalid 告警）
    for table, rels in RELATIONS:
        rows = db.get(table)
        if rows is None:
            continue
        for rel_field, target, cn in rels:
            valid = _ids(db, target)
            if not valid:
                continue
            for r in rows:
                v = r.get(rel_field)
                if _empty(v) or v in valid:
                    continue
                add("error", table, r.get("Id"), rel_field,
                    "%s 指向不存在的 %s Id=%s（%s）" % (rel_field, target, v, cn),
                    log_invalid(table, r.get("Id"), rel_field))

    # ② 重复行（只有这 4 张表游戏会查重）
    for table, keys in UNIQUE_KEYS:
        rows = db.get(table)
        if not rows:
            continue
        seen = {}
        for r in rows:
            k = tuple(r.get(x) for x in keys)
            if k in seen:
                add("error", table, r.get("Id"), "+".join(keys),
                    "与 Id=%s **重复**（%s 相同）" % (seen[k], "、".join(keys)),
                    log_duplicate(table, seen[k], r.get("Id")))
            else:
                seen[k] = r.get("Id")

    # ③ 武器有没有被任何单位引用（第 70 轮定案：不被引用 ⇒ 军械库/战场都看不见）
    weapons = {r.get("Id") for r in db.get("Weapons", [])}
    if weapons and db.get("SquadMembers") is not None:
        used = set()
        for r in db.get("SquadMembers", []):
            for f in ("PrimaryWeaponId", "SpecialWeaponId"):
                if not _empty(r.get(f)):
                    used.add(r.get(f))
        for r in db.get("TurretWeapons", []):
            if not _empty(r.get("WeaponId")):
                used.add(r.get("WeaponId"))
        for wid in sorted(weapons - used, key=lambda x: (x is None, x)):
            add("warn", "Weapons", wid, "—",
                "这件武器**没有任何单位/炮塔引用** ⇒ 军械库与战场都不会出现它",
                "（不报错，但按军械库武器列表的构造方式 = 永远看不见）")

    # ④ 单位有没有专精可用性行（没有 ⇒ 带不进卡组）—— 顺带查"有单位引用但没弹药行"的武器（第 31 轮加）
    units = db.get("Units")
    if units and db.get("SpecializationAvailabilities") is not None:
        have = {r.get("UnitId") for r in db.get("SpecializationAvailabilities", [])}
        for r in units:
            if r.get("Id") in have:
                continue
            hidden = r.get("DisplayInArmory") in (False, 0, "False", "false")
            add("warn" if not hidden else "info", "Units", r.get("Id"), "SpecAvails",
                "没有专精可用性行 ⇒ 任何专精都带不进来%s" % ("（该单位 DisplayInArmory=false，可能是有意的）" if hidden else ""),
                "[SpecAvail]（缺行则不报错，但军械库/卡组里没有它）")

    # ⑤ 被引用的武器有没有弹药行（第 31 轮加）——
    #    实测依据：T6 测试件（表尾追加 `Weapons` 行 Id=900，并把 UnitId 113 的成员行指过去）**没有加**
    #    `WeaponAmmunitions` 行 ⇒ 军械库/武器栏会显示新武器，但**弹药为 0**（这就是 T6 的第二个判据）。
    #    游戏自己的告警族里没有"缺弹药行"这条（它是静默的）⇒ 只能离线这样查 ✓
    if weapons and db.get("WeaponAmmunitions") is not None:
        ammo = {r.get("WeaponId") for r in db.get("WeaponAmmunitions", []) if not _empty(r.get("WeaponId"))}
        for wid in sorted(weapons - ammo, key=lambda x: (x is None, x)):
            if wid in used:                      # 只提醒"真会被用到"的那些（没人引用的已在 ③ 报过）
                add("warn", "Weapons", wid, "WeaponAmmunitions",
                    "有单位/炮塔引用它，但**没有任何弹药行**（`WeaponAmmunitions.WeaponId` 找不到它）"
                    " ⇒ 军械库/武器栏会列出它，但**弹药显示 0**（静默，不报错）",
                    "（无日志；对照 T6 实验的第二个判据）")

    # ⑥ 名字像本地化键（提示级；真判据是 loc_lookup.py）
    for table, fields in NAME_FIELDS:
        for r in db.get(table, []):
            for f in fields:
                v = r.get(f)
                if looks_like_loc_key(v):
                    add("info", table, r.get("Id"), f,
                        "值 %r 看起来像**本地化键**；这些字段要写**字面量**（除非它确实是 ui_* 键）" % v,
                        "（显示成键名/空白）")

    return issues, stats


# 这些表缺了会**削弱**检查（不是报错，而是静默跳过）⇒ 单独列出来提醒
IMPORTANT_TABLES = ("SquadMembers", "SquadWeapons", "WeaponAmmunitions", "SpecializationAvailabilities",
                    "Units", "Weapons", "Turrets", "TurretWeapons", "UnitArmors", "UnitPropulsions",
                    "SensorUnits", "UnitAbilities", "Modifications", "TransportAvailabilities")


def report(issues, log=print):
    err = [i for i in issues if i["severity"] == "error"]
    warn = [i for i in issues if i["severity"] == "warn"]
    info = [i for i in issues if i["severity"] == "info"]
    log("=" * 78)
    log("Mod 自查结果：✗ 错误 %d · ⚠ 提示 %d · ℹ 说明 %d" % (len(err), len(warn), len(info)))
    log("=" * 78)
    for tag, group, title in (("✗", err, "错误（游戏会报 invalid / 内容一定不出现）"),
                              ("⚠", warn, "提示（不报错，但很可能「看不见」）"),
                              ("ℹ", info, "说明（值得看一眼）")):
        if not group:
            continue
        log("\n【%s】%d 条" % (title, len(group)))
        for i in group[:60]:
            log("  %s %s Id=%s  %s" % (tag, i["table"], i["id"], i["msg"]))
            if i["log_hint"]:
                log("        对应日志原文：%s" % i["log_hint"])
        if len(group) > 60:
            log("  …（还有 %d 条）" % (len(group) - 60))
    if not issues:
        log("\n✓ 没发现已知问题（关联齐全、无重复、名字是字面量）")
        log("  ⚠ 这只覆盖「能被离线判出来」的那部分：资源地址/图标/模型仍要进游戏看（见 13 号实机清单）")
    return 2 if err else (1 if warn else 0)


def main():
    ap = argparse.ArgumentParser(description="Mod 自查：改完 DB 进游戏前先查一遍")
    ap.add_argument("--db-dir", help="已导出的表 JSON 目录（每张表一个 .json）")
    ap.add_argument("--baseline", help="★ 原始导出目录（同格式）当基线：提示级只报你新增/改过的行")
    ap.add_argument("--all", action="store_true", help="连原版残留也全报（默认给 baseline 时只报改动）")
    ap.add_argument("--data", help="★ 直接给 data.unity3d（会用**按内容扫**的方式导出全部表，见下）")
    ap.add_argument("--baseline-package", help="★ 直接给**原版** data.unity3d 当基线（同样按内容扫导出）")
    ap.add_argument("--pid", type=int, default=52084, help="要导出/检查的 DataBaseCompiled pathID（默认 52084 = 游戏实际加载那份）")
    ap.add_argument("--json", help="把结果写成 JSON")
    a = ap.parse_args()

    import subprocess
    # ⛔ 2026-10 第 33 轮踩到：这些目录以前写成**相对路径** ⇒ 按**调用者的 cwd** 解析 ✗
    #   ⇒ 从别的目录调用（例如回归套件）就报「没有这个目录」；一律用**基于模块位置**的绝对路径 ✓

    def export_pkg(pkg, out):
        """★ 用 **按内容扫** 的 `db_tables_scan.py --export-dir`（**不是**偏移式的 db_table_census ✗）
        理由（第 31/32 轮实测）：走**变长重切**改过的包（如加行件），偏移式导出只能读到编辑点之前的表
        —— **24 张只剩 12 张** ⇒ 缺表会让检查静默跳过、"0 问题"被误读成"没问题" ✗✗"""
        scanner = os.path.join(WS, "技术资料", "scripts", "db_tables_scan.py")
        if not os.path.isfile(scanner):
            print("✗ 找不到 %s（无法按内容导出全量表）" % scanner)
            return False
        cmd = [sys.executable, scanner, pkg, "--export-dir", out, "--copy", "2" if a.pid == 52084 else "1"]
        print("导出中（按内容扫，能拿全 24 张）：%s" % " ".join(cmd[1:]))
        r = subprocess.run(cmd, cwd=WS)
        return r.returncode in (0, 1)              # 该工具"有表解不开"时返回 1，但已导出的表仍可用

    db_dir = a.db_dir
    if not db_dir:
        if not a.data:
            print(__doc__)
            return 2
        db_dir = os.path.join(WS, "_rev_tools", "out", "checkup_export")   # ★ 绝对路径（见下）
        if not export_pkg(a.data, db_dir):
            print("✗ 导出失败")
            return 2
    base_dir = a.baseline
    if not base_dir and a.baseline_package:
        base_dir = os.path.join(WS, "_rev_tools", "out", "checkup_baseline")   # ★ 绝对路径（见下）
        if not export_pkg(a.baseline_package, base_dir):
            print("✗ 基线导出失败")
            return 2
    if not os.path.isdir(db_dir):
        print("✗ 没有这个目录：%s" % db_dir)
        return 2
    db = load_dir(db_dir)
    if not db:
        print("✗ %s 里一个表 JSON 都没有" % db_dir)
        return 2
    print("读入 %d 张表：%s" % (len(db), ", ".join("%s(%d)" % (k, len(v)) for k, v in sorted(db.items()))))
    missing = [t for t in IMPORTANT_TABLES if t not in db]
    if missing:
        print("⛔ **这次导出不全**（缺 %d 张关键表）：%s" % (len(missing), ", ".join(missing)))
        print("   ⇒ 涉及这些表的检查会被**静默跳过**，所以下面的「0 问题」**不能**当作「没问题」✗")
        print("   为什么会缺：走**变长重切**改过的包（如加行件）——偏移式导出只能读到编辑点之前的表（实测 24→12）。")
        print("   ★ 正确导法（**按内容**扫，能拿全 24 张）：")
        print("     python 技术资料\\scripts\\db_tables_scan.py <包> --export-dir _rev_tools\\out\\db_full")
        print("     python 工具制作资源\\BA_Mod_Maker\\mod_checkup.py --db-dir _rev_tools\\out\\db_full "
              "--baseline _rev_tools\\out\\db_pristine")
        print("   （实测：T6 加行件的包用这条能拿全 24 张，并准确报出「新增 1 行 + 改动 9 行」✓）")
    base = None
    if base_dir and not a.all:
        if not os.path.isdir(base_dir):
            print("✗ 基线目录不存在：%s" % base_dir)
            return 2
        base = load_dir(base_dir)
        print("基线：%s（%d 张表）" % (base_dir, len(base)))
    issues, stats = check(db, baseline=base)
    if stats:
        print("与你改动的关系：新增 %d 行 · 改动 %d 行 · 消失 %d 行（提示级只报这些行）"
              % (stats["added"], stats["changed"], stats["gone"]))
    rc = report(issues)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(issues, f, ensure_ascii=False, indent=1)
        print("\n结果已写入 %s" % a.json)
    return rc


if __name__ == "__main__":
    sys.exit(main())
