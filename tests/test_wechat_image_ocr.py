import unittest

from wechat_image_ocr import MAX_IMAGES, is_allowed_image_url, ocr_wechat_images


class WeChatImageOCRTests(unittest.TestCase):
    def test_allows_only_official_https_wechat_image_hosts(self):
        self.assertTrue(is_allowed_image_url("https://mmbiz.qpic.cn/mmbiz_png/example/0"))
        self.assertFalse(is_allowed_image_url("http://mmbiz.qpic.cn/mmbiz_png/example/0"))
        self.assertFalse(is_allowed_image_url("https://example.com/internal.png"))

    def test_empty_or_disallowed_images_do_not_run_ocr(self):
        result = ocr_wechat_images(["https://example.com/nope.png"])
        self.assertEqual(result.text, "")
        self.assertEqual(result.attempted, 0)

    def test_batch_limit_covers_the_full_saved_image_set(self):
        self.assertGreaterEqual(MAX_IMAGES, 20)
