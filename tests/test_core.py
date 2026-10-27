# -*- coding: utf-8 -*-
"""核心算法回归测试（unittest，零第三方依赖，可离线跑）。

覆盖评审最关心的四个点：
  1. AHP 一致性（CR<0.1）与 na 重归一化（权重和=1）
  2. 集中度 CR1/CR3 与 HHI 的手算值
  3. 服务水平趋势外推：点预测 + 90% 区间（含"样本太少不出预测"的边界）
  4. 出网守卫、静默兜底留痕、覆盖度/对照分口径

跑法：
    python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


class TestAHP(unittest.TestCase):
    def test_cr_below_threshold(self):
        from core.ahp import get_weights
        w, cr = get_weights()
        self.assertLess(cr, 0.1, "AHP 一致性比率应 <0.1，实际 %s" % cr)
        # get_weights 按 4 位小数四舍五入输出，允许 1e-3 级误差
        self.assertAlmostEqual(sum(w.values()), 1.0, places=3, msg="权重之和必须为 1")

    def test_renormalize_drops_na(self):
        from core.ahp import renormalize
        w = {"a": 0.5, "b": 0.3, "c": 0.2}
        out = renormalize(w, ["a", "c"])
        self.assertAlmostEqual(sum(out.values()), 1.0, places=9)
        self.assertNotIn("b", out)
        self.assertGreater(out["a"], w["a"])          # 缺失维度权重摊到剩余维度


class TestConcentration(unittest.TestCase):
    def test_cr_and_hhi_hand_calc(self):
        from core import concentration, roster
        fake = [
            {"id": "1", "name": "甲公司", "annual_amount": 6000, "business_lines": ["核心账务系统"]},
            {"id": "2", "name": "乙公司", "annual_amount": 4000, "business_lines": ["客服中心"]},
        ]
        orig = roster.load_roster
        roster.load_roster = lambda: fake
        try:
            c = concentration.build_concentration()
        finally:
            roster.load_roster = orig
        self.assertAlmostEqual(c["cr1"], 60.0, places=1)
        self.assertAlmostEqual(c["cr3"], 100.0, places=1)
        # HHI = 60² + 40² = 5200
        self.assertAlmostEqual(c["hhi"], 5200.0, places=1)
        self.assertEqual(c["solo_line_count"], 2)      # 两条线各只有一家有效支撑
        self.assertEqual(c["critical_solo_count"], 1)  # 只有核心账务系统算重要
        self.assertIn("市场", c["hhi_note"])           # 口径说明必须在结果里

    def test_hhi_levels(self):
        from core.concentration import _hhi_level
        self.assertEqual(_hhi_level(0)[0], "无数据")
        self.assertEqual(_hhi_level(1000)[0], "竞争充分")
        self.assertEqual(_hhi_level(2000)[0], "中度集中")
        self.assertEqual(_hhi_level(3000)[0], "高度集中")


class TestDecayWeighting(unittest.TestCase):
    """时效衰减必须是「近重远轻」：最近一期拿最大权重。

    历史缺陷：`decayed_score` 把「由新到旧」的权重表直接与时间升序的期次 zip，
    0.5 落在**最旧**一期上，恶化企业反而拿到偏高的衰减分（100→80→60 报 86 而非 74）。
    这组用例把方向钉死，避免再次反转。
    """

    def _rows(self, scores):
        return [{"period": "2026-%02d" % (i + 1), "score": s}
                for i, s in enumerate(scores)]

    def test_newest_period_gets_highest_weight(self):
        from core.servicelevel import decayed_score
        # 100 → 80 → 60：降序，衰减分应贴近最近的 60，而不是贴近最初的 100
        self.assertAlmostEqual(decayed_score(self._rows([100, 80, 60]))["score"], 74.0, places=1)
        # 反向序列应给出对称的高分，证明不是"一律偏低"
        self.assertAlmostEqual(decayed_score(self._rows([60, 80, 100]))["score"], 86.0, places=1)

    def test_two_periods_use_leading_weights(self):
        from core.servicelevel import decayed_score
        # 两期只能用 0.5/0.3 两个权重（再归一化），最新一期仍是更大的那个
        self.assertAlmostEqual(decayed_score(self._rows([60, 90]))["score"], 78.8, places=1)

    def test_delta_sign_matches_direction(self):
        from core.servicelevel import decayed_score
        self.assertLess(decayed_score(self._rows([100, 80, 60]))["delta"], 0)   # 恶化 → 负
        self.assertGreater(decayed_score(self._rows([60, 80, 100]))["delta"], 0)  # 改善 → 正



class TestCredentialPlaceholder(unittest.TestCase):
    """占位符凭据必须视为「未配置」（否则会拿假 key 真调外部 API，报 401 而非降级）。"""

    def test_placeholders_detected(self):
        from core.credential import is_placeholder, real_secret
        for v in ("your_deepseek_api_key", "your_qcc_api_token", "<YOUR_KEY>",
                  "${VAR}", "changeme", "", "   ", "xxxx", "Placeholder"):
            self.assertTrue(is_placeholder(v), v)
            self.assertEqual(real_secret(v), "", v)

    def test_real_secrets_pass_through(self):
        from core.credential import is_placeholder, real_secret
        for v in ("sk-abc123def456", "qcc_real_token_123456", "my_prod_api_key"):
            self.assertFalse(is_placeholder(v), v)
            self.assertEqual(real_secret(v), v)

    def test_deepseek_treats_placeholder_as_unconfigured(self):
        import os
        from unittest import mock
        from llm.deepseek import DeepseekClient
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "your_deepseek_api_key"}):
            self.assertEqual(DeepseekClient().api_key, "")
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-real-key-123"}):
            self.assertEqual(DeepseekClient().api_key, "sk-real-key-123")


class TestForecastInterval(unittest.TestCase):
    def _rows(self, scores, start=1):
        return [{"period": "2026-%02d" % (start + i), "score": s} for i, s in enumerate(scores)]

    def test_flat_series_has_zero_width(self):
        from core.servicelevel import decayed_score
        out = decayed_score(self._rows([90, 90, 90]))
        fc = out["forecast"]
        self.assertIsNotNone(fc)
        self.assertEqual(fc["direction"], "flat")
        self.assertAlmostEqual(fc["p_lower"], fc["p_upper"], places=1)
        self.assertEqual(fc["n_obs"], 3)

    def test_decline_gives_bounded_interval(self):
        from core.servicelevel import decayed_score
        # 带噪点的下行序列：残差不为 0，区间必须真正张开
        out = decayed_score(self._rows([95, 84, 77, 63]))
        fc = out["forecast"]
        self.assertEqual(fc["direction"], "down")
        self.assertLess(fc["p_lower"], fc["value"])
        self.assertGreater(fc["p_upper"], fc["value"])
        self.assertGreaterEqual(fc["p_lower"], 0.0)
        self.assertLessEqual(fc["p_upper"], 100.0)
        self.assertIn("resid_se", fc)

    def test_perfect_line_has_zero_width(self):
        from core.servicelevel import decayed_score
        # 完全共线的四点：残差为 0，区间退化为点（这是正确的，不是缺陷）
        fc = decayed_score(self._rows([95, 85, 75, 65]))["forecast"]
        self.assertAlmostEqual(fc["p_lower"], fc["value"], places=1)
        self.assertAlmostEqual(fc["p_upper"], fc["value"], places=1)

    def test_too_few_periods_no_forecast(self):
        from core.servicelevel import decayed_score
        self.assertIsNone(decayed_score(self._rows([88, 86]))["forecast"])


class TestRatingCaliber(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from core import roster
        items = [it for it in roster.load_roster() if it.get("name")]
        cls.item = items[0] if items else None

    def test_coverage_and_baseline(self):
        if not self.item:
            self.skipTest("台账为空")
        from server import _fetch_profile
        from core.rating import rate_supplier
        res = _fetch_profile(self.item["name"], "public")
        r = rate_supplier(res.profile, supplier_id=self.item.get("id", ""))
        cov = r.coverage
        self.assertGreaterEqual(cov["used"], 1)
        self.assertEqual(set(cov["available"]) | set(cov["missing"]),
                         set(r.dimension_scores.keys()))
        self.assertEqual(cov["group"], "+".join(sorted(cov["missing"])) or "full")
        if r.baseline_score is not None:
            self.assertGreaterEqual(r.baseline_score, 0.0)
            self.assertLessEqual(r.baseline_score, 100.0)
        # 归一化权重和为 1（ahp 权重按 4 位小数输出，容 1e-3 级误差）
        self.assertAlmostEqual(sum(r.weights.values()), 1.0, places=3)
        self.assertEqual(r.data_origin.get("performance"), "simulated")


class TestSSRFGuard(unittest.TestCase):
    def test_rejects_internal_and_non_http(self):
        from adapters.generic_adapter import _assert_public_url
        bad = [
            "file:///etc/passwd",
            "http://127.0.0.1:8000/api/scan",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.7/",
            "gopher://example.com:70/",
        ]
        for url in bad:
            with self.assertRaises(ValueError, msg="应拒绝：%s" % url):
                _assert_public_url(url)

    def test_accepts_public_host(self):
        from adapters.generic_adapter import _assert_public_url
        try:
            _assert_public_url("https://www.example.com/api")
        except ValueError as exc:
            self.skipTest("离线环境无法解析公网域名：%s" % exc)


class TestPDFBackend(unittest.TestCase):
    """PyMuPDF 是 AGPL-3.0，商业分发要换 pypdf。兜底路径不能只是"写了没跑过"。"""

    def test_pypdf_fallback_when_fitz_missing(self):
        import sys
        import types
        from security import file_parser

        class _Page:
            def extract_text(self):
                return "营业收入 12,340,000 元"

        class _Reader:
            is_encrypted = False

            def __init__(self, _path):
                self.pages = [_Page()]

            def close(self):
                pass

        saved_fitz = sys.modules.get("fitz")
        saved_pypdf = sys.modules.get("pypdf")
        sys.modules["fitz"] = None                       # 令 import fitz 抛 ImportError
        sys.modules["pypdf"] = types.SimpleNamespace(PdfReader=_Reader)
        try:
            doc, backend = file_parser._open_pdf("dummy.pdf")
            self.assertEqual(backend, "pypdf")
            self.assertEqual(doc.pages[0].extract_text(), "营业收入 12,340,000 元")
        finally:
            for name, saved in (("fitz", saved_fitz), ("pypdf", saved_pypdf)):
                if saved is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = saved


class TestSwallowLog(unittest.TestCase):
    def test_logs_once_then_counts(self):
        from core import quiet_log
        quiet_log.reset_swallowed()
        try:
            raise RuntimeError("demo")
        except RuntimeError:
            quiet_log.log_swallow("test:1")
        try:
            raise RuntimeError("demo")
        except RuntimeError:
            quiet_log.log_swallow("test:1")
        snap = quiet_log.swallowed_snapshot()
        self.assertEqual(snap.get("test:1|RuntimeError"), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
