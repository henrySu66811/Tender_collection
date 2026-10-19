"""本地图片文字识别；OCR引擎延迟加载，普通文字公告不承担启动开销。"""
import re
from pathlib import Path


_ENGINE = None


def needs_image_ocr(text: str, image_count: int, minimum_text: int = 80) -> bool:
    """正文有效字符过少且包含图片时才启用OCR。"""
    compact = re.sub(r"\s+", "", text or "")
    return image_count > 0 and len(compact) < minimum_text


def text_from_result(result, minimum_score: float = 0.45) -> tuple[str, float]:
    """从RapidOCR 3.x结果中提取达到置信度阈值的文字。"""
    texts = list(getattr(result, "txts", None) or [])
    scores = list(getattr(result, "scores", None) or [])
    accepted = []
    accepted_scores = []
    for index, text in enumerate(texts):
        score = float(scores[index]) if index < len(scores) else 1.0
        value = str(text).strip()
        if value and score >= minimum_score:
            accepted.append(value)
            accepted_scores.append(score)
    confidence = (sum(accepted_scores) / len(accepted_scores)
                  if accepted_scores else 0.0)
    return "\n".join(accepted), confidence


def recognize_image(path: str | Path) -> tuple[str, float]:
    """使用本地RapidOCR识别图片，不调用远程OCR服务。"""
    global _ENGINE
    try:
        from rapidocr import RapidOCR
    except ImportError as exc:
        raise RuntimeError(
            "图片正文需要OCR，请先执行："
            ".\\.venv\\Scripts\\python.exe -m pip install -r requirements.txt"
        ) from exc
    if _ENGINE is None:
        _ENGINE = RapidOCR()
    result = _ENGINE(str(path))
    return text_from_result(result)
