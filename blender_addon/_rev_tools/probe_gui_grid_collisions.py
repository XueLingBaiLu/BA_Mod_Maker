# -*- coding: utf-8 -*-
r"""**tkinter grid 同格冲突探针** —— 找出"两个控件被放进同一个 (row, column)"的地方。

为什么需要：`grid()` 里同容器同 (row, column) 的两个控件会**互相叠着画**
（后创建的压在先创建的上面）⇒ 表现为「字压字 / 输入框看不见 / 按钮被盖住」。
这类 bug **不随窗口宽度变化**（grid 单元格宽度 = 该格最宽子控件的请求宽度，
两个控件都在这格里 ⇒ 必然重叠），窗口拉多宽都治不好 ⇒ 只能改布局。

用法：
    python probe_gui_grid_collisions.py                        # 扫产品目录下所有用 grid 的 .py
    python probe_gui_grid_collisions.py <文件.py> [<文件2.py>]   # 只扫指定文件
    python probe_gui_grid_collisions.py --all                  # 连"单控件占一格"也列出来
退出码：发现冲突 ⇒ 1；无冲突 ⇒ 0（可直接当回归判据用）。

判据（可直接写进测试）：`同容器 (row, column) 的控件数 > 1` ⇒ 失败。
局限：只认**字面量** row/column；动态表达式（`row=next_row`）单独列在"未判定"区，不误报。
"""
import ast
import io
import os
import re
import sys

PROD = r"<工作目录>\工具制作资源\BA_Mod_Maker"


class Scan(ast.NodeVisitor):
    """⛔ 必须**按作用域分组**：两个不同对话框里各自的局部变量 `frm` 是**两个不同的 Frame**
    （实测踩过：`mod_assets.py` 的 `TexturePackDialog.frm` 与 `AssetImportDialog.frm` 被当成同一个 ⇒ 10 处假冲突）。"""

    def __init__(self):
        self.scope = []        # 当前作用域栈（类名/函数名）
        self.parents = {}      # (作用域, 变量名) -> 容器表达式
        self.hits = []         # (作用域, 容器, row, column, 行号, 描述)
        self.dyn = []          # 动态 row/column
        self.lines = []

    def _sc(self):
        return ".".join(self.scope) or "<module>"

    def visit_ClassDef(self, node):
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node):
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    # ---- 工具：从调用里取"父容器" ----
    @staticmethod
    def _ctor_parent(node):
        """`ttk.Label(frm3, text=…)` ⇒ 'frm3'；`ttk.Frame(top)` ⇒ 'top'。"""
        if isinstance(node, ast.Call) and node.args:
            try:
                return ast.unparse(node.args[0])
            except Exception:                                          # noqa: BLE001
                return None
        return None

    def visit_Assign(self, node):
        # self.X = ttk.Widget(parent, ...)   /   X = ttk.Widget(parent, ...)
        src = None
        if isinstance(node.value, ast.Call):
            src = self._ctor_parent(node.value)
        elif isinstance(node.value, ast.Name):
            src = self.parents.get((self._sc(), node.value.id))
        if src is None:
            return self.generic_visit(node)
        for t in node.targets:
            try:
                self.parents[(self._sc(), ast.unparse(t))] = src
                if isinstance(t, ast.Attribute):
                    self.parents[(self._sc(), t.attr)] = src
            except Exception:                                          # noqa: BLE001
                pass
        self.generic_visit(node)

    def visit_Call(self, node):
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr == "grid":
            recv = f.value
            parent = None
            if isinstance(recv, ast.Call):
                parent = self._ctor_parent(recv)
            else:
                key = None
                try:
                    key = ast.unparse(recv)
                except Exception:                                      # noqa: BLE001
                    pass
                parent = (self.parents.get((self._sc(), key))
                          or self.parents.get((self._sc(), key.split(".")[-1] if key else "")))
            row = col = None
            for kw in node.keywords:
                if kw.arg == "row":
                    row = kw.value.value if isinstance(kw.value, ast.Constant) else "?"
                if kw.arg == "column":
                    col = kw.value.value if isinstance(kw.value, ast.Constant) else "?"
            if isinstance(recv, ast.Call) and len(node.args) >= 2:      # 位置参数写法 grid(r, c)
                if isinstance(node.args[0], ast.Constant):
                    row = node.args[0].value
                if isinstance(node.args[1], ast.Constant):
                    col = node.args[1].value
            desc = ""
            if isinstance(recv, ast.Call):
                for kw in recv.keywords:
                    if kw.arg in ("text", "textvariable") and isinstance(kw.value, ast.Constant):
                        desc = "%s=%r" % (kw.arg, str(kw.value.value)[:34])
            else:
                desc = "变量 %s" % (ast.unparse(recv) if hasattr(ast, "unparse") else "?")
            if row in (None, "?") or col in (None, "?"):
                self.dyn.append((node.lineno, parent or "?", desc))
            else:
                self.hits.append((self._sc(), parent or "?", row, col, node.lineno, desc))
        self.generic_visit(node)


def scan_file(path):
    src = io.open(path, encoding="utf-8", errors="replace").read()
    tree = ast.parse(src)
    s = Scan()
    s.lines = src.splitlines()
    s.visit(tree)
    return s


def report(path, s, show_all=False):
    groups = {}
    for scope, parent, row, col, line, desc in s.hits:
        groups.setdefault((scope, parent, row, col), []).append((line, desc))
    bad = {k: v for k, v in groups.items() if len(v) > 1}
    name = os.path.basename(path)
    print("=" * 78)
    print("%s  grid 落点 %d 个 · 同格冲突 %d 处 · 动态行/列 %d 个"
          % (name, len(s.hits), len(bad), len(s.dyn)))
    for (scope, parent, row, col), items in sorted(
            bad.items(), key=lambda kv: min(i[0] for i in kv[1])):
        print("  ⛔ 同格冲突 作用域=%s 容器=%s  row=%s column=%s  ← %d 个控件抢同一格"
              % (scope, parent, row, col, len(items)))
        for line, desc in sorted(items):
            print("        L%-6d %s" % (line, desc))
    if show_all:
        print("  ---- 全部落点 ----")
        for (scope, parent, row, col), items in sorted(
                groups.items(), key=lambda kv: min(i[0] for i in kv[1])):
            print("    %-28s %-8s r=%-3s c=%-3s : %s"
                  % (scope, parent, row, col, " | ".join("L%d %s" % i for i in sorted(items))))
    for line, parent, desc in s.dyn:
        print("  · 未判定（动态行列）L%-6d 容器=%s %s" % (line, parent, desc))
    return len(bad)


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    show_all = "--all" in argv
    files = args or [os.path.join(PROD, f) for f in sorted(os.listdir(PROD))
                     if f.endswith(".py") and ".grid(" in io.open(
                         os.path.join(PROD, f), encoding="utf-8", errors="replace").read()]
    total = 0
    for f in files:
        try:
            s = scan_file(f)
        except Exception as e:                                          # noqa: BLE001
            print("!! %s 解析失败：%s: %s" % (f, type(e).__name__, e))
            continue
        total += report(f, s, show_all)
    print("=" * 78)
    print("合计同格冲突 %d 处 ⇒ %s" % (total, "FAIL（有冲突）" if total else "PASS（无冲突）"))
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
