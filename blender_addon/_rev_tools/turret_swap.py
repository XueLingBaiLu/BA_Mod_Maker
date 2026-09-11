# -*- coding: utf-8 -*-
r"""网格顶点数据构建（直接构造炮塔用）。

build_mesh_data：把顶点/UV/三角/蒙皮数据拼成 Unity 2022 顶点流字节。
法线平滑重算；切线按 UV 梯度计算（UV 退化时 W 默认 -1，匹配 Unity 约定）。
"""
import struct, math


def comp_size(fmt):
    return {0: 4, 1: 2, 2: 1, 3: 1, 4: 2, 5: 2, 6: 1, 7: 1, 8: 2, 9: 2, 10: 4, 11: 4}.get(fmt, 4)


def build_mesh_data(verts, uvs, faces, skin, skin_mode=2):
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
    svol = 0.0
    for (p0, _), (p1, _), (p2, _) in faces:
        ax, ay, az = verts[p0]; bx, by, bz = verts[p1]; cx, cy, cz = verts[p2]
        svol += ax * (by * cz - bz * cy) - ay * (bx * cz - bz * cx) + az * (bx * cy - by * cx)
    flip = svol < 0
    if flip:
        normals = [(-x, -y, -z) for x, y, z in normals]
        tangents = [(x, y, z, -w) for x, y, z, w in tangents]

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
