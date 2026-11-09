"""The catalog: each entry fixes everything about a deployment.

While the GPU quota is 0 (OVH ticket CS17114051) the only entry runs on CPU, so
the whole start, serve, refresh and teardown path can be exercised cheaply.
"""

from __future__ import annotations

from dataclasses import dataclass

# Public OVH catalog, ai-app.ai1-1-cpu.hour.consumption, excluding VAT.
AI1_1_CPU_EUR_PER_HOUR = 0.033


@dataclass(frozen=True)
class CatalogEntry:
    id: str
    display_name: str
    engine: str
    image: str
    flavor: str
    flavor_count: int
    gpu_count: int
    price_eur_per_hour: float
    ctx_tokens: int
    parallel: int
    hf_repo: str
    hf_file: str
    max_spend_eur: float | None = None


_ENTRIES = (
    CatalogEntry(
        id="smollm3-3b-q4-cpu",
        display_name="SmolLM3 3B · Q4_K_M · llama.cpp · 4×ai1-1-cpu · ctx M",
        engine="llama.cpp",
        image="ghcr.io/ggml-org/llama.cpp:server-b12063",
        flavor="ai1-1-cpu",
        flavor_count=4,
        gpu_count=0,
        price_eur_per_hour=round(4 * AI1_1_CPU_EUR_PER_HOUR, 4),
        ctx_tokens=32768,
        parallel=1,
        hf_repo="ggml-org/SmolLM3-3B-GGUF",
        hf_file="SmolLM3-Q4_K_M.gguf",
    ),
)

CATALOG: dict[str, CatalogEntry] = {entry.id: entry for entry in _ENTRIES}
