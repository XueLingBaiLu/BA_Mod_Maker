# -*- coding: utf-8 -*-
r"""网格顶点数据构建（直接构造炮塔用）。

build_mesh_data：把顶点/UV/三角/蒙皮数据拼成 Unity 2022 顶点流字节。
法线平滑重算；切线按 UV 梯度计算（UV 退化时 W 默认 -1，匹配 Unity 约定）。

⛔ v1.8.76 重要修复 —— **顶点布局必须照着目标网格自己的通道表写**：
   以前这里写死"位置+法线+切线 / 2 个 UV 通道 / 2 权重+2 骨索引"（= 68 字节/顶点），
   而它**从不同步 `m_Channels`**。实测整个 units bundle 里 6421 个蒙皮网格有
   **29 种布局**，68 字节那种只占 309 个（4.8%）：
       76B ×2123（4 骨权重，无 uv1）、88B ×1262、56B ×649（单骨索引）、48B ×606 …
   布局不匹配时引擎按**原通道表**解释新数据 ⇒ UV/法线/骨骼整体错位（贴图花、装甲板乱）✗
   ⇒ 现在按目标 Mesh 的 `m_VertexData.m_Channels` 逐通道写（stream/offset/format/dimension
      全部照抄），与 `extract_model._read_mesh_data` 的读法互为逆运算 ✓
"""
import struct, math

_FMT_STRUCT = {0: "f", 1: "e", 2: "B", 3: "b", 4: "H", 5: "h",
               6: "B", 7: "b", 8: "H", 9: "h", 10: "I", 11: "i"}
# UNorm/SNorm 需要先换算再写整型
_FMT_SCALE = {2: 255.0, 3: 127.0, 4: 65535.0, 5: 32767.0}


def comp_size(fmt):
    return {0: 4, 1: 2, 2: 1, 3: 1, 4: 2, 5: 2, 6: 1, 7: 1, 8: 2, 9: 2, 10: 4, 11: 4}.get(fmt, 4)


def mesh_stride(mesh):
    """目标网格每个顶点的字节数（= 各 stream stride 之和）。"""
    vd = getattr(mesh, "m_VertexData", None)
    strides = {}
    for ch in (getattr(vd, "m_Channels", None) or []):
        s = int(getattr(ch, "stream", 0) or 0)
        strides.setdefault(s, 0)
        d = int(getattr(ch, "dimension", 0) or 0) & 0xF
        if d:
            strides[s] += d * comp_size(int(getattr(ch, "format", 0) or 0))
    return sum(strides.values())


def _channel_values(idx, dim, fmt, pos, nrm, tan, uv, skin, src=None):
    """按通道序号给出该通道要写的值（Unity 通道序号语义，与 extract_model 读法一致）。

    src: 可选，来自**源网格**的同顶点数据 `(法线, 切线, UV1)`（按「位置+UV0」匹配到）。
         ⛔ 能对上就照抄，理由见 `pack_by_layout` 的说明 —— 工具的重算会抹平硬边、
            并把用不到的第二套 UV（原版常是常量）改成变化的 UV0 ✗
    """
    if idx == 0:
        return list(pos)[:dim]
    if idx == 1:
        if src is not None and src[0]:
            return list(src[0])[:dim]
        return list(nrm)[:dim]
    if idx == 2:
        if src is not None and src[1]:
            return list(src[1])[:dim]
        return list(tan)[:dim]
    if idx == 3:
        # 顶点色：没有来源数据 ⇒ 写白色（f32 用 1.0，UNorm8 用 255）
        v = 1.0 if fmt in (0, 1) else (255.0 if fmt == 2 else 127.0)
        return [v] * dim
    if idx == 4:
        vals = list(uv)[:dim]
        while len(vals) < dim:
            vals.append(0.0)
        return vals
    if 5 <= idx <= 11:
        # UV1：源里有就**照抄**（原版 UV1 常是常量/有意义的数据，用 UV0 覆盖会改外观 ✗）
        if src is not None and src[2]:
            vals = list(src[2])[:dim]
            while len(vals) < dim:
                vals.append(0.0)
            return vals
        # UV2..UV7 或源里没有：用 UV0 兜底（原来就是这个行为）
        vals = list(uv)[:dim]
        while len(vals) < dim:
            vals.append(0.0)
        return vals
    if idx == 12:
        w0, w1 = skin[2], skin[3]
        vals = [w0, w1, 0.0, 0.0][:dim]
        while len(vals) < dim:
            vals.append(0.0)
        return vals
    if idx == 13:
        b0, b1 = skin[0], skin[1]
        vals = [b0, b1, 0, 0][:dim]
        while len(vals) < dim:
            vals.append(0)
        return vals
    # 未知通道：补零（保持长度正确即可）
    return [0] * dim


def pack_by_layout(mesh, faces, flip, verts, uvs, normals, tangents, skin, src_attrs=None):
    """按 `mesh` 自己的通道表把顶点数据打包成字节（非索引化，一个三角角一个顶点）。

    与 `extract_model._read_mesh_data` 完全互逆：每个 stream 的 stride = 该 stream
    各通道大小之和；stream 之间按 stream 号排序、每块 16 字节对齐。

    src_attrs（v1.8.82，可选）：源网格里「(位置, UV0) -> (法线, 切线, UV1)」的表。
      ⛔ 为什么需要它：工具原来把**法线按"同位置面法线求平均"重算**、把 **UV1 覆盖成 UV0**，
         这两件事都会改变外观：
           · 原版网格常有**分离（硬边）法线**，平均后棱角被抹平（实测 ACV 有 8.1% 顶点
             与原版夹角 >20°、最大 91°）⇒ 整车光照变"圆润"、装甲板细节丢失 ✗
           · 原版 UV1 常常是**常量**（ACV 就是全 0），覆盖成 UV0 后，着色器里任何依赖 UV1
             的采样就从"取一个固定纹素"变成"跟着 UV0 变化" ⇒ 贴图/遮罩出现异常 ✗
         能按「位置+UV0」对上的顶点就照抄源数据 ✓；对不上（被改过的顶点）才用重算值 ✓
    """
    chans = list(getattr(mesh.m_VertexData, "m_Channels", None) or [])
    n = len(faces) * 3
    strides = {}
    for ch in chans:
        s = int(getattr(ch, "stream", 0) or 0)
        strides.setdefault(s, 0)
        d = int(getattr(ch, "dimension", 0) or 0) & 0xF
        if d:
            strides[s] += d * comp_size(int(getattr(ch, "format", 0) or 0))
    offs, total = {}, 0
    for s in sorted(strides):
        offs[s] = total
        block = strides[s] * n
        total += block
        if block % 16:
            total += 16 - (block % 16)
    buf = bytearray(total)

    def put(base, vals, fmt, dim):
        code = _FMT_STRUCT.get(fmt)
        if code is None:
            code = "f"
        scale = _FMT_SCALE.get(fmt)
        for k in range(dim):
            v = vals[k] if k < len(vals) else 0
            if scale is not None:
                v = int(round(max(0.0, min(1.0, float(v))) * scale))
            struct.pack_into("<" + code, buf, base + k * comp_size(fmt), v)

    i = 0
    for (p0, u0), (p1, u1), (p2, u2) in faces:
        order = ((p0, u0), (p2, u2), (p1, u1)) if flip else ((p0, u0), (p1, u1), (p2, u2))
        for pi, ui in order:
            uv = uvs[ui] if ui >= 0 else (0.0, 0.0)
            src = None
            if src_attrs is not None and not flip:
                src = src_attrs.get((round(verts[pi][0], 3), round(verts[pi][1], 3),
                                     round(verts[pi][2], 3),
                                     round(uv[0], 4), round(uv[1], 4)))
            for ci, ch in enumerate(chans):
                d = int(getattr(ch, "dimension", 0) or 0) & 0xF
                if not d:
                    continue
                s = int(getattr(ch, "stream", 0) or 0)
                base = offs.get(s, 0) + i * strides.get(s, 1) + int(getattr(ch, "offset", 0) or 0)
                put(base,
                    _channel_values(ci, d, int(getattr(ch, "format", 0) or 0),
                                    verts[pi], normals[pi], tangents[pi], uv, skin[pi], src),
                    int(getattr(ch, "format", 0) or 0), d)
            i += 1
    return bytes(buf)


def build_mesh_data(verts, uvs, faces, skin, skin_mode=2, layout_from=None, src_attrs=None):
    """重建顶点数据（非索引化）。skin 是按位置索引的 [(b0,b1,w0,w1), ...]。
    返回 (vertex_bytes, index_bytes, vertex_count)。
    法线平滑重算；切线按 UV 梯度计算（与 Unity 同算法），保证法线贴图正确。"""
    n = len(faces) * 3
    pos_nrm = [None] * len(verts)
    pos_tan = [None] * len(verts)
    pos_tanw = [0.0] * len(verts)
    for (p0, u0), (p1, u1), (p2, u2) in faces:
        ax, ay, az = verts[p0]
        ux, uy, uz = verts[p1][0] - ax, verts[p1][1] - ay, verts[p1][2] - az
        vx, vy, vz = verts[p2][0] - ax, verts[p2][1] - ay, verts[p2][2] - az
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        # 面切线（UV 梯度）+ 手性 w
        t0 = uvs[u0] if u0 >= 0 else (0.0, 0.0)
        t1 = uvs[u1] if u1 >= 0 else (0.0, 0.0)
        t2 = uvs[u2] if u2 >= 0 else (0.0, 0.0)
        du1 = t1[0] - t0[0]; dv1 = t1[1] - t0[1]
        du2 = t2[0] - t0[0]; dv2 = t2[1] - t0[1]
        det = du1 * dv2 - du2 * dv1
        tx = ty = tz = 0.0
        w = -1.0
        if abs(det) >= 1e-12:
            r = 1.0 / det
            tx = (ux * dv2 - vx * dv1) * r
            ty = (uy * dv2 - vy * dv1) * r
            tz = (uz * dv2 - vz * dv1) * r
            bx = (vx * du1 - ux * du2) * r
            by = (vy * du1 - uy * du2) * r
            bz = (vz * du1 - uz * du2) * r
            cx = ny * tz - nz * ty
            cy = nz * tx - nx * tz
            cz = nx * ty - ny * tx
            w = 1.0 if (cx * bx + cy * by + cz * bz) >= 0 else -1.0
        for pi in (p0, p1, p2):
            if pos_nrm[pi] is None:
                pos_nrm[pi] = [0.0, 0.0, 0.0]
                pos_tan[pi] = [0.0, 0.0, 0.0]
            pos_nrm[pi][0] += nx; pos_nrm[pi][1] += ny; pos_nrm[pi][2] += nz
            pos_tan[pi][0] += tx; pos_tan[pi][1] += ty; pos_tan[pi][2] += tz
            pos_tanw[pi] += w
    normals = []
    tangents = []
    for i in range(len(verts)):
        a = pos_nrm[i]
        if a is None:
            normals.append((0.0, 0.0, 1.0))
            tangents.append((1.0, 0.0, 0.0, 1.0))
            continue
        l = math.sqrt(a[0] ** 2 + a[1] ** 2 + a[2] ** 2)
        nrm = (a[0] / l, a[1] / l, a[2] / l) if l > 1e-9 else (0.0, 0.0, 1.0)
        t = pos_tan[i]
        nd = t[0] * nrm[0] + t[1] * nrm[1] + t[2] * nrm[2]
        tx = t[0] - nrm[0] * nd; ty = t[1] - nrm[1] * nd; tz = t[2] - nrm[2] * nd
        tl = math.sqrt(tx * tx + ty * ty + tz * tz)
        if tl > 1e-9:
            tx, ty, tz = tx / tl, ty / tl, tz / tl
        else:
            if abs(nrm[0]) < 0.9:
                tx, ty, tz = 1.0, 0.0, 0.0
            else:
                tx, ty, tz = 0.0, 1.0, 0.0
            nd = tx * nrm[0] + ty * nrm[1] + tz * nrm[2]
            tx -= nrm[0] * nd; ty -= nrm[1] * nd; tz -= nrm[2] * nd
            tl = math.sqrt(tx * tx + ty * ty + tz * tz)
            if tl > 1e-9:
                tx, ty, tz = tx / tl, ty / tl, tz / tl
        w = 1.0 if pos_tanw[i] >= 0 else -1.0
        normals.append(nrm)
        tangents.append((tx, ty, tz, w))

    # 检测绕序是否反了（Blender 导出 OBJ 常与 Unity 相反）：有符号体积 < 0 表示里外翻。
    # 里外翻时：法线翻转 + 三角形绕序反转，否则背面剔除会让模型完全看不见。
    #
    # ⛔ 这个判据只对**近似闭合**的网格成立。对开放曲面/薄片（例如新做的旋翼桨叶做成
    #    平面或薄板），有符号体积 ≈ 0 或符号没有意义 ⇒ 可能翻转错误 ⇒ 游戏里因背面
    #    剔除而**完全不可见** ✗。所以只在体积足够大时才启用；否则保持原样并把决定权
    #    交回用户（Blender 里手动翻转法线即可）。
    svol = 0.0
    for (p0, _), (p1, _), (p2, _) in faces:
        ax, ay, az = verts[p0]; bx, by, bz = verts[p1]; cx, cy, cz = verts[p2]
        svol += ax * (by * cz - bz * cy) - ay * (bx * cz - bz * cx) + az * (bx * cy - by * cx)
    xs = [v[0] for v in verts] or [0.0]
    ys = [v[1] for v in verts] or [0.0]
    zs = [v[2] for v in verts] or [0.0]
    diag = max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)) or 1.0
    # 体积的相对量级（除以包围盒对角线立方）；太小就判定为"开放/薄片"，不翻转
    rel = abs(svol) / (diag ** 3)
    flip = svol < 0 and rel > 1e-4
    if svol < 0 and not flip:
        print("[网格] 有符号体积接近 0（相对 %.2e）：判定为开放/薄片网格，**不自动翻转**"
              "绕序 —— 若游戏里看不到模型，请手动翻转法线后重导" % rel)
    if flip:
        normals = [(-x, -y, -z) for x, y, z in normals]
        tangents = [(x, y, z, -w) for x, y, z, w in tangents]

    # ⛔ 传了目标网格 ⇒ 按**它自己的通道表**逐通道写（修 29 种布局里 95% 写错的问题）；
    #    没传 ⇒ 退回旧的写死 68 字节布局（其它调用方保持不变）
    if layout_from is not None:
        vertex_bytes = pack_by_layout(layout_from, faces, flip, verts, uvs,
                                      normals, tangents, skin, src_attrs=src_attrs)
        if n <= 65535:
            index_bytes = b"".join(struct.pack("<H", i) for i in range(n))
        else:
            index_bytes = b"".join(struct.pack("<I", i) for i in range(n))
        return vertex_bytes, index_bytes, n

    s0 = bytearray(); s1 = bytearray(); s2 = bytearray()
    for (p0, u0), (p1, u1), (p2, u2) in faces:
        order = ((p0, u0), (p2, u2), (p1, u1)) if flip else ((p0, u0), (p1, u1), (p2, u2))
        for pi, ui in order:
            x, y, z = verts[pi]
            nx, ny, nz = normals[pi]
            tx, ty, tz, tw = tangents[pi]
            s0 += struct.pack("<fff", x, y, z)
            s0 += struct.pack("<fff", nx, ny, nz)
            s0 += struct.pack("<ffff", tx, ty, tz, tw)
            u, v = uvs[ui] if ui >= 0 else (0.0, 0.0)
            s1 += struct.pack("<ee", u, v)
            s1 += struct.pack("<ff", u, v)
            b0, b1, w0, w1 = skin[pi]
            if skin_mode == 1:
                s2 += struct.pack("<I", b0)
            else:
                s2 += struct.pack("<ff", w0, w1)
                s2 += struct.pack("<II", b0, b1)

    def pad(b):
        b = bytearray(b)
        while len(b) % 16:
            b.append(0)
        return bytes(b)

    vertex_bytes = pad(s0) + pad(s1) + bytes(s2)
    if n <= 65535:
        index_bytes = b"".join(struct.pack("<H", i) for i in range(n))
    else:
        index_bytes = b"".join(struct.pack("<I", i) for i in range(n))
    return vertex_bytes, index_bytes, n
