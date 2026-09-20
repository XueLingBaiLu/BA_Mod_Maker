# -*- coding: utf-8 -*-
"""v1.8.55 / v1.8.56 冒烟测试：枚举绿色箭头 + 箭头统一 + 位掩码拆解。

无窗口运行：Tk 根窗口 withdraw 后构建真实控件，验证：
  1) 主表格单元格 = "11 → 主战坦克 (Tank)"；外键/枚举箭头字形统一（都是 REF_ARROW "→"）
  2) 字段编辑器枚举字段带绿色 → 标签，值实时更新，未知值黄色告警
  3) 位掩码（Ammunitions.TargetType / Units.Type）按加法拆解：36 → 舰船 + 载具 (32 + 4)
  4) 外键箭头可点击跳转到被引用行
  5) 词典窗口 show_dictionary("Units.Role") 定位并渲染说明 + 成员名
用法: python smoke_v1855.py
"""
import os
import sys
import traceback

try:                                    # GUI/无控制台环境 sys.stdout 可能是 None
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import tkinter as tk  # noqa: E402
import ba_db_tool as T  # noqa: E402

UNITS = [
    {"Id": 1, "Name": "T-72B", "HUDName": "T-72B", "CountryId": 1, "Cost": 120,
     "Type": 4, "CategoryType": 2, "Role": 11},
    {"Id": 2, "Name": "BTR-82", "HUDName": "BTR-82", "CountryId": 1, "Cost": 60,
     "Type": 4, "CategoryType": 2, "Role": 12},
    {"Id": 3, "Name": "Weird", "HUDName": "Weird", "CountryId": 1, "Cost": 10,
     "Type": 6, "CategoryType": 2, "Role": 999},  # 未知 Role + 掩码 Type 6=4+2
]

AMMO = [
    {"Id": 1, "Name": "120mm AP", "TargetType": 36, "TrajectoryType": 10},
    {"Id": 2, "Name": "Tor missile", "TargetType": 1944, "TrajectoryType": 100},
    {"Id": 3, "Name": "Bad", "TargetType": 64, "TrajectoryType": 10},  # 未知位 64
]

fails = []


def check(name, cond, extra=""):
    print(("  [OK]   " if cond else "  [FAIL] ") + name + (("  -> " + str(extra)) if extra and not cond else ""))
    if not cond:
        fails.append(name)


def find_text(w):
    for c in w.winfo_children():
        if isinstance(c, tk.Text):
            return c
        r = find_text(c)
        if r is not None:
            return r
    return None


def main():
    root = tk.Tk()
    root.withdraw()
    app = T.EditorApp(root)
    app.tables = {"Units": [dict(u) for u in UNITS], "Ammunitions": [dict(a) for a in AMMO]}
    app._data_version += 1
    app._fmt_cache.clear()
    app._fmt_ver.clear()

    # ---- 1. 主表格（枚举 + 掩码）----
    app.current_table = "Units"
    cols = app._table_columns("Units")
    check("Units 列含 CategoryType/Role", "CategoryType" in cols and "Role" in cols, cols)
    cells = app._fmt_row(app.tables["Units"][0], cols, "Units")
    joined = " | ".join(str(c) for c in cells)
    print("Units 第 1 行:", cells)
    check("表格枚举箭头", "→" in joined and "主战坦克" in joined, joined)
    check("表格无旧箭头 ➜", "➜" not in joined, joined)
    mask_joined = " | ".join(str(c) for c in app._fmt_row(app.tables["Units"][2], cols, "Units"))
    check("Units.Type 掩码拆解 (6=载具+步兵)", "载具 + 步兵 (4 + 2)" in mask_joined, mask_joined)
    check("表格未知枚举值告警", "⚠" in mask_joined and "999" in mask_joined, mask_joined)

    app.current_table = "Ammunitions"
    acols = app._table_columns("Ammunitions")
    arow = app._fmt_row(app.tables["Ammunitions"][0], acols, "Ammunitions")
    aj = " | ".join(str(c) for c in arow)
    print("弹药第 1 行:", arow)
    check("TargetType 36 → 舰船 + 载具 (32 + 4)", "舰船 + 载具 (32 + 4)" in aj, aj)
    aj2 = " | ".join(str(c) for c in app._fmt_row(app.tables["Ammunitions"][1], acols, "Ammunitions"))
    check("TargetType 1944 拆解",
          "弹道导弹 + 巡航导弹 + 反辐射导弹 + 弹丸 + 飞机 + 直升机" in aj2
          and "(1024 + 512 + 256 + 128 + 16 + 8)" in aj2, aj2)
    aj3 = " | ".join(str(c) for c in app._fmt_row(app.tables["Ammunitions"][2], acols, "Ammunitions"))
    check("TargetType 未知位 64 告警", "⚠" in aj3 and "未知位 64" in aj3, aj3)

    app.tr.set("en")
    app._fmt_cache.clear()
    app._fmt_ver.clear()
    en_cells = " | ".join(str(c) for c in app._fmt_row(app.tables["Units"][0], cols, "Units"))
    en_ammo = " | ".join(str(c) for c in app._fmt_row(app.tables["Ammunitions"][0], acols, "Ammunitions"))
    check("英文枚举文本", "Tank" in en_cells, en_cells)
    check("英文掩码拆解", "Ship + Vehicle (32 + 4)" in en_ammo, en_ammo)
    app.tr.set("zh")
    app._fmt_cache.clear()
    app._fmt_ver.clear()

    app._select_table("Units")
    kids = app.tree.get_children()
    check("表格渲染出行", len(kids) >= 1, len(kids))
    if kids:
        vals = app.tree.item(kids[0], "values")
        check("渲染单元格含统一箭头", any("→" in str(v) for v in vals), vals)

    # ---- 2. 字段编辑器：枚举 / 掩码 ----
    dw = T.DetailWindow(app)
    app.detail = dw
    inner = tk.Frame(root)
    widgets = {}
    dw.table = "Units"
    dw.row = 0
    try:
        dw._build_form(inner, row=dict(UNITS[0]), widgets=widgets,
                       dirty_cb=lambda *a: None, table="Units")
    except Exception:
        traceback.print_exc()
        check("_build_form 构建成功", False)
    else:
        check("_build_form 构建成功", True)
        entry, _o = widgets["Role"]
        lab = entry._enum_label
        check("Role 绿色箭头 + 含义", lab.cget("text").startswith(T.REF_ARROW)
              and "主战坦克" in lab.cget("text"), lab.cget("text"))
        check("箭头为绿色(ok)", lab.cget("fg") == app.c["ok"], lab.cget("fg"))
        entry.delete(0, "end")
        entry.insert(0, "35")
        dw._enum_update_label(entry, lab, entry._enum_key, "Units")
        check("改 35 → ATGM 步兵", "反坦克导弹步兵" in lab.cget("text"), lab.cget("text"))
        entry.delete(0, "end")
        entry.insert(0, "777")
        dw._enum_update_label(entry, lab, entry._enum_key, "Units")
        check("未知值转告警色", lab.cget("fg") == app.c["warn"] and "777" in lab.cget("text"),
              lab.cget("text"))
        check("Name（非枚举）无箭头", not hasattr(widgets["Name"][0], "_enum_key"))

        t_entry, _o = widgets["Type"]
        check("Type 掩码单 bit 显示", "载具" in t_entry._enum_label.cget("text"),
              t_entry._enum_label.cget("text"))
        t_entry.delete(0, "end")
        t_entry.insert(0, "6")
        dw._enum_update_label(t_entry, t_entry._enum_label, t_entry._enum_key, "Units")
        check("Type 6 → 载具 + 步兵 (4 + 2)",
              "载具 + 步兵 (4 + 2)" in t_entry._enum_label.cget("text"),
              t_entry._enum_label.cget("text"))
        tip_role = dw._enum_tip_text("Units.Role", widgets["Role"][0])
        check("Role 悬浮提示含说明+成员名",
              "TargetingPresetOverrides" in tip_role and "Tank" in tip_role, tip_role[:80])

    dw2 = T.DetailWindow(app)
    inner2 = tk.Frame(root)
    w2 = {}
    dw2.table = "Ammunitions"
    dw2.row = 0
    dw2._build_form(inner2, row=dict(AMMO[0]), widgets=w2, dirty_cb=lambda *a: None,
                    table="Ammunitions")
    tt, _o = w2["TargetType"]
    check("编辑器掩码 → 舰船 + 载具 (32 + 4)",
          "舰船 + 载具 (32 + 4)" in tt._enum_label.cget("text"), tt._enum_label.cget("text"))
    tip2 = dw2._enum_tip_text("Ammunitions.TargetType", tt)
    check("掩码提示含当前值拆解与加法说明",
          "36 = 32 (舰船) + 4 (载具)" in tip2 and "加法合成" in tip2, tip2[:140])

    # ---- 3. 外键箭头统一 + 可点击跳转 ----
    dw3 = T.DetailWindow(app)
    inner3 = tk.Frame(root)
    w3 = {}
    dw3.table = "Ammunitions"
    dw3.row = 0
    ammo_row = dict(AMMO[0])
    ammo_row["UnitId"] = 1
    dw3._build_form(inner3, row=ammo_row, widgets=w3, dirty_cb=lambda *a: None,
                    table="Ammunitions")
    if "UnitId" in w3:
        fk_entry, _o = w3["UnitId"]
        fk_lab = fk_entry._fk_name_label
        check("外键箭头同为 →", fk_lab.cget("text").startswith(T.REF_ARROW)
              and "T-72B" in fk_lab.cget("text"), fk_lab.cget("text"))
        check("外键箭头同为绿色", fk_lab.cget("fg") == app.c["ok"], fk_lab.cget("fg"))
        check("外键箭头可点击(cursor=hand2)", str(fk_lab.cget("cursor")) == "hand2",
              fk_lab.cget("cursor"))
        before = (dw3.table, dw3.row)
        dw3._fk_jump(fk_entry)
        check("外键箭头点击可跳转", (dw3.table, dw3.row) != before and dw3.table == "Units",
              (dw3.table, dw3.row))
    else:
        check("Ammunitions 有外键字段 UnitId", False)

    r_entry, _o = widgets["Role"]
    check("枚举箭头可点击(cursor=hand2)", str(r_entry._enum_label.cget("cursor")) == "hand2",
          r_entry._enum_label.cget("cursor"))

    # ---- 4. 词典窗口 ----
    app.show_dictionary("Units.Role")
    win = getattr(app, "_dict_win", None)
    check("词典窗口打开", win is not None and win.winfo_exists())
    if win is not None:
        txt = find_text(win)
        if txt is not None:
            body = txt.get("1.0", "end")
            check("词典含 Role 说明", "TargetingPresetOverrides" in body)
            check("词典含枚举成员名", "[Tank]" in body)
            check("词典箭头统一为 →", "→" in body and "➜" not in body)
        else:
            check("找到词典文本控件", False)
        win.destroy()

    app.show_dictionary("Ammunitions.TargetType")
    win2 = getattr(app, "_dict_win", None)
    if win2 is not None:
        txt2 = find_text(win2)
        body2 = txt2.get("1.0", "end") if txt2 is not None else ""
        check("词典标注位掩码加法合成", "位掩码" in body2 and "加法" in body2, body2[:100])
        win2.destroy()

    root.destroy()
    print()
    if fails:
        print("失败 %d 项: %s" % (len(fails), "; ".join(fails)))
        return 1
    print("全部通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
