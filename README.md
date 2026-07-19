<div align="center">

![:name](https://count.getloli.com/@astrbot_plugin_image_to_png?name=astrbot_plugin_image_to_png&theme=minecraft&padding=6&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

# astrbot_plugin_image_to_png

_✨ 图片格式兼容插件：非 PNG/JPEG 自动转 PNG，GIF 动图展开为逐帧拼贴 ✨_

[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-4.16%2B-orange.svg)](https://github.com/AstrBotDevs/AstrBot)
[![Version](https://img.shields.io/badge/Version-1.1.0-brightgreen.svg)](https://github.com/BCXW-0/astrbot_plugin_image-to-PNG)
[![GitHub](https://img.shields.io/badge/作者-Xiawan-blue)](https://github.com/BCXW-0)

</div>

> 注意：建议使用 **AstrBot v4.16.0+**。本插件主要用于解决部分模型（如 Gemini）不支持 `image/gif` 等格式时，QQ 表情包/动图导致识图失败、回复跑偏的问题。

## 🤝 介绍

在 QQ 等平台，用户经常会发送 **GIF 表情包**、WebP 动图或其他非标准格式图片。  
部分大模型（尤其是 Gemini 系）只支持有限 MIME 类型，例如：

- `image/jpeg`
- `image/png`
- `image/webp`（部分接口）

**不支持 `image/gif`** 时，AstrBot 在“图片描述 / 多模态请求”阶段会直接失败，模型拿不到真实图片内容，就容易出现：

- 日志报 `mime type is not supported by Gemini: 'image/gif'`
- 回复内容与表情包完全对不上
- 看起来像“幻觉”

本插件会在图片进入模型链路前，自动完成：

1. **格式检查**
2. **PNG / JPEG 原样放行**
3. **其他格式统一转为 PNG**
4. **GIF / 动态 WebP 展开为逐帧拼贴静态图**（保留动画过程信息）

安装后通常无需额外操作，开箱即用。

## ✨ 功能特色

### 🎯 自动兼容
- **PNG / JPEG**：直接放行，不做多余处理
- **GIF / WebP / BMP 等**：自动转换为 PNG
- **消息阶段 + 请求阶段双重处理**：尽早转换，并在 LLM 请求时兜底

### 🎞️ 动图逐帧保留
- 自动识别动画帧
- 按时间顺序抽取帧内容
- 拼成一张**从左到右、从上到下**的静态拼贴 PNG
- 默认标注帧号与时长（如 `#3 80ms`）
- 帧数过多时自动均匀采样，避免图片过大

### 🧠 更利于模型理解
- 转换后是 Gemini 等模型可接受的 PNG
- 拼贴图保留动画过程，而不是只看第一帧
- 请求阶段可附带简短“动画帧说明”，提示模型按整张拼贴理解

### 🧩 平台友好
支持 AstrBot 常见消息平台（以实际适配器为准），特别适合：
- QQ（aiocqhttp）表情包
- 引用消息中的图片
- 图片描述（image caption）链路

## 📦 安装

### 方式一：插件市场（推荐）
1. 打开 AstrBot 管理面板
2. 进入 **插件市场**
3. 搜索 `astrbot_plugin_image_to_png` 或 `图片转 PNG`
4. 点击安装并启用

### 方式二：Git 安装
在 AstrBot 插件目录执行：

```bash
# 进入 AstrBot 的 plugins 目录后
git clone https://github.com/BCXW-0/astrbot_plugin_image-to-PNG.git astrbot_plugin_image_to_png
```

然后在管理面板中启用插件，或重启 AstrBot。

> 本地目录名建议使用：`astrbot_plugin_image_to_png`  
> 与插件 `metadata.yaml` 中的 `name` 保持一致，便于管理。

## 🚀 使用方法

安装启用后即可自动工作，**无需命令**。

典型场景：

1. 用户发送 QQ GIF 表情包
2. 插件检测到非 PNG/JPEG
3. 自动转换为 PNG
4. 若为动图，则生成逐帧拼贴图
5. 再交给 AstrBot 的识图 / LLM 流程

你也可以在日志中看到类似信息：

```text
[图片转PNG] GIF 动图(12帧) -> 逐帧拼贴 PNG (...)
[图片转PNG] 消息图片已转为 PNG: ...
```

## ⚙️ 配置说明

在 AstrBot 管理面板 → 插件配置中修改。

| 配置项 | 类型 | 说明 | 默认值 |
|:------:|:----:|:-----|:------:|
| `enabled` | 开关 | 是否启用图片格式转换 | `true` |
| `convert_message_images` | 开关 | 收到消息时尽早转换图片（推荐开启） | `true` |
| `convert_request_images` | 开关 | LLM 请求阶段再次检查并转换 `image_urls`（兜底） | `true` |
| `keep_alpha` | 开关 | 转为 PNG 时尽量保留透明通道；关闭则铺白底 | `true` |
| `animated_expand` | 开关 | 动图展开为逐帧拼贴；关闭则只取第一帧 | `true` |
| `max_frames` | 整数 | 动图最多保留帧数，超出时均匀采样 | `24` |
| `contact_sheet_columns` | 整数 | 拼贴图列数 | `4` |
| `max_cell_size` | 整数 | 拼贴中单帧最大边长（像素） | `256` |
| `show_frame_labels` | 开关 | 是否显示帧编号和时长 | `true` |

### 配置建议

- **只想兼容 GIF、尽量少改图**：保持默认即可
- **动图很长 / 帧很多**：可把 `max_frames` 调到 `36` 或 `48`
- **拼贴太大影响速度**：减小 `max_cell_size` 或 `max_frames`
- **只要封面帧**：关闭 `animated_expand`

## 🧪 工作原理

```text
收到图片
  ├─ 格式是 PNG / JPEG ？
  │    └─ 是 → 原样提交给模型
  └─ 否
       ├─ 是动图（GIF / 动态 WebP 等）且开启 animated_expand
       │    └─ 抽取帧 → 采样 → 生成逐帧拼贴 PNG
       └─ 静态其他格式
            └─ 转为单张 PNG
```

处理时机：

1. **消息接入阶段**（高优先级）  
   改写消息链中的图片组件，尽量在压缩 / 图片描述前完成转换
2. **LLM 请求阶段**（高优先级兜底）  
   再次检查 `ProviderRequest.image_urls`，覆盖第三方 Agent 等路径

## 📌 注意事项

1. **动图无法以“真·GIF 动画”形式发给不支持 GIF 的模型**  
   本插件采用“静态拼贴保留逐帧内容”的兼容方案，而不是继续提交 GIF。
2. **超长动图会采样**  
   为控制体积和 token，超过 `max_frames` 时会均匀抽帧，并在说明中提示“已采样”。
3. **透明表情包**  
   默认尽量保留透明通道；若下游链路对 alpha 不友好，可关闭 `keep_alpha`。
4. **不会修改用户原始聊天记录中的远端图片资源**  
   转换结果写入 AstrBot 临时目录，并在事件结束后尽量清理。
5. **依赖**  
   使用 AstrBot 环境中的 Pillow；一般无需额外安装依赖。

## 🐞 常见问题

### 1. 发了 GIF，模型还是答非所问？
请确认：
- 插件已启用
- 日志中出现 `[图片转PNG]` 转换记录
- 图片描述提供商 / 主对话模型本身可用

### 2. 拼贴图太糊 / 太小？
提高：
- `max_cell_size`（如 `320` / `384`）
- 或减少 `contact_sheet_columns`

### 3. 拼贴图太大、请求变慢？
降低：
- `max_frames`
- `max_cell_size`

### 4. 只想转格式，不要拼贴？
关闭 `animated_expand`，将只转换第一帧为 PNG。

## 📁 文件结构

```text
astrbot_plugin_image_to_png/
├── main.py              # 插件主逻辑
├── metadata.yaml        # 插件元数据
├── _conf_schema.json    # 配置项定义
├── README.md            # 说明文档
├── LICENSE              # MIT 许可证
└── .gitignore
```

## 👥 贡献指南

- 🌟 **Star 这个项目**（点右上角星星，感谢支持！）
- 🐛 提交 Issue 反馈 bug
- 💡 提出新功能建议（例如：导出多张关键帧图、自定义拼贴样式）
- 🔧 提交 Pull Request 改进代码

## 📜 更新日志

### v1.1.0
- 支持 GIF / 动态图展开为**逐帧拼贴静态 PNG**
- 支持帧号、时长标注
- 支持最大帧数均匀采样
- LLM 请求阶段补充动画说明提示

### v1.0.0
- 首次发布
- PNG / JPEG 放行
- 其他格式统一转 PNG
- 消息阶段 + 请求阶段双重转换

## 📄 许可证

本项目基于 [MIT License](LICENSE) 开源。

## 🔗 相关链接

- 仓库：[BCXW-0/astrbot_plugin_image-to-PNG](https://github.com/BCXW-0/astrbot_plugin_image-to-PNG)
- 作者主页：[BCXW-0](https://github.com/BCXW-0)
- AstrBot：[AstrBotDevs/AstrBot](https://github.com/AstrBotDevs/AstrBot)

---

<div align="center">

如果这个插件帮你解决了 GIF 表情包识图失败的问题，欢迎 Star ⭐

</div>
