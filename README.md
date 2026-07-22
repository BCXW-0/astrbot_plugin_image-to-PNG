<div align="center">

![:name](https://count.getloli.com/@astrbot_plugin_image_to_png?name=astrbot_plugin_image_to_png&theme=minecraft&padding=6&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

# astrbot_plugin_image_to_png

_✨ 图片格式兼容 · GIF 表情包防翻车 ✨_

[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-4.16%2B-orange.svg)](https://github.com/AstrBotDevs/AstrBot)
[![Version](https://img.shields.io/badge/Version-1.3.0-brightgreen.svg)](https://github.com/BCXW-0/astrbot_plugin_image-to-PNG)
[![GitHub](https://img.shields.io/badge/作者-Xiawan-blue)](https://github.com/BCXW-0)

</div>

> 注意：建议使用 **AstrBot v4.16.0+**。  
> 主要解决 Gemini 等模型不支持 `image/gif` 时，QQ 表情包识图失败、回复跑偏的问题。

## 🤝 介绍

部分大模型（尤其 Gemini）不支持 GIF 等格式，用户发送 QQ 表情包时容易出现：

- 日志报 `mime type is not supported ... image/gif`
- 模型没看见图，回复与图片不符

本插件会在图片交给模型前自动处理：

- PNG / JPEG 原样放行
- 其他格式转为 PNG（可配置 JPEG）
- GIF 动图展开为逐帧/关键帧拼贴
- 按内容哈希缓存，重复表情包直接复用
- 失败时提示模型不要臆测图片内容

**安装启用即可，无需命令。**

## ✨ 功能

- 格式自适应：能透传就透传，不能再转换
- 动图策略：逐帧拼贴 / 关键帧 / 仅首帧
- 场景预设：聊天表情包、文档截图、高保真
- 哈希缓存 + 近重复识别 + LRU 容量限制
- 每日自动清理，支持手动清理/诊断
- 超大图保护、去元数据、失败防幻觉

## 📦 安装

### 插件市场（推荐）

在 AstrBot 插件市场搜索 `astrbot_plugin_image_to_png` 或 `图片转 PNG`，点击安装并启用。

### Git 安装

```bash
git clone https://github.com/BCXW-0/astrbot_plugin_image-to-PNG.git astrbot_plugin_image_to_png
```

> 本地目录名建议：`astrbot_plugin_image_to_png`

## ⌨️ 命令

| 命令 | 说明 |
|:----:|:-----|
| `图片转png缓存状态` | 查看缓存占用、命中率、清理计划 |
| `图片转png缓存清理` | 清理过期/失效缓存 |
| `图片转png清空缓存` | 清空全部缓存 |
| `图片转png诊断` | 查看最近处理记录 |

## ⚙️ 配置说明

### 场景预设 `preset`

| 值 | 说明 |
|:--:|:-----|
| `chat_sticker` | 默认，适合 QQ 表情包 |
| `document_screenshot` | 文档/长截图，偏首帧清晰 |
| `high_fidelity` | 更高采样、更大边长 |

### 常用配置

| 配置项 | 类型 | 说明 | 默认值 |
|:------:|:----:|:-----|:------:|
| `enabled` | 开关 | 插件总开关 | `true` |
| `model_adaptive` | 开关 | 按模型能力透传 GIF/WebP | `true` |
| `fail_antihallucination` | 开关 | 失败时注入“勿臆测图片”提示 | `true` |
| `animated_mode` | 文本 | `contact_sheet` / `key_frames` / `first_frame` | `contact_sheet` |
| `max_frames` | 整数 | 拼贴最多帧数 | `24` |
| `output_format` | 文本 | `png` 或 `jpeg` | `png` |
| `cache_enabled` | 开关 | 启用哈希缓存 | `true` |
| `cache_ttl_days` | 整数 | 缓存保留天数 | `7` |
| `cache_max_entries` | 整数 | 最大缓存条数（LRU） | `500` |
| `cache_max_mb` | 整数 | 最大缓存占用 MB（LRU） | `512` |
| `near_duplicate_enabled` | 开关 | 近重复表情识别 | `true` |
| `cache_cleanup_hour` | 整数 | 每日清理小时 | `3` |
| `cache_cleanup_minute` | 整数 | 每日清理分钟 | `30` |

更多项见插件配置页提示（拼贴列数、单帧大小、源图限制等）。

## 📌 注意事项

1. 不支持 GIF 的模型无法“真动画播放”，拼贴是兼容方案。  
2. 修改动图策略/帧数等参数后，会生成新缓存，不会误用旧结果。  
3. 近重复识别是近似匹配，必要时可关闭或调低阈值。  
4. 升级后会自动清理与当前转换参数不兼容的旧缓存，无需手工处理。
5. 一般无需额外依赖，使用 AstrBot 自带 Pillow 即可。

## 🐞 常见问题

**发了 GIF 还是答飞？**  
先看 `图片转png诊断`，确认插件已转换成功，并检查主模型/识图提供商是否正常。

**缓存不命中？**  
可能改过转换参数，或图片字节本身不同（重新导出/压缩）。

**磁盘占用高？**  
调小 `cache_max_mb` / `cache_ttl_days`，或执行 `图片转png缓存清理`。

## 📜 更新日志

### v1.3.0
- 模型自适应透传
- 动图多策略与场景预设
- 近重复缓存、LRU 容量治理
- 失败防幻觉、诊断命令

### v1.2.0
- 内容哈希缓存与每日清理

### v1.1.0
- GIF 逐帧拼贴

### v1.0.0
- 基础格式转换

## 👥 贡献

- 🌟 Star 支持
- 🐛 Issue 反馈
- 🔧 PR 欢迎

## 📄 许可证

[MIT License](LICENSE)

## 🔗 链接

- 仓库：[BCXW-0/astrbot_plugin_image-to-PNG](https://github.com/BCXW-0/astrbot_plugin_image-to-PNG)
- 作者：[BCXW-0](https://github.com/BCXW-0)
- AstrBot：[AstrBotDevs/AstrBot](https://github.com/AstrBotDevs/AstrBot)
