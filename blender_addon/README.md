# BA Mod Maker —— Blender 插件 v2.5.0

把「导入游戏模型（参考）→ 建模 → 蒙皮 → 直接构造 prefab → 写回 .bamod」放进 Blender。
不丢骨骼权重、不丢 UV 接缝、法线贴图切线正确（W 符号已匹配 Unity 约定）。

## 安装

1. **安装插件**：Blender → 编辑 → 偏好设置 → 插件 → 安装 → 选 `BA_Mod_Maker_blender_addon.zip` → 勾选启用。
2. **配置**：偏好里点「自动检测并填充路径」自动填游戏 bundle / UnityPy 目录 / 工具目录。
   - `_rev_tools` 与 `_unitypy`（UnityPy 运行时）已内置在插件 zip 里，**不用单独安装**；
     若 zip 内运行时与 Blender 自带 Python 版本不匹配，才需 pip 安装 UnityPy。

## 使用（侧边栏「BA Mod」面板）

1. ① 模型导入：刷新 prefab 列表 → 选中 → 导入（挂载点 Empty 树 + 蒙皮网格；
   自动处理流式 .resS、root_scale、ParentConstraint）。
2. ② 挂载点：查看/添加/删除挂载点，所见即所得摆放；④ 工具支持权重转移。
3. ④ 构建写回：挂载点树 + 蒙皮网格 → 新 prefab（含 TI / LODGroup / Hub 组件），
   默认导出 `.bamod` 素材包；用 BA_Mod_Maker（exe）导入进游戏（自动合并 + CRC）。
4. ③ 词典：挂载点 / 组件 / 模板 / **地址与常量**（数据结构要点、地址映射、关键常量）
   —— 全部模组知识收纳于此，无独立教程。

## 挂点命名约定

| 名字 | 类型 | 作用 |
|---|---|---|
| `turret_N` / `turret_N_M` | 顶点组（骨骼）/ Empty | 炮塔旋转座；方块蒙皮到它才跟着转 |
| `weapon_N` | Empty | 俯仰挂点 |
| `shell_spawn_N` | Empty | 开火点（+Z 射向） |
| `recoil_N` | 骨骼 | 后坐动画（须真实蒙皮骨骼） |

后缀相同的 `weapon_N` ↔ `shell_spawn_N` 自动配对；不放 Empty 时默认 weapon_0 / shell_spawn_0 在原点。
炮塔几何必须抬到世界 y≈1.674（body y=0.863 + turret_0 相对 0.811）。

## 注意事项

- 换现有模型外形只能**移动顶点**（增删顶点/改拓扑会破坏权重动画）；直接构造的新模型拓扑任意。
- 骨骼名必须用词典内的标准名（bone_hashes.py 查找表，哈希算法未还原）。
- 改完 bundle 必须更新 CRC（exe 导入 .bamod 时自动处理；手动改时跑 `compute_bundle_crc.py --update-catalog`）。
- 完整知识见 BA_Mod_Maker 的《数据库词典.md》与插件内置词典。
