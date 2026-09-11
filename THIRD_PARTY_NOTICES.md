# 第三方组件声明 · Third-Party Notices

本仓库的代码以 **MIT** 发布（见 `LICENSE`）。但 `blender_addon/_unitypy/` 目录下**捆绑**了若干
第三方开源库，以便 Blender 插件开箱可用。这些库**各自保留其原始许可证**，不受本仓库 MIT 许可影响。

| 组件 | 用途 | 许可证（以目录内文件为准） |
|---|---|---|
| **UnityPy** | 读取/写入 Unity 资产包（`.bundle` / AssetBundle） | MIT |
| **lz4** | Unity 压缩块的解压 | BSD-2-Clause |
| **Pillow (PIL)** | 贴图解码（部分子集） | MIT-CMU (Pillow License) |
| **fsspec** | 文件系统抽象（UnityPy 依赖） | BSD-3-Clause |
| **brotli** | 压缩（UnityPy 依赖） | MIT |
| **texture2ddecoder** | 压缩纹理解码 | MIT |
| **其他 `_unitypy` 子目录** | 传递依赖 | 见各自目录内 `LICENSE` |

## 你应该做什么

1. **分发本工具（含插件）时，请连同 `blender_addon/_unitypy/` 内的 `LICENSE` 文件一起分发** ——
   不要删除或替换它们。
2. 如某个库的许可证要求署名（attribution），请在发布页/文档中保留本文件。
3. 若要**替换**或**移除**某个第三方库，请确认插件的对应功能仍然可用，并同步更新本文件。

---

## English

The code in this repository is released under **MIT** (see `LICENSE`). However, the directory
`blender_addon/_unitypy/` **bundles** several third-party open-source libraries (UnityPy, lz4,
Pillow, fsspec, brotli, …) so the addon works out of the box. **Each bundled library keeps its own
license**, as stated in the `LICENSE` files inside that directory. Please keep those files when
redistributing.
