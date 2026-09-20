# -*- coding: utf-8 -*-
r"""**`[界面-02]` 静态判据探针** —— 「挂载点 · 游戏坐标三行」的①对齐 ②入口可见性。

为什么需要（工具优先）：这两条需求都是"**看界面才知道好不好**"，而本机不适合每条都开 Blender 截图；
但它们的**成因全部是源码里的字面属性**，可以**静态判死**，且可复跑、可给验收线独立复算：

* ① **对齐**（成因 = `icon=` 有无/不同 + 数字格式宽度）
  * `col.label(..., icon=X)` 会在文字前**多画一个图标**（固定宽度）⇒ 三行里只要有一行多/少 icon，
    文字左边界就整体错开一个图标宽 → 判据 **(a) 三行 icon 表达式完全一致**；
  * `%.4f` 无符号位、宽度随值浮动（`-0.0005`(7) vs `0.2028`(6)）⇒ 小数点不在一列
    → 判据 **(b) 每个转换说明在样本上的渲染宽度恒定**（`%+9.4f` = 9 恒定）；
  * 判据 **(c) 三行的"等宽骨架"后缀（从第一个 `X` 起）逐字符相同** ⇒ 三行之间 X/Y/Z 三列真的对齐。
* ② **入口可见性**（成因 = 顺序）：可编辑入口 `ba_mod.set_game_coord` 必须**紧跟三行数字**
  （"数字视线内"），⛔ 不许被压在 `⚠ 两者不等` 告警 / `flatten_parent_inverse` **之后**
  （原实现就在那儿 ⇒ 用户看不见 = 用户报的"不可更改"）；且**只读原因文案必须在入口之前**。

退出码：`0 = PASS / 1 = FAIL`（可直接当回归判据）。
负向对照：把**旧版**源码路径传进来应报 FAIL（本机可复跑：
`python _rev_tools\out\probe_coord_label_align.py 测试\_gate_tmp\mirror\B_before\blender_addon\__init__.py`
⇒ 改前 3 条 icon/定宽失败 + 2 条入口被遮挡 + 1 条缺原因文案 = 6 条 FAIL）。

用法：
    python _rev_tools\out\probe_coord_label_align.py <blender_addon\__init__.py 路径> [--show]

局限（**如实声明**）：静态探针**不能**证明"肉眼看着对齐"—— 它证明的是
「icon 一致 + 数字定宽 + 三行骨架相同」这三个**成因**已消除；最终观感仍以用户截图为准。
另外 `%+9.4f` 只保证 **|值| ≤ 999.9999** 时宽度恒定（≥1000 会溢出成 10 列）——
探针把这一档单列为**软提示**，⛔ 不据此判 FAIL。
"""
import ast
import io
import re
import sys

ICON_W = 6          # 一个图标在文字前占的虚拟宽度（只用于算列位，绝对像素不重要）
LABELS = ("游戏坐标:", "视图坐标:", "网格几何中心:")
# 定宽判据样本：**真实量级**（游戏坐标 ≈ 米，实测用户值 0.0005–2.3）＋ 跨正负 ＋ 三位整数
SAMPLES = [(0.1, -0.0005, 2.2969), (-12.3456, 3.5, 0.0), (999.9999, -1.0, 123.4567)]
OVERFLOW = 1e4      # 软提示档：|值| ≥ 1000 会溢出 %+9.4f 的 9 列
SPEC = re.compile(r"%[-+ #0]*\d*(?:\.\d+)?[diouxXeEfFgGrsc]")


def skeleton(fmt, sample):
    """把格式串渲染成**等宽骨架**：每个转换说明按其渲染宽度替换为 `#`，字面量原样保留。

    返回 (骨架串, [每个转换说明在该样本下的渲染宽度])。
    """
    out, widths = [], []
    pos, vals = 0, list(sample)
    k = 0
    for m in SPEC.finditer(fmt):
        out.append(fmt[pos:m.start()])
        spec = m.group(0)
        try:
            w = len(spec % vals[k])
        except Exception:                                              # noqa: BLE001
            w = -1
        widths.append(w)
        out.append("#" * max(w, 0))
        pos, k = m.end(), k + 1
    out.append(fmt[pos:])
    return "".join(out), widths


def _raw_text(node):
    """label(text=…) 里取出**字面量格式串**（支持 `"…" % (...)` 的 BinOp）。"""
    if isinstance(node, ast.BinOp) and isinstance(node.left, ast.Constant):
        return node.left.value
    if isinstance(node, ast.Constant):
        return node.value
    return None


def scan(path):
    src = io.open(path, encoding="utf-8", errors="replace").read()
    tree = ast.parse(src)
    res = {"coord": [], "entry_line": None, "warn_line": None, "reason_line": None,
           "flatten_line": None, "fname": None}
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef) or fn.name != "_draw_game_coord":
            continue
        res["fname"] = fn.name
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                attr = node.func.attr
                if attr == "label":
                    txt = icon = None
                    for kw in node.keywords:
                        if kw.arg == "text":
                            txt = _raw_text(kw.value)
                        if kw.arg == "icon":
                            try:
                                icon = ast.unparse(kw.value)
                            except Exception:                          # noqa: BLE001
                                icon = "?"
                    if isinstance(txt, str) and any(k in txt for k in LABELS):
                        res["coord"].append((txt, icon, node.lineno))
                    elif isinstance(txt, str) and "只读" in txt:
                        res["reason_line"] = node.lineno
                if attr == "operator" and node.args and isinstance(node.args[0], ast.Constant):
                    v = node.args[0].value
                    if v == "ba_mod.set_game_coord":
                        res["entry_line"] = node.lineno
                    if v == "ba_mod.flatten_parent_inverse":
                        res["flatten_line"] = node.lineno
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "label":
                for kw in node.keywords:
                    if kw.arg == "text" and isinstance(kw.value, ast.Constant) \
                            and isinstance(kw.value.value, str) \
                            and kw.value.value.startswith("⚠ 两者不等"):
                        res["warn_line"] = node.lineno
    return res


def main(argv):
    show = "--show" in argv
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 1
    path = args[0]
    r = scan(path)
    fails, notes = [], []
    print("=" * 78)
    print("探针：[界面-02] 挂载点坐标三行 · %s" % path)
    print("-" * 78)

    # ①-a icon 一致性（比**表达式**，不是只比有无）
    if len(r["coord"]) < 2:
        fails.append("没扫到三行坐标 label（扫到 %d 行）" % len(r["coord"]))
    else:
        icons = sorted({(h if h else "<无>") for _t, h, _l in r["coord"]})
        print("①-a icon 一致性：三行 icon = %s ⇒ %s"
              % (icons, "一致 ✓" if len(icons) == 1 else "不一致 ✗"))
        for txt, icon, ln in r["coord"]:
            print("      L%-5d icon=%-22s %s" % (ln, icon or "<无>", txt.strip()[:58]))
        if len(icons) != 1:
            fails.append("三行 icon 不一致 ⇒ 文字左边界错开一个图标宽（= 用户报的「未对齐」成因之一）")

    # ①-b 定宽 + ①-c 骨架后缀一致
    skels = []
    for txt, icon, ln in r["coord"]:
        rows = [skeleton(txt, s) for s in SAMPLES]
        per_spec = list(zip(*[w for _sk, w in rows]))
        bad = [i for i, ws in enumerate(per_spec) if len(set(ws)) != 1]
        sk = rows[0][0]
        skels.append((ln, sk))
        print("①-b 定宽 L%-5d 各字段渲染宽度=%s ⇒ %s"
              % (ln, [sorted(set(ws)) for ws in per_spec],
                 "恒定 ✓" if not bad else "✗ 字段 %s 随值浮动" % bad))
        if bad:
            fails.append("L%d 数字非定宽（字段 %s）⇒ 小数点不在同一列" % (ln, bad))
        ov = [w for _sk, ws in [skeleton(txt, (OVERFLOW,) * 3)] for w in ws]
        if any(w > 9 for w in ov):
            notes.append("L%d |值| ≥ 1000 时会溢出 9 列（%s）—— 属格式上限，非缺陷" % (ln, ov))
    if len(skels) >= 2:
        tail = []
        for ln, sk in skels:
            i = sk.find("X")
            tail.append((ln, sk[i:] if i >= 0 else sk))
        same = len({t for _l, t in tail}) == 1
        print("①-c 三行骨架后缀一致 ⇒ %s" % ("✓" if same else "✗"))
        for ln, t in tail:
            print("      L%-5d %s" % (ln, t))
        if not same:
            fails.append("三行的『X…』骨架后缀不同 ⇒ 三行之间 X/Y/Z 三列不对齐")
        i0 = skels[0][1].find("X")
        for ln, sk in skels:
            i = sk.find("X")
            print("      · 绝对列位（含图标宽 %d）：L%d 『X』在第 %d 列；其前标签占 %d 列"
                  % (ICON_W, ln, (i + ICON_W if icons else i), i))

    # ② 入口可见性
    e, w, rs, fl = r["entry_line"], r["warn_line"], r["reason_line"], r["flatten_line"]
    last_coord = max([l for _t, _h, l in r["coord"]], default=None)
    print("-" * 78)
    print("② 入口可见性：末行数字 L%s → 只读原因 L%s → 入口 L%s ｜ ⚠告警 L%s ｜ 摊平算子 L%s"
          % (last_coord, rs, e, w, fl))
    if e is None:
        fails.append("没找到可编辑入口 `ba_mod.set_game_coord`")
    else:
        if last_coord is not None and e < last_coord:
            fails.append("入口在数字之前 ⇒ 不合格")
        if w is not None and e > w:
            fails.append("入口被压在「⚠ 两者不等」告警之后 ⇒ 用户看不见（原缺陷形态）")
        if fl is not None and e > fl:
            fails.append("入口被压在「摊平父级逆变换」之后 ⇒ 不合格")
        if rs is None:
            fails.append("缺少「只读原因」文案（需求①要求写明置灰原因）")
        elif rs > e:
            fails.append("只读原因文案出现在入口之后 ⇒ 用户先看到按钮才看到原因")
        if not any("入口" in f for f in fails):
            print("      ⇒ 入口紧贴数字、原因在入口之前、未被 ⚠/摊平算子遮挡 ✓")

    if show:
        print("-" * 78)
        print("渲染样本（icon 占 %d 宽；⚠ 第三行标签本身比前两行宽 2 字 = 名字长度差，非缺陷）" % ICON_W)
        for txt, icon, ln in r["coord"]:
            for s in SAMPLES:
                t, _w = skeleton(txt, s)
                t = t.replace("#", "·")           # 骨架：· 的个数 = 字段宽度
                print("      L%-5d %s%s" % (ln, " " * ICON_W if icon else "", t))

    print("=" * 78)
    for n in notes:
        print("  · 软提示：%s" % n)
    if fails:
        for f in fails:
            print("  ⛔ %s" % f)
        print("结论：FAIL（%d 条）" % len(fails))
        return 1
    print("结论：PASS（icon 一致 ✓ 数字定宽 ✓ 三行骨架一致 ✓ 入口在数字视线内 ✓）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
