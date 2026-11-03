"""cng_task_05 acceptance: thresholding parameters moved into a typed Pydantic config.

Accepts a pydantic BaseModel or a pydantic dataclass defined in the preprocessor module, whose
defaults keep today's adaptive-threshold values (block size 11, constant 2), and requires the
preprocessor to keep producing the same set of variants with default settings.
"""

import importlib
import io
import inspect

from PIL import Image

MODULE = "app.adapters.ocr.preprocessor"


def _pydantic_configs():
    mod = importlib.import_module(MODULE)
    found = []
    for _, cls in inspect.getmembers(mod, inspect.isclass):
        if cls.__module__ == mod.__name__ and hasattr(cls, "__pydantic_fields__"):
            found.append(cls)
    return found


def _defaults(cls) -> list:
    return [f.default for f in cls.__pydantic_fields__.values()]


def test_threshold_config_model_exists_with_current_defaults() -> None:
    configs = _pydantic_configs()
    assert configs, f"no pydantic model or dataclass defined in {MODULE}"
    assert any(11 in _defaults(c) and 2 in _defaults(c) for c in configs), (
        "no config keeps the adaptive threshold defaults (block size 11, C 2): "
        + ", ".join(f"{c.__name__}={_defaults(c)}" for c in configs)
    )


def test_default_behaviour_preserved() -> None:
    from app.adapters.ocr.preprocessor import ImagePreprocessor

    buf = io.BytesIO()
    Image.new("RGB", (300, 100), color=(255, 255, 255)).save(buf, format="PNG")
    names = [name for name, _ in ImagePreprocessor().create_ocr_variants(buf.getvalue())]
    assert names == ["autocontrast_sharpened", "clahe_contrast", "adaptive_threshold"]
