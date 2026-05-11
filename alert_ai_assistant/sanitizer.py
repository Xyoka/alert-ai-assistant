from __future__ import annotations

import re
from collections.abc import Iterable


RESPONSIBLE_LINE_RE = re.compile(r"负责人[：:][^\n\r]*")
ACCOUNT_NAME_RE = re.compile(r"[\u4e00-\u9fff]{2,4}\([A-Za-z0-9_.-]+\)")


def sanitize_text(text: str, mask_names: Iterable[str] | None = None) -> str:
    result = text or ""
    result = RESPONSIBLE_LINE_RE.sub("负责人：<已脱敏>", result)
    result = ACCOUNT_NAME_RE.sub("<负责人>", result)
    for name in mask_names or []:
        name = str(name).strip()
        if name:
            result = result.replace(name, "<已脱敏>")
    return result

