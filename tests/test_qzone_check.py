# -*- coding: utf-8 -*-
"""
tools/qzone_check.py 的离线单元测试（不联网）。

运行：
    python3 -m unittest discover -s tests -v
或：
    python3 tests/test_qzone_check.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))

from qzone_check import (  # noqa: E402
    QzoneClient,
    calc_gtk,
    parse_cookie,
    unescape_qzone_html,
)


class TestCalcGtk(unittest.TestCase):
    """g_tk 算法必须和 QZone 前端一致，否则所有写接口都会失败"""

    def test_known_vectors(self):
        self.assertEqual(calc_gtk("TESTp_skey123"), "1922077510")
        self.assertEqual(calc_gtk("@FAKEskey_TEST"), "982067927")

    def test_empty(self):
        # 空字符串时只有初始值 5381
        self.assertEqual(calc_gtk(""), "5381")

    def test_result_is_positive_int_string(self):
        for skey in ("a", "abc", "\u4e2d\u6587", "x" * 500):
            v = calc_gtk(skey)
            self.assertTrue(v.isdigit())
            self.assertLess(int(v), 0x80000000)


class TestParseCookie(unittest.TestCase):
    def test_plain(self):
        jar = parse_cookie("uin=o123456; skey=abc; p_skey=def")
        self.assertEqual(jar["uin"], "o123456")
        self.assertEqual(jar["p_skey"], "def")

    def test_with_cookie_prefix(self):
        jar = parse_cookie("Cookie: uin=o123456; p_skey=def")
        self.assertEqual(jar["uin"], "o123456")
        self.assertEqual(jar["p_skey"], "def")

    def test_multiline(self):
        jar = parse_cookie("uin=o1;\np_skey=abc;\nskey=xyz")
        self.assertEqual(jar["p_skey"], "abc")
        self.assertEqual(jar["skey"], "xyz")

    def test_empty(self):
        self.assertEqual(parse_cookie(""), {})
        self.assertEqual(parse_cookie(None), {})


class TestUnescape(unittest.TestCase):
    def test_unescape_like_button(self):
        raw = (
            'class=\\x22item qz_like_btn_v3 \\x22 data-islike=\\x220\\x22 '
            'data-unikey=\\x22http:\\/\\/user.qzone.qq.com\\/123\\/mood\\/abc\\x22'
        )
        out = unescape_qzone_html(raw)
        self.assertIn('data-islike="0"', out)
        self.assertIn("http://user.qzone.qq.com/123/mood/abc", out)


class TestParseLikeButtons(unittest.TestCase):
    """这是本项目的核心判据：从动态流 HTML 里读真实点赞状态"""

    SAMPLE = (
        '<a class="item qz_like_btn_v3 " data-islike="0" data-likecnt="0" '
        'data-showcount="1" data-unikey="http://user.qzone.qq.com/111/mood/aaa111" '
        'data-curkey="http://user.qzone.qq.com/111/mood/aaa111">'
        '<a class="item qz_like_btn_v3 item-on" data-islike="1" data-likecnt="13" '
        'data-showcount="12" data-unikey="http://user.qzone.qq.com/222/mood/bbb222" '
        'data-curkey="http://user.qzone.qq.com/222/mood/bbb222">'
    )

    def test_parses_both_buttons(self):
        items = QzoneClient._parse_like_buttons(self.SAMPLE)
        self.assertEqual(len(items), 2)

        first, second = items
        self.assertEqual(first["islike"], 0)
        self.assertEqual(first["likecnt"], 0)
        self.assertEqual(first["owner"], "111")
        self.assertEqual(first["tid"], "aaa111")

        self.assertEqual(second["islike"], 1)
        self.assertEqual(second["likecnt"], 13)
        self.assertEqual(second["owner"], "222")

    def test_dedup_same_unikey(self):
        items = QzoneClient._parse_like_buttons(self.SAMPLE + self.SAMPLE)
        self.assertEqual(len(items), 2)

    def test_empty_html(self):
        self.assertEqual(QzoneClient._parse_like_buttons(""), [])

    def test_unikey_keeps_http_scheme(self):
        """unikey 必须原样保留（http://），这正是点赞能否生效的关键"""
        items = QzoneClient._parse_like_buttons(self.SAMPLE)
        self.assertTrue(all(i["unikey"].startswith("http://") for i in items))


class TestClientValidation(unittest.TestCase):
    def test_missing_uin(self):
        with self.assertRaises(ValueError):
            QzoneClient("skey=abc; p_skey=def")

    def test_missing_p_skey(self):
        with self.assertRaises(ValueError):
            QzoneClient("uin=o123456")

    def test_empty_cookie(self):
        with self.assertRaises(ValueError):
            QzoneClient("")

    def test_accepts_uin_without_prefix(self):
        c = QzoneClient("uin=123456; p_skey=def")
        self.assertEqual(c.uin, 123456)

    def test_extra_cookie_fields_are_forwarded(self):
        c = QzoneClient("uin=o123456; p_skey=def; ptcz=xyz")
        self.assertIn("ptcz=xyz", c.header_cookie)


if __name__ == "__main__":
    unittest.main(verbosity=2)
