<div align="center">

![:name](https://count.getloli.com/@astrbot_plugin_image_to_png?name=astrbot_plugin_image_to_png&theme=minecraft&padding=6&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

# astrbot_plugin_image_to_png

_✨ 图片格式兼容插件：非 PNG/JPEG 自动转 PNG，GIF 动图展开为逐帧拼贴，哈希缓存加速重复表情包 ✨_

[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-4.16%2B-orange.svg)](https://github.com/AstrBotDevs/AstrBot)
[![Version](https://img.shields.io/badge/Version-1.2.0-brightgreen.svg)](https://github.com/BCXW-0/astrbot_plugin_image-to-PNG)
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
5. **按内容哈希缓存转换结果**，重复表情包直接复用
6. **每日定时清理过期缓存**

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

### 🗂️ 哈希缓存（v1.2.0）
- 对聊天中获取到的原始图片内容计算 **SHA256**
- 再结合当前转换参数生成缓存键
- **相同表情包重复出现**时，直接命中缓存，跳过再次解码/拼贴
- 缓存文件持久化存放，不会在单次消息结束后被临时清理误删
- 支持查看缓存状态、手动清理、每日自动清理

### 🧠 更利于模型理解
- 转换后是 Gemini 等模型可接受的 PNG
- 拼贴图保留动画过程，而不是只看第一帧
- 请求阶段可附带简短“动画帧说明”，提示模型按整张拼贴理解

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

安装启用后即可自动工作。

典型场景：

1. 用户发送 QQ GIF 表情包
2. 插件计算图片内容哈希
3. 若缓存未命中：转换/拼贴并写入缓存
4. 若缓存命中：直接复用已转换 PNG
5. 再交给 AstrBot 的识图 / LLM 流程

日志示例：

```text
[图片转PNG] GIF 动图(12帧) -> 逐帧拼贴 PNG (...)
[图片转PNG] 缓存命中 content=ab12cd34ef56... options=1a2b3c4d5e6f (...)
```

## ⌨️ 命令表

| 命令 | 别名 | 说明 |
|:----:|:----:|:-----|
| `图片转png缓存状态` | `image2png_cache_status` / `图片转png缓存` | 查看缓存条目数、命中次数、占用、清理计划 |
| `图片转png缓存清理` | `image2png_cache_clean` / `清理图片转png缓存` | 立即清理过期条目与孤儿文件 |

## ⚙️ 配置说明

在 AstrBot 管理面板 → 插件配置中修改。

### 基础转换

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

### 缓存与清理

| 配置项 | 类型 | 说明 | 默认值 |
|:------:|:----:|:-----|:------:|
| `cache_enabled` | 开关 | 启用内容哈希缓存 | `true` |
| `cache_ttl_days` | 整数 | 缓存保留天数（按最后访问时间） | `7` |
| `cache_cleanup_enabled` | 开关 | 启用每日自动清理 | `true` |
| `cache_cleanup_hour` | 整数 | 每日清理小时（0-23） | `3` |
| `cache_cleanup_minute` | 整数 | 每日清理分钟（0-59） | `30` |
| `cache_timezone` | 文本 | 清理时区（IANA，如 `Asia/Shanghai`） | `Asia/Shanghai` |

### 配置建议

- **表情包很重复**：保持 `cache_enabled=true`，可显著减少重复转换开销
- **磁盘紧张**：把 `cache_ttl_days` 调小（如 `3`），或更频繁手动清理
- **动图很长 / 帧很多**：可把 `max_frames` 调到 `36` 或 `48`
- **拼贴太大影响速度**：减小 `max_cell_size` 或 `max_frames`
- **只要封面帧**：关闭 `animated_expand`

## 🧪 工作原理

```text
收到图片
  ├─ 读取原始字节，计算 SHA256
  ├─ 结合转换参数生成 cache_key
  ├─ 缓存命中？
  │    └─ 是 → 直接复用已转换 PNG
  └─ 否
       ├─ 格式是 PNG / JPEG 且非动图？
       │    └─ 是 → 原样提交给模型（不写缓存）
       └─ 否
            ├─ 动图且开启 animated_expand
            │    └─ 抽帧 → 采样 → 生成逐帧拼贴 PNG → 写入缓存
            └─ 其他静态格式
                 └─ 转为单张 PNG → 写入缓存
```

### 缓存键设计

```text
content_hash = SHA256(原始图片字节)
options_sig  = SHA1(转换参数 JSON)[:12]
cache_key    = SHA256(content_hash + ":" + options_sig)
```

因此：

- **同一张表情包**重复发送 → 命中同一缓存
- **同一张图但转换参数不同**（例如改了 max_frames）→ 使用不同缓存条目，互不影响

### 处理时机

1. **消息接入阶段**（高优先级）  
   改写消息链中的图片组件，尽量在压缩 / 图片描述前完成转换
2. **LLM 请求阶段**（高优先级兜底）  
   再次检查 `ProviderRequest.image_urls`，覆盖第三方 Agent 等路径
3. **每日定时清理**  
   删除超过 TTL 的缓存条目、缺失文件索引、孤儿 PNG

## 📌 注意事项

1. **动图无法以“真·GIF 动画”形式发给不支持 GIF 的模型**  
   本插件采用“静态拼贴保留逐帧内容”的兼容方案，而不是继续提交 GIF。
2. **超长动图会采样**  
   为控制体积和 token，超过 `max_frames` 时会均匀抽帧，并在说明中提示“已采样”。
3. **缓存按内容识别，不依赖文件名/URL**  
   即使平台每次给出不同临时路径，只要图片字节相同即可命中。
4. **修改拼贴参数后不会复用旧结果**  
   因为 `options_sig` 会变化，避免参数不一致导致“脏缓存”。
5. **透明表情包**  
   默认尽量保留透明通道；若下游链路对 alpha 不友好，可关闭 `keep_alpha`。
6. **依赖**  
   使用 AstrBot 环境中的 Pillow；一般无需额外安装依赖。

## 🐞 常见问题

### 1. 发了 GIF，模型还是答非所问？
请确认：
- 插件已启用
- 日志中出现 `[图片转PNG]` 转换或缓存命中记录
- 图片描述提供商 / 主对话模型本身可用

### 2. 为什么第二次发同一表情包仍然慢？
请检查：
- `cache_enabled` 是否开启
- 是否修改过转换参数（会生成新缓存键）
- 缓存是否刚被清理

可用命令：`图片转png缓存状态`

### 3. 拼贴图太糊 / 太小？
提高：
- `max_cell_size`（如 `320` / `384`）
- 或减少 `contact_sheet_columns`

### 4. 拼贴图太大、请求变慢？
降低：
- `max_frames`
- `max_cell_size`

### 5. 只想转格式，不要拼贴？
关闭 `animated_expand`，将只转换第一帧为 PNG。

### 6. 磁盘占用变高？
- 调小 `cache_ttl_days`
- 执行 `图片转png缓存清理`
- 或关闭 `cache_enabled`

## 📁 文件结构

```text
astrbot_plugin_image_to_png/
├── main.py              # 插件主逻辑（转换 + 缓存 + 清理）
├── metadata.yaml        # 插件元数据
├── _conf_schema.json    # 配置项定义
├── README.md            # 说明文档
├── LICENSE              # MIT 许可证
└── .gitignore
```

运行时数据（示例）：

```text
data/plugin_data/astrbot_plugin_image_to_png/
└── cache/
    ├── index.json       # 缓存索引（哈希、路径、访问时间、命中次数）
    └── files/
        └── ab/
            └── <cache_key>.png
```

## 👥 贡献指南

- 🌟 **Star 这个项目**（点右上角星星，感谢支持！）
- 🐛 提交 Issue 反馈 bug
- 💡 提出新功能建议（例如：导出多张关键帧图、自定义拼贴样式、LRU 容量上限）
- 🔧 提交 Pull Request 改进代码

## 📜 更新日志

### v1.2.0
- 新增 **内容哈希缓存**：重复表情包直接复用转换结果
- 缓存键结合原始内容 SHA256 与转换参数，避免脏缓存
- 新增 **每日定时清理**（可配置时刻与时区）
- 新增命令：`图片转png缓存状态` / `图片转png缓存清理`
- 缓存文件持久化，避免被单次事件临时文件清理误删

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
