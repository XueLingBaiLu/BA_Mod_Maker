# Blender 工具词典（断箭模组制作 · 系统性词典）

> 覆盖 Blender 插件（BA Mod）所需全部知识：数据结构 / 挂载点 / 组件 / 动画 / 皮肤 /
> 步兵姿势 / 地址与常量。数据库 24 表字段释义见《数据库词典.md》。

---

## 1. 数据结构

| 主题 | 要点 |
|---|---|
| bundle（UnityFS） | units bundle `aa/PC/units_assets_all_3cc1eb58d8b7f6cdf82bbfbf3b5b8aaf.bundle`（3.66GB）；块压缩 LZ4；对象 pathID=int64（可为负）；PPtr={i32 fileID + i64 pathID} |
| bundle CRC | `zlib.crc32(所有数据块解压后字节按序拼接)`；改 bundle 必跑 `compute_bundle_crc.py --update-catalog`；游戏报 `CRC Mismatch` 时日志里的 calculated 值可直接写回 catalog |
| catalog 寻址 | Addressables 1.19+：InternalId=资产路径；所在 bundle 经 DependencyKey（依赖集桶第一个条目）解析；文件名锚点 = AssetBundleRequestOptions.m_Hash |
| .bamod 素材包 | zip(manifest.json)：`{format:"bamod-assets", bundle, prefab_path, root_pid, preload, objects[{pid,class_id,script_id,tree_hash,raw(b64)}]}`；导入时按类型匹配 → 新 pid 单遍字节重映射 → 补容器/preload → CRC。皮肤包另含 `skin:{...}`，贴图替换包另含 `matswap:{...}` |
| 网格顶点流 | 通道索引：0=位置 1=法线 2=切线 4=UV0 5=UV1 12=蒙皮权重 13=骨骼索引；format 0=float32 1=float16 10=uint32 11=int32；内联数据每流 16B 对齐；流式在 `<bundle>_unpacked/CAB-*.resS` |
| 骨骼名哈希（已破解） | `zlib.crc32(从 root 起的层级路径, '/' 连接, UTF-8)`；跳过 root_scale；多索引槽位名取首个 `_N`（weapon_0_1_2→weapon_0，turret_0_0→turret_0，非纯数字尾缀不截断如 frontwing3_3_L）。例：crc32("root/body")=0x65A7524A、crc32("root/body/turret_0")=0x2A839EFE。工具用 `bone_hashes.bone_hash()` 直接计算 |
| 蒙皮 | Mesh 的 m_BoneNameHashes 按上述算法写入；m_BindPose = 世界矩阵的逆；切线 W 必须 -1 |
| 皮肤桥（SkinStorageBridge） | MonoBehaviour 头 32B（含空 m_Name）后：`i32 N`；每条 `i32 Id + i32 keyCount + keyCount×PPtr(材质12B) + i32 valCount + valCount×{i32 rCount + rCount×PPtr(渲染器12B)}`。实测 322 个桥全部在炮塔(316)/步兵(6) prefab 上；车体/枪械无桥 |
| 步兵装配 | `Units.ModelFileName`=单位锚点（仅 weapon_0_1/recoil_0/shell_spawn_0 等挂点）；`SquadMembers.ModelFileName`=完整士兵 prefab（48 骨骼 + 3 蒙皮网格 + 3 纹理 + Avatar + Animator + 22 AnimationClip + LODGroup + 皮肤桥，preload 137 对象）；姿势=烘焙在骨骼本地 Transform |

## 2. 挂载点

挂载点 = 无渲染器的 GO（骨骼/功能点），导入 Blender 后是空物体（ba_mount）。
核心分类与命名规律（历史全游戏扫描共 690 个挂载点名，高频名：shell_spawn_0(1083)、
weapon_0(964)、VFX_point(739)、root(676)、body(672)、turret_0(494)、death(198)、
turret_1(174)、dust_point_left/right(169)、exhaust(148) 等）：

| 分类 | 识别 | 说明 |
|---|---|---|
| 结构 | root / body | root=prefab 根（组件挂这）；body=根骨骼（蒙皮 m_RootBone，y≈0.863） |
| 炮塔 | turret_N | 旋转座（TI 驱动）；turret_0_0=子转塔；turret=无编号座 |
| 武器 | weapon_N / weapon_0_1 / weapon_0_1_2 | 俯仰点（weapon_炮塔_武器_身管）；TI 的 WeaponPointData 引用 |
| 弹药/抛壳 | shell_spawn_X_Y | 开火/抛壳口；带 (N) 后缀=多管逐个口 |
| 后坐 | recoil_N_M | 开火后坐骨（recoil_武器_骨），AnimationManagerBridge 驱动 |
| 观瞄/传感器 | scope / radar / antenna / SpecialScope | 常被演示动画 AxisRandom/MathConnect 引用 |
| 特效 | fire_y/z / turret_fly_VFX / VFX_point / exhaust / dust / death / Afterburner | VFX 出生点 |
| 辅助/约束 | helper / aim / anim_* / WeaponLook / WeaponPlace | 瞄准辅助、IK 目标、约束参照、动画容器 |
| 舱门/座位 | Entrances / Door / Seats / Seat (N) | 进出舱逻辑挂载 |
| 轮系/履带 | Track_* / LR000-LR010 / RR000-RR010 / L001-L006 | 悬挂/行走动画驱动 |
| 飞行/旋翼 | Rotorangle_* / wing_* / Engine_* / joint_* | 飞行器部件 |

关键条目：turret_0（必须有）、weapon_0_1（双联装）、shell_spawn_0、recoil_0_0、scope_0、
antenna、VFXPoint_OnSmokeAbility、WeaponLook（RU 步兵左手持枪）、WeaponPlace（US 步兵右手）。

## 3. 组件（IL2CPP MonoBehaviour）

| 组件 | 字段要点 |
|---|---|
| UnitPrefabTurretInfo | TurretIndex(int32 炮塔下标) + Weapons(list 每项 24B：{WeaponTransform(PPtr)→weapon_N, ShellSpawn(PPtr)→shell_spawn_N}) |
| WeaponPrefabInfo | ShellSpawns(每项 {ShellSpawn(PPtr)→发射口, AmmunitionObject(PPtr)→弹药模型, EmptyLauncherCover(PPtr)→空筒盖}) |
| EffectsSpawnPoint | VFXPrefabRef(PPtr) · LifeTime(float) · AttachVfxToObject(bool) · PlayForce(bool) · SoundEventString |
| AnimationManager | _root/_body/_wheels/_suspension/_traks(车体动画) · _recoils(dict 武器→recoil_N) · _weaponsVfxContainer(dict 武器→炮口特效) |
| UnitPrefabRoot | PrefabRoot / PrefabBody / SpawnTimeObjects / Renderers / Skins.Data |
| ContainerSeatInitializer | UnitSeatAnimation → 座位动画 |
| InfantryHandWeapon | ShellSpawnPosition → 枪口 |
| PlaneInitializer | PilotID → 飞行员 |
| FmodTurretsTurn | EventName / TargetBone（炮塔转向音效） |
| FastIKFabric | 士兵脚部 IK |
| SkinStorageBridge | 换肤桥（布局见「数据结构」） |
| DecalProjector / DriverMarker / DestroyInBattleMarker / UnitPersistentObjectMarker / MaterialQualityManager / PhysFly / SoldierAnimationManager | 标记类，无字段 |

## 4. 动画（AnimationHub）

| 主题 | 要点 |
|---|---|
| 数组（array） | universal=战场+军械库；demo=军械库演示；game=战场游戏；preDeath=死亡前；death=死亡 |
| AxisRandom（随机扫掠） | 字段：speed / min_time / max_time / lod / x·y·z 源骨骼名与最小·最大角。源填骨骼名（antenna、scope_0 等），该骨骼按角度范围随机扫掠 |
| MathConnect（跟随） | 布局：lod(i32) + ObjectFollower{freq,damper,reaction,defaultSerialized}(16B) + root PPtr + target PPtr + freeze 3 bool(4B) + WeaponShotForces 字典 |
| WeaponShotForces 字典 | `{i32 1, i32 0, N, N×{i32 0 + i64 TransformPid}, N, N×float}`：骨骼→力度；面板结构化显示，rawB64 兜底无损 |
| 行为类型 | 22 个行为类（AxisRandom/AxisRepeater/SpawnVFX/ShowVFX/AnimatorConnect/TurretFly/ShellCasingDrop 等，含嵌套类型）；未完全破译的类按字节透传（rawB64） |
| 写回方式 | ④ 构建（copy-full）勾选「应用动画」→ 面板/文本块 bamod_anim 的 JSON 重新序列化进 AnimationHub |

## 5. 皮肤

| 主题 | 要点 |
|---|---|
| 机制 | 皮肤=材质替换：SkinStorageBridge 存 {皮肤Id + {材质PPtr→渲染器[]}}；游戏 SetSkin 按 id 把材质赋到渲染器 |
| 分布 | 322 个桥：炮塔 316 + 步兵 6；车体/武器 prefab 无桥——**枪械没有皮肤机制**（游戏设计如此） |
| 皮肤材质 | HDRP：纹理槽名 `Layer_<hash>`（如 Layer_A97CDC25）；BaseMap 贴图名如 T90_1_BaseMap；贴图多为流式（.resS），提取 PNG 需要 `<bundle>_unpacked` 目录 |
| 菜单配置 | UserItemsConfig（data.unity3d，MonoBehaviour pid 52173，220KB 无 typetree）管皮肤菜单（SkinGroupDict/SkinPackDict）；**重涂现有皮肤槽无需改它**；新增皮肤槽进菜单需改该资产（暂未工具化） |
| 皮肤重涂流程 | ① 导入炮塔变体（坦克炮塔 prefab 自带车体网格）→ ⑥ 扫描 → 应用（材质+贴图实时显示；未覆盖部件回退默认材质）→ 导出贴图 → 改 PNG → 打包皮肤 .bamod → BA_Mod_Maker 导入（新纹理 + 克隆材质换纹理槽 + 按 id 更新全部桥 + CRC） |
| 枪械贴图替换 | 无桥模型：① 导入 → ⑥ 默认材质看外观 → 导出贴图（无皮肤时自动导出默认材质贴图）→ 改 PNG → 打包贴图替换包 → 导入（克隆材质 + 原地更新该模型渲染器 m_Materials，任何时候生效） |

## 6. 步兵姿势

| 主题 | 要点 |
|---|---|
| 装配 | 单位锚点（Units.ModelFileName）+ 士兵成员 prefab（SquadMembers.ModelFileName）组合 |
| 士兵结构 | 48 骨骼（Hips→Spine→Spine1→Spine2→Neck→Head、双臂/双手/五指、双腿/双脚/脚趾）+ WeaponLook/WeaponPlace/Heavy Weapons + lod_0/1/2；3 蒙皮网格 + 3 纹理 + Avatar + Animator + 22 AnimationClip + LODGroup + 皮肤桥 |
| 姿势机制 | 姿势=烘焙在骨骼本地 Transform（RightHand 局部旋转即持枪姿势）；改姿势只动骨骼，网格/bind pose 不变 |
| 角色变体 | 同名系列 `_rifle/_MG/_officer/_grenade/_Weapon/_heavy/_rifle1/_rifle_light`；RU 系左手持枪 + WeaponLook，US 系右手 + WeaponPlace |
| Blender 流程 | ① 导入士兵（自动建 Armature + 骨骼修改器，网格随姿势实时变形）→ ⑦ 滑条摆姿势 / 从其他游戏模型按骨骼名复制姿势 / 姿势库 → 打包姿势模型（copy-full 只 patch 骨骼 Transform，网格/动画/材质原样）→ BA_Mod_Maker 导入 → 新增模型时 DB 把 `SquadMembers.ModelFileName` 改成新模型名 + 注册地址；原地替换则不用改数据库 |

## 7. 地址与常量

### 地址映射（数据表地址 ↔ bundle 内部路径）

| 类型 | 地址 | 内部路径 |
|---|---|---|
| 车体/载具 | RU_BMPT | Assets/Resources_moved/ModelPrefabs/RU/RU_BMPT.prefab |
| 炮塔 | RU_BMPT/RU_BMPT1 | Assets/Resources_moved/ModelPrefabs/RU/Turrets/RU_BMPT/RU_BMPT1.prefab |
| 新炮塔(例) | RU_BMPT/RU_BMPT2_MOD | Assets/Resources_moved/ModelPrefabs/RU/Turrets/RU_BMPT/RU_BMPT2_MOD.prefab |
| 飞机 | RU_AN72P | Assets/Resources_moved/ModelPrefabs/Aircraft/RU_AN72P.prefab |
| 直升机 | RU_KA52 | Assets/Resources_moved/ModelPrefabs/Helicopters/RU_KA52.prefab |
| 机载武器/导弹 | AIM9_double | Assets/Resources_moved/ModelPrefabs/Weapon/AIM9/AIM9_double.prefab |
| 步兵武器 | A545 | Assets/Resources_moved/ModelPrefabs/Infantry Weapons/RU/A545.prefab |
| 弹药模型 | 100mm_RUS | Assets/Resources_moved/Shared/Prefabs/Ammo/100mm_RUS.prefab |
| 单位肖像 | RU\BMPT_TERMINATOR\BMPT_TERMINATOR | Assets/Resources_moved/Images/UnitPortraits/RU/BMPT_TERMINATOR/BMPT_TERMINATOR.png |
| 缩略图/标签 | RU_BMPT-Label | Assets/Resources_moved/Images/Labels/Icons/RU_BMPT-Label.png |
| 武器图标 | 2A72 | Assets/Resources_moved/Images/Weapons/Icons/2A72.png |
| 弹药图标 | AMMO_US_TANK_120_AP | Assets/Resources_moved/Images/Ammunition/Icons/AMMO_US_TANK_120_AP.png |

规则：新模型内部路径 = 原模型路径换名加后缀（如 _MOD）保持唯一；数据表地址与内部路径同名
（炮塔为 目录名/文件名 两段）；前缀 RU=俄罗斯 / US=美国，DLC 单位在 DLC3/ 等子目录。
改模型三步：bundle 加 prefab（.bamod 导入）→ catalog 注册地址 → 数据表 ModelFileName 指向新地址。

### 关键常量

| 常量 | 值 | 用途 |
|---|---|---|
| 数据库魔数 | fhk3s0g3 (8B) | 加密字段前缀 |
| 数据库 AES 密钥 | ASCII 09234237536700238099172758697347 | AES-256-CBC |
| mod 对象 pid 段(units) | 0x4355424500000000("CUBE") ~ +0x10000 | 工具新增对象专属，重导先清理 |
| mod 对象 pid 段(肖像) | 0x4355424600000000("CUBF") | unitportraits bundle 新增对象 |
| UnitPrefabTurretInfo script pid | 6426374804064612000 | MonoBehaviour m_Script 指向 |
| AnimationHub script pid | 4665939560152279323 | 军械库演示动画 |
| AnimationManagerBridge script pid | 8775279424834731323 | 后坐/开火联动 |
| FmodTurretsTurn script pid | -1118201132209727813 | 炮塔转向音效 |
| units bundle | aa/PC/units_assets_all_3cc1eb58d8b7f6cdf82bbfbf3b5b8aaf.bundle | 模型主存放区 |
| 炮塔骨骼高度 | body y=0.863；turret_0 在 body 上方 0.811（世界 y≈1.674） | 炮塔几何必须在 y≈1.674 |
| 游戏主命名空间 | BrokenArrow（Steel Balalaika Studio） | IL2CPP 类所在 |
