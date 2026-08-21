# Test fixtures

The test suite is hermetic and needs no image files: vision results are injected
directly (see the `seen_items` fixture in `tests/test_workflows.py`), and the
frame decoder is tested with synthetic byte strings.

For manual end-to-end checks of the real Gemini vision path, point the browser's
camera at an actual scene — that is also how you generate real demo data, which
is worth more in a submission than anything staged.
