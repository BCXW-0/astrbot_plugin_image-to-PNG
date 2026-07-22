from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import math
import os
import time
import uuid
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import Image, Reply
from astrbot.core.utils.io import download_image_by_url
from PIL import Image as PILImage
from PIL import ImageDraw, ImageFont, ImageOps
from PIL import UnidentifiedImageError

PASSTHROUGH_BASE = {"JPEG", "PNG"}
CACHE_INDEX_VERSION = 2
STATS_VERSION = 1
PRESETS: dict[str, dict[str, Any]] = {
    "chat_sticker": {
        "animated_mode": "contact_sheet",
        "max_frames": 24,
        "max_cell_size": 256,
        "max_source_side": 2048,
        "output_format": "png",
    },
    "document_screenshot": {
        "animated_mode": "first_frame",
        "max_frames": 1,
        "max_cell_size": 512,
        "max_source_side": 3072,
        "output_format": "png",
    },
    "high_fidelity": {
        "animated_mode": "key_frames",
        "max_frames": 36,
        "max_cell_size": 384,
        "max_source_side": 4096,
        "output_format": "png",
    },
}

# Provider/model capability hints. Used only as soft guidance.
STRICT_NO_GIF_HINTS = (
    "gemini",
    "google",
    "imagen",
    "vertex",
)
GIF_FRIENDLY_HINTS = (
    "gpt-4o",
    "gpt-4.1",
    "gpt-5",
    "o1",
    "o3",
    "o4",
    "claude",
    "anthropic",
    "qwen-vl",
    "qwen2-vl",
    "qwen2.5-vl",
)


@register(
    "astrbot_plugin_image_to_png",
    "Xiawan",
    "多模态图片兼容中台：格式自适应、动图多策略、哈希/近重缓存、失败防幻觉与诊断。",
    "1.3.0",
)
class ImageToPngPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        super().__init__(context)
        self.config = config or {}

        # Core switches
        self.enabled = bool(self._cfg("enabled", True))
        self.convert_message_images = bool(self._cfg("convert_message_images", True))
        self.convert_request_images = bool(self._cfg("convert_request_images", True))
        self.model_adaptive = bool(self._cfg("model_adaptive", True))
        self.fail_antihallucination = bool(self._cfg("fail_antihallucination", True))

        # Preset + conversion
        self.preset = str(self._cfg("preset", "chat_sticker") or "chat_sticker").strip()
        preset = PRESETS.get(self.preset, PRESETS["chat_sticker"])
        self.keep_alpha = bool(self._cfg("keep_alpha", True))
        self.animated_mode = str(
            self._cfg("animated_mode", preset["animated_mode"]) or preset["animated_mode"]
        ).strip().lower()
        if self.animated_mode in {"expand", "sheet", "true", "1"}:
            self.animated_mode = "contact_sheet"
        if self.animated_mode in {"false", "0", "off", "none"}:
            self.animated_mode = "first_frame"

        self.max_frames = max(1, int(self._cfg("max_frames", preset["max_frames"]) or preset["max_frames"]))
        self.contact_sheet_columns = max(1, int(self._cfg("contact_sheet_columns", 4) or 4))
        self.max_cell_size = max(
            32,
            int(self._cfg("max_cell_size", preset["max_cell_size"]) or preset["max_cell_size"]),
        )
        self.show_frame_labels = bool(self._cfg("show_frame_labels", True))
        self.output_format = str(
            self._cfg("output_format", preset["output_format"]) or "png"
        ).strip().lower()
        if self.output_format not in {"png", "jpeg", "jpg"}:
            self.output_format = "png"
        if self.output_format == "jpg":
            self.output_format = "jpeg"
        self.jpeg_quality = min(95, max(40, int(self._cfg("jpeg_quality", 85) or 85)))

        # Source protection
        self.max_source_side = max(
            256,
            int(self._cfg("max_source_side", preset["max_source_side"]) or preset["max_source_side"]),
        )
        self.max_source_bytes_mb = max(
            1,
            int(self._cfg("max_source_bytes_mb", 15) or 15),
        )
        self.strip_metadata = bool(self._cfg("strip_metadata", True))

        # Cache
        self.cache_enabled = bool(self._cfg("cache_enabled", True))
        self.cache_ttl_days = max(1, int(self._cfg("cache_ttl_days", 7) or 7))
        self.cache_max_entries = max(10, int(self._cfg("cache_max_entries", 500) or 500))
        self.cache_max_mb = max(16, int(self._cfg("cache_max_mb", 512) or 512))
        self.near_duplicate_enabled = bool(self._cfg("near_duplicate_enabled", True))
        self.near_duplicate_threshold = max(
            0,
            min(16, int(self._cfg("near_duplicate_threshold", 5) or 5)),
        )
        self.cache_cleanup_enabled = bool(self._cfg("cache_cleanup_enabled", True))
        self.cache_cleanup_hour = min(23, max(0, int(self._cfg("cache_cleanup_hour", 3) or 3)))
        self.cache_cleanup_minute = min(
            59,
            max(0, int(self._cfg("cache_cleanup_minute", 30) or 30)),
        )
        self.cache_timezone = str(
            self._cfg("cache_timezone", "Asia/Shanghai") or "Asia/Shanghai"
        ).strip()

        self.pad_color = (245, 245, 245, 255)
        self.label_bg = (0, 0, 0, 160)
        self.label_fg = (255, 255, 255, 255)

        self.data_dir = StarTools.get_data_dir("astrbot_plugin_image_to_png")
        self.cache_dir = self.data_dir / "cache"
        self.cache_files_dir = self.cache_dir / "files"
        self.cache_index_path = self.cache_dir / "index.json"
        self.stats_path = self.data_dir / "stats.json"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_files_dir.mkdir(parents=True, exist_ok=True)

        self._cache_lock = asyncio.Lock()
        self._stats_lock = asyncio.Lock()
        self._index: dict[str, Any] = {"version": CACHE_INDEX_VERSION, "entries": {}}
        self._stats: dict[str, Any] = self._default_stats()
        self._recent: deque[dict[str, Any]] = deque(maxlen=30)
        self._cleanup_task: asyncio.Task | None = None
        self._options_sig = self._build_options_sig()

    def _cfg(self, key: str, default: Any) -> Any:
        try:
            value = self.config.get(key, default)  # type: ignore[attr-defined]
        except AttributeError:
            try:
                value = self.config[key]  # type: ignore[index]
            except Exception:
                value = default
        return default if value is None else value

    @staticmethod
    def _default_stats() -> dict[str, Any]:
        return {
            "version": STATS_VERSION,
            "convert_ok": 0,
            "convert_fail": 0,
            "cache_hit": 0,
            "cache_near_hit": 0,
            "passthrough": 0,
            "adaptive_skip": 0,
            "bytes_saved_est": 0,
            "last_cleanup_at": 0,
            "last_cleanup_result": {},
        }

    def _build_options_sig(self) -> str:
        payload = {
            "preset": self.preset,
            "animated_mode": self.animated_mode,
            "max_frames": self.max_frames,
            "contact_sheet_columns": self.contact_sheet_columns,
            "max_cell_size": self.max_cell_size,
            "show_frame_labels": self.show_frame_labels,
            "keep_alpha": self.keep_alpha,
            "output_format": self.output_format,
            "jpeg_quality": self.jpeg_quality,
            "max_source_side": self.max_source_side,
            "strip_metadata": self.strip_metadata,
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha1(raw).hexdigest()[:12]

    async def initialize(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_files_dir.mkdir(parents=True, exist_ok=True)
        await self._load_index()
        await self._migrate_cache_index()
        await self._load_stats()
        if self.cache_cleanup_enabled:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info(
            "[图片转PNG] v1.3 已初始化 preset=%s mode=%s adaptive=%s cache=%s",
            self.preset,
            self.animated_mode,
            self.model_adaptive,
            self.cache_enabled,
        )

    async def terminate(self) -> None:
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        async with self._cache_lock:
            await self._save_index_unlocked()
        async with self._stats_lock:
            await self._save_stats_unlocked()
        logger.info("[图片转PNG] 插件已卸载。")

    # ───────────────────────── event hooks ─────────────────────────

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10000)
    async def convert_incoming_images(self, event: AstrMessageEvent) -> None:
        if not self.enabled or not self.convert_message_images:
            return
        try:
            messages = event.get_messages() or []
            await self._convert_message_chain(event, messages)
        except Exception as exc:  # noqa: BLE001
            logger.error("[图片转PNG] 转换消息图片失败: %s", exc, exc_info=True)
            self._record_recent("message_error", str(exc), ok=False)

    @filter.on_llm_request(priority=10000)
    async def convert_request_images_hook(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        if not self.enabled or not self.convert_request_images:
            return
        try:
            if not req.image_urls:
                return

            allowed_formats = self._resolve_allowed_formats(event, req)
            converted: list[str] = []
            changed = False
            notes: list[str] = []
            any_fail = False

            for ref in list(req.image_urls):
                new_ref, note, meta = await self.ensure_allowed_image_ref(
                    ref,
                    with_meta=True,
                    allowed_formats=allowed_formats,
                    event=event,
                )
                if new_ref != ref:
                    changed = True
                    if self._is_local_path(new_ref) and not self._is_cache_path(new_ref):
                        event.track_temporary_local_file(new_ref)
                if note:
                    notes.append(note)
                if meta.get("failed"):
                    any_fail = True
                converted.append(new_ref)

            if changed:
                req.image_urls = converted
                logger.info("[图片转PNG] 已处理请求图片 %d 张", len(converted))

            inject_parts: list[str] = []
            if notes:
                inject_parts.append("；".join(notes))
            if any_fail and self.fail_antihallucination:
                inject_parts.append(
                    "部分图片无法可靠解析/转换。请不要臆测未成功读取的图片内容，"
                    "仅根据文本和其他成功读取的图片作答；若必须提及图片，请明确说明“未能可靠识别”。"
                )
            if inject_parts:
                marker = "[图片转PNG说明]"
                existing = req.prompt or ""
                if marker not in existing:
                    text = " ".join(inject_parts)
                    req.prompt = (
                        f"{existing}\n{marker}{text}".strip() if existing else f"{marker}{text}"
                    )
        except Exception as exc:  # noqa: BLE001
            logger.error("[图片转PNG] 转换请求图片失败: %s", exc, exc_info=True)
            self._record_recent("request_error", str(exc), ok=False)
            if self.fail_antihallucination:
                marker = "[图片转PNG说明]"
                msg = (
                    "图片预处理异常。请不要臆测图片内容，优先依据用户文本回复，"
                    "并在必要时说明图片未能可靠识别。"
                )
                existing = req.prompt or ""
                if marker not in existing:
                    req.prompt = f"{existing}\n{marker}{msg}".strip() if existing else f"{marker}{msg}"

    # ───────────────────────── commands ─────────────────────────

    @filter.command("图片转png缓存清理", alias={"image2png_cache_clean", "清理图片转png缓存"})
    async def clean_cache_command(self, event: AstrMessageEvent):
        """清理过期/超额/失效缓存。"""
        result = await self.cleanup_cache(force_all=False)
        yield event.plain_result(
            "[图片转PNG] 缓存清理完成："
            f"删除条目 {result['removed_entries']}，"
            f"删除文件 {result['removed_files']}，"
            f"释放约 {self._fmt_bytes(result['freed_bytes'])}，"
            f"剩余 {result['remaining_entries']} 条。"
        )

    @filter.command("图片转png清空缓存", alias={"image2png_cache_purge"})
    async def purge_cache_command(self, event: AstrMessageEvent):
        """清空全部图片转换缓存。"""
        result = await self.cleanup_cache(force_all=True)
        yield event.plain_result(
            "[图片转PNG] 已清空缓存："
            f"删除条目 {result['removed_entries']}，"
            f"删除文件 {result['removed_files']}，"
            f"释放约 {self._fmt_bytes(result['freed_bytes'])}。"
        )

    @filter.command("图片转png缓存状态", alias={"image2png_cache_status", "图片转png缓存"})
    async def cache_status_command(self, event: AstrMessageEvent):
        """查看缓存与运行状态。"""
        async with self._cache_lock:
            entries = self._index.get("entries", {})
            total = len(entries)
            total_bytes = sum(int(i.get("size", 0) or 0) for i in entries.values())
            hits = sum(int(i.get("hit_count", 0) or 0) for i in entries.values())
        async with self._stats_lock:
            stats = dict(self._stats)
        hit_rate = 0.0
        denom = stats["cache_hit"] + stats["cache_near_hit"] + stats["convert_ok"]
        if denom:
            hit_rate = (stats["cache_hit"] + stats["cache_near_hit"]) / denom * 100
        yield event.plain_result(
            "[图片转PNG] 状态 v1.3\n"
            f"- 预设: {self.preset}\n"
            f"- 动图策略: {self.animated_mode}\n"
            f"- 模型自适应: {self.model_adaptive}\n"
            f"- 缓存启用: {self.cache_enabled}\n"
            f"- 缓存条目: {total}/{self.cache_max_entries}\n"
            f"- 缓存占用: {self._fmt_bytes(total_bytes)} / {self.cache_max_mb} MB\n"
            f"- 条目累计命中: {hits}\n"
            f"- 运行命中率: {hit_rate:.1f}% "
            f"(exact={stats['cache_hit']}, near={stats['cache_near_hit']}, convert={stats['convert_ok']})\n"
            f"- 透传/自适应跳过: {stats['passthrough']}/{stats['adaptive_skip']}\n"
            f"- 失败: {stats['convert_fail']}\n"
            f"- TTL: {self.cache_ttl_days} 天\n"
            f"- 每日清理: {'开' if self.cache_cleanup_enabled else '关'} "
            f"{self.cache_cleanup_hour:02d}:{self.cache_cleanup_minute:02d} ({self.cache_timezone})\n"
            f"- 目录: {self.cache_dir}"
        )

    @filter.command("图片转png诊断", alias={"image2png_diag", "图片转png体检"})
    async def diagnose_command(self, event: AstrMessageEvent):
        """输出最近图片处理记录与健康检查。"""
        async with self._cache_lock:
            entries = len(self._index.get("entries", {}))
            total_bytes = sum(
                int(i.get("size", 0) or 0) for i in self._index.get("entries", {}).values()
            )
        recent = list(self._recent)[-10:]
        lines = [
            "[图片转PNG] 诊断报告",
            f"- 插件启用: {self.enabled}",
            f"- 消息/请求转换: {self.convert_message_images}/{self.convert_request_images}",
            f"- 预设/策略: {self.preset}/{self.animated_mode}",
            f"- 输出格式: {self.output_format}",
            f"- 近重识别: {self.near_duplicate_enabled} (阈值≤{self.near_duplicate_threshold})",
            f"- 缓存: {entries} 条, {self._fmt_bytes(total_bytes)}",
            f"- 源图限制: side≤{self.max_source_side}px, size≤{self.max_source_bytes_mb}MB",
            "- 最近记录:",
        ]
        if not recent:
            lines.append("  (暂无)")
        else:
            for item in recent:
                ts = datetime.fromtimestamp(item.get("ts", 0)).strftime("%m-%d %H:%M:%S")
                flag = "OK" if item.get("ok", True) else "FAIL"
                lines.append(
                    f"  [{ts}] {flag} {item.get('action')} {item.get('detail', '')}"
                )
        yield event.plain_result("\n".join(lines))

    # ───────────────────────── conversion pipeline ─────────────────────────

    async def _convert_message_chain(
        self,
        event: AstrMessageEvent,
        chain: list[Any] | None,
    ) -> None:
        if not chain:
            return
        for comp in chain:
            if isinstance(comp, Image):
                await self._convert_image_component(event, comp)
            elif isinstance(comp, Reply) and comp.chain:
                await self._convert_message_chain(event, comp.chain)

    async def _convert_image_component(
        self,
        event: AstrMessageEvent,
        image: Image,
    ) -> None:
        try:
            source_path = await image.convert_to_file_path()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[图片转PNG] 读取消息图片失败: %s", exc)
            await self._inc_stat("convert_fail")
            self._record_recent("read_fail", str(exc), ok=False)
            return

        # Message stage uses conservative allow-list (caption often uses strict models).
        new_path, _note, meta = await self.ensure_allowed_image_ref(
            source_path,
            with_meta=True,
            allowed_formats=set(PASSTHROUGH_BASE),
            event=event,
        )
        if not new_path or new_path == source_path:
            return
        if not self._is_local_path(new_path):
            return

        abs_path = os.path.abspath(new_path)
        image.file = f"file:///{abs_path}"
        image.path = abs_path
        if hasattr(image, "url"):
            image.url = abs_path
        if not self._is_cache_path(abs_path):
            event.track_temporary_local_file(abs_path)
        logger.info(
            "[图片转PNG] 消息图片已处理: %s cache=%s",
            abs_path,
            meta.get("from_cache"),
        )

    async def ensure_allowed_image_ref(
        self,
        image_ref: str,
        *,
        with_meta: bool = False,
        allowed_formats: set[str] | None = None,
        event: AstrMessageEvent | None = None,
    ) -> str | tuple[str, str | None, dict[str, Any]]:
        meta: dict[str, Any] = {
            "from_cache": False,
            "failed": False,
            "action": "none",
        }
        if not image_ref:
            return (image_ref, None, meta) if with_meta else image_ref

        data, source_hint = await self._load_image_bytes(image_ref)
        if data is None:
            meta["failed"] = True
            meta["action"] = "load_fail"
            await self._inc_stat("convert_fail")
            self._record_recent("load_fail", source_hint, ok=False)
            return (image_ref, None, meta) if with_meta else image_ref

        # Size guard
        max_bytes = self.max_source_bytes_mb * 1024 * 1024
        if len(data) > max_bytes:
            meta["failed"] = True
            meta["action"] = "too_large"
            await self._inc_stat("convert_fail")
            note = f"图片过大({self._fmt_bytes(len(data))})，已跳过转换。"
            self._record_recent("too_large", source_hint, ok=False)
            return (image_ref, note, meta) if with_meta else image_ref

        content_hash = hashlib.sha256(data).hexdigest()
        cache_key = self._make_cache_key(content_hash)
        allow = {x.upper() for x in (allowed_formats or PASSTHROUGH_BASE)}

        if self.cache_enabled:
            cached = await self._get_cache_entry(cache_key)
            if cached:
                await self._inc_stat("cache_hit")
                meta.update({"from_cache": True, "action": "cache_hit"})
                self._record_recent("cache_hit", content_hash[:12], ok=True)
                return (
                    (cached["path"], cached.get("note"), meta)
                    if with_meta
                    else cached["path"]
                )

            if self.near_duplicate_enabled:
                near = await self._find_near_duplicate(data)
                if near:
                    await self._inc_stat("cache_near_hit")
                    meta.update({"from_cache": True, "action": "near_hit"})
                    self._record_recent("near_hit", content_hash[:12], ok=True)
                    return (
                        (near["path"], near.get("note"), meta)
                        if with_meta
                        else near["path"]
                    )

        try:
            result = await asyncio.to_thread(
                self._convert_bytes_to_output_with_meta,
                data,
                source_hint,
                allow,
                cache_key,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("[图片转PNG] 转换失败(%s): %s", source_hint, exc, exc_info=True)
            meta["failed"] = True
            meta["action"] = "convert_fail"
            await self._inc_stat("convert_fail")
            self._record_recent("convert_fail", f"{source_hint}:{exc}", ok=False)
            return (image_ref, None, meta) if with_meta else image_ref

        if result is None:
            # passthrough / adaptive skip
            await self._inc_stat("passthrough")
            meta["action"] = "passthrough"
            self._record_recent("passthrough", source_hint, ok=True)
            return (image_ref, None, meta) if with_meta else image_ref

        out_path, note, fmt, frame_count, action = result
        if action == "adaptive_skip":
            await self._inc_stat("adaptive_skip")
            meta["action"] = "adaptive_skip"
            self._record_recent("adaptive_skip", f"{fmt}:{source_hint}", ok=True)
            return (image_ref, note, meta) if with_meta else image_ref

        await self._inc_stat("convert_ok")
        meta["action"] = action
        self._record_recent(action, f"{fmt}/{frame_count}f", ok=True)

        if self.cache_enabled and action.startswith("convert"):
            phash = self._average_hash_hex(data)
            await self._put_cache_entry(
                cache_key=cache_key,
                content_hash=content_hash,
                path=out_path,
                note=note,
                source_format=fmt,
                frame_count=frame_count,
                phash=phash,
            )
            await self._enforce_cache_limits()

        if with_meta:
            return out_path, note, meta
        return out_path

    def _resolve_allowed_formats(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> set[str]:
        """Return formats that can be passed through without conversion."""
        if not self.model_adaptive:
            return set(PASSTHROUGH_BASE)

        hints: list[str] = []
        try:
            prov = None
            if hasattr(self.context, "get_using_provider"):
                prov = self.context.get_using_provider(event.unified_msg_origin)
            if prov is not None:
                cfg = getattr(prov, "provider_config", {}) or {}
                hints.append(str(cfg.get("type", "")))
                hints.append(str(cfg.get("id", "")))
                hints.append(str(cfg.get("model", "")))
                if hasattr(prov, "get_model"):
                    try:
                        hints.append(str(prov.get_model() or ""))
                    except Exception:
                        pass
            if getattr(req, "model", None):
                hints.append(str(req.model))
        except Exception as exc:  # noqa: BLE001
            logger.debug("[图片转PNG] 解析模型能力失败: %s", exc)

        blob = " ".join(hints).lower()
        allowed = set(PASSTHROUGH_BASE)
        # Most modern VL models accept WEBP.
        allowed.add("WEBP")

        if any(h in blob for h in STRICT_NO_GIF_HINTS):
            allowed.discard("GIF")
            return allowed
        if any(h in blob for h in GIF_FRIENDLY_HINTS):
            allowed.add("GIF")
            return allowed
        # Default conservative: no GIF
        allowed.discard("GIF")
        return allowed

    def _make_cache_key(self, content_hash: str) -> str:
        raw = f"{content_hash}:{self._options_sig}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def _cache_file_path(self, cache_key: str, ext: str = "png") -> Path:
        return self.cache_files_dir / cache_key[:2] / f"{cache_key}.{ext}"

    def _is_cache_path(self, path: str) -> bool:
        try:
            resolved = Path(path).resolve()
            cache_root = self.cache_files_dir.resolve()
            return resolved == cache_root or cache_root in resolved.parents
        except Exception:
            return False

    # ───────────────────────── cache / stats IO ─────────────────────────

    async def _load_index(self) -> None:
        async with self._cache_lock:
            if not self.cache_index_path.exists():
                self._index = {"version": CACHE_INDEX_VERSION, "entries": {}}
                return
            try:
                raw = self.cache_index_path.read_text(encoding="utf-8-sig")
                data = json.loads(raw)
                entries = data.get("entries") if isinstance(data, dict) else {}
                if not isinstance(entries, dict):
                    entries = {}
                self._index = {
                    "version": int((data or {}).get("version") or 1),
                    "entries": entries,
                }
            except Exception as exc:  # noqa: BLE001
                logger.warning("[图片转PNG] 读取缓存索引失败，将重建: %s", exc)
                self._index = {"version": CACHE_INDEX_VERSION, "entries": {}}

    async def _migrate_cache_index(self) -> None:
        """清理旧版缓存冲突项：失效文件、过期参数签名、孤儿文件。"""
        removed = 0
        freed = 0
        async with self._cache_lock:
            entries = self._index.setdefault("entries", {})
            alive_paths: set[str] = set()
            for key, item in list(entries.items()):
                if not isinstance(item, dict):
                    entries.pop(key, None)
                    removed += 1
                    continue
                path = str(item.get("path") or "")
                options_sig = str(item.get("options_sig") or "")
                missing = not path or not os.path.exists(path)
                stale_options = bool(options_sig) and options_sig != self._options_sig
                # 旧版索引可能无 options_sig / phash；参数已变的条目不可安全复用
                if missing or stale_options:
                    if path and os.path.exists(path):
                        try:
                            freed += os.path.getsize(path)
                            os.remove(path)
                        except OSError:
                            pass
                    entries.pop(key, None)
                    removed += 1
                    continue
                # normalize fields for v2
                item.setdefault("phash", None)
                item.setdefault("options_sig", self._options_sig)
                item.setdefault("hit_count", 0)
                alive_paths.add(os.path.abspath(path))

            # orphan files
            if self.cache_files_dir.exists():
                for file_path in self.cache_files_dir.rglob("*"):
                    if not file_path.is_file():
                        continue
                    abs_path = str(file_path.resolve())
                    if abs_path not in alive_paths:
                        try:
                            freed += file_path.stat().st_size
                            file_path.unlink(missing_ok=True)
                            removed += 1
                        except OSError:
                            pass

            for sub in sorted(self.cache_files_dir.glob("*"), reverse=True):
                if sub.is_dir():
                    try:
                        next(sub.iterdir())
                    except StopIteration:
                        try:
                            sub.rmdir()
                        except OSError:
                            pass

            self._index["version"] = CACHE_INDEX_VERSION
            await self._save_index_unlocked()

        if removed:
            logger.info(
                "[图片转PNG] 缓存迁移完成: removed=%s freed≈%s remaining=%s",
                removed,
                freed,
                len(self._index.get("entries", {})),
            )

    async def _save_index_unlocked(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_index_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self._index, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.cache_index_path)

    async def _load_stats(self) -> None:
        async with self._stats_lock:
            if not self.stats_path.exists():
                self._stats = self._default_stats()
                return
            try:
                data = json.loads(self.stats_path.read_text(encoding="utf-8-sig"))
                base = self._default_stats()
                if isinstance(data, dict):
                    base.update({k: data.get(k, v) for k, v in base.items()})
                self._stats = base
            except Exception:
                self._stats = self._default_stats()

    async def _save_stats_unlocked(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.stats_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._stats, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.stats_path)

    async def _inc_stat(self, key: str, amount: int = 1) -> None:
        async with self._stats_lock:
            self._stats[key] = int(self._stats.get(key, 0) or 0) + amount
            # flush lightly
            if int(self._stats[key]) % 5 == 0:
                await self._save_stats_unlocked()

    def _record_recent(self, action: str, detail: str, *, ok: bool = True) -> None:
        self._recent.append(
            {
                "ts": time.time(),
                "action": action,
                "detail": (detail or "")[:160],
                "ok": ok,
            }
        )

    async def _get_cache_entry(self, cache_key: str) -> dict[str, Any] | None:
        async with self._cache_lock:
            entries = self._index.setdefault("entries", {})
            item = entries.get(cache_key)
            if not item:
                return None
            path = str(item.get("path") or "")
            if not path or not os.path.exists(path):
                entries.pop(cache_key, None)
                await self._save_index_unlocked()
                return None
            now = time.time()
            item["last_access"] = now
            item["hit_count"] = int(item.get("hit_count", 0) or 0) + 1
            if item["hit_count"] == 1 or item["hit_count"] % 10 == 0:
                await self._save_index_unlocked()
            return {
                "path": path,
                "note": item.get("note"),
                "frame_count": item.get("frame_count", 1),
                "source_format": item.get("source_format"),
            }

    async def _find_near_duplicate(self, data: bytes) -> dict[str, Any] | None:
        try:
            phash = self._average_hash_hex(data)
        except Exception:
            return None
        async with self._cache_lock:
            entries = self._index.get("entries", {})
            best_key = None
            best_dist = 999
            best_item = None
            for key, item in entries.items():
                if item.get("options_sig") != self._options_sig:
                    continue
                other = item.get("phash")
                if not other:
                    continue
                dist = self._hamming_hex(phash, other)
                if dist < best_dist:
                    best_dist = dist
                    best_key = key
                    best_item = item
            if best_item is None or best_dist > self.near_duplicate_threshold:
                return None
            path = str(best_item.get("path") or "")
            if not path or not os.path.exists(path):
                return None
            best_item["last_access"] = time.time()
            best_item["hit_count"] = int(best_item.get("hit_count", 0) or 0) + 1
            if best_key and (best_item["hit_count"] % 10 == 0):
                await self._save_index_unlocked()
            note = best_item.get("note")
            extra = f"已命中近似重复表情缓存(距离={best_dist})。"
            note = f"{note} {extra}".strip() if note else extra
            return {"path": path, "note": note}

    async def _put_cache_entry(
        self,
        *,
        cache_key: str,
        content_hash: str,
        path: str,
        note: str | None,
        source_format: str,
        frame_count: int,
        phash: str | None,
    ) -> None:
        async with self._cache_lock:
            now = time.time()
            size = 0
            try:
                size = os.path.getsize(path)
            except OSError:
                pass
            self._index.setdefault("entries", {})[cache_key] = {
                "content_hash": content_hash,
                "options_sig": self._options_sig,
                "path": path,
                "note": note,
                "source_format": source_format,
                "frame_count": frame_count,
                "size": size,
                "created_at": now,
                "last_access": now,
                "hit_count": 0,
                "phash": phash,
            }
            await self._save_index_unlocked()

    async def _enforce_cache_limits(self) -> None:
        """LRU eviction by max entries and max size."""
        async with self._cache_lock:
            entries: dict[str, Any] = self._index.setdefault("entries", {})
            if not entries:
                return

            def total_size() -> int:
                return sum(int(v.get("size", 0) or 0) for v in entries.values())

            max_bytes = self.cache_max_mb * 1024 * 1024
            changed = False

            while len(entries) > self.cache_max_entries or total_size() > max_bytes:
                # Evict least recently accessed
                oldest_key = min(
                    entries.keys(),
                    key=lambda k: float(
                        entries[k].get("last_access")
                        or entries[k].get("created_at")
                        or 0
                    ),
                )
                item = entries.pop(oldest_key)
                path = str(item.get("path") or "")
                if path and os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                changed = True
                if not entries:
                    break
            if changed:
                await self._save_index_unlocked()

    async def cleanup_cache(self, *, force_all: bool = False) -> dict[str, int]:
        ttl_seconds = self.cache_ttl_days * 86400
        now = time.time()
        removed_entries = 0
        removed_files = 0
        freed_bytes = 0

        async with self._cache_lock:
            entries = self._index.setdefault("entries", {})
            alive_paths: set[str] = set()
            to_delete: list[str] = []

            for key, item in list(entries.items()):
                path = str(item.get("path") or "")
                last_access = float(item.get("last_access") or item.get("created_at") or 0)
                expired = force_all or (now - last_access > ttl_seconds)
                missing = not path or not os.path.exists(path)
                if expired or missing:
                    to_delete.append(key)
                    if path and os.path.exists(path):
                        try:
                            freed_bytes += os.path.getsize(path)
                            os.remove(path)
                            removed_files += 1
                        except OSError as exc:
                            logger.warning("[图片转PNG] 删除缓存文件失败 %s: %s", path, exc)
                elif path:
                    alive_paths.add(os.path.abspath(path))

            for key in to_delete:
                entries.pop(key, None)
                removed_entries += 1

            if self.cache_files_dir.exists():
                for file_path in self.cache_files_dir.rglob("*"):
                    if not file_path.is_file():
                        continue
                    abs_path = str(file_path.resolve())
                    if abs_path not in alive_paths:
                        try:
                            freed_bytes += file_path.stat().st_size
                            file_path.unlink(missing_ok=True)
                            removed_files += 1
                        except OSError:
                            pass

            for sub in sorted(self.cache_files_dir.glob("*"), reverse=True):
                if sub.is_dir():
                    try:
                        next(sub.iterdir())
                    except StopIteration:
                        try:
                            sub.rmdir()
                        except OSError:
                            pass

            await self._save_index_unlocked()
            remaining = len(entries)

        result = {
            "removed_entries": removed_entries,
            "removed_files": removed_files,
            "freed_bytes": freed_bytes,
            "remaining_entries": remaining,
        }
        async with self._stats_lock:
            self._stats["last_cleanup_at"] = now
            self._stats["last_cleanup_result"] = result
            await self._save_stats_unlocked()
        logger.info("[图片转PNG] 缓存清理完成: %s", result)
        return result

    async def _cleanup_loop(self) -> None:
        logger.info(
            "[图片转PNG] 每日清理任务: %02d:%02d %s",
            self.cache_cleanup_hour,
            self.cache_cleanup_minute,
            self.cache_timezone,
        )
        while True:
            try:
                await asyncio.sleep(self._seconds_until_next_cleanup())
                await self.cleanup_cache(force_all=False)
                await self._enforce_cache_limits()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error("[图片转PNG] 定时清理失败: %s", exc, exc_info=True)
                await asyncio.sleep(3600)

    def _seconds_until_next_cleanup(self) -> float:
        try:
            tz = ZoneInfo(self.cache_timezone)
        except Exception:
            tz = ZoneInfo("Asia/Shanghai")
        now = datetime.now(tz)
        target = now.replace(
            hour=self.cache_cleanup_hour,
            minute=self.cache_cleanup_minute,
            second=0,
            microsecond=0,
        )
        if target <= now:
            target += timedelta(days=1)
        return max(5.0, (target - now).total_seconds())

    # ───────────────────────── IO helpers ─────────────────────────

    async def _load_image_bytes(self, image_ref: str) -> tuple[bytes | None, str]:
        ref = image_ref.strip()
        try:
            if ref.startswith("data:image"):
                _, encoded = ref.split(",", 1)
                return base64.b64decode(encoded), "data-url"
            if ref.startswith("base64://"):
                encoded = ref.removeprefix("base64://")
                if encoded.startswith("data:image") and "," in encoded:
                    encoded = encoded.split(",", 1)[1]
                return base64.b64decode(encoded), "base64"
            if ref.startswith("http://") or ref.startswith("https://"):
                path = await download_image_by_url(ref)
                return Path(path).read_bytes(), f"url:{ref[:80]}"
            path = self._normalize_local_path(ref)
            if path and os.path.exists(path):
                return Path(path).read_bytes(), path
            if len(ref) > 64 and all(ch.isalnum() or ch in "+/=\n\r" for ch in ref[:120]):
                try:
                    return base64.b64decode(ref, validate=False), "raw-base64"
                except Exception:
                    pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("[图片转PNG] 加载图片失败: %s (%s)", exc, ref[:120])
        return None, ref[:120]

    @staticmethod
    def _normalize_local_path(image_ref: str) -> str | None:
        if image_ref.startswith("file://"):
            parsed = urlparse(image_ref)
            path = unquote(parsed.path or "")
            if parsed.netloc and parsed.netloc not in {"", "localhost"}:
                if len(parsed.netloc) == 2 and parsed.netloc[1] == ":":
                    path = f"{parsed.netloc}{path}"
            if path.startswith("/") and len(path) >= 3 and path[2] == ":":
                path = path[1:]
            return path or None
        if os.path.exists(image_ref):
            return os.path.abspath(image_ref)
        return None

    @staticmethod
    def _is_local_path(image_ref: str) -> bool:
        if not image_ref:
            return False
        if image_ref.startswith(("http://", "https://", "data:image", "base64://")):
            return False
        if image_ref.startswith("file://"):
            return True
        return os.path.exists(image_ref)

    @staticmethod
    def _fmt_bytes(n: int) -> str:
        n = float(n)
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024 or unit == "GB":
                return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
            n /= 1024
        return f"{n:.1f}GB"

    # ───────────────────────── image convert core ─────────────────────────

    def _convert_bytes_to_output_with_meta(
        self,
        data: bytes,
        source_hint: str,
        allowed_formats: set[str],
        cache_key: str | None,
    ) -> tuple[str, str | None, str, int, str] | None:
        try:
            img = PILImage.open(io.BytesIO(data))
        except (OSError, UnidentifiedImageError):
            return None

        with img:
            fmt = (img.format or "UNKNOWN").upper()
            if fmt == "JPG":
                fmt = "JPEG"
            n_frames = int(getattr(img, "n_frames", 1) or 1)
            is_animated = bool(getattr(img, "is_animated", False) or n_frames > 1)

            # Adaptive / passthrough
            if fmt in allowed_formats and not is_animated:
                return None
            if is_animated and fmt in allowed_formats and fmt == "GIF":
                # Model accepts GIF animation natively.
                return (
                    "",
                    "模型支持 GIF 动图，已保留原始动画不做转换。",
                    fmt,
                    n_frames,
                    "adaptive_skip",
                )

            # Downscale oversized static sources first (protect tokens/latency).
            # Animated images are resized per-frame later to preserve frame sequence.
            if (not is_animated) and max(img.size) > self.max_source_side:
                base = img.convert("RGBA") if img.mode not in {"RGB", "RGBA"} else img
                img = ImageOps.contain(
                    base,
                    (self.max_source_side, self.max_source_side),
                    method=PILImage.Resampling.LANCZOS,
                )

            ext = "jpg" if self.output_format == "jpeg" else "png"
            target_path = None
            if self.cache_enabled and cache_key:
                target_path = str(self._cache_file_path(cache_key, ext=ext))

            if is_animated:
                return self._convert_animated(img, fmt, target_path)

            single = self._prepare_static_frame(img)
            out_path = self._save_image(single, target_path=target_path)
            single.close()
            return out_path, None, fmt, 1, "convert_static"

    def _convert_animated(
        self,
        img: PILImage.Image,
        fmt: str,
        target_path: str | None,
    ) -> tuple[str, str | None, str, int, str]:
        frames = self._extract_animation_frames(img)
        total = len(frames)
        mode = self.animated_mode

        if mode == "first_frame" or total <= 1:
            frame = frames[0][0]
            prepared = frame.copy()
            out = self._save_image(prepared, target_path=target_path)
            prepared.close()
            note = f"该图原为动画({fmt})，共 {total} 帧；当前策略为仅保留首帧。"
            return out, note, fmt, total, "convert_first_frame"

        if mode == "key_frames":
            key_indices = self._key_frame_indices(total)
            selected = [(frames[i][0], frames[i][1], i + 1) for i in key_indices]
            out = self._save_contact_sheet(selected, fmt=fmt, target_path=target_path, title_prefix="Key frames")
            note = (
                f"该图原为动画({fmt})，共 {total} 帧；"
                f"已提取关键帧 {len(selected)} 张并拼贴（首/中/尾等）。"
                "请结合整张拼贴理解动态过程。"
            )
            return out, note, fmt, total, "convert_key_frames"

        # contact_sheet (default)
        sampled = self._sample_frames(frames)
        out = self._save_contact_sheet(sampled, fmt=fmt, target_path=target_path)
        used = len(sampled)
        note = (
            f"该图原为动画({fmt})，共 {total} 帧；"
            f"已展开为按时间顺序从左到右、从上到下排列的 {used} 帧静态拼贴图。"
            "请结合整张拼贴理解动态过程，不要只看其中一格。"
        )
        if total > used:
            note += f"为控制体积，已从 {total} 帧中均匀采样 {used} 帧。"
        return out, note, fmt, total, "convert_contact_sheet"

    @staticmethod
    def _key_frame_indices(total: int) -> list[int]:
        if total <= 3:
            return list(range(total))
        mid = total // 2
        q1 = total // 4
        q3 = (total * 3) // 4
        # unique ordered
        idxs = [0, q1, mid, q3, total - 1]
        out: list[int] = []
        for i in idxs:
            if i not in out:
                out.append(i)
        return out

    def _extract_animation_frames(
        self,
        img: PILImage.Image,
    ) -> list[tuple[PILImage.Image, int]]:
        n_frames = int(getattr(img, "n_frames", 1) or 1)
        size = img.size
        scale = 1.0
        if max(size) > self.max_source_side:
            scale = self.max_source_side / float(max(size))
            size = (max(1, int(size[0] * scale)), max(1, int(size[1] * scale)))
        frames: list[tuple[PILImage.Image, int]] = []
        canvas = PILImage.new("RGBA", size, (0, 0, 0, 0))
        previous = canvas.copy()

        for index in range(n_frames):
            img.seek(index)
            duration = int(img.info.get("duration", 0) or 0)
            dispose = int(getattr(img, "disposal_method", 0) or 0)
            frame_rgba = img.convert("RGBA")
            if scale != 1.0:
                frame_rgba = frame_rgba.resize(size, PILImage.Resampling.LANCZOS)
            composed = canvas.copy()
            composed.alpha_composite(frame_rgba)
            frames.append((composed.copy(), duration))
            if dispose == 2:
                canvas = PILImage.new("RGBA", size, (0, 0, 0, 0))
            elif dispose == 3:
                canvas = previous.copy()
            else:
                previous = composed.copy()
                canvas = composed
            frame_rgba.close()

        if not frames:
            img.seek(0)
            frames = [(img.convert("RGBA"), int(img.info.get("duration", 0) or 0))]
        return frames

    def _sample_frames(
        self,
        frames: list[tuple[PILImage.Image, int]],
    ) -> list[tuple[PILImage.Image, int, int]]:
        total = len(frames)
        if total <= self.max_frames:
            return [(f, d, i + 1) for i, (f, d) in enumerate(frames)]
        if self.max_frames == 1:
            indices = [0]
        else:
            indices = [
                round(i * (total - 1) / (self.max_frames - 1))
                for i in range(self.max_frames)
            ]
            seen: set[int] = set()
            unique: list[int] = []
            for idx in indices:
                if idx not in seen:
                    seen.add(idx)
                    unique.append(idx)
            indices = unique
        return [(frames[i][0], frames[i][1], i + 1) for i in indices]

    def _prepare_static_frame(self, img: PILImage.Image) -> PILImage.Image:
        try:
            img.seek(0)
        except Exception:
            pass
        if self.keep_alpha and self.output_format == "png":
            if img.mode in ("RGBA", "LA"):
                return img.convert("RGBA")
            if img.mode == "P":
                return img.convert("RGBA")
            if img.mode == "RGB":
                return img.copy()
            return img.convert("RGBA")

        # Flatten alpha for JPEG or when keep_alpha is false
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            rgba = img.convert("RGBA")
            background = PILImage.new("RGB", rgba.size, (255, 255, 255))
            background.paste(rgba, mask=rgba.split()[-1])
            rgba.close()
            return background
        if img.mode != "RGB":
            return img.convert("RGB")
        return img.copy()

    def _save_contact_sheet(
        self,
        frames: list[tuple[PILImage.Image, int, int]],
        *,
        fmt: str,
        target_path: str | None = None,
        title_prefix: str = "Animation frames",
    ) -> str:
        if not frames:
            raise ValueError("no frames to compose")

        cells: list[tuple[PILImage.Image, int, int]] = []
        cell_w = 0
        cell_h = 0
        for frame, duration, original_idx in frames:
            cell = frame.copy()
            if max(cell.size) > self.max_cell_size:
                cell.thumbnail(
                    (self.max_cell_size, self.max_cell_size),
                    PILImage.Resampling.LANCZOS,
                )
            if cell.mode != "RGBA":
                cell = cell.convert("RGBA")
            cell_w = max(cell_w, cell.width)
            cell_h = max(cell_h, cell.height)
            cells.append((cell, duration, original_idx))

        cols = min(self.contact_sheet_columns, len(cells))
        rows = int(math.ceil(len(cells) / cols))
        pad = 8
        header_h = 36 if self.show_frame_labels else 8
        label_h = 22 if self.show_frame_labels else 0
        sheet_w = cols * cell_w + (cols + 1) * pad
        sheet_h = header_h + rows * (cell_h + label_h) + (rows + 1) * pad
        sheet = PILImage.new("RGBA", (sheet_w, sheet_h), self.pad_color)
        draw = ImageDraw.Draw(sheet)
        font = self._get_font(16)
        small_font = self._get_font(14)

        if self.show_frame_labels:
            title = f"{title_prefix} ({fmt}) · {len(cells)} shown · left→right, top→bottom"
            draw.text((pad, 8), title, fill=(30, 30, 30, 255), font=font)

        for i, (cell, duration, original_idx) in enumerate(cells):
            row, col = divmod(i, cols)
            x = pad + col * (cell_w + pad)
            y = header_h + pad + row * (cell_h + label_h + pad)
            ox = x + (cell_w - cell.width) // 2
            oy = y + (cell_h - cell.height) // 2
            backing = PILImage.new("RGBA", cell.size, (255, 255, 255, 255))
            backing.alpha_composite(cell)
            sheet.paste(backing, (ox, oy))
            backing.close()
            if self.show_frame_labels:
                label = f"#{original_idx}"
                if duration > 0:
                    label += f" {duration}ms"
                ly = y + cell_h + 2
                draw.rectangle((x, ly, x + cell_w, ly + label_h - 2), fill=self.label_bg)
                draw.text((x + 4, ly + 2), label, fill=self.label_fg, font=small_font)

        out = self._save_image(sheet, target_path=target_path)
        sheet.close()
        for cell, _, _ in cells:
            cell.close()
        return out

    def _save_image(
        self,
        image: PILImage.Image,
        *,
        target_path: str | None = None,
    ) -> str:
        ext = "jpg" if self.output_format == "jpeg" else "png"
        if target_path:
            out_path = Path(target_path)
            out_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            tmp_dir = self.cache_dir / "tmp"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            out_path = tmp_dir / f"img2png_{uuid.uuid4().hex}.{ext}"

        to_save = image
        created = False
        if self.output_format == "jpeg":
            if image.mode != "RGB":
                if image.mode in ("RGBA", "LA") or (
                    image.mode == "P" and "transparency" in getattr(image, "info", {})
                ):
                    rgba = image.convert("RGBA")
                    bg = PILImage.new("RGB", rgba.size, (255, 255, 255))
                    bg.paste(rgba, mask=rgba.split()[-1])
                    rgba.close()
                    to_save = bg
                else:
                    to_save = image.convert("RGB")
                created = True
        else:
            if not self.keep_alpha and image.mode == "RGBA":
                bg = PILImage.new("RGB", image.size, (255, 255, 255))
                bg.paste(image, mask=image.split()[-1])
                to_save = bg
                created = True
            elif image.mode not in ("RGB", "RGBA"):
                to_save = image.convert("RGBA")
                created = True

        if self.strip_metadata:
            data = list(to_save.getdata())
            clean = PILImage.new(to_save.mode, to_save.size)
            clean.putdata(data)
            if created:
                to_save.close()
            to_save = clean
            created = True

        try:
            tmp = out_path.with_suffix(out_path.suffix + ".tmp")
            if self.output_format == "jpeg":
                to_save.save(tmp, format="JPEG", quality=self.jpeg_quality, optimize=True)
            else:
                to_save.save(tmp, format="PNG", optimize=True)
            tmp.replace(out_path)
        finally:
            if created:
                to_save.close()
        return str(out_path.resolve())

    def _average_hash_hex(self, data: bytes, hash_size: int = 8) -> str:
        with PILImage.open(io.BytesIO(data)) as img:
            try:
                img.seek(0)
            except Exception:
                pass
            gray = img.convert("L").resize((hash_size, hash_size), PILImage.Resampling.LANCZOS)
            pixels = list(gray.getdata())
            avg = sum(pixels) / len(pixels)
            bits = "".join("1" if p >= avg else "0" for p in pixels)
        # pack bits to hex
        value = int(bits, 2)
        return f"{value:0{hash_size * hash_size // 4}x}"

    @staticmethod
    def _hamming_hex(a: str, b: str) -> int:
        try:
            return bin(int(a, 16) ^ int(b, 16)).count("1")
        except Exception:
            return 999

    @staticmethod
    def _get_font(size: int):
        candidates = [
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        ]
        for path in candidates:
            if os.path.exists(path):
                try:
                    return ImageFont.truetype(path, size=size)
                except Exception:
                    continue
        return ImageFont.load_default()
