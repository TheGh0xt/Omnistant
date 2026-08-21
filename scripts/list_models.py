"""Print the Gemini models this project's credentials can actually reach.

The build spec named "Gemini 3.5", which is not a released model id.  Run this
to see what is available, then set GEMINI_MODEL in .env accordingly.

    uv run python scripts/list_models.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from google import genai  # noqa: E402

from utils.config import get_config  # noqa: E402


def main() -> int:
    cfg = get_config()
    if not cfg.genai_available:
        print("No credentials. Set GOOGLE_API_KEY (or GEMINI_API_KEY) in .env.")
        return 1
    client = genai.Client(api_key=cfg.api_key) if not cfg.use_vertex else genai.Client(
        vertexai=True, project=cfg.gcp_project, location=cfg.gcp_location
    )
    print(f"{'model':45} {'in':>8} {'out':>8}  actions")
    print("-" * 90)
    for m in client.models.list():
        actions = ",".join(m.supported_actions or [])
        if "generateContent" not in actions:
            continue
        print(f"{m.name:45} {str(m.input_token_limit):>8} {str(m.output_token_limit):>8}  {actions}")
    print(f"\nConfigured GEMINI_MODEL = {cfg.model}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
