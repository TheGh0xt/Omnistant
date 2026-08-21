"""Generate test/demo scene images with Gemini's image model.

Used to produce repeatable fixtures for the vision tests without needing a
physical desk in front of a camera.

    uv run python scripts/make_test_scene.py tests/fixtures/desk_full.jpg "a desk with ..."
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from google.genai import types  # noqa: E402

from utils.gemini import get_client  # noqa: E402

MODEL = "gemini-3.1-flash-image"


def generate(out_path: str, prompt: str) -> int:
    client = get_client()
    if client is None:
        print("No Gemini credentials configured.")
        return 1
    resp = client.models.generate_content(
        model=MODEL,
        contents=[prompt],
        config=types.GenerateContentConfig(response_modalities=["IMAGE"]),
    )
    for part in resp.candidates[0].content.parts:
        if part.inline_data and part.inline_data.data:
            Path(out_path).write_bytes(part.inline_data.data)
            print(f"wrote {out_path} ({len(part.inline_data.data)} bytes, {part.inline_data.mime_type})")
            return 0
    print("model returned no image part")
    return 1


if __name__ == "__main__":
    raise SystemExit(generate(sys.argv[1], sys.argv[2]))
