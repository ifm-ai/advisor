"""Check that a solver endpoint continues a prefilled reasoning block, before a long run.

    export SOLVER_BASE_URL=http://localhost:30000/v1 SOLVER_API_KEY=...
    export SOLVER_MODEL=IFM/K2-Horizon-375B-A23B   # the default
    python examples/check_reasoning_prefill.py [--prefix ""]

Sends one short conversation that ends in an assistant message holding the start of a thought,
with continue_final_message=True, and reports what comes back. An endpoint that supports reasoning
prefill returns non-empty reasoning_content that continues the thought, with a reasoning-token
count well above single digits. Serve K2-Horizon with SGLang and `--reasoning-parser k2_horizon`;
the default empty prefix is right for it, because the template's generation prompt opens the think
block. Exit 0 when the check passes; otherwise use SelfCheckCue with this endpoint.
"""

from __future__ import annotations

import argparse
import asyncio
import os

import litellm

from ifm_advisor.responses import response_reasoning_tokens

PREFILL = (
    "Hmm. The odd-length case passes, but nothing so far shows "
    "what an even-length list returns, and"
)
MIN_REASONING_TOKENS = 16


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--prefix", default="", help="text before the prefill, e.g. a reasoning-open token"
    )
    args = parser.parse_args()
    model = os.environ.get("SOLVER_MODEL", "IFM/K2-Horizon-375B-A23B")

    response = await litellm.acompletion(
        model=f"openai/{model}",
        messages=[
            {
                "role": "user",
                "content": (
                    "`median([4, 1, 3, 2])` in stats.py returns 3. Think about why, then explain."
                ),
            },
            {"role": "assistant", "content": args.prefix + PREFILL},
        ],
        api_base=os.environ["SOLVER_BASE_URL"],
        api_key=os.environ["SOLVER_API_KEY"],
        extra_body={"continue_final_message": True},
        max_tokens=512,
        temperature=1.0,
    )
    message = response.choices[0].message
    reasoning = getattr(message, "reasoning_content", None) or ""
    tokens = response_reasoning_tokens(response)
    print("reasoning_content, first 400 characters:")
    print(reasoning[:400] or "(empty)")
    print(f"reasoning tokens: {tokens}")
    print(f"content, first 200 characters: {(message.content or '')[:200] or '(empty)'}")
    ok = bool(reasoning.strip()) and (tokens is None or tokens >= MIN_REASONING_TOKENS)
    print(
        "OK: the endpoint continued the prefilled reasoning."
        if ok
        else "NOT OK: use SelfCheckCue with this endpoint, or fix its prefix or reasoning parser."
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
