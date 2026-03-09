"""
Archive operations — bundling multiple files into a ZIP.
"""

from __future__ import annotations

import logging
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)


def images_to_zip(image_paths: list[Path], output_path: Path) -> int:
    """
    Bundle a list of image files into a single ZIP archive.

    Returns the number of files added.
    """
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for img_path in image_paths:
            if img_path.exists():
                zf.write(img_path, arcname=img_path.name)
            else:
                logger.warning("File not found, skipping: %s", img_path)

    count = len(image_paths)
    logger.debug("ZIP created: %s (%d file(s))", output_path.name, count)
    return count


def create_zip_from_dir(source_dir: Path, output_path: Path) -> int:
    """Recursively zip an entire directory."""
    count = 0
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in sorted(source_dir.rglob("*")):
            if file.is_file():
                zf.write(file, arcname=file.relative_to(source_dir))
                count += 1
    logger.debug("Dir→ZIP: %s (%d file(s))", output_path.name, count)
    return count
