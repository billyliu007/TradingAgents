from __future__ import annotations

import io
import zipfile
from pathlib import Path

from fastapi.responses import StreamingResponse


def pdf_zip_response(paths: list[Path], attachment_filename: str) -> StreamingResponse:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in paths:
            zf.write(p, arcname=p.name)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{attachment_filename}"'},
    )
