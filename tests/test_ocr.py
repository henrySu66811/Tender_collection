import unittest
from types import SimpleNamespace

from tender_app.ocr import needs_image_ocr, text_from_result


class OcrTests(unittest.TestCase):
    def test_only_short_image_content_needs_ocr(self):
        self.assertTrue(needs_image_ocr("查看下图", 1))
        self.assertFalse(needs_image_ocr("查看下图", 0))
        self.assertFalse(needs_image_ocr("有效正文" * 30, 1))

    def test_low_confidence_lines_are_removed(self):
        result = SimpleNamespace(
            txts=["采购人：某公司", "错误噪声", "预算：100万元"],
            scores=[0.98, 0.2, 0.8],
        )
        text, confidence = text_from_result(result)
        self.assertEqual(text, "采购人：某公司\n预算：100万元")
        self.assertAlmostEqual(confidence, 0.89)


if __name__ == "__main__":
    unittest.main()
