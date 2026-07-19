from __future__ import annotations

import asyncio
import base64
import io
import math
import os
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import Image, Reply
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
from astrbot.core.utils.io import download_image_by_url
from PIL import Image as PILImage
from PIL import ImageDraw, ImageFont, ImageSequence
from PIL import UnidentifiedImageError

PASSTHROUGH_FORMATS = {"JPEG", "PNG"}


@register(
    "astrbot_plugin_image_to_png",
    "Xiawan",
    "将非 PNG/JPEG 图片统一转为 PNG；动图会展开为逐帧拼贴静态图后再交给大模型。",
    "1.1.0",
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
        self.pad_color = (245, 245, 245, 255)
        self.label_bg = (0, 0, 0, 160)
        self.label_fg = (255, 255, 255, 255)
        self._temp_dir = Path(get_astrbot_temp_path()) / "image_to_png"
        self._temp_dir.mkdir(parents=True, exist_ok=True)

    def _cfg(self, key: str, default: Any) -> Any:
        try:
            value = self.config.get(key, default)  # type: ignore[attr-defined]
        except AttributeError:
            try:
                value = self.config[key]  # type: ignore[index]
            except Exception:
                value = default
        return default if value is None else value

    async def initialize(self) -> None:
        self._temp_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            "[图片转PNG] 插件已初始化。enabled=%s animated_expand=%s max_frames=%s",
            self.enabled,
            self.animated_expand,
            self.max_frames,
        )

    async def terminate(self) -> None:
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
                new_ref, note = await self.ensure_allowed_image_ref(ref, with_note=True)
                if new_ref != ref:
                    changed = True
                    if self._is_local_path(new_ref):
                        event.track_temporary_local_file(new_ref)
                if note:
                    notes.append(note)
                converted.append(new_ref)
            if changed:
                req.image_urls = converted
                logger.info("[图片转PNG] 已处理请求图片 %d 张", len(converted))
            if notes:
                # Help the model understand this is an animation contact sheet.
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

        new_path, _note = await self.ensure_allowed_image_ref(
            source_path,
            with_note=True,
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
        event.track_temporary_local_file(abs_path)
        logger.info("[图片转PNG] 消息图片已转为 PNG: %s", abs_path)

    async def ensure_allowed_image_ref(
        self,
        image_ref: str,
        *,
        with_note: bool = False,
    ) -> str | tuple[str, str | None]:
        if not image_ref:
            return (image_ref, None) if with_note else image_ref

        data, source_hint = await self._load_image_bytes(image_ref)
        if data is None:
            return (image_ref, None) if with_note else image_ref

        try:
            result = await asyncio.to_thread(
                self._convert_bytes_to_png_with_meta,
                data,
                source_hint,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "[图片转PNG] 转换失败(%s): %s",
                source_hint,
                exc,
                exc_info=True,
            )
            return (image_ref, None) if with_note else image_ref

        if result is None:
            return (image_ref, None) if with_note else image_ref

        png_path, note, fmt, frame_count = result
        if frame_count > 1:
            logger.info(
                "[图片转PNG] %s 动图(%d帧) -> 逐帧拼贴 PNG (%s)",
                fmt,
                frame_count,
                source_hint,
            )
        else:
            logger.info("[图片转PNG] %s -> PNG (%s)", fmt, source_hint)
        return (png_path, note) if with_note else png_path

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
    ) -> tuple[str, str | None, str, int] | None:
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

            # PNG/JPEG static images pass through without rewriting.
            if fmt in PASSTHROUGH_FORMATS and not is_animated:
                return None

            if is_animated and self.animated_expand:
                frames = self._extract_animation_frames(img)
                sampled = self._sample_frames(frames)
                out_path = self._save_contact_sheet(sampled, fmt=fmt)
                total = len(frames)
                used = len(sampled)
                note = (
                    f"该图原为动画({fmt})，共 {total} 帧；"
                    f"已展开为按时间顺序从左到右、从上到下排列的 {used} 帧静态拼贴图。"
                    "请结合整张拼贴理解动态过程，不要只看其中一格。"
                )
                if total > used:
                    note += f"为控制体积，已从 {total} 帧中均匀采样 {used} 帧。"
                return out_path, note, fmt, total

            # Static non-PNG/JPEG, or animated expand disabled.
            single = self._prepare_static_frame(img)
            out_path = self._save_png(single)
            single.close()
            return out_path, None, fmt, 1

    def _extract_animation_frames(
        self,
        img: PILImage.Image,
    ) -> list[tuple[PILImage.Image, int]]:
        """Extract composited RGBA frames and durations (ms)."""
        n_frames = int(getattr(img, "n_frames", 1) or 1)
        size = img.size
        frames: list[tuple[PILImage.Image, int]] = []

        # Prefer robust sequential compositing for GIF disposal.
        canvas = PILImage.new("RGBA", size, (0, 0, 0, 0))
        previous = canvas.copy()

        for index in range(n_frames):
            img.seek(index)
            duration = int(img.info.get("duration", 0) or 0)
            dispose = int(getattr(img, "disposal_method", 0) or 0)

            frame_rgba = img.convert("RGBA")
            # Some GIFs encode per-frame offsets via tile; convert() usually handles it.
            composed = canvas.copy()
            composed.alpha_composite(frame_rgba)
            frames.append((composed.copy(), duration))

            if dispose == 2:
                # Restore to background (transparent).
                canvas = PILImage.new("RGBA", size, (0, 0, 0, 0))
            elif dispose == 3:
                # Restore to previous.
                canvas = previous.copy()
            else:
                # Leave as-is for next frame.
                previous = composed.copy()
                canvas = composed

            frame_rgba.close()

        if not frames:
            # Fallback: first frame only.
            img.seek(0)
            frames = [(img.convert("RGBA"), int(img.info.get("duration", 0) or 0))]
        return frames

    def _sample_frames(
        self,
        frames: list[tuple[PILImage.Image, int]],
    ) -> list[tuple[PILImage.Image, int, int]]:
        """Return (frame, duration, original_index_1based), sampling if needed."""
        total = len(frames)
        if total <= self.max_frames:
            return [
                (frame, duration, idx + 1)
                for idx, (frame, duration) in enumerate(frames)
            ]

        # Evenly sample max_frames indices, always include first and last.
        if self.max_frames == 1:
            indices = [0]
        else:
            indices = [
                round(i * (total - 1) / (self.max_frames - 1))
                for i in range(self.max_frames)
            ]
            # de-dup while preserving order
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
    ) -> str:
        if not frames:
            raise ValueError("no frames to compose")

        # Resize cells.
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

            # Center cell in its grid slot.
            ox = x + (cell_w - cell.width) // 2
            oy = y + (cell_h - cell.height) // 2
            # White/checker-like backing for transparent stickers.
            backing = PILImage.new("RGBA", cell.size, (255, 255, 255, 255))
            backing.alpha_composite(cell)
            sheet.paste(backing, (ox, oy))
            backing.close()

            if self.show_frame_labels:
                label = f"#{original_idx}"
                if duration > 0:
                    label += f" {duration}ms"
                # Label bar under the cell
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

        out = self._save_png(sheet)
        sheet.close()
        for cell, _, _ in cells:
            cell.close()
        return out

    def _save_png(self, image: PILImage.Image) -> str:
        self._temp_dir.mkdir(parents=True, exist_ok=True)
        out_path = self._temp_dir / f"img2png_{uuid.uuid4().hex}.png"
        to_save = image
        if not self.keep_alpha and image.mode == "RGBA":
            bg = PILImage.new("RGB", image.size, (255, 255, 255))
            bg.paste(image, mask=image.split()[-1])
            to_save = bg
            bg_created = True
        else:
            bg_created = False
            if image.mode not in ("RGB", "RGBA"):
                to_save = image.convert("RGBA")

        try:
            to_save.save(out_path, format="PNG", optimize=True)
        finally:
            if bg_created or to_save is not image:
                to_save.close()
        return str(out_path.resolve())

    @staticmethod
    def _get_font(size: int):
        # Prefer common CJK-capable fonts when available; fall back to default.
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
