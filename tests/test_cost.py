"""Token cost attribution invariants.

`config.cost_of` turns a model run's prompt/completion token counts into a USD
figure, and the dashboard folds that into per-model and per-task `cost` fields.
These tests pin the arithmetic and the failure modes: a model with no price
must yield 0.0 rather than a guess, an operator env override must win over the
table, and junk in either input must never raise — a crash here would take the
whole usage/pages API down with it.
"""
import os
import unittest

from helpers import capture_events  # noqa: F401  (sys.path)

import config


def _clear_price_env():
    """Remove every operator pricing override so the table is authoritative."""
    for k in [k for k in os.environ if k.startswith("ARC_PRICE_")]:
        del os.environ[k]


class CostOfArithmetic(unittest.TestCase):
    """`cost_of` must price prompt and completion independently, per million."""

    def setUp(self):
        _clear_price_env()

    def test_known_token_count_times_price_equals_cost(self):
        """A known token count at a known price must return that product."""
        model = "gpt-oss-120b"
        p, c = config.MODEL_PRICING[model]["prompt_per_mtok"], \
            config.MODEL_PRICING[model]["completion_per_mtok"]
        prompt, completion = 1_500_000, 750_000
        expected = prompt / 1e6 * p + completion / 1e6 * c
        self.assertAlmostEqual(
            config.cost_of(model, prompt, completion), expected, places=8)

    def test_prompt_and_completion_are_priced_separately(self):
        """A model whose prompt and completion rates differ must charge each at
        its own rate — collapsing them to one rate would inflate prompt-heavy
        loads and understate completion-heavy ones."""
        model = "GLM-5.3"
        p, c = config.MODEL_PRICING[model]["prompt_per_mtok"], \
            config.MODEL_PRICING[model]["completion_per_mtok"]
        self.assertNotEqual(p, c, "this test assumes a prompt/completion gap")
        prompt = config.cost_of(model, 1_000_000, 0)
        completion = config.cost_of(model, 0, 1_000_000)
        self.assertAlmostEqual(prompt, p, places=8)
        self.assertAlmostEqual(completion, c, places=8)
        self.assertAlmostEqual(prompt + completion,
                               config.cost_of(model, 1_000_000, 1_000_000), places=8)

    def test_zero_tokens_cost_zero(self):
        """No tokens means no money; a zero serves 0.0, not NaN."""
        self.assertEqual(config.cost_of("gpt-oss-120b", 0, 0), 0.0)

    def test_unpriced_model_costs_zero_without_raising(self):
        """An unknown model has no rate; guessing one would invent a charge,
        so the honest figure is 0.0 — and it must not raise."""
        self.assertEqual(config.cost_of("no-such-model", 100, 200), 0.0)
        self.assertEqual(config.cost_of(None, 100, 200), 0.0)

    def test_cost_of_never_raises_on_junk_inputs(self):
        """A junk token count (None, negative, a string) must not crash — this
        function feeds the live dashboard."""
        self.assertIsInstance(config.cost_of("gpt-oss-120b", None, None), float)
        self.assertIsInstance(config.cost_of("gpt-oss-120b", "x", -5), float)


class CostOfEnvOverride(unittest.TestCase):
    """Operator env overrides must win over the table, and junk must fall back."""

    def setUp(self):
        _clear_price_env()

    def tearDown(self):
        _clear_price_env()

    def test_env_override_is_honoured(self):
        """ARC_PRICE_<MODEL>_PROMPT/COMPLETION replace the table for that model."""
        os.environ["ARC_PRICE_KIMI_K3_PROMPT"] = "0.9"
        os.environ["ARC_PRICE_KIMI_K3_COMPLETION"] = "1.1"
        self.assertAlmostEqual(config.cost_of("Kimi-K3", 1_000_000, 0), 0.9, places=8)
        self.assertAlmostEqual(config.cost_of("Kimi-K3", 0, 5_000_000), 5.5, places=8)

    def test_a_partial_override_falls_back_for_the_unset_half(self):
        """Setting only one rate must not zero the other; the unset half keeps
        the table value."""
        os.environ["ARC_PRICE_DEEPSEEK_V4_FLASH_PROMPT"] = "0.3"
        table_c = config.MODEL_PRICING["DeepSeek-V4-Flash"]["completion_per_mtok"]
        self.assertAlmostEqual(config.cost_of("DeepSeek-V4-Flash", 2_000_000, 0), 0.6, places=8)
        self.assertAlmostEqual(config.cost_of("DeepSeek-V4-Flash", 0, 1_000_000), table_c, places=8)

    def test_a_junk_override_falls_back_rather_than_crashing(self):
        """A non-numeric override value falls back to the table, and must not
        raise (an exception here breaks the whole usage API)."""
        os.environ["ARC_PRICE_GLM_5_3_PROMPT"] = "lots"
        p = config.MODEL_PRICING["GLM-5.3"]["prompt_per_mtok"]
        self.assertAlmostEqual(config.cost_of("GLM-5.3", 1_000_000, 0), p, places=8)

    def test_override_key_uses_model_uppercased_with_underscores(self):
        """The env key normalises the model name (Kimi-K3 -> ARC_PRICE_KIMI_K3_*)."""
        os.environ["ARC_PRICE_KIMI_K3_COMPLETION"] = "1.4"
        self.assertAlmostEqual(config.cost_of("Kimi-K3", 0, 1_000_000), 1.4, places=8)


if __name__ == "__main__":
    unittest.main()
