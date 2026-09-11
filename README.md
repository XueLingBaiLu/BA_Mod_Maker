# BA Mod Maker · 断箭模组工具

面向《**Broken Arrow（断箭）**》的模组制作套件：**桌面端数据库编辑器** + **Blender 插件**。

> ⚠️ **本仓库不含任何游戏资源** —— 模型、贴图、图标、`.bundle`、`data.unity3d` 等**均不在**本仓库内，
> 也不随发布包分发。使用本工具需要你**自备正版游戏**；游戏资源版权归其权利人所有。
>
> ⚠️ 仅供**单机模组制作与学习**使用，请勿用于联机作弊或绕过游戏保护。修改前请**备份存档与游戏文件**。
>
> 📦 代码以 **MIT** 发布（见 `LICENSE`）；捆绑的第三方库各自保留原许可（见 `THIRD_PARTY_NOTICES.md`）。

---

# BA Mod Maker — 断箭 Mod 制作工具 v1.8.56

[English](#english) | [Русский](#russian)

一个用于制作《断箭》(Broken Arrow) MOD 的数据库编辑与素材导入工具。
可打开由 UABEA 从 `data.unity3d` 导出的 `DataBaseCompiled` 资产，编辑全部 24 张数据表，
保存时**自动重新加密、保持原文件名**；同时支持把 Blender 插件导出的 **.bamod 素材包**
（模型/皮肤/姿势）合并进游戏 bundle。

配套 Blender 插件（`BA_Mod_Maker_blender_addon.zip`，v2.7.56）负责模型侧：
提取游戏模型、可视化编辑挂载点、构建写回 .bamod、动画编辑、皮肤重涂。
（注：早期版本的「步兵姿势/动画」功能已于 v1.8.52 整体移除。）

## 文档

- **《使用教程-BA_Mod_Maker.md》** —— 本工具全部功能的操作教程（数据库编辑、.bamod 导入、
  图片导入、四个标准工作流、常见问题）。
- **《使用教程-Blender插件.md》** —— Blender 插件全部面板（①导入 ②挂载点 ③词典 ④构建
  ⑤动画 ⑥皮肤 ⑦步兵姿势）的操作教程与工作流速查。
- **《数据库词典.md》** —— 唯一词典文档：24 张表全字段释义、实证枚举值、数据结构与文件格式
  （数据库加密 / bundle+CRC / catalog / .bamod / IL2CPP 组件 / 网格顶点流 / 载具皮肤 / 步兵姿势）、
  地址映射与内部路径、常量速查、挂载点与组件词典、防崩溃铁律。
- `Change Log.txt` —— 版本历史。

## 功能

- 打开 UABEA 导出的数据库 JSON（自动解密：AES-256-CBC），24 张表全量编辑
- 搜索、按列筛选、类型化字段编辑；外键自动显示指向名称，按名称或 Id 都能搜到
- 添加 / 复制 / 删除行（自动分配新 Id）；撤销 / 重做（Ctrl+Z/Y，最多 100 步）
- 字段悬浮提示 + 内置词典窗口（中英俄），枚举值已按 Il2CppDumper 实证值修正
- **绿色箭头 →（枚举与外键统一字形）**：单位大类 `Type` / 槽位类别 `CategoryType` / 角色 `Role`（以及武器类型、
  弹道/目标类型等全部枚举字段）右侧直接显示该 ID 对应的种类（`Role=11` → 绿色 `→ 主战坦克 (Tank)`），
  点箭头打开词典对照表；外键箭头点击跳到被引用行；**位掩码按加法拆解**
  （`TargetType=36` → `→ 舰船 + 载具 (32 + 4)`）；未知值 / 未知位黄色告警（游戏会走兜底分支，不会崩）
- F7 校验：重复 Id、缺失 UnitPropulsions、武器缺弹药、装甲缺默认等
- 查找引用（哪些行引用了当前行 Id，双击跳转）
- 双击行 → 钻取编辑窗口：整行字段 + 关联树（技能/炮塔/武器/弹药/装甲/机动/改装）
- 名称查 Id（工具栏常驻输入框，F3/Ctrl+F 聚焦）
- 复制 / 克隆为新单位（连带炮塔/武器/弹药/装甲等关联数据复制）
- 保存并加密：写回原文件名（旧文件自动备份 .bak）
- 导出解密表文件夹 / 从文件夹导入
- **导入 .bamod 素材包**（模型/炮塔，自动合并进游戏 bundle + 自动 CRC + 可选注册地址）
- **导入图片/图标/肖像**（多选图片，每行自定义容器路径与映射地址，按类别自动导入对应
  bundle——肖像/标签/武器图标/弹药图标，自动 CRC + 注册地址；也可只打包 .bamod）
- **打包图标 / 肖像**（Texture2D+Sprite 成对写进 unitportraits bundle + 自动 CRC）
- 界面语言：中文 / English / Русский（F10）

## 快速上手

**所有功能的操作教程见《使用教程-BA_Mod_Maker.md》和《使用教程-Blender插件.md》**。最短路径：

1. 用 UABEA 打开 `data.unity3d`，找到资产 `DataBaseCompiled`（路径 51978）→ Export Dump
   （或本工具 文件 → 从 data.unity3d 打开，全自动）
2. 双击 `BA_Mod_Maker_v1.8.11\BA_Mod_Maker_v1.8.11.exe`（免安装版）或源码版 `启动编辑器.bat`
3. 文件 → 打开数据库文件（自动解密）→ 编辑 → 保存并加密 → UABEA Import Dump（或 文件 → 导入到 data.unity3d）
4. 改模型/皮肤/姿势：Blender 插件打包 .bamod → 本工具 文件 → 导入 .bamod 素材包（自动合并 + CRC）

## 命令行

```bat
python ba_db_tool.py decrypt  dump.json -o 文件夹           :: 解密导出为每表一个 JSON
python ba_db_tool.py encrypt  文件夹 -t 模板.json -o 输出.json  :: 重新加密为数据库文件
python ba_db_tool.py validate dump.json|文件夹             :: 校验（--full 显示全部问题，--json 输出 JSON）
python ba_db_tool.py tables   dump.json                   :: 列出各表行数
python ba_db_tool.py find     dump.json Units 1           :: 查找引用 Units.Id=1 的行
```

环境变量 `BA_LANG=zh|en|ru` 切换命令行输出语言。

## 加密格式（已逆向验证）

每个表字段 = `base64( "fhk3s0g3" + IV(16B) + AES-256-CBC(JSON) )`，
密钥为 ASCII 字符串 `09234237536700238099172758697347`。
内置纯 Python 实现的 AES-256（优先走 Windows CNG 硬件加速），无需第三方库。

## 文件说明

- `ba_db_tool.py` 主程序（GUI + CLI，含 EditorApp / DetailWindow / LookupBox / UndoStack / 词典窗口）
- `ba_crypto.py` 数据库格式 / 加解密 / UABEA dump 读写（与 UABEA 导出格式逐字节兼容）
- `ba_aes.py` 纯 Python AES-256-CBC（CNG 加速 + T-table 回退）
- `ba_glossary.py` 词典数据（中英俄，表/字段/实证枚举；generate_glossary.py 的唯一数据源）
- `i18n.py` 三语文案
- `mod_assets.py` .bamod 素材导入对话框 + 图标/肖像打包对话框
- `generate_glossary.py` 生成 `数据库词典.md` + `database_glossary.json`（单一数据源）
- `clean_baseline.json` 内置纯净数据库（F7 只报新增问题）
- `icons\` 彩色 emoji 图标；`icons_extracted\` 游戏图标（单位/武器/弹药/专精/指示）
- `blender_addon\` Blender 插件源码（含 `_rev_tools\` 逆向工具：bundle/CRC/catalog/bamod/mesh）
- `_unitypy\` 本地 UnityPy 1.25.3 运行时（免 pip）
- `build_exe.py` / `auto_build.py` / `package_zip.py` 打包脚本

## 开发者 / 维护指南

### 架构总览

- `EditorApp`：主窗口 —— 表列表、搜索/列筛选、名称查 Id、撤销/重做栈。
- `DetailWindow`：双击行弹出的钻取编辑器 —— 左栏整行字段表单，右栏关联树。
- `LookupBox`：即时名称→Id 下拉。
- `UndoStack`：基于快照的撤销/重做（最多 100 步）。
- 词典：`ba_glossary.py` 是唯一数据源；改释义后跑 `python generate_glossary.py` 同步
  `数据库词典.md` 与 `database_glossary.json`（exe 打包时自动带上）。

### 如何新增一张表

1. `ba_crypto.TABLE_FIELDS` 加入字段名。
2. `ba_db_tool.FIELD_REF_MAP` 登记新的外键字段。
3. `ba_glossary.py`（表/字段释义）与 `i18n.py`（三语文案）补充条目。
4. 需要列表关键列时加入 `ba_db_tool.KEY_COLUMNS`。

### 打包与构建

- **版本策略**：任何改动都要同时更新两个工具的版本 —— 只改 `version.py`
  （`APP_VERSION` 与 `ADDON_VERSION` 成对 +1），补三语 Change Log，然后重新打包。
  所有模块（ba_db_tool / build_exe / package_zip / 插件 bl_info）都从 `version.py` 读版本号。
- `python build_exe.py`：PyInstaller onedir 构建 `BA_Mod_Maker_v<版本>\` + zip（含词典/插件/图标）。
- `python auto_build.py --watch`：监视源码改动自动重建（先重建插件 zip）。
- `python package_zip.py`：源码版 zip。
- `python generate_glossary.py`：再生成词典。
- 打包前先关闭正在运行的旧 exe（文件占用会覆盖失败）。

## 注意事项

- 修改 Id 后请务必按 F7 校验重复与引用。
- 新单位用「克隆」占原生槽位（详见《数据库词典.md》防崩溃铁律）；Country 只能 1(RUS) / 2(USA)。
- 改 bundle 后 CRC 必须同步（.bamod 导入已自动处理；手动改 bundle 时用
  `compute_bundle_crc.py --update-catalog`）。
- 本工具仅供个人 MOD 制作学习使用，请遵守游戏的相关条款。

---

<a id="english"></a>
# English

Broken Arrow modding tool v1.6.0: database editor (all 24 tables, automatic AES-256-CBC
encrypt/decrypt) + .bamod asset-pack importer + icon/portrait packer, paired with the
Blender addon v2.5.0 for model extraction / mount-point editing / .bamod export.

All modding knowledge now lives in one dictionary — **《数据库词典.md》** (Chinese):
24 tables & fields, verified enum values, data structures & formats, address mapping,
constants, mount/component dictionary, and anti-crash rules. No separate tutorials.

- Open UABEA dump JSON (auto-decrypt) → edit → Save & Encrypt (same filename) → UABEA Import Dump.
- File → Import .bamod pack: merges new objects into the game bundle + auto CRC (+ address registration).
- F7 validation, undo/redo, per-row drill-down editor with relation tree, name→Id lookup,
  built-in dictionary window (zh/en/ru).
- CLI: `python ba_db_tool.py decrypt|encrypt|validate|tables|find ...`（`BA_LANG=en`）.

---

<a id="russian"></a>
# Русский

Инструмент для моддинга Broken Arrow v1.6.0: редактор базы данных (все 24 таблицы,
автоматическое шифрование/дешифрование AES-256-CBC) + импорт пакетов .bamod +
упаковка иконок/портретов, в связке с аддоном Blender v2.5.0 (извлечение моделей,
редактирование точек крепления, экспорт .bamod).

Все знания по моддингу собраны в одном словаре — **《数据库词典.md》** (на китайском):
24 таблицы и поля, проверенные значения enum, структуры данных и форматы, карта адресов,
константы, словарь точек/компонентов и правила против крашей. Отдельных туториалов нет.

- Открыть дамп UABEA (автодешифровка) → редактировать → Сохранить и зашифровать (то же имя файла) → Import Dump в UABEA.
- Файл → Импорт .bamod: слияние новых объектов в bundle игры + авто-CRC (+ регистрация адреса).
- F7 валидация, отмена/повтор, редактор строки с деревом связей, поиск Id по имени,
  встроенное окно словаря (zh/en/ru).
- CLI: `python ba_db_tool.py decrypt|encrypt|validate|tables|find ...`（`BA_LANG=ru`）.
