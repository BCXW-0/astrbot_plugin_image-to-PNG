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
from PIL import ImageDraw, ImageFont
from PIL import UnidentifiedImageError

PASSTHROUGH_FORMATS = {"JPEG", "PNG"}
CACHE_INDEX_VERSION = 1


@register(
    "astrbot_plugin_image_to_png",
    "Xiawan",
    "将非 PNG/JPEG 图片统一转为 PNG；动图展开为逐帧拼贴，并支持哈希缓存与每日清理。",
    "1.2.0",
)
class ImageToPngPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.enabled = bool(self._cfg("enabled", True))
        self.convert_message_images = bool(self._cfg("convert_message_images", True))
        self.convert_request_images = bool(self._cfg("convert_request_images", True))
        self.keep_alpha = bool(self._cfg("keep_alpha", True))
        self.animated_expand = bool(self._cfg("animated_expand", True))
        self.max_frames = max(1, int(self._cfg("max_frames", 24) or 24))
        self.contact_sheet_columns = max(
            1,
            int(self._cfg("contact_sheet_columns", 4) or 4),
        )
        self.max_cell_size = max(32, int(self._cfg("max_cell_size", 256) or 256))
        self.show_frame_labels = bool(self._cfg("show_frame_labels", True))

        self.cache_enabled = bool(self._cfg("cache_enabled", True))
        self.cache_ttl_days = max(1, int(self._cfg("cache_ttl_days", 7) or 7))
        self.cache_cleanup_enabled = bool(self._cfg("cache_cleanup_enabled", True))
        self.cache_cleanup_hour = min(
            23,
            max(0, int(self._cfg("cache_cleanup_hour", 3) or 3)),
        )
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
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_files_dir.mkdir(parents=True, exist_ok=True)

        self._cache_lock = asyncio.Lock()
        self._index: dict[str, Any] = {
            "version": CACHE_INDEX_VERSION,
            "entries": {},
        }
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

    def _build_options_sig(self) -> str:
        payload = {
            "animated_expand": self.animated_expand,
            "max_frames": self.max_frames,
            "contact_sheet_columns": self.contact_sheet_columns,
            "max_cell_size": self.max_cell_size,
            "show_frame_labels": self.show_frame_labels,
            "keep_alpha": self.keep_alpha,
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha1(raw).hexdigest()[:12]

    async def initialize(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_files_dir.mkdir(parents=True, exist_ok=True)
        await self._load_index()
        if self.cache_cleanup_enabled:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info(
            "[图片转PNG] 插件已初始化。enabled=%s cache=%s ttl_days=%s cleanup=%02d:%02d(%s)",
            self.enabled,
            self.cache_enabled,
            self.cache_ttl_days,
            self.cache_cleanup_hour,
            self.cache_cleanup_minute,
            self.cache_timezone,
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
        logger.info("[图片转PNG] 插件已卸载。")

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10000)
    async def convert_incoming_images(self, event: AstrMessageEvent) -> None:
        if not self.enabled or not self.convert_message_images:
            return
        try:
            messages = event.get_messages() or []
            await self._convert_message_chain(event, messages)
        except Exception as exc:  # noqa: BLE001
            logger.error("[图片转PNG] 转换消息图片失败: %s", exc, exc_info=True)

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
            converted: list[str] = []
            changed = False
            notes: list[str] = []
            for ref in list(req.image_urls):
                new_ref, note, from_cache = await self.ensure_allowed_image_ref(
                    ref,
                    with_meta=True,
                )
                if new_ref != ref:
                    changed = True
                    # Cache files are durable; only track non-cache temps.
                    if self._is_local_path(new_ref) and not self._is_cache_path(new_ref):
                        event.track_temporary_local_file(new_ref)
                if note:
                    notes.append(note)
                converted.append(new_ref)
            if changed:
                req.image_urls = converted
                logger.info("[图片转PNG] 已处理请求图片 %d 张", len(converted))
            if notes:
                note_text = "；".join(notes)
                existing = req.prompt or ""
                marker = "[动画帧说明]"
                if marker not in existing:
                    req.prompt = (
                        f"{existing}\n{marker}{note_text}".strip()
                        if existing
                        else f"{marker}{note_text}"
                    )
        except Exception as exc:  # noqa: BLE001
            logger.error("[图片转PNG] 转换请求图片失败: %s", exc, exc_info=True)

    @filter.command("图片转png缓存清理", alias={"image2png_cache_clean", "清理图片转png缓存"})
    async def clean_cache_command(self, event: AstrMessageEvent):
        """手动清理过期/失效的图片转换缓存。"""
        result = await self.cleanup_cache(force_all=False)
        yield event.plain_result(
            "[图片转PNG] 缓存清理完成："
            f"删除条目 {result['removed_entries']}，"
            f"删除文件 {result['removed_files']}，"
            f"释放约 {result['freed_bytes']} 字节，"
            f"剩余 {result['remaining_entries']} 条。"
        )

    @filter.command("图片转png缓存状态", alias={"image2png_cache_status", "图片转png缓存"})
    async def cache_status_command(self, event: AstrMessageEvent):
        """查看图片转换缓存状态。"""
        async with self._cache_lock:
            entries = self._index.get("entries", {})
            total = len(entries)
            total_bytes = 0
            hits = 0
            for item in entries.values():
                total_bytes += int(item.get("size", 0) or 0)
                hits += int(item.get("hit_count", 0) or 0)
        yield event.plain_result(
            "[图片转PNG] 缓存状态\n"
            f"- 启用: {self.cache_enabled}\n"
            f"- 条目数: {total}\n"
            f"- 累计命中: {hits}\n"
            f"- 占用约: {total_bytes} 字节\n"
            f"- TTL: {self.cache_ttl_days} 天\n"
            f"- 每日清理: "
            f"{'开' if self.cache_cleanup_enabled else '关'} "
            f"{self.cache_cleanup_hour:02d}:{self.cache_cleanup_minute:02d} "
            f"({self.cache_timezone})\n"
            f"- 目录: {self.cache_dir}"
        )

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
            return

        new_path, _note, _from_cache = await self.ensure_allowed_image_ref(
            source_path,
            with_meta=True,
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
        logger.info("[图片转PNG] 消息图片已转为 PNG: %s", abs_path)

    async def ensure_allowed_image_ref(
        self,
        image_ref: str,
        *,
        with_meta: bool = False,
    ) -> str | tuple[str, str | None, bool]:
        if not image_ref:
            return (image_ref, None, False) if with_meta else image_ref

        data, source_hint = await self._load_image_bytes(image_ref)
        if data is None:
            return (image_ref, None, False) if with_meta else image_ref

        content_hash = hashlib.sha256(data).hexdigest()
        cache_key = self._make_cache_key(content_hash)

        if self.cache_enabled:
            cached = await self._get_cache_entry(cache_key)
            if cached:
                logger.info(
                    "[图片转PNG] 缓存命中 content=%s... options=%s (%s)",
                    content_hash[:12],
                    self._options_sig,
                    source_hint,
                )
                note = cached.get("note")
                if with_meta:
                    return cached["path"], note, True
                return cached["path"]

        try:
            result = await asyncio.to_thread(
                self._convert_bytes_to_png_with_meta,
                data,
                source_hint,
                cache_key=cache_key,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "[图片转PNG] 转换失败(%s): %s",
                source_hint,
                exc,
                exc_info=True,
            )
            return (image_ref, None, False) if with_meta else image_ref

        if result is None:
            return (image_ref, None, False) if with_meta else image_ref

        png_path, note, fmt, frame_count, from_cache = result
        if frame_count > 1:
            logger.info(
                "[图片转PNG] %s 动图(%d帧) -> 逐帧拼贴 PNG%s (%s)",
                fmt,
                frame_count,
                " [cache]" if from_cache else "",
                source_hint,
            )
        else:
            logger.info(
                "[图片转PNG] %s -> PNG%s (%s)",
                fmt,
                " [cache]" if from_cache else "",
                source_hint,
            )

        if self.cache_enabled and not from_cache:
            await self._put_cache_entry(
                cache_key=cache_key,
                content_hash=content_hash,
                path=png_path,
                note=note,
                source_format=fmt,
                frame_count=frame_count,
            )

        if with_meta:
            return png_path, note, from_cache
        return png_path

    def _make_cache_key(self, content_hash: str) -> str:
        # Same sticker + same conversion options => same cache entry.
        raw = f"{content_hash}:{self._options_sig}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def _cache_file_path(self, cache_key: str) -> Path:
        return self.cache_files_dir / cache_key[:2] / f"{cache_key}.png"

    def _is_cache_path(self, path: str) -> bool:
        try:
            resolved = Path(path).resolve()
            cache_root = self.cache_files_dir.resolve()
            return resolved == cache_root or cache_root in resolved.parents
        except Exception:
            return False

    async def _load_index(self) -> None:
        async with self._cache_lock:
            if not self.cache_index_path.exists():
                self._index = {"version": CACHE_INDEX_VERSION, "entries": {}}
                return
            try:
                raw = self.cache_index_path.read_text(encoding="utf-8")
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError("invalid index root")
                entries = data.get("entries") or {}
                if not isinstance(entries, dict):
                    entries = {}
                self._index = {
                    "version": int(data.get("version") or CACHE_INDEX_VERSION),
                    "entries": entries,
                }
            except Exception as exc:  # noqa: BLE001
                logger.warning("[图片转PNG] 读取缓存索引失败，将重建: %s", exc)
                self._index = {"version": CACHE_INDEX_VERSION, "entries": {}}

    async def _save_index_unlocked(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_index_path.with_suffix(".json.tmp")
        payload = json.dumps(self._index, ensure_ascii=False, indent=2)
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(self.cache_index_path)

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
            # Persist occasionally: every hit would be heavy; save every 10 hits or first hit.
            if item["hit_count"] == 1 or item["hit_count"] % 10 == 0:
                await self._save_index_unlocked()
            return {
                "path": path,
                "note": item.get("note"),
                "frame_count": item.get("frame_count", 1),
                "source_format": item.get("source_format"),
            }

    async def _put_cache_entry(
        self,
        *,
        cache_key: str,
        content_hash: str,
        path: str,
        note: str | None,
        source_format: str,
        frame_count: int,
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
            }
            await self._save_index_unlocked()

    async def cleanup_cache(self, *, force_all: bool = False) -> dict[str, int]:
        """Remove expired / missing cache entries and orphan files.

        Args:
            force_all: If True, delete all cache entries regardless of TTL.
        """
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
                else:
                    if path:
                        alive_paths.add(os.path.abspath(path))

            for key in to_delete:
                entries.pop(key, None)
                removed_entries += 1

            # Orphan files under cache/files
            if self.cache_files_dir.exists():
                for file_path in self.cache_files_dir.rglob("*.png"):
                    abs_path = str(file_path.resolve())
                    if abs_path not in alive_paths:
                        try:
                            freed_bytes += file_path.stat().st_size
                            file_path.unlink(missing_ok=True)
                            removed_files += 1
                        except OSError as exc:
                            logger.warning(
                                "[图片转PNG] 删除孤儿缓存失败 %s: %s",
                                abs_path,
                                exc,
                            )

            # Clean empty subdirs
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

        logger.info(
            "[图片转PNG] 缓存清理完成: removed_entries=%s removed_files=%s freed=%s remaining=%s",
            removed_entries,
            removed_files,
            freed_bytes,
            remaining,
        )
        return {
            "removed_entries": removed_entries,
            "removed_files": removed_files,
            "freed_bytes": freed_bytes,
            "remaining_entries": remaining,
        }

    async def _cleanup_loop(self) -> None:
        logger.info(
            "[图片转PNG] 已启动每日缓存清理任务: %02d:%02d %s",
            self.cache_cleanup_hour,
            self.cache_cleanup_minute,
            self.cache_timezone,
        )
        while True:
            try:
                delay = self._seconds_until_next_cleanup()
                await asyncio.sleep(delay)
                await self.cleanup_cache(force_all=False)
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
            target = target + timedelta(days=1)
        return max(5.0, (target - now).total_seconds())

    async def _load_image_bytes(
        self,
        image_ref: str,
    ) -> tuple[bytes | None, str]:
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

            if len(ref) > 64 and all(
                ch.isalnum() or ch in "+/=\n\r" for ch in ref[:120]
            ):
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

    def _convert_bytes_to_png_with_meta(
        self,
        data: bytes,
        source_hint: str,
        *,
        cache_key: str | None = None,
    ) -> tuple[str, str | None, str, int, bool] | None:
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

            if fmt in PASSTHROUGH_FORMATS and not is_animated:
                return None

            target_path = None
            if self.cache_enabled and cache_key:
                target_path = str(self._cache_file_path(cache_key))

            if is_animated and self.animated_expand:
                frames = self._extract_animation_frames(img)
                sampled = self._sample_frames(frames)
                out_path = self._save_contact_sheet(
                    sampled,
                    fmt=fmt,
                    target_path=target_path,
                )
                total = len(frames)
                used = len(sampled)
                note = (
                    f"该图原为动画({fmt})，共 {total} 帧；"
                    f"已展开为按时间顺序从左到右、从上到下排列的 {used} 帧静态拼贴图。"
                    "请结合整张拼贴理解动态过程，不要只看其中一格。"
                )
                if total > used:
                    note += f"为控制体积，已从 {total} 帧中均匀采样 {used} 帧。"
                return out_path, note, fmt, total, False

            single = self._prepare_static_frame(img)
            out_path = self._save_png(single, target_path=target_path)
            single.close()
            return out_path, None, fmt, 1, False

    def _extract_animation_frames(
        self,
        img: PILImage.Image,
    ) -> list[tuple[PILImage.Image, int]]:
        n_frames = int(getattr(img, "n_frames", 1) or 1)
        size = img.size
        frames: list[tuple[PILImage.Image, int]] = []

        canvas = PILImage.new("RGBA", size, (0, 0, 0, 0))
        previous = canvas.copy()

        for index in range(n_frames):
            img.seek(index)
            duration = int(img.info.get("duration", 0) or 0)
            dispose = int(getattr(img, "disposal_method", 0) or 0)

            frame_rgba = img.convert("RGBA")
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
            return [
                (frame, duration, idx + 1)
                for idx, (frame, duration) in enumerate(frames)
            ]

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

        sampled: list[tuple[PILImage.Image, int, int]] = []
        for idx in indices:
            frame, duration = frames[idx]
            sampled.append((frame, duration, idx + 1))
        return sampled

    def _prepare_static_frame(self, img: PILImage.Image) -> PILImage.Image:
        try:
            img.seek(0)
        except Exception:
            pass
        if self.keep_alpha:
            if img.mode in ("RGBA", "LA"):
                return img.convert("RGBA")
            if img.mode == "P":
                return img.convert("RGBA")
            if img.mode == "RGB":
                return img.copy()
            return img.convert("RGBA")

        if img.mode in ("RGBA", "LA") or (
            img.mode == "P" and "transparency" in img.info
        ):
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
            title = (
                f"Animation frames ({fmt}) · {len(cells)} shown · "
                "left→right, top→bottom"
            )
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
                draw.rectangle(
                    (x, ly, x + cell_w, ly + label_h - 2),
                    fill=self.label_bg,
                )
                draw.text(
                    (x + 4, ly + 2),
                    label,
                    fill=self.label_fg,
                    font=small_font,
                )

        out = self._save_png(sheet, target_path=target_path)
        sheet.close()
        for cell, _, _ in cells:
            cell.close()
        return out

    def _save_png(
        self,
        image: PILImage.Image,
        *,
        target_path: str | None = None,
    ) -> str:
        if target_path:
            out_path = Path(target_path)
            out_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            # Non-cache fallback temp file
            tmp_dir = self.cache_dir / "tmp"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            out_path = tmp_dir / f"img2png_{uuid.uuid4().hex}.png"

        to_save = image
        bg_created = False
        if not self.keep_alpha and image.mode == "RGBA":
            bg = PILImage.new("RGB", image.size, (255, 255, 255))
            bg.paste(image, mask=image.split()[-1])
            to_save = bg
            bg_created = True
        elif image.mode not in ("RGB", "RGBA"):
            to_save = image.convert("RGBA")
            bg_created = True

        try:
            # Atomic write
            tmp = out_path.with_suffix(out_path.suffix + ".tmp")
            to_save.save(tmp, format="PNG", optimize=True)
            tmp.replace(out_path)
        finally:
            if bg_created or to_save is not image:
                to_save.close()
        return str(out_path.resolve())

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
