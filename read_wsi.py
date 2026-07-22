#!/usr/bin/env python3
"""Auto-detect a WSI format, choose a reader, and save metadata + thumbnail.

Reader priority:
  - CZI: aicspylibczi
  - SVS / NDPI / pyramidal TIFF and OpenSlide-readable slides: openslide
  - ordinary TIFF fallback: tifffile

Examples:
  python wsi_thumbnail.py --slide slide.svs
  python wsi_thumbnail.py --slide slide.czi --max-size 1200
  python wsi_thumbnail.py --slide slide.tif --reader tifffile
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageEnhance


DEFAULT_SLIDE_DIR = Path(__file__).resolve().parent
OPENSLIDE_EXTS = {".svs", ".ndpi", ".mrxs", ".scn", ".vms", ".vmu", ".bif", ".tif", ".tiff"}
TIFF_EXTS = {".tif", ".tiff"}
CZI_EXTS = {".czi"}


def _is_probably_truncated_error(exc: Exception, slide_path: Path) -> str | None:
    msg = str(exc)
    match = re.search(r"offset\s+(\d+).*actually got 0 bytes", msg)
    if not match:
        return None

    offset = int(match.group(1))
    file_size = slide_path.stat().st_size
    if offset <= file_size:
        return None

    return (
        f"file may be incomplete/truncated: reader requested byte offset {offset}, "
        f"but file size is only {file_size} bytes ({file_size / 1024**3:.3f} GB)"
    )


def _json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _normalize_to_uint8(arr: np.ndarray, color_order: str = "RGB") -> np.ndarray:
    arr = np.asarray(arr)
    arr = np.squeeze(arr)

    if arr.ndim == 2:
        arr = np.stack([arr, arr, arr], axis=-1)
    elif arr.ndim > 3:
        shape = arr.shape
        color_axes = [i for i, n in enumerate(shape) if n in (3, 4)]
        color_axis = color_axes[-1] if color_axes else None
        if color_axis is not None:
            arr = np.moveaxis(arr, color_axis, -1)
            while arr.ndim > 3:
                drop_axis = min(range(arr.ndim - 1), key=lambda i: arr.shape[i])
                arr = np.take(arr, 0, axis=drop_axis)
        else:
            while arr.ndim > 2:
                drop_axis = min(range(arr.ndim), key=lambda i: arr.shape[i])
                arr = np.take(arr, 0, axis=drop_axis)
            arr = np.stack([arr, arr, arr], axis=-1)

    if arr.ndim != 3:
        raise ValueError(f"Cannot convert array with shape {arr.shape} to RGB")

    if arr.shape[-1] > 4:
        arr = arr[..., :3]
    if arr.shape[-1] == 1:
        arr = np.repeat(arr, 3, axis=-1)
    if arr.shape[-1] == 4:
        arr = arr[..., :3]

    if arr.dtype != np.uint8:
        arr = arr.astype(np.float32, copy=False)
        finite = np.isfinite(arr)
        if not finite.any():
            return np.zeros((*arr.shape[:2], 3), dtype=np.uint8)

        lo, hi = np.percentile(arr[finite], [0.5, 99.5])
        if hi <= lo:
            lo, hi = float(arr[finite].min()), float(arr[finite].max())
        if hi <= lo:
            return np.zeros_like(arr, dtype=np.uint8)

        arr = (arr - lo) / (hi - lo)
        arr = np.clip(arr, 0, 1)
        arr = (arr * 255).astype(np.uint8)

    if color_order.upper() == "BGR" and arr.shape[-1] >= 3:
        arr = arr[..., [2, 1, 0]]
    return np.ascontiguousarray(arr)


def _resize_to_max(img: Image.Image, max_size: int) -> Image.Image:
    width, height = img.size
    scale = min(max_size / max(width, height), 1.0)
    if scale >= 1:
        return img
    new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
    return img.resize(new_size, Image.Resampling.LANCZOS)


def _parse_rgb(text: str) -> tuple[int, int, int]:
    presets = {
        "white": (255, 255, 255),
        "black": (0, 0, 0),
        "gray": (230, 230, 230),
        "grey": (230, 230, 230),
    }
    lowered = text.lower()
    if lowered in presets:
        return presets[lowered]

    parts = text.split(",")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("Use white, black, gray, or R,G,B, for example 255,255,255")
    try:
        rgb = tuple(int(p) for p in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("RGB values must be integers") from exc
    if any(v < 0 or v > 255 for v in rgb):
        raise argparse.ArgumentTypeError("RGB values must be between 0 and 255")
    return rgb


def _adjust_color(img: Image.Image, brightness: float, contrast: float, saturation: float, gamma: float) -> Image.Image:
    if brightness != 1.0:
        img = ImageEnhance.Brightness(img).enhance(brightness)
    if contrast != 1.0:
        img = ImageEnhance.Contrast(img).enhance(contrast)
    if saturation != 1.0:
        img = ImageEnhance.Color(img).enhance(saturation)
    if gamma != 1.0:
        arr = np.asarray(img).astype(np.float32) / 255.0
        arr = np.clip(arr, 0, 1) ** (1.0 / gamma)
        img = Image.fromarray(np.ascontiguousarray((arr * 255).astype(np.uint8)))
    return img


def _auto_white_balance(
    img: Image.Image,
    threshold: int,
    max_value: int,
    tolerance: int,
    target: int,
) -> tuple[Image.Image, dict[str, Any]]:
    arr = np.asarray(img.convert("RGB")).astype(np.float32)
    max_channel = arr.max(axis=2)
    min_channel = arr.min(axis=2)
    low_saturation = (max_channel - min_channel) <= tolerance
    gray_background = low_saturation & (min_channel >= threshold) & (max_channel <= max_value)

    if gray_background.mean() < 0.005:
        return img, {
            "enabled": True,
            "applied": False,
            "reason": "not enough gray/white reference pixels",
            "reference_pixel_fraction": round(float(gray_background.mean()), 4),
        }

    reference = np.median(arr[gray_background], axis=0)
    reference = np.maximum(reference, 1.0)
    gains = np.clip(target / reference, 0.75, 1.8)
    corrected = np.clip(arr * gains, 0, 255).astype(np.uint8)
    return Image.fromarray(np.ascontiguousarray(corrected)), {
        "enabled": True,
        "applied": True,
        "threshold": threshold,
        "max_value": max_value,
        "tolerance": tolerance,
        "target": target,
        "reference_rgb": [round(float(v), 2) for v in reference],
        "gains_rgb": [round(float(v), 4) for v in gains],
        "reference_pixel_fraction": round(float(gray_background.mean()), 4),
    }


def _find_first_text(root: Any, tag_name: str) -> str | None:
    if root is None:
        return None
    for elem in root.iter():
        if elem.tag.split("}")[-1] == tag_name:
            text = (elem.text or "").strip()
            if text:
                return text
    return None


def _find_all_text(root: Any, tag_name: str) -> list[str]:
    if root is None:
        return []
    values = []
    for elem in root.iter():
        if elem.tag.split("}")[-1] == tag_name:
            text = (elem.text or "").strip()
            if text:
                values.append(text)
    return values


def _largest_numeric_text(values: list[str]) -> str | None:
    numeric_values = []
    for value in values:
        try:
            numeric_values.append(float(value))
        except ValueError:
            continue
    if not numeric_values:
        return None
    value = max(numeric_values)
    return str(int(value)) if value.is_integer() else str(value)


def _find_scaling_um_per_pixel(root: Any, axis: str) -> float | None:
    if root is None:
        return None
    for elem in root.iter():
        if elem.tag.split("}")[-1] != "Distance" or elem.attrib.get("Id") != axis:
            continue
        for child in elem:
            if child.tag.split("}")[-1] == "Value" and child.text:
                return float(child.text) * 1_000_000.0
    return None


def _czi_metadata(czi: Any, slide_path: Path) -> dict[str, Any]:
    root = czi.meta
    info: dict[str, Any] = {
        "reader": "aicspylibczi",
        "format": "czi",
        "pixel_type": str(getattr(czi, "pixel_type", "")),
        "dims": str(czi.dims),
        "dims_shape": czi.get_dims_shape() if hasattr(czi, "get_dims_shape") else str(czi.size),
        "is_mosaic": bool(czi.is_mosaic()),
    }

    if czi.is_mosaic():
        bbox = czi.get_mosaic_bounding_box()
        scene_boxes = czi.get_all_mosaic_scene_bounding_boxes()
        tile_boxes = czi.get_all_mosaic_tile_bounding_boxes()
        tile_sizes = sorted({(int(box.w), int(box.h)) for box in tile_boxes.values()})
        info.update(
            {
                "scene_count": len(scene_boxes),
                "tile_count": len(tile_boxes),
                "tile_sizes": tile_sizes,
                "level_dimensions": [[int(bbox.w), int(bbox.h)]],
                "mosaic_bbox": {"x": int(bbox.x), "y": int(bbox.y), "width": int(bbox.w), "height": int(bbox.h)},
            }
        )

        pyramid_layers_text = _find_first_text(root, "PyramidLayersCount")
        minification_text = _find_first_text(root, "MinificationFactor")
        pyramid_layers = int(pyramid_layers_text) if pyramid_layers_text else None
        minification = int(minification_text) if minification_text else None
        level_info = []
        if pyramid_layers and minification:
            for level in range(pyramid_layers):
                downsample = minification**level
                level_info.append(
                    {
                        "level": level,
                        "downsample": downsample,
                        "estimated_width": int(np.ceil(bbox.w / downsample)),
                        "estimated_height": int(np.ceil(bbox.h / downsample)),
                    }
                )
            info["level_dimensions"] = [[entry["estimated_width"], entry["estimated_height"]] for entry in level_info]
        info["pyramid_layers_count"] = pyramid_layers
        info["pyramid_minification_factor"] = minification
        info["pyramid_levels"] = level_info

    nominal_magnification_values = sorted(set(_find_all_text(root, "NominalMagnification")))
    total_magnification_values = sorted(set(_find_all_text(root, "TotalMagnification")))
    info.update(
        {
            "objective_name": _find_first_text(root, "ObjectiveName"),
            "nominal_magnification": _largest_numeric_text(nominal_magnification_values),
            "total_magnification": _largest_numeric_text(total_magnification_values),
            "nominal_magnification_values_in_metadata": nominal_magnification_values,
            "total_magnification_values_in_metadata": total_magnification_values,
            "pixel_size_x_um": _find_scaling_um_per_pixel(root, "X"),
            "pixel_size_y_um": _find_scaling_um_per_pixel(root, "Y"),
            "source": str(slide_path),
            "source_size_gb": round(slide_path.stat().st_size / 1024**3, 3),
        }
    )
    return info


def read_with_czi(slide_path: Path, max_size: int, background_color: tuple[int, int, int]) -> tuple[Image.Image, dict[str, Any]]:
    from aicspylibczi import CziFile

    czi = CziFile(str(slide_path))
    metadata = _czi_metadata(czi, slide_path)
    pixel_type = str(getattr(czi, "pixel_type", ""))
    color_order = "BGR" if "bgr" in pixel_type.lower() else "RGB"

    if czi.is_mosaic():
        bbox = czi.get_mosaic_bounding_box()
        downsample = max(1, int(np.ceil(max(bbox.w, bbox.h) / max_size)))
        scale_factor = 1.0 / downsample
        try:
            data = czi.read_mosaic(scale_factor=scale_factor, background_color=background_color, C=0)
        except TypeError:
            data = czi.read_mosaic(scale_factor=scale_factor, background_color=background_color)
        if isinstance(data, tuple):
            data = data[0]
        metadata["thumbnail_read"] = {"method": "read_mosaic", "downsample": downsample, "scale_factor": scale_factor}
    else:
        data = czi.read_image(C=0)
        if isinstance(data, tuple):
            data = data[0]
        metadata["thumbnail_read"] = {"method": "read_image"}

    arr = _normalize_to_uint8(data, color_order=color_order)
    img = _resize_to_max(Image.fromarray(arr), max_size)
    metadata["color_order_converted_to_rgb"] = color_order == "BGR"
    metadata["thumbnail_size"] = list(img.size)
    return img, metadata


def read_with_openslide(slide_path: Path, max_size: int) -> tuple[Image.Image, dict[str, Any]]:
    import openslide

    slide = openslide.OpenSlide(str(slide_path))
    level_dimensions = [list(dim) for dim in slide.level_dimensions]
    level_downsamples = [float(v) for v in slide.level_downsamples]
    properties = dict(slide.properties)

    img = slide.get_thumbnail((max_size, max_size)).convert("RGB")
    metadata = {
        "reader": "openslide",
        "format": properties.get("openslide.vendor") or slide_path.suffix.lower().lstrip("."),
        "source": str(slide_path),
        "source_size_gb": round(slide_path.stat().st_size / 1024**3, 3),
        "level_count": slide.level_count,
        "level_dimensions": level_dimensions,
        "level_downsamples": level_downsamples,
        "dimensions": list(slide.dimensions),
        "objective_power": properties.get("openslide.objective-power"),
        "mpp_x": properties.get("openslide.mpp-x"),
        "mpp_y": properties.get("openslide.mpp-y"),
        "associated_images": list(slide.associated_images.keys()),
        "properties": properties,
        "thumbnail_size": list(img.size),
    }
    slide.close()
    return img, metadata


def _tiff_page_shape(page: Any) -> tuple[int, int] | None:
    shape = getattr(page, "shape", None)
    if not shape or len(shape) < 2:
        return None
    return int(shape[0]), int(shape[1])


def read_with_tifffile(slide_path: Path, max_size: int, max_full_read_pixels: int) -> tuple[Image.Image, dict[str, Any]]:
    import tifffile

    with tifffile.TiffFile(str(slide_path)) as tif:
        series_info = []
        for idx, series in enumerate(tif.series):
            levels = getattr(series, "levels", None) or [series]
            series_info.append(
                {
                    "index": idx,
                    "shape": list(series.shape),
                    "dtype": str(series.dtype),
                    "axes": str(series.axes),
                    "level_count": len(levels),
                    "level_shapes": [list(level.shape) for level in levels],
                }
            )

        candidates = []
        for sidx, series in enumerate(tif.series):
            levels = getattr(series, "levels", None) or [series]
            for lidx, level in enumerate(levels):
                shape = tuple(level.shape)
                if len(shape) >= 2:
                    height, width = int(shape[0]), int(shape[1])
                    candidates.append((max(height, width), height * width, sidx, lidx, level))

        if not candidates:
            page_candidates = []
            for pidx, page in enumerate(tif.pages):
                shape = _tiff_page_shape(page)
                if shape:
                    height, width = shape
                    page_candidates.append((max(height, width), height * width, pidx, page))
            if not page_candidates:
                raise ValueError("No image-like TIFF series or pages found")
            page_candidates.sort(key=lambda x: (abs(x[0] - max_size), x[1]))
            _, pixels, pidx, page = page_candidates[0]
            if pixels > max_full_read_pixels:
                raise MemoryError(
                    f"TIFF has no small pyramid/thumbnail page; refusing to read {pixels} pixels. "
                    f"Increase --max-full-read-pixels if this is expected."
                )
            arr = page.asarray()
            read_info = {"method": "page.asarray", "page_index": pidx}
        else:
            candidates.sort(key=lambda x: (0 if x[0] >= max_size else 1, abs(x[0] - max_size), x[1]))
            _, pixels, sidx, lidx, level = candidates[0]
            if pixels > max_full_read_pixels and len(candidates) == 1:
                raise MemoryError(
                    f"TIFF has no small pyramid/thumbnail level; refusing to read {pixels} pixels. "
                    f"Increase --max-full-read-pixels if this is expected."
                )
            arr = level.asarray()
            read_info = {"method": "series_level.asarray", "series_index": sidx, "level_index": lidx}

        metadata = {
            "reader": "tifffile",
            "format": "tiff",
            "source": str(slide_path),
            "source_size_gb": round(slide_path.stat().st_size / 1024**3, 3),
            "is_bigtiff": bool(tif.is_bigtiff),
            "page_count": len(tif.pages),
            "series_count": len(tif.series),
            "series": series_info,
            "thumbnail_read": read_info,
        }

    img = _resize_to_max(Image.fromarray(_normalize_to_uint8(arr)), max_size)
    metadata["thumbnail_size"] = list(img.size)
    return img, metadata


def choose_reader(slide_path: Path, requested_reader: str) -> list[str]:
    if requested_reader != "auto":
        return [requested_reader]

    suffix = slide_path.suffix.lower()
    if suffix in CZI_EXTS:
        return ["czi"]
    if suffix in {".svs", ".ndpi", ".mrxs", ".scn", ".vms", ".vmu", ".bif"}:
        return ["openslide"]
    if suffix in TIFF_EXTS:
        return ["openslide", "tifffile"]
    return ["openslide", "czi", "tifffile"]


def read_slide(
    slide_path: Path,
    requested_reader: str,
    max_size: int,
    background_color: tuple[int, int, int],
    max_full_read_pixels: int,
) -> tuple[Image.Image, dict[str, Any], list[str]]:
    errors = []
    for reader in choose_reader(slide_path, requested_reader):
        try:
            if reader == "czi":
                img, metadata = read_with_czi(slide_path, max_size, background_color)
            elif reader == "openslide":
                img, metadata = read_with_openslide(slide_path, max_size)
            elif reader == "tifffile":
                img, metadata = read_with_tifffile(slide_path, max_size, max_full_read_pixels)
            else:
                raise ValueError(f"Unknown reader: {reader}")
            metadata["selected_reader"] = reader
            metadata["reader_attempt_errors"] = errors
            return img, metadata, errors
        except ModuleNotFoundError as exc:
            errors.append(f"{reader}: missing package {exc.name}")
        except Exception as exc:
            truncated_hint = _is_probably_truncated_error(exc, slide_path)
            if truncated_hint:
                errors.append(f"{reader}: {type(exc).__name__}: {exc}; Hint: {truncated_hint}")
            else:
                errors.append(f"{reader}: {type(exc).__name__}: {exc}")

    raise RuntimeError("Could not read slide.\nTried:\n  - " + "\n  - ".join(errors))


def _save_outputs(img: Image.Image, slide_path: Path, out_png: Path, out_html: Path, out_json: Path, metadata: dict[str, Any]) -> None:
    out_png.parent.mkdir(parents=True, exist_ok=True)
    out_html.parent.mkdir(parents=True, exist_ok=True)
    out_json.parent.mkdir(parents=True, exist_ok=True)

    img.save(out_png)
    out_json.write_text(json.dumps(_json_safe(metadata), indent=2, ensure_ascii=False), encoding="utf-8")

    rel_png = os.path.relpath(out_png, out_html.parent)
    html_text = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{html.escape(slide_path.name)} thumbnail</title>
  <style>
    body {{
      margin: 0;
      padding: 24px;
      background: #f4f4f2;
      color: #222;
      font-family: Arial, sans-serif;
    }}
    h1 {{
      margin: 0 0 12px;
      font-size: 18px;
      font-weight: 700;
    }}
    .meta {{
      margin-bottom: 16px;
      font-size: 13px;
      line-height: 1.45;
      color: #555;
      white-space: pre-wrap;
    }}
    img {{
      display: block;
      max-width: 100%;
      height: auto;
      border: 1px solid #bbb;
      background: white;
    }}
  </style>
</head>
<body>
  <h1>{html.escape(slide_path.name)}</h1>
  <div class="meta">{html.escape(json.dumps(_json_safe(metadata), indent=2, ensure_ascii=False))}</div>
  <img src="{html.escape(rel_png)}" alt="WSI thumbnail">
</body>
</html>
"""
    out_html.write_text(html_text, encoding="utf-8")


def create_wsi_preview(
    slide_path: str | Path,
    output_dir: str | Path,
    *,
    max_size: int = 1200,
    reader: str = "auto",
    white_balance: str = "czi",
    background: tuple[int, int, int] = (255, 255, 255),
    brightness: float = 1.0,
    contrast: float = 1.0,
    saturation: float = 1.0,
    gamma: float = 1.0,
    max_full_read_pixels: int = 80_000_000,
) -> dict[str, Any]:
    """Create thumbnail, HTML preview, and metadata JSON for one WSI."""

    slide_path = Path(slide_path).resolve()
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    img, metadata, _ = read_slide(
        slide_path,
        reader,
        max_size,
        background,
        max_full_read_pixels,
    )

    should_white_balance = white_balance == "auto" or (
        white_balance == "czi" and metadata.get("selected_reader") == "czi"
    )
    if should_white_balance:
        img, white_balance_meta = _auto_white_balance(
            img,
            threshold=120,
            max_value=230,
            tolerance=30,
            target=245,
        )
    else:
        white_balance_meta = {"enabled": False, "applied": False, "mode": white_balance}

    img = _adjust_color(img, brightness, contrast, saturation, gamma)
    metadata["thumbnail_size_after_display_adjustment"] = list(img.size)
    metadata["white_balance"] = white_balance_meta
    metadata["display_adjustment"] = {
        "brightness": brightness,
        "contrast": contrast,
        "saturation": saturation,
        "gamma": gamma,
    }

    out_prefix = output_dir / f"{slide_path.stem}_wsi"
    out_png = out_prefix.with_suffix(".png")
    out_html = out_prefix.with_suffix(".html")
    out_json = out_prefix.with_suffix(".metadata.json")

    _save_outputs(img, slide_path, out_png, out_html, out_json, metadata)
    return {
        "slide_path": str(slide_path),
        "thumbnail_path": str(out_png),
        "html_path": str(out_html),
        "metadata_path": str(out_json),
        "metadata": _json_safe(metadata),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Auto-detect WSI format, choose a reader, and save metadata + thumbnail.")
    parser.add_argument("--slide", required=True, type=Path, help="Input WSI path: svs, ndpi, tif/tiff, czi, etc.")
    parser.add_argument("--reader", choices=("auto", "openslide", "tifffile", "czi"), default="auto", help="Force a reader or use auto detection")
    parser.add_argument("--max-size", type=int, default=1200, help="Maximum width or height of output thumbnail")
    parser.add_argument("--background", type=_parse_rgb, default=(255, 255, 255), help="CZI mosaic background: white, black, gray, or R,G,B")
    parser.add_argument(
        "--white-balance",
        choices=("czi", "auto", "off"),
        default="czi",
        help="Display white-balance: czi applies it only to CZI, auto applies it to every reader, off disables it",
    )
    parser.add_argument("--white-balance-threshold", type=int, default=120)
    parser.add_argument("--white-balance-max", type=int, default=230)
    parser.add_argument("--white-balance-tolerance", type=int, default=30)
    parser.add_argument("--white-balance-target", type=int, default=245)
    parser.add_argument("--brightness", type=float, default=1.0)
    parser.add_argument("--contrast", type=float, default=1.0)
    parser.add_argument("--saturation", type=float, default=1.0)
    parser.add_argument("--gamma", type=float, default=1.0)
    parser.add_argument("--max-full-read-pixels", type=int, default=80_000_000, help="Safety limit for non-pyramidal TIFF fallback")
    parser.add_argument("--out-prefix", type=Path, default=None, help="Output prefix; default is input path without suffix + _wsi")
    parser.add_argument("--out-png", type=Path, default=None)
    parser.add_argument("--out-html", type=Path, default=None)
    parser.add_argument("--out-json", type=Path, default=None)
    parser.add_argument("--metadata-only", action="store_true", help="Print/save metadata without saving PNG/HTML")
    args = parser.parse_args()

    slide_path = args.slide.resolve()
    if not slide_path.exists():
        raise FileNotFoundError(slide_path)

    img, metadata, _ = read_slide(slide_path, args.reader, args.max_size, args.background, args.max_full_read_pixels)

    should_white_balance = args.white_balance == "auto" or (
        args.white_balance == "czi" and metadata.get("selected_reader") == "czi"
    )
    if should_white_balance:
        img, white_balance_meta = _auto_white_balance(
            img,
            args.white_balance_threshold,
            args.white_balance_max,
            args.white_balance_tolerance,
            args.white_balance_target,
        )
    else:
        white_balance_meta = {"enabled": False, "applied": False, "mode": args.white_balance}
    img = _adjust_color(img, args.brightness, args.contrast, args.saturation, args.gamma)

    metadata["thumbnail_size_after_display_adjustment"] = list(img.size)
    metadata["white_balance"] = white_balance_meta
    metadata["display_adjustment"] = {
        "brightness": args.brightness,
        "contrast": args.contrast,
        "saturation": args.saturation,
        "gamma": args.gamma,
    }

    out_prefix = args.out_prefix or slide_path.with_name(slide_path.stem + "_wsi")
    out_png = args.out_png or out_prefix.with_suffix(".png")
    out_html = args.out_html or out_prefix.with_suffix(".html")
    out_json = args.out_json or out_prefix.with_suffix(".metadata.json")

    if args.metadata_only:
        out_json.write_text(json.dumps(_json_safe(metadata), indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(_json_safe(metadata), indent=2, ensure_ascii=False))
        print(f"Saved metadata: {out_json}")
        return 0

    _save_outputs(img, slide_path, out_png, out_html, out_json, metadata)
    print(f"Selected reader: {metadata['selected_reader']}")
    print(f"Saved PNG:      {out_png}")
    print(f"Saved HTML:     {out_html}")
    print(f"Saved metadata: {out_json}")
    print(json.dumps(_json_safe(metadata), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(2)
