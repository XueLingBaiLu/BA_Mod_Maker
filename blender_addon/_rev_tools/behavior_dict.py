# -*- coding: utf-8 -*-
r"""行为词典：24 个 IAnimationBehaviour 类的**语义注解**（中文名 / 用途 / 字段标签 / 模板组）。

为什么要有这个模块（v1.8.67）：
  ④ 动画面板原本只对 5 个类（AxisRandom / MathConnect / Torque / FloatEffect /
  WaterFloatEffect）有像样的界面，另外 19 个类虽然在「新增」下拉里能选到、字节也能
  正确写出，但字段名是 `_wingLeft` / `_maxAngle` / `_activeVfx` 这种**裸标识符** ——
  用户根本不知道该填什么，等于"能加但不会用" ✗。
  这些类的真实语义其实**游戏自己就写好了**：dump.cs 里有作者写的 `[Tooltip]` 与
  有意义的字段名（`_verticalAmplitude`、`_dropDirection`、`OnStartReloading`…）。
  把它们抽出来做成词典，面板就能显示中文标签，并且给出**现成的模板组**。

本模块只放**注解**（纯数据、无副作用、不依赖 dump.cs）：
  - 结构（有哪些字段）来自 `behavior_meta` 注册表；
  - 语义（这个字段是什么）来自本模块。
两者按「类名 + 字段名」合并。

数据来源：`<工作目录>\工具制作资源\il2cpp\dump\dump.cs`
（抽取脚本：`<工作目录>\动画模块\extract_behavior_dict.py`）
"""

# ---------------------------------------------------------------------------
# 1. 类级注解
# ---------------------------------------------------------------------------
# cn      中文名（面板显示）
# desc    这个行为让模型做什么
# arrays  建议放进哪个数组（实测/语义推断）
# target  主目标字段的路径（面板置顶显示，其余字段折叠）
# danger  会删除/替换/销毁东西 —— 面板要标红提醒
_ARRAY_TIP = {
    "universal": "通用（战场 + 军械库都生效）",
    "demo": "军械库演示（**战场不播**）",
    "game": "战场游戏",
    "preDeath": "被打后（残血/受损）",
    "death": "死亡时",
}

CLASS_INFO = {
    "Torque": dict(
        cn="恒速自转", icon="FILE_REFRESH",
        desc="绕指定轴以固定角速度转 —— 直升机主桨/尾桨、雷达、风扇都靠它。",
        arrays=["game", "preDeath"], target="_target",
        nown="绕 (0,-1,0) 转 1080°/s（美系主旋翼实测值）"),
    "FloatEffect": dict(
        cn="空中浮动", icon="IPO_EASE_IN_OUT",
        desc="四个正弦通道让机体起伏：垂直 / 横滚 / 水平 / 前后，每通道一个速度 + 一个振幅。"
             "直升机滞空、气垫船漂浮都用它。",
        arrays=["game"], target="_rootBone",
        nown="振幅 0.35/0.25/0.33/0.25，速度 1.3/5.3/1.3/1.6（四架真机实测均值）"),
    "WaterFloatEffect": dict(
        cn="水面浮动", icon="IPO_EASE_IN_OUT",
        desc="FloatEffect 的水面版：只有垂直 + 旋转两个通道，幅度更小。",
        arrays=["game"], target="_rootBone", nown=""),
    "AxisRandom": dict(
        cn="随机扫掠", icon="DRIVER_ROTATIONAL_DIFFERENCE",
        desc="每隔 最小~最大间隔 秒，绕 X/Y/Z 各轴朝『源节点』方向随机扫一个角度。"
             "天线晃动、炮塔小幅摆动用它。",
        arrays=["demo"], target="_y.Source",
        nown="Y 轴跟随 turret_0，±45°"),
    "MathConnect": dict(
        cn="弹簧跟随", icon="CONSTRAINT",
        desc="像弹簧一样跟随另一个节点（频率/阻尼/反作用力）。炮塔跟随、悬挂、"
             "开火后坐力都靠它。_shotForces 还能让指定节点在开火时被推一下。",
        arrays=["universal"], target="_target", nown="root → turret_0"),
    "RotateBySpeed": dict(
        cn="速度姿态", icon="DRIVER_DISTANCE",
        desc="按单位当前速度在「静止位置」和「最高速位置」之间插值 —— 车轮转向、"
             "机翼后掠、悬挂压缩。",
        arrays=["game"], target="_target", nown=""),
    "AxisRepeater": dict(
        cn="往复摆动", icon="ARROW_LEFTRIGHT",
        desc="在 X/Y/Z 各自的 source→destination 之间来回摆 —— 雨刷、雷达旋转、"
             "起落架开合。与 AxisRandom 的区别是**确定性的往返**。",
        arrays=["game", "demo"], target="_xSource", nown=""),
    "PositionRandom": dict(
        cn="随机位移", icon="ORIENTATION_GLOBAL",
        desc="在 最小/最大范围 里随机游走，按 AnimMethod 插值（Lerp/WildLerp/Slerp/Towards）。",
        arrays=["demo", "game"], target="_target", nown=""),
    "Afterburner": dict(
        cn="加力尾焰", icon="OUTLINER_OB_LIGHTPROBE",
        desc="飞机开加力：左右舵面/机翼张开到最大角（_duration 秒内），同时在 _vfxPlace "
             "播放尾焰 VFX 与音效。",
        arrays=["game", "preDeath"], target="_wingLeft", nown=""),
    "AircraftTrails": dict(
        cn="翼尖拉烟", icon="PARTICLES",
        desc="当 _root 的姿态角超过 _angleToStartTrail 时，在 _vfxPoints 上拉起 VFX 尾迹。",
        arrays=["game"], target="_root", nown=""),
    "AnimatorConnect": dict(
        cn="动画状态机桥", icon="ARMATURE_DATA",
        desc="**唯一能播 Unity 关键帧 AnimationClip 的行为**：把游戏事件（装卸载 / 雷达 / "
             "静止起飞 / 换弹 / 瞄准 / 地形 / 舱门 / 开火）翻译成 Animator 的 Trigger 参数名。"
             "想让模型播自己做/改过的动画，就靠它。",
        arrays=["universal"], target="_animator", nown=""),
    "AudioPlayer": dict(
        cn="事件音效", icon="SPEAKER",
        desc="按 FMOD 事件路径播音效；可设成「开火时播」或「定时播（播完 destroyTimer 秒后销毁）」。",
        arrays=["game", "death"], target="_eventPath", nown=""),
    "ShowVFX": dict(
        cn="定点特效", icon="HIDE_OFF",
        desc="在若干 Points 上挂 Vfx 并控制显隐（炮口焰、灯光、损伤烟雾）。",
        arrays=["game", "death"], target="_data", nown=""),
    "SpawnVFX": dict(
        cn="生成特效组", icon="DUPLICATE",
        desc="按地形类型 / 权重分组，一次生成一批 VFX（扬尘、开炮烟、履带泥）。"
             "MinMaxSpawnCount 为 0 时用全部 vfx。",
        arrays=["game", "death"], target="_data", nown=""),
    "SetVFXProperty": dict(
        cn="特效属性", icon="OPTIONS",
        desc="往 VFX 里塞属性（粒子参数容器），让特效按状态变化。",
        arrays=["game"], target="_data", nown=""),
    "UnitBaseTerrainVFX": dict(
        cn="地形特效", icon="WORLD",
        desc="按单位所处地形类型切换特效（PlayOnlyOnTerrains 为空则不限地形）。",
        arrays=["game"], target="_data", nown=""),
    "DecalProjection": dict(
        cn="贴花投影", icon="IMAGE_REFERENCE",
        desc="控制一组 DecalProjector 的显隐（弹孔、履带印、油渍）。",
        arrays=["game"], target="_data", nown=""),
    "ProceduralDirt": dict(
        cn="程序化脏污", icon="TEXTURE",
        desc="给一组 Renderer 加程序化泥土遮罩（跑过泥地会变脏）。",
        arrays=["game"], target="_renderers", nown=""),
    "ShellCasingDrop": dict(
        cn="抛壳", icon="MESH_UVSPHERE",
        desc="开火时从抛壳口按物理参数（质量 / 线速度 / 角速度 / 阻力 / 生命期）抛出弹壳。",
        arrays=["game"], target="_shellSpawn", nown=""),
    "SpawnCorrector": dict(
        cn="生成位置校正", icon="EMPTY_ARROWS",
        desc="单位生成时校正目标节点的位置 / 旋转（车体贴地、炮塔归零）。",
        arrays=["universal"], target="_data", nown=""),
    "ModelReplace": dict(
        cn="模型替换", icon="MODELING",
        desc="事件时把 _remove 里的模型移除、把 _moveToRoot 搬过去、换上 _newModel"
             "（_delay 秒后）—— 毁伤换模型、展开支架。",
        arrays=["game"], target="_newModel", danger=True, nown=""),
    "DestroyMesh": dict(
        cn="销毁部件", icon="TRASH",
        desc="事件时销毁 _objects 里的 GameObject（被打掉后整块消失）。",
        arrays=["death", "preDeath"], target="_objects", danger=True, nown=""),
    "HideMesh": dict(
        cn="隐藏部件", icon="HIDE_ON",
        desc="事件时隐藏 _root 节点（比 DestroyMesh 轻，可恢复）。",
        arrays=["game", "preDeath"], target="_root", nown=""),
    "TurretFly": dict(
        cn="炮塔飞脱", icon="OUTLINER_OB_EMPTY",
        desc="死亡时炮塔按物理（质量/线速度/角速度）飞出去，可带 VFX 与音效、"
             "并可顺手移除炮塔。",
        arrays=["death"], target="Bone", danger=True,
        nown="Chance 是触发概率，Delay 是延迟秒数"),
}

# 面板上「危险」警告文案
DANGER_TIP = "⚠ 该行为会删除/替换/隐藏模型部件，先在备份上试"


# ---------------------------------------------------------------------------
# 2. 字段中文标签
# ---------------------------------------------------------------------------
# 键 = 类名；值 = {字段名: 中文}。字段名按 dump.cs 里的 C# 字段名（大小写敏感）。
FIELD_CN = {
    "Torque": {
        "_lodGroup": "LOD 阈值",
        "_target": "目标节点（转谁）",
        "_direction": "旋转轴（单位向量）",
        "_speed": "角速度（度/秒）",
    },
    "FloatEffect": {
        "_lodGroup": "LOD 阈值",
        "_rootBone": "浮动根节点",
        "_verticalSpeed": "上下·速度",
        "_verticalAmplitude": "上下·振幅",
        "_rotationSpeed": "横滚·速度",
        "_rotationAmplitude": "横滚·振幅",
        "_horizontalSpeed": "左右·速度",
        "_horizontalAmplitude": "左右·振幅",
        "_forwardBackwardSpeed": "前后·速度",
        "_forwardBackwardAmplitude": "前后·振幅",
    },
    "WaterFloatEffect": {
        "_lodGroup": "LOD 阈值",
        "_rootBone": "浮动根节点",
        "_verticalSpeed": "上下·速度",
        "_verticalAmplitude": "上下·振幅",
        "_rotationSpeed": "横滚·速度",
        "_rotationAmplitude": "横滚·振幅",
    },
    "AxisRandom": {
        "_lodGroup": "LOD 阈值",
        "_inspectorName": "备注名（只给人看）",
        "_speed": "扫掠速度",
        "_minimalTime": "最短间隔（秒）",
        "_maximalTime": "最长间隔（秒）",
        "_x": "X 轴",
        "_y": "Y 轴",
        "_z": "Z 轴",
        "Source": "源节点（朝它扫）",
        "MinimalAngle": "最小角",
        "MaximalAngle": "最大角",
    },
    "MathConnect": {
        "_lodGroup": "LOD 阈值",
        "_settings": "弹簧参数",
        "_root": "根节点（被跟随者·自身）",
        "_target": "目标节点（跟随谁）",
        "_freezeXPos": "冻结 X 位移",
        "_freezeYPos": "冻结 Y 位移",
        "_freezeZPos": "冻结 Z 位移",
        "_shotForces": "开火后坐力（节点 → 力度）",
        "_frequency": "频率（越大越硬）",
        "_damper": "阻尼",
        "_reaction": "反作用力（默认 -10）",
        "_defaultSerialized": "使用默认参数",
    },
    "RotateBySpeed": {
        "_lodGroup": "LOD 阈值",
        "_target": "目标节点",
        "_IdlePosition": "静止时旋转",
        "_FinalPosition": "最高速时旋转",
    },
    "AxisRepeater": {
        "_inspectorName": "备注名（只给人看）",
        "_xSource": "X 起点",
        "_xDistanation": "X 终点（原字段名拼写如此）",
        "_ySource": "Y 起点",
        "_yDistanation": "Y 终点",
        "_zSource": "Z 起点",
        "_zDistanation": "Z 终点",
    },
    "PositionRandom": {
        "_target": "目标节点",
        "_minRange": "最小偏移",
        "_maxRange": "最大偏移",
        "_speed": "速度",
        "_minTime": "最短间隔",
        "_maxTime": "最长间隔",
        "_animMethod": "插值方式",
    },
    "Afterburner": {
        "_wingLeft": "左舵面/机翼",
        "_wingRight": "右舵面/机翼",
        "_maxAngle": "最大张开角（度）",
        "_wingsDelay": "舵面延迟（秒）",
        "_duration": "到最大角所需时长（秒）",
        "_vfxPlace": "特效挂点",
        "_vfx": "特效 prefab",
        "_activeVfx": "同时生成的 VFX 列表",
        "_sfx": "音效事件名",
        "_effectsDelay": "特效延迟（秒）",
    },
    "AircraftTrails": {
        "_root": "姿态参考节点",
        "_updateTime": "更新间隔（秒）",
        "_angleToStartTrail": "启动尾迹的角度阈值（度）",
        "_vfxPoints": "尾迹点列表",
        "_vfx": "尾迹 VFX",
    },
    "AnimatorConnect": {
        "_animator": "目标 Animator",
        "_loadStartTrigger": "开始装载 Trigger",
        "_loadEndTrigger": "装载完成 Trigger",
        "_unloadStartTrigger": "开始卸载 Trigger",
        "_unloadEndTrigger": "卸载完成 Trigger",
        "_radarActivateTrigger": "雷达开启 Trigger",
        "_radarDeactivateTrigger": "雷达关闭 Trigger",
        "_staticPositionTrigger": "静止待机 Trigger",
        "_startMovingTrigger": "开始移动 Trigger",
        "_reloadTrigger": "换弹 Trigger",
        "_aimStartTrigger": "开始瞄准 Trigger",
        "_aimEndTrigger": "结束瞄准 Trigger",
        "_hangarExcludedTriggers": "军械库中**不播**的 Trigger",
        "_hangarOrderedTriggers": "军械库中按顺序播",
        "_hangarAnimationSpeed": "军械库播放倍速",
        "_weaponTriggers": "每把武器 → 开火/瞄准/换弹 Trigger",
        "_terrainTypeTriggers": "地形 → 进/出 Trigger",
        "_entranceTriggers": "舱门 → 开/关 Trigger",
        "_extraParameters": "额外 Animator 参数（名字 → int）",
        "OnShot": "开火 Trigger",
        "OnStartAiming": "开始瞄准 Trigger",
        "OnEndAiming": "结束瞄准 Trigger",
        "OnStartReloading": "开始换弹 Trigger",
        "OnEndReloading": "结束换弹 Trigger",
        "OnOpen": "打开 Trigger",
        "OnClose": "关闭 Trigger",
        "OnEnter": "进入 Trigger",
        "OnExit": "离开 Trigger",
    },
    "AudioPlayer": {
        "_eventPath": "FMOD 事件路径",
        "_isShotPlay": "开火时播",
        "_isTimerPlay": "定时播",
        "_destroyTimer": "播完多久销毁（秒）",
    },
    "ShowVFX": {
        "_data": "特效条目列表",
        "Points": "挂点列表",
        "Vfx": "特效 prefab 列表",
        "DefaultVfxType": "默认特效类型",
        "VfxHandler": "特效句柄",
        "PointParent": "挂点父级",
        "PointLocalPosition": "挂点本地坐标",
        "PointLocalRotation": "挂点本地旋转",
    },
    "SpawnVFX": {
        "_data": "特效组列表",
        "GroupName": "组名",
        "GroupWeight": "组权重（随机挑组用）",
        "TerrainType": "地形类型",
        "MinMaxSpawnCount": "生成数量范围（0 = 全部）",
        "DeathDelay": "死亡后延迟（秒）",
        "Data": "组内特效列表",
        "SpawnPoint": "生成挂点",
        "DynamicSpawnPoint": "挂点动态跟随",
        "Vfx": "特效 prefab",
        "AudioEvent": "音效事件",
        "SpawnAsChild": "作为子物体生成",
        "SpawnPointLocalPosition": "生成点本地坐标",
        "SpawnPointLocalRotation": "生成点本地旋转",
        "SpawnPointParent": "生成点父级",
    },
    "SetVFXProperty": {
        "_data": "属性容器",
        "BaseData": "基础属性列表",
    },
    "UnitBaseTerrainVFX": {
        "_data": "地形特效列表",
        "Points": "挂点列表",
        "OverrideVfx": "按地形覆盖的特效",
        "PlayOnlyOnTerrains": "只在这些地形播（空 = 不限）",
    },
    "DecalProjection": {
        "_decals": "贴花投影器列表",
    },
    "ProceduralDirt": {
        "_renderers": "受影响的渲染器列表",
    },
    "ShellCasingDrop": {
        "_shellSpawn": "抛壳触发节点（开火时判断）",
        "_prefab": "弹壳 prefab",
        "_initialRotation": "初始旋转",
        "_mass": "质量",
        "_spawnPoint": "生成点",
        "_spawnOffset": "生成点偏移（相对 spawnPoint 的本地坐标）",
        "_dropDirection": "抛出方向",
        "_randomFactor": "各轴随机量",
        "_linearForce": "线速度",
        "_linearDrag": "线性阻力",
        "_angularForce": "角速度",
        "_maxAngularVelocity": "最大角速度",
        "_angularDrag": "角阻力",
        "_lifeTime": "生命期（秒）",
        "_dropDelay": "抛出延迟（秒）",
        "_showDropDirection": "在编辑器里画出抛出方向",
        "_endPointSize": "终点显示大小",
    },
    "SpawnCorrector": {
        "_data": "校正条目列表",
        "Target": "校正目标",
        "NewRotation": "新旋转",
        "PositionOffset": "位置偏移",
        "ApplyRotation": "应用旋转",
        "ApplyPositionOffset": "应用位置偏移",
    },
    "ModelReplace": {
        "_root": "根节点",
        "_moveToRoot": "要搬过去的节点",
        "_remove": "要移除的模型",
        "_newModel": "换成的新模型",
        "_delay": "延迟（秒）",
    },
    "DestroyMesh": {
        "_objects": "要销毁的对象列表",
    },
    "HideMesh": {
        "_root": "要隐藏的节点",
    },
    "TurretFly": {
        "Bone": "炮塔根骨骼",
        "DestroyedRenderers": "摧毁后隐藏的渲染器",
        "DestroyedMaterial": "摧毁后换上的材质",
        "Colliders": "要禁用的碰撞体",
        "Mass": "质量",
        "LinearForce": "线速度",
        "AngularForce": "角速度",
        "Chance": "触发概率（0~1）",
        "Delay": "延迟（秒）",
        "RemoveTurretIfNotThrow": "不飞脱时也移除炮塔",
        "DeathVFXs": "死亡特效（随机取一个）",
        "VFXParentPosition": "特效父级挂点",
        "SoundEvent": "音效事件",
    },
}

# 枚举中文名（用于面板显示成员）
ENUM_CN = {
    "LODGroupEnum": {
        "LOD0": "LOD0（最精细）", "LOD1": "LOD1", "LOD2": "LOD2",
        "LOD3": "LOD3", "LOD4": "LOD4（最粗糙）",
    },
    "AnimMethod": {
        "Lerp": "线性插值", "WildLerp": "抖动线性", "Slerp": "球面插值",
        "Towards": "匀速靠近",
    },
}

# 字段级补充提示（面板上作为 tooltip 显示）
FIELD_TIP = {
    ("Torque", "_speed"): "实测：直升机 game=1080、preDeath=540",
    ("Torque", "_direction"): "美系主旋翼 (0,-1,0)、俄系 (0,1,0)、尾桨一律 (-1,0,0)",
    ("FloatEffect", "_rootBone"): "四架真机的 FloatEffect 全部指向 root",
    ("MathConnect", "_reaction"): "默认 -10（ObjectFollower.REACTION_DEFAULT）",
    ("SpawnVFX", "MinMaxSpawnCount"): "两个分量都为 0 时使用全部 vfx",
    ("AxisRandom", "_y"): "面板里 Y 轴默认跟随 turret_0、±45°；X/Z 默认无源",
}


# ---------------------------------------------------------------------------
# 3. 模板组（"自己制作动画"的起点）
# ---------------------------------------------------------------------------
# 每个模板项：
#   type   行为类
#   array  放进哪个数组（可写 "%A%" 表示按 operator 的数组参数）
#   @slot  节点占位符 —— 由操作符上同名参数填（root/turret/barrel/muzzle/shell/
#          main/tail/antenna/radar/target），解析成挂载点名
#   vals   字段路径 → 值；路径用 "." 连接，数组下标写 [n]
#
# ⛔ 值一律是**面板字段值的 JSON 表示**（跟 values_json 同一套），不是字节。
PRESETS = [
    # ---------------- 直升机 ----------------
    dict(key="heli_rotor", group="直升机", name="主旋翼 + 尾桨自转",
         desc="game 1080°/s 转、被打后减速到 540°/s；美系主旋翼绕 -Y、尾桨绕 -X。"
              "（四架真机实测约定）",
         items=[
             dict(type="Torque", array="game", vals={
                 "_lodGroup": 2, "_target": "@main",
                 "_direction": [0.0, -1.0, 0.0], "_speed": 1080.0}),
             dict(type="Torque", array="game", vals={
                 "_lodGroup": 2, "_target": "@tail",
                 "_direction": [-1.0, 0.0, 0.0], "_speed": 1080.0}),
             dict(type="Torque", array="preDeath", vals={
                 "_lodGroup": 2, "_target": "@main",
                 "_direction": [0.0, -1.0, 0.0], "_speed": 540.0}),
             dict(type="Torque", array="preDeath", vals={
                 "_lodGroup": 2, "_target": "@tail",
                 "_direction": [-1.0, 0.0, 0.0], "_speed": 540.0}),
         ]),
    dict(key="heli_float", group="直升机", name="滞空浮动",
         desc="四通道正弦浮动（上下 / 横滚 / 左右 / 前后），值取四架真机实测均值。",
         items=[dict(type="FloatEffect", array="game", vals={
             "_lodGroup": 1, "_rootBone": "@root",
             "_verticalSpeed": 1.3, "_verticalAmplitude": 0.35,
             "_rotationSpeed": 5.3, "_rotationAmplitude": 0.25,
             "_horizontalSpeed": 1.3, "_horizontalAmplitude": 0.33,
             "_forwardBackwardSpeed": 1.6, "_forwardBackwardAmplitude": 0.25})]),
    dict(key="heli_dust", group="直升机", name="旋翼扬尘（需指定特效）",
         desc="生成特效组：留空的数据条目不会有效果，需要填 _data 里的 Vfx 引用 —— "
              "建议直接从一个真机的 hub 抄（用「从 prefab 抄」按钮）。",
         items=[dict(type="SpawnVFX", array="game", vals={"_data": []})]),
    # ---------------- 履带 / 轮式 ----------------
    dict(key="tracks", group="履带/轮式", name="按速度转动",
         desc="按单位速度在静止姿态与最高速姿态之间插值 —— 车轮、负重轮、履带。",
         items=[dict(type="RotateBySpeed", array="game", vals={
             "_lodGroup": 1, "_target": "@target",
             "_IdlePosition": [0.0, 0.0, 0.0], "_FinalPosition": [0.0, 360.0, 0.0]})]),
    dict(key="suspension", group="履带/轮式", name="悬挂跟随",
         desc="弹簧跟随：让目标节点软化地跟着 root（颠簸时车体晃动）。",
         items=[dict(type="MathConnect", array="universal", vals={
             "_lodGroup": 1,
             "_settings": {"_frequency": 2.0, "_damper": 0.0, "_reaction": -10.0,
                           "_defaultSerialized": True},
             "_root": "@root", "_target": "@target",
             "_freezeXPos": False, "_freezeYPos": False, "_freezeZPos": True})]),
    # ---------------- 炮塔 ----------------
    dict(key="turret_follow", group="炮塔", name="炮塔跟随（+开火后坐）",
         desc="炮塔弹簧跟随 root；开火时按 _shotForces 把炮身后推。"
              "改完记得展开通用字段把 _shotForces 的节点填上。",
         items=[dict(type="MathConnect", array="universal", vals={
             "_lodGroup": 1,
             "_settings": {"_frequency": 2.0, "_damper": 0.0, "_reaction": -10.0,
                           "_defaultSerialized": True},
             "_root": "@root", "_target": "@turret",
             "_freezeXPos": False, "_freezeYPos": False, "_freezeZPos": True})]),
    dict(key="turret_idle", group="炮塔", name="炮塔待机微晃",
         desc="军械库演示里的炮塔小幅随机摆动（原版放 demo 数组）。",
         items=[dict(type="AxisRandom", array="demo", vals={
             "_lodGroup": 1, "_inspectorName": "Turret", "_speed": 20.0,
             "_minimalTime": 2.0, "_maximalTime": 10.0,
             "_x": {"Source": 0, "MinimalAngle": 0.0, "MaximalAngle": 0.0},
             "_y": {"Source": "@turret", "MinimalAngle": -45.0, "MaximalAngle": 45.0},
             "_z": {"Source": 0, "MinimalAngle": 0.0, "MaximalAngle": 0.0}})]),
    dict(key="shell_drop", group="炮塔", name="抛壳",
         desc="开火时从抛壳口抛出弹壳（需要 _prefab 指到一个弹壳 prefab）。",
         items=[dict(type="ShellCasingDrop", array="game", vals={
             "_shellSpawn": "@muzzle", "_initialRotation": [0.0, 0.0, 0.0],
             "_mass": 0.02, "_dropDirection": [0.6, 0.4, 0.0],
             "_randomFactor": [0.2, 0.2, 0.2], "_linearForce": 1.4, "_linearDrag": 0.1,
             "_angularForce": 12.0, "_maxAngularVelocity": 30.0, "_angularDrag": 1.0,
             "_lifeTime": 6.0, "_dropDelay": 0.05})]),
    # ---------------- 飞机 ----------------
    dict(key="afterburner", group="飞机", name="加力尾焰",
         desc="左右舵面张开 + 尾焰 VFX + 音效（需要填 _vfx / _activeVfx）。",
         items=[dict(type="Afterburner", array="game", vals={
             "_wingLeft": "@wingL", "_wingRight": "@wingR",
             "_maxAngle": 25.0, "_wingsDelay": 0.4, "_duration": 0.8,
             "_effectsDelay": 0.2})]),
    dict(key="trails", group="飞机", name="翼尖拉烟",
         desc="机身姿态角超过阈值时拉起 VFX 尾迹。",
         items=[dict(type="AircraftTrails", array="game", vals={
             "_root": "@root", "_updateTime": 0.05, "_angleToStartTrail": 25.0})]),
    # ---------------- 通用（在无 AnimationHub 的模型上从零做动画） ----------------
    dict(key="radar_spin", group="通用", name="雷达旋转",
         desc="让某个节点恒速自转 —— 雷达天线、风扇、螺旋桨。",
         items=[dict(type="Torque", array="game", vals={
             "_lodGroup": 1, "_target": "@antenna",
             "_direction": [0.0, 1.0, 0.0], "_speed": 90.0})]),
    dict(key="wiper", group="通用", name="雨刷/开合往复",
         desc="在起点节点与终点节点之间确定性往复摆动。",
         items=[dict(type="AxisRepeater", array="game", vals={
             "_inspectorName": "Wiper", "_xSource": "@target", "_xDistanation": "@source"})]),
    dict(key="antenna_shake", group="通用", name="天线晃动",
         desc="三轴随机小幅扫掠，让静止的模型不那么死板。",
         items=[dict(type="AxisRandom", array="game", vals={
             "_lodGroup": 1, "_inspectorName": "Antenna", "_speed": 8.0,
             "_minimalTime": 1.0, "_maximalTime": 3.0,
             "_x": {"Source": 0, "MinimalAngle": -3.0, "MaximalAngle": 3.0},
             "_y": {"Source": 0, "MinimalAngle": -3.0, "MaximalAngle": 3.0},
             "_z": {"Source": 0, "MinimalAngle": 0.0, "MaximalAngle": 0.0}})]),
    dict(key="hide_part", group="通用", name="隐藏部件",
         desc="事件时隐藏指定节点（战斗中被击毁的可选表现）。",
         items=[dict(type="HideMesh", array="preDeath", vals={"_root": "@target"})]),
    dict(key="destroy_part", group="通用", name="销毁部件（死亡时）",
         desc="死亡时销毁指定 GameObject。⚠ 会真的删模型，先在备份上试。",
         items=[dict(type="DestroyMesh", array="death", vals={"_objects": []})]),
    dict(key="terrain_vfx", group="通用", name="地形特效",
         desc="按地形类型切换特效（泥地扬尘 / 沙地扬尘）。",
         items=[dict(type="UnitBaseTerrainVFX", array="game", vals={"_data": []})]),
]

PRESET_BY_KEY = {p["key"]: p for p in PRESETS}

# 操作符上要暴露的节点槽位（@名字 → (面板标签, 默认值, 说明)）
NODE_SLOTS = {
    "root": ("根节点", "root", "车体/机体根 —— 多数行为的挂载基准"),
    "main": ("主旋翼", "Rotorangle_0", "美系 Rotorangle_0 / 苏系 rot_blade_1"),
    "tail": ("尾桨", "Rotorangle_1", "美系 Rotorangle_1 / 苏系 tail"),
    "target": ("目标节点", "", "留空 = 保持元数据默认（通常是自身）"),
    "turret": ("炮塔", "turret_0", "炮塔根节点"),
    "barrel": ("炮管", "gun_0", "炮管节点"),
    "muzzle": ("炮口/抛壳口", "shell_spawn_0", "开火特效与抛壳的生成点"),
    "antenna": ("天线/雷达", "", "要转/要晃的那个节点"),
    "source": ("起点节点", "", "往复摆动的起点"),
    "wingL": ("左舵面", "", "加力时张开的左侧部件"),
    "wingR": ("右舵面", "", "加力时张开的右侧部件"),
}


# ---------------------------------------------------------------------------
# 4. 查询接口
# ---------------------------------------------------------------------------
def info(cls):
    """类级注解（缺失时给一个安全的空壳）。"""
    return CLASS_INFO.get(cls) or dict(cn=cls, icon="QUESTION", desc="", arrays=[],
                                       target="", nown="")


def cn_name(cls):
    return info(cls)["cn"]


def arrays_for(cls):
    return list(info().get("arrays") or [])


def type_items(all_classes, extra=None):
    """类型下拉的 items：`(标识, 显示名, 说明)`。显示名 = 中文名（类名）。"""
    out, seen = [], set()

    def _add(c):
        if not c or c in seen:
            return
        seen.add(c)
        i = info(c)
        label = "%s（%s）" % (i["cn"], c) if i["cn"] != c else c
        tip = i["desc"] or "来自 IL2CPP dump 的行为类"
        if i.get("danger"):
            tip = DANGER_TIP + " · " + tip
        out.append((c, label, tip[:1024]))

    for c in (extra or []):
        _add(c)
    for c in sorted(CLASS_INFO, key=lambda k: info(k)["cn"]):
        _add(c)
    for c in (all_classes or []):
        _add(c)
    return out


def label(cls, path, name):
    """字段中文标签。path = 从根起的路径（含下标），name = 字段名。

    先试「类 + 完整路径」（能区分 `_y.Source` 与 `_x.Source`），
    再试「类 + 字段名」，最后退回原来的下划线转空格。
    """
    tbl = FIELD_CN.get(cls) or {}
    if path:
        dotted = ".".join(str(p) for p in path if not isinstance(p, int))
        if dotted in tbl:
            return tbl[dotted]
    if name in tbl:
        return tbl[name]
    return (name or "").replace("_", " ").strip() or (name or "")


def tip(cls, path, name):
    """字段提示（没有返回空串）。"""
    if (cls, name) in FIELD_TIP:
        return FIELD_TIP[(cls, name)]
    if path:
        dotted = ".".join(str(p) for p in path if not isinstance(p, int))
        if (cls, dotted) in FIELD_TIP:
            return FIELD_TIP[(cls, dotted)]
    return ""


def enum_items(enum_name, members):
    """枚举成员 → `(标识, 显示名, 说明)`。"""
    tbl = ENUM_CN.get(enum_name) or {}
    out = []
    for nm, val in (members or []):
        out.append((nm, tbl.get(nm, nm), ""))
    return out


def array_label(a):
    return _ARRAY_TIP.get(a, a)


def preset_items():
    """模板下拉：`(key, "组 · 名称", 说明)`。"""
    return [(p["key"], "%s · %s" % (p["group"], p["name"]), p["desc"])
            for p in PRESETS]
