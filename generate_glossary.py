# -*- coding: utf-8 -*-
"""Generate the Broken Arrow database glossary (数据库词典.md + database_glossary.json).

v1.6.0 起的数据源（单一事实来源）：
  - ba_glossary.py              表/字段/枚举的中英俄释义（GUI 悬浮提示与词典窗口同源，
                                枚举为 dump.cs 实证值）
  - blender_addon/mount_dict.py 挂载点分类、组件
  - clean_baseline.json         行数统计（相对脚本目录）
  - 本文档内的 DATA_STRUCTURES / ADDRESS_MAP / CONSTANTS / RULES
    —— 数据结构、地址映射、常量与防崩溃铁律（原技术总结/教程知识全部收纳于此）

用法：python generate_glossary.py
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "blender_addon"))
# ★ v1.8.118：`bundle_paths` 在 `_rev_tools\` 下 ⇒ 目录名以 `_` 开头，**必须显式加进 sys.path**
#   （第一版漏了它 ⇒ 运行时解析永远失败、又退回到写死的名字 ✗）
sys.path.insert(0, os.path.join(HERE, "_rev_tools"))


import ba_glossary as BG

with open(os.path.join(HERE, "clean_baseline.json"), "r", encoding="utf-8-sig") as f:
    DATA = json.load(f)


def c(s):
    return "`" + s + "`"


# 词典内表顺序（与历史版本一致）
TABLE_ORDER = [
    "Countries", "Units", "Weapons", "Ammunitions", "Armors", "Mobility", "FlyPresets",
    "Sensors", "Turrets", "Abilities", "Modifications", "Options", "Specializations",
    "UnitAbilities", "UnitPropulsions", "UnitArmors", "SensorUnits", "TurretUnits",
    "TurretWeapons", "WeaponAmmunitions", "SquadMembers", "SquadWeapons",
    "SpecializationAvailabilities", "TransportAvailabilities",
]

# ---------------------------------------------------------------------------
# 数据结构与文件格式（原技术总结 + 逆向知识库收纳）
# ---------------------------------------------------------------------------
# ★ 2026-10：`DATA_STRUCTURES` / `ADDRESS_MAP` / `ADDRESS_RULES` / `CONSTANTS` / `RULES` 的**正文**
#   已**并入 `ba_glossary.py` 的知识层**（= 内置词典；GUI 词典窗口「格式与要点」页读的就是它，
#   分类见 `BG.KNOW_LAYERS`：字段层 / 引用层 / 格式层 + ★ 跨表 + 附录·铁律）。
#   ⇒ 本文件从此只负责**渲染**：`BG.KNOW_SECTIONS`（格式层 + 铁律）、`BG.CONSTANTS`（经
#     `BG.resolve_constants()` 现场解析占位符）、`BG.ADDRESS_MAP` / `BG.ADDRESS_RULES`、
#     `BG.MOUNT_CATEGORIES` / `BG.COMPONENTS`、`BG.REFS`（引用层）、`BG.TROUBLESHOOT`（故障排查）。
#   ⛔ 别把正文写回这里 —— 写回来就又变成"两份"，两边一定会分叉 ✗


def field_type(rows, name):
    for r in rows:
        if isinstance(r, dict) and r.get(name) not in (None, ""):
            return type(r.get(name)).__name__
    return "unknown"


def zh_field(table, name):
    entry = BG.FIELDS.get(table, {}).get(name) or BG.COMMON.get(name)
    if entry:
        pair = entry.get("zh") or entry.get("en")
        if pair and isinstance(pair, (list, tuple)) and len(pair) >= 1:
            return (pair[0] or name, pair[1] if len(pair) > 1 else "")
    return (name, "")


def enum_lines():
    lines = ["## 枚举值对照", ""]
    for fname in sorted(BG.ENUMS):
        note = BG.enum_note(fname, "zh")
        members = BG.enum_members(fname)
        is_mask = BG.enum_is_mask(fname)
        lines.append("### " + fname)
        lines.append("")
        if is_mask:
            lines.append("**位掩码（加法合成）** —— 值按位相加，例如 "
                         "`36 = 32(舰船) + 4(载具)`；工具会把掩码自动拆成种类显示。")
            lines.append("")
        if note:
            # 「真实作用」说明（v1.8.55 起：Units.Role / Units.Type / Units.CategoryType 等）
            lines.append("> " + note)
            lines.append("")
        if members:
            lines.append("| 值 | 含义 | 枚举成员 (dump.cs) |")
            lines.append("|---|---|---|")
        else:
            lines.append("| 值 | 含义 |")
            lines.append("|---|---|")
        for item in BG.ENUMS[fname]:
            if not isinstance(item, (list, tuple)) or not item:
                continue
            val = item[0]
            meaning = item[1] if len(item) > 1 else ""
            if members:
                lines.append("| " + c(str(val)) + " | " + str(meaning)
                             + " | " + c(members.get(str(val), "")) + " |")
            else:
                lines.append("| " + c(str(val)) + " | " + str(meaning) + " |")
        lines.append("")
    return lines


def format_lines():
    """## 数据结构与文件格式 —— 正文来自 `BG.KNOW_SECTIONS`（layer="format"）"""
    lines = ["## 数据结构与文件格式", ""]
    for s in BG.KNOW_SECTIONS:
        if s["layer"] != "format":
            continue
        lines.append("### " + s["title"])
        lines.append("")
        lines.append(s["body"])
        lines.append("")
    return lines


def rules_lines():
    """## 防崩溃铁律与已知坑 —— 正文来自 `BG.KNOW_SECTIONS`（layer="rules"）"""
    lines = ["## 防崩溃铁律与已知坑", ""]
    for s in BG.KNOW_SECTIONS:
        if s["layer"] != "rules":
            continue
        lines.append("### " + s["title"])
        lines.append("")
        lines.append(s["body"])
        lines.append("")
    return lines


def layers_lines():
    """## 词典分层（怎么找东西）—— 规格卡 `.re-kb\\tools\\db-glossary-spec.md` §1 的三层 + ★ 跨表。"""
    lines = ["## 词典分层（怎么找东西）", ""]
    lines.append("> 这份词典按**四类**组织（与内置词典「词典 / 格式与要点 / 故障排查」页一一对应）："
                 "**要改哪个字段**看「字段层」；**值是指向哪儿的**看「引用层」；"
                 "**文件格式长什么样**看「格式层」；**两张表怎么对上**看「★ 跨表」。")
    lines.append("")
    lines.append("| 层 | 放什么 | 在哪看 |")
    lines.append("|---|---|---|")
    where = {"fields": "本文档各表的「字段」小节（GUI：词典「数据库词典」页）",
             "refs": "本文档「引用层」节（GUI：词典「格式与要点」页）",
             "format": "本文档「数据结构与文件格式」节（GUI：同上）",
             "xref": "本文档「地址映射与内部路径」「挂载点与组件词典」节（GUI：同上）",
             "rules": "本文档「防崩溃铁律与已知坑」节（GUI：同上）",
             "trouble": "本文档「故障排查」节（GUI：词典「故障排查」页）"}
    for key, name, desc in BG.KNOW_LAYERS:
        lines.append("| **" + name + "** | " + desc + " | " + where.get(key, "") + " |")
    lines.append("")
    return lines


def refs_lines():
    """## 引用层（字段值引用了什么）—— 数据来自 `BG.REFS` / `BG.REF_KINDS`。

    ★ 这是规格卡 §1 ② 那一层：用户改一个字段前，先要知道这个值是**键**还是**字面量**还是**外键**
      （搞错就会"改了没反应"）。
    """
    lines = ["## 引用层（字段值引用了什么）", ""]
    lines.append("> ⛔ 「地址」这个词有三层，别混：DB 字段值（= Addressables **键**）／"
                 "`Assets/…`（**内部路径**）／`{RuntimePath}\\PC\\x.bundle`（**bundle 文件**）。"
                 "**改键才是换资产**；改内部路径无效。")
    lines.append("")
    lines.append("| 字段 | 引用类型 | 指向什么 | 反推公式（值 ⇒ 资产路径） | 产品内入口 | 判据（取证命令） |")
    lines.append("|---|---|---|---|---|---|")
    for (tbl, fld), info in BG.REFS.items():
        lines.append("| " + c(tbl + "." + fld) + " | " + info["kind"] + " | " + info["target"]
                     + " | " + (info.get("formula") or "—")
                     + " | " + info["probe"] + " | " + c(info["judge"]) + " |")
    lines.append("")
    lines.append("### 引用类型（kind）含义")
    lines.append("")
    lines.append("| kind | 含义 |")
    lines.append("|---|---|")
    for k, v in BG.REF_KINDS.items():
        lines.append("| " + c(k) + " | " + v + " |")
    lines.append("")
    return lines


def address_lines():
    lines = ["## 地址映射与内部路径", ""]
    for ent in BG.ADDRESS_MAP:
        lines.append("### " + ent["title"])
        lines.append("")
        lines.append("| 类型 | 地址（数据表填） | 内部路径（bundle 内） |")
        lines.append("|---|---|---|")
        for kind, addr, path in ent["rows"]:
            lines.append("| " + kind + " | " + c(addr) + " | " + c(path) + " |")
        lines.append("")
    lines.append(BG.ADDRESS_RULES)
    return lines


def constants_lines():
    lines = ["## 常量速查", ""]
    lines.append("| 常量 | 值 | 用途 |")
    lines.append("|---|---|---|")
    for name, value, use in BG.resolve_constants():     # ★ 占位符在这里现场解析（包名/体积随版本变）
        lines.append("| " + name + " | " + c(value) + " | " + use + " |")
    lines.append("")
    return lines


def mounts_lines():
    lines = ["## 挂载点与组件词典", ""]
    lines.append("（数据源 `blender_addon/mount_dict.py`，已并入内置词典 `ba_glossary.MOUNT_CATEGORIES` /"
                 " `COMPONENTS`；全游戏 690 名扫描归纳（历史统计）。）")
    lines.append("")
    lines.append("### 挂载点分类")
    lines.append("")
    lines.append("| 分类 | 说明 |")
    lines.append("|---|---|")
    for cat in BG.MOUNT_CATEGORIES:
        lines.append("| " + cat["name"] + " | " + cat["desc"] + " |")
    lines.append("")
    lines.append("高频名称：shell_spawn_0(1083)、weapon_0(964)、VFX_point(739)、root(676)、"
                "body(672)、turret_0(494)、death(198)、turret_1(174)、dust_point_left/right(169)、"
                "exhaust(148)、shell_spawn_0_0(131)、VFXPoint_OnSmokeAbility(127)、recoil_0_0(96)、"
                "weapon_0_1(91)（全游戏扫描频次）。")
    lines.append("")
    lines.append("### 组件（%d 条）" % len(BG.COMPONENTS))
    lines.append("")
    lines.append("| 组件 | 类别 | 关键字段 |")
    lines.append("|---|---|---|")
    for nm, info in sorted(BG.COMPONENTS.items()):
        fields = info.get("fields", "")
        lines.append("| " + nm + " | " + info.get("cat", "") + " | " + fields + " |")

    return lines


def troubleshoot_lines():
    r"""## 故障排查（症状行 → 真因行）—— ★ 数据来自 `ba_glossary.TROUBLESHOOT`（单一来源）。

    为什么要有这一节（2026-10 用户点名 ⑧ 日志可归因）：游戏绝大多数"模组坏了"的表现**只是日志里一句话**，
    作者拿着英文原文不知道该改哪儿；知识侧已经把「症状行 ↔ 真因行」成对核过（每对带出处），
    这里把它做成**用户看得到的产品文档** ✓
    ⛔ 光贴英文原文不算完成 —— 每行都给**中文真因 + 怎么改**。
    """
    pairs = BG.troubleshoot_pairs("zh")
    logp = [(i, p) for i, p in enumerate(pairs, 1) if not p[4]]
    sil = [(i, p) for i, p in enumerate(pairs, 1) if p[4]]
    lines = ["## 故障排查（症状行 → 真因行）", ""]
    lines.append("> ★ **怎么用**：先打开 `GameLogs\\Gamelog__*.log`，拿下表**左列的原文片段**去搜（左列就是"
                 "可直接搜的日志原文）⇒ 命中后看**同一行右侧的真因**，那才是要改的地方。")
    lines.append("> ⚠ 同一句文案可能由**多个调用点**发出 ⇒ 真要下「就是这个原因」的结论时，按「出处」列的"
                 "类/方法名复核（本表**不写 RVA**：地址随游戏更新漂移，对改 mod 也没用）。")
    lines.append("")
    lines.append("### 日志里搜得到原文的（%d 对）" % len(logp))
    lines.append("")
    lines.append("| # | 症状行（日志原文，可直接搜） | 真因（怎么改） | 出处 |")
    lines.append("|---|---|---|---|")
    for i, (sym, fix, src, probe, _s) in logp:
        cell = fix + (("　⇒ **产品内入口**：" + probe) if probe else "")
        # ⛔ 用 .format 而不是 % —— 表格单元格里会出现 `%`（概率/百分比类文案）时 %-格式化会炸
        lines.append("| {} | {} | {} | {} |".format(i, c(sym), cell, src))
    lines.append("")
    lines.append("### ⚠ 日志里**查不到**的静默症状（%d 对 —— 游戏不报错，靠现象认）" % len(sil))
    lines.append("")
    lines.append("| # | 现象（**日志里一条都没有**） | 真因（怎么改） | 出处 |")
    lines.append("|---|---|---|---|")
    for i, (sym, fix, src, probe, _s) in sil:
        lines.append("| {} | {} | {} | {} |".format(i, sym, fix, src))
    lines.append("")
    lines.append("**产品内能直接跑的自查**（不用装源码包）：")
    lines.append("")
    for _t, _f, _s, probe, _sl in pairs:
        if probe:
            lines.append("- " + probe)
    lines.append("")
    lines.append("**用词规矩（本表口径）**：「地址」有三层 —— DB 字段值（= Addressables **键**）／"
                 "`Assets/…`（**内部路径**）／`{RuntimePath}\\PC\\x.bundle`（**bundle 文件**）。"
                 "改键才是换资产，改内部路径无效。")
    lines.append("")
    return lines


def build_md():
    md = ["# Broken Arrow 数据库词典 (Database Glossary)", ""]
    md.append("本文档由 clean_baseline.json（解密后的干净数据库）自动生成，")
    md.append("覆盖全部 24 张表、所有字段、外键关系与枚举值，")
    md.append("并收纳数据结构、地址映射、常量、防崩溃铁律与**故障排查**（原教程/技术总结内容全部并入本文档）。")
    md.append("")
    md.append("> ★ 生成命令：`python 工具制作资源\\BA_Mod_Maker\\generate_glossary.py`"
              "（⛔ 别手改本文档 —— 正文的**单一来源**是 `ba_glossary.py`，本文件只负责渲染）。")
    md.append("")

    md += layers_lines()

    md.append("## 数据总览")
    md.append("")
    md.append("| 表名 | 中文 | 行数 | 说明 |")
    md.append("|---|---|---:|---|")
    for t in TABLE_ORDER:
        if t not in BG.TABLES:
            continue
        zh, desc = BG.table_info(t, "zh")
        n = len(DATA.get(t, []))
        md.append("| " + c(t) + " | " + zh + " | " + str(n) + " | " + desc + " |")
    md.append("")

    # 每表字段
    for t in TABLE_ORDER:
        if t not in BG.TABLES or t not in DATA:
            continue
        zh, desc = BG.table_info(t, "zh")
        rows = DATA[t]
        md.append("## " + t + "（" + zh + "）")
        md.append("")
        md.append("> " + desc + " （行数：" + str(len(rows)) + "）")
        md.append("")
        md.append("| 字段 | 类型 | 含义 |")
        md.append("|---|---|---|")
        seen = []
        for r in rows:
            if isinstance(r, dict):
                for k in r.keys():
                    if k not in seen:
                        seen.append(k)
        for name in seen:
            zhname, fdesc = zh_field(t, name)
            ft = field_type(rows, name)
            cell = zhname + ("：" + fdesc if fdesc else "")
            md.append("| " + c(name) + " | " + ft + " | " + cell + " |")
        md.append("")

    md += format_lines()                # ★ 格式层（正文在 ba_glossary.KNOW_SECTIONS）
    md += enum_lines()
    md += refs_lines()                  # ★ 引用层（规格卡 §1 ②，本次并入的新层）
    md += address_lines()
    md += constants_lines()
    md += mounts_lines()
    md += rules_lines()                 # ★ 附录·铁律（正文在 ba_glossary.KNOW_SECTIONS）
    md += troubleshoot_lines()          # ★ ⑧ 日志可归因：症状行 ↔ 真因行（产品文档侧）
    return "\n".join(md)


def main():
    text = build_md()
    out_md = os.path.join(HERE, "数据库词典.md")
    with open(out_md, "w", encoding="utf-8") as f:
        f.write(text)

    # JSON 版（zh）
    gloss = {"tables": {}}
    for t in TABLE_ORDER:
        if t not in BG.TABLES or t not in DATA:
            continue
        zh, desc = BG.table_info(t, "zh")
        rows = DATA[t]
        seen = []
        for r in rows:
            if isinstance(r, dict):
                for k in r.keys():
                    if k not in seen:
                        seen.append(k)
        fields = {}
        for name in seen:
            zhname, fdesc = zh_field(t, name)
            fields[name] = {"zh": zhname, "desc": fdesc, "type": field_type(rows, name)}
        gloss["tables"][t] = {"zh": zh, "desc": desc, "row_count": len(rows), "fields": fields}
    gloss["enums"] = {
        k: [{"value": str(item[0]), "meaning": (item[1] if len(item) > 1 else ""),
             "member": (str(item[4]) if len(item) > 4 and item[4] else "")}
            for item in BG.ENUMS[k] if isinstance(item, (list, tuple)) and item]
        for k in BG.ENUMS
    }
    # 每个枚举的「真实作用」说明（GUI 绿色箭头弹窗 / 词典窗口取用）
    gloss["enum_notes"] = {k: BG.enum_note(k, "zh") for k in BG.ENUMS if BG.enum_note(k, "zh")}
    gloss["enum_notes_en"] = {k: BG.enum_note(k, "en") for k in BG.ENUMS if BG.enum_note(k, "en")}
    gloss["enum_notes_ru"] = {k: BG.enum_note(k, "ru") for k in BG.ENUMS if BG.enum_note(k, "ru")}
    # 位掩码型枚举（值按位相加，如 Ammunitions.TargetType：36 = 32 舰船 + 4 载具）
    gloss["enum_masks"] = sorted(k for k in BG.ENUMS if BG.enum_is_mask(k))
    gloss["constants"] = [{"name": n, "value": v, "use": u} for n, v, u in BG.resolve_constants()]
    gloss["mount_categories"] = [{"id": x["id"], "name": x["name"], "desc": x["desc"]}
                                 for x in BG.MOUNT_CATEGORIES]
    gloss["components"] = BG.COMPONENTS
    # ★ 知识层（并入的内置词典内容）：分层说明 + 格式层/铁律正文 + ★ 跨表
    gloss["know_layers"] = [{"id": k, "name": n, "desc": d} for k, n, d in BG.KNOW_LAYERS]
    gloss["know_sections"] = [{"id": s["id"], "layer": s["layer"], "title": s["title"],
                               "body": s["body"]} for s in BG.KNOW_SECTIONS]
    gloss["address_map"] = BG.ADDRESS_MAP
    gloss["address_rules"] = BG.ADDRESS_RULES
    # ★ 引用层（规格卡 §1 ②）：字段值到底引用了什么 + 去哪查
    gloss["ref_kinds"] = BG.REF_KINDS
    gloss["refs"] = [{"table": t, "field": f, "kind": v["kind"], "target": v["target"],
                      "probe": v["probe"], "judge": v["judge"]}
                     for (t, f), v in BG.REFS.items()]
    # ★ ⑧ 日志可归因：症状行 ↔ 真因行（三语；GUI「故障排查」页与本 md 同名节同源）
    gloss["troubleshoot"] = [
        {"id": i + 1,
         "symptom": it.get("symptom", ""),
         "silent": it.get("kind") == "silent",
         "fix": it.get("fix") or {},
         "src": it.get("src", ""),
         "probe": it.get("probe", "")}
        for i, it in enumerate(BG.TROUBLESHOOT)
    ]
    with open(os.path.join(HERE, "database_glossary.json"), "w", encoding="utf-8") as f:
        json.dump(gloss, f, ensure_ascii=False, indent=2)

    print("OK 数据库词典.md chars:", len(text))
    print("tables:", len(gloss["tables"]), "enums:", len(gloss["enums"]),
          "troubleshoot:", len(gloss["troubleshoot"]),
          "（其中静默 %d）" % sum(1 for t in gloss["troubleshoot"] if t["silent"]))


if __name__ == "__main__":
    main()
