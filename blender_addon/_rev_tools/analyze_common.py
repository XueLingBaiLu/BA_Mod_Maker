# -*- coding: utf-8 -*-
r"""通用分析工具：bundle 加载、层级遍历、PPtr 扫描（断箭逆向分析工具箱）。

用法：
    from analyze_common import load_bundle, Hierarchy, mb_header, scan_refs
    env, sf, objs, by_pid = load_bundle(path)
    h = Hierarchy(objs, by_pid)
    root = h.root_of(some_go_pid)
    print(h.tree(root))

知识要点（详见 REVERSE_ENGINEERING.md）：
    - MonoBehaviour 原始字节布局：m_GameObject PPtr(12) + m_Enabled(1+3pad)
      + m_Script PPtr(12) + m_Name string + 派生字段。
      m_GameObject 的 pathID 在偏移 4，m_Script 的 pathID 在偏移 20。
    - PPtr = fileID(int32) + pathID(int64)，pathID 不强制 8 字节对齐。
    - 本工具创建的炮塔对象 pid 固定在 0x4355424500000000 起的段内。
"""
import sys, os, struct

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "_unitypy"))
import UnityPy

# 本工具分配对象的 pid 段（build_turret_direct 专用，勿改）
OUR_BASE = 0x4355424500000000
OUR_RANGE = 0x10000


def load_bundle(path):
    """返回 (env, sf, objs, by_pid)。"""
    env = UnityPy.load(path)
    sf = list(env.objects)[0].assets_file
    objs = list(sf.objects.values())
    by_pid = {o.path_id: o for o in objs}
    return env, sf, objs, by_pid


def mb_header(raw):
    """解析 MB 头部。返回 (m_GameObject pathID, m_Script pathID, m_Name)。"""
    if len(raw) < 32:
        return None
    go = struct.unpack_from("<q", raw, 4)[0]
    sp = struct.unpack_from("<q", raw, 20)[0]
    n = struct.unpack_from("<i", raw, 28)[0]
    name = raw[32:32 + n].decode("utf-8", "replace") if 0 < n < 512 else ""
    return go, sp, name


class Hierarchy:
    """GameObject/Transform 层级工具。"""

    def __init__(self, objs, by_pid):
        self.by_pid = by_pid
        self.tr_go = {}   # Transform pid -> GO pid
        self.go_tr = {}   # GO pid -> Transform pid
        for o in objs:
            if o.type.name == "Transform":
                try:
                    tr = o.read()
                    g = tr.m_GameObject.m_PathID if tr.m_GameObject else 0
                    self.tr_go[o.path_id] = g
                    self.go_tr[g] = o.path_id
                except Exception:
                    pass

    def name(self, gpid):
        o = self.by_pid.get(gpid)
        if not o:
            return "?"
        try:
            return o.read().m_Name
        except Exception:
            return "?"

    def root_of(self, gpid):
        for _ in range(32):
            tpid = self.go_tr.get(gpid)
            if not tpid:
                break
            t = self.by_pid.get(tpid)
            if not t:
                break
            try:
                f = t.read().m_Father.m_PathID if t.read().m_Father else 0
            except Exception:
                break
            if not f:
                break
            pg = self.tr_go.get(f, 0)
            if not pg:
                break
            gpid = pg
        return gpid

    def children(self, gpid):
        out = []
        tpid = self.go_tr.get(gpid)
        if not tpid:
            return out
        t = self.by_pid.get(tpid)
        if not t:
            return out
        try:
            for c in (t.read().m_Children or []):
                cg = self.tr_go.get(c.m_PathID, 0)
                out.append((self.name(cg), cg, c.m_PathID))
        except Exception:
            pass
        return out

    def tree(self, gpid, depth=0, max_depth=10):
        """递归打印 GO 树。"""
        lines = []
        if depth > max_depth:
            return lines
        lines.append("  " * depth + f"[{self.name(gpid)}] GO={gpid} TR={self.go_tr.get(gpid, 0)}")
        for nm, cg, ct in self.children(gpid):
            lines.append("  " * (depth + 1) + f"{nm} (GO={cg} TR={ct})")
            lines.extend(self.tree(cg, depth + 2, max_depth))
        return lines


def resolve(pid, by_pid):
    """把一个 pathID 解析成人类可读描述。"""
    o = by_pid.get(pid)
    if not o:
        return f"<外部/不存在 {pid}>"
    t = o.type.name
    if t == "Transform":
        try:
            tr = o.read()
            g = tr.m_GameObject.m_PathID if tr.m_GameObject else 0
            go = by_pid.get(g)
            return f"Transform '{go.read().m_Name if go else '?'}'"
        except Exception:
            return "Transform ?"
    if t == "GameObject":
        try:
            return f"GameObject '{o.read().m_Name}'"
        except Exception:
            return "GameObject ?"
    if t == "MonoBehaviour":
        h = mb_header(o.get_raw_data())
        return f"MonoBehaviour(len={len(o.get_raw_data())} script={h[1] if h else '?'})"
    if t == "MonoScript":
        try:
            return f"MonoScript '{o.read().m_Name}'"
        except Exception:
            return "MonoScript ?"
    try:
        return f"{t} '{o.read().m_Name}'"
    except Exception:
        return t


def scan_refs(raw, by_pid, align=4, min_pid=1 << 40, step=4):
    """扫描原始字节里所有像 pathID 的 int64（4 字节步进），按出现位置解析对象。

    注意：会把 float/字符串误判成 pathID，需要结合 resolve() 结果人工判断；
    PPtr 的 pathID 前有 4 字节 fileID（通常为 0），真正的引用在 fileID 后的偏移。
    """
    seen = {}
    for off in range(0, len(raw) - 8, step):
        v = struct.unpack_from("<q", raw, off)[0]
        if abs(v) < min_pid:
            continue
        if v not in seen:
            seen[v] = off
    return sorted(((off, v, resolve(v, by_pid)) for v, off in seen.items()), key=lambda x: x[0])


def strings(raw, min_len=4):
    """提取可打印 ASCII 字符串 [(offset, str)]。"""
    out = []
    import re
    for m in re.finditer(rb"[\x20-\x7e]{%d,}" % min_len, raw):
        out.append((m.start(), m.group().decode()))
    return out


def hexdump(raw, start=0, end=None, width=16):
    """十六进制 + ASCII 对照输出。"""
    end = min(len(raw), end if end is not None else len(raw))
    out = []
    for off in range(start, end, width):
        chunk = raw[off:off + width]
        hx = " ".join(f"{b:02x}" for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append(f"{off:04x}: {hx:<{width * 3}}  {asc}")
    return "\n".join(out)
