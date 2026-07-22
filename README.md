<div align="center">

![:name](https://count.getloli.com/@astrbot_plugin_image_to_png?name=astrbot_plugin_image_to_png&theme=minecraft&padding=6&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

# astrbot_plugin_image_to_png

_✨ 多模态图片兼容中台：表情包防翻车 · 动图可理解 · 缓存可治理 ✨_

[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-4.16%2B-orange.svg)](https://github.com/AstrBotDevs/AstrBot)
[![Version](https://img.shields.io/badge/Version-1.3.0-brightgreen.svg)](https://github.com/BCXW-0/astrbot_plugin_image-to-PNG)
[![GitHub](https://img.shields.io/badge/作者-Xiawan-blue)](https://github.com/BCXW-0)

</div>

> 建议 **AstrBot v4.16.0+**。  
> 解决：QQ GIF 表情包导致 Gemini 等模型报错、模型看不到图、回复“幻觉乱编”。

## 🤝 介绍

用户发来 GIF / WebP / 怪异格式图片时，部分模型（尤其 Gemini）会因 MIME 不支持直接失败。  
本插件在图片进入模型前自动完成：

1. **格式检查与自适应**
2. **能透传则透传，不能则转换**
3. **动图多策略理解（拼贴 / 关键帧 / 首帧）**
4. **哈希 + 近重复缓存**
5. **容量治理与每日清理**
6. **失败防幻觉提示**
7. **诊断命令**

安装后默认可直接用，无需命令。

## ✨ 功能一览

### 用户侧
- 发表情包不再轻易触发识图失败
- 动图不再只剩“第一帧猜测”
- 转换失败时模型被约束“别瞎编图片内容”

### 主人/运维侧
- 缓存命中率、占用、失败次数可查
- 每日自动清理 + 手动清理/清空
- 超大图保护，避免拖垮请求
- 场景预设：聊天表情包 / 文档截图 / 高保真

### 开发侧
- 消息阶段 + LLM 请求阶段双重处理
- 缓存键绑定转换参数，避免脏缓存
- 近重复感知哈希，兼容轻微重压缩表情包

## 📦 安装

### 插件市场
搜索 `astrbot_plugin_image_to_png` 或 `图片转 PNG`，安装并启用。

### Git

```bash
git clone https://github.com/BCXW-0/astrbot_plugin_image-to-PNG.git astrbot_plugin_image_to_png
```

目录名建议：`astrbot_plugin_image_to_png`。

## 🚀 使用

启用即可。

日志示例：

```text
[图片转PNG] v1.3 已初始化 preset=chat_sticker mode=contact_sheet ...
[图片转PNG] 缓存命中 content=...
[图片转PNG] GIF 动图(12帧) -> 逐帧拼贴 ...
```

## ⌨️ 命令

| 命令 | 说明 |
|:----:|:-----|
| `图片转png缓存状态` | 查看预设、命中率、占用、清理计划 |
| `图片转png缓存清理` | 清理过期/失效缓存 |
| `图片转png清空缓存` | 清空全部缓存 |
| `图片转png诊断` | 输出最近处理记录与健康检查 |

## ⚙️ 配置

### 场景预设 `preset`
| 值 | 适用 |
|:--:|:-----|
| `chat_sticker` | 默认，聊天表情包 |
| `document_screenshot` | 文档/长截图，偏清晰首帧 |
| `high_fidelity` | 更高采样与更大边长 |

### 关键配置

| 配置项 | 默认 | 说明 |
|:------:|:----:|:-----|
| `model_adaptive` | `true` | 按模型能力透传 GIF/WebP |
| `fail_antihallucination` | `true` | 失败时注入防幻觉约束 |
| `animated_mode` | `contact_sheet` | `contact_sheet` / `key_frames` / `first_frame` |
| `max_frames` | `24` | 拼贴最大帧数 |
| `output_format` | `png` | `png` / `jpeg` |
| `max_source_side` | `2048` | 源图最大边长 |
| `max_source_bytes_mb` | `15` | 源图最大体积 |
| `cache_enabled` | `true` | 缓存开关 |
| `cache_ttl_days` | `7` | 缓存 TTL |
| `cache_max_entries` | `500` | 最大条目，LRU |
| `cache_max_mb` | `512` | 最大占用，LRU |
| `near_duplicate_enabled` | `true` | 近重复识别 |
| `near_duplicate_threshold` | `5` | 感知哈希距离阈值 |
| `cache_cleanup_hour/minute` | `3:30` | 每日清理时刻 |
| `cache_timezone` | `Asia/Shanghai` | 清理时区 |

## 🧪 工作原理

```text
收到图片
  → 读取字节 / 体积校验
  → SHA256 + 参数签名查精确缓存
  → 感知哈希查近重复缓存
  → 判断模型是否可透传该格式
      ├─ 可透传 → 原样放行
      └─ 不可透传
           ├─ 动图: first_frame / key_frames / contact_sheet
           └─ 静态: 转 PNG/JPEG
  → 写入缓存并执行 LRU 限额
  → 失败则注入防幻觉提示
```

### 缓存键
```text
content_hash = SHA256(原始字节)
options_sig  = SHA1(转换参数)
cache_key    = SHA256(content_hash + ":" + options_sig)
```

## 📌 注意

1. 不支持 GIF 的模型无法“真动画播放”，拼贴是兼容方案。  
2. 修改拼贴参数会生成新缓存键。  
3. 近重复识别是近似匹配，极端情况下可能误命中，可调高/关闭阈值。  
4. 消息阶段默认更保守（服务图片描述链路），请求阶段可按模型自适应。

## 🐞 FAQ

**Q: 还是答飞？**  
A: 看 `图片转png诊断` 是否失败；并确认主模型/识图提供商本身可用。

**Q: 缓存不命中？**  
A: 检查是否改了 `animated_mode/max_frames` 等参数；或图片本身字节不同。

**Q: 磁盘涨？**  
A: 降 `cache_max_mb` / `cache_ttl_days`，或执行清理命令。

## 📁 结构

```text
astrbot_plugin_image_to_png/
├── main.py
├── metadata.yaml
├── _conf_schema.json
├── README.md
├── LICENSE
└── .gitignore
```

运行时：

```text
data/plugin_data/astrbot_plugin_image_to_png/
├── stats.json
└── cache/
    ├── index.json
    └── files/**.png
```

## 📜 更新日志

### v1.3.0
- 模型能力自适应（GIF/WebP 智能透传）
- 动图策略：`contact_sheet` / `key_frames` / `first_frame`
- 场景预设：聊天表情包 / 文档截图 / 高保真
- 缓存容量上限 + LRU
- 感知哈希近重复命中
- 超大图保护、元数据剥离、输出 PNG/JPEG
- 失败防幻觉提示
- 诊断命令与运行统计

### v1.2.0
- 内容哈希缓存、每日清理、缓存状态/清理命令

### v1.1.0
- GIF 逐帧拼贴

### v1.0.0
- 基础格式转换

## 📄 许可证

[MIT License](LICENSE)

## 🔗 链接

- 仓库：https://github.com/BCXW-0/astrbot_plugin_image-to-PNG
- 作者：https://github.com/BCXW-0
- AstrBot：https://github.com/AstrBotDevs/AstrBot

---

<div align="center">
如果它减少了你的表情包翻车，欢迎 Star ⭐
</div>
