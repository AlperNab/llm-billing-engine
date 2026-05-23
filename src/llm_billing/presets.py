"""Model pricing presets — updated periodically."""
from decimal import Decimal
from dataclasses import dataclass


@dataclass(frozen=True)
class Pricing:
    name: str
    input_cost_per_token: Decimal   # USD per token
    output_cost_per_token: Decimal

    def estimate(self, input_tokens: int, output_tokens: int = 0) -> Decimal:
        return (
            self.input_cost_per_token * input_tokens
            + self.output_cost_per_token * output_tokens
        )


class ModelPricing:
    # Anthropic
    CLAUDE_HAIKU_4      = Pricing("claude-haiku-4",      Decimal("0.0000008"),  Decimal("0.000004"))
    CLAUDE_SONNET_4     = Pricing("claude-sonnet-4",     Decimal("0.000003"),   Decimal("0.000015"))
    CLAUDE_OPUS_4       = Pricing("claude-opus-4",       Decimal("0.000015"),   Decimal("0.000075"))

    # Google
    GEMINI_25_FLASH     = Pricing("gemini-2.5-flash",    Decimal("0.000000075"),Decimal("0.0000003"))
    GEMINI_25_PRO       = Pricing("gemini-2.5-pro",      Decimal("0.00000125"), Decimal("0.000010"))

    # OpenAI
    GPT4O               = Pricing("gpt-4o",              Decimal("0.0000025"),  Decimal("0.00001"))
    GPT4O_MINI          = Pricing("gpt-4o-mini",         Decimal("0.00000015"), Decimal("0.0000006"))

    # Audio — Gemini (per audio token, ~32 tokens/sec)
    GEMINI_25_FLASH_AUDIO = Pricing("gemini-2.5-flash-audio", Decimal("0.000000075"), Decimal("0"))
