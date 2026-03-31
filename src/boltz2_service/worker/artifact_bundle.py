from __future__ import annotations

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


def bundle_output(output_dir: Path, destination_zip: Path) -> Path:
    """Create a ZIP archive of all files under *output_dir*."""
    destination_zip.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(destination_zip, "w", compression=ZIP_DEFLATED) as handle:
        for path in sorted(output_dir.rglob("*")):
            if path.is_file():
                handle.write(path, arcname=path.relative_to(output_dir))
    return destination_zip
