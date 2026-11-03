"""cng_task_04 acceptance: structured latency + aspect-ratio logging in the OCR preprocessor.

Accepts structlog events or stdlib logging records (message or `extra` fields). Requires one
event that mentions latency and aspect ratio, and reports the real aspect ratio of the input.
"""

import io
import logging
import re

from PIL import Image
from structlog.testing import capture_logs

from app.adapters.ocr.preprocessor import ImagePreprocessor

WIDTH, HEIGHT = 300, 100  # aspect ratio 3.0 (or 0.333 if reported as h/w)


def _image_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (WIDTH, HEIGHT), color=(255, 255, 255)).save(buf, format="PNG")
    return buf.getvalue()


def _events(caplog) -> list[str]:
    caplog.set_level(logging.DEBUG)
    with capture_logs() as structured:
        ImagePreprocessor().create_ocr_variants(_image_bytes())
    texts = [" ".join(f"{k}={v}" for k, v in ev.items()) for ev in structured]
    for rec in caplog.records:
        extras = {k: v for k, v in rec.__dict__.items() if k not in logging.LogRecord("", 0, "", 0, "", None, None).__dict__}
        texts.append(rec.getMessage() + " " + " ".join(f"{k}={v}" for k, v in extras.items()))
    return texts


def _mentions_ratio(text: str) -> bool:
    for num in re.findall(r"\d+\.\d+|\d+", text):
        value = float(num)
        if abs(value - 3.0) < 0.05 or abs(value - 1 / 3) < 0.01:
            return True
    return False


def test_logs_latency_and_aspect_ratio(caplog) -> None:
    texts = _events(caplog)
    matching = [t for t in texts if "latency" in t.lower() and "aspect" in t.lower()]
    assert matching, f"no log event mentions both latency and aspect ratio; saw: {texts[:10]}"
    assert any(_mentions_ratio(t) for t in matching), f"aspect ratio of a 300x100 image not reported: {matching}"


def test_variants_still_produced() -> None:
    variants = ImagePreprocessor().create_ocr_variants(_image_bytes())
    assert len(variants) >= 1
