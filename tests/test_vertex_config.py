"""Vertex AI wiring.

The service switched from an AI Studio key to Vertex so model calls draw on
Google Cloud billing rather than the free tier's 20 requests a day. The switch
looked complete — env vars set, container healthy, `/health` reporting
`"gemini": "configured"` — and every single model call returned 404.

`gemini-3.5-flash` is not served from regional Vertex endpoints. The default was
`us-central1`, which is the obvious thing to write and is wrong.

This is the same failure shape as the rest of this repo's bugs: nothing crashed,
the health check stayed green, and the only symptom was a 500 on the one path
nobody curls before a demo.
"""

from __future__ import annotations

import pytest

from utils.config import Config

# Probed with generateContent against each endpoint on 25 Aug 2026.
# The regional column is why this is quiet: pin an older model and it works.
PROBED = {
    ("gemini-3.5-flash", "us-central1"): 404,
    ("gemini-3.5-flash", "global"): 200,
    ("gemini-3.1-flash-image", "us-central1"): 404,
    ("gemini-3.1-flash-image", "global"): 200,
    ("gemini-2.5-flash", "us-central1"): 200,
    ("gemini-2.5-flash", "global"): 200,
}


class TestTheLocationDefault:
    def test_vertex_defaults_to_the_global_endpoint(self):
        assert Config().gcp_location == "global"

    def test_the_default_model_is_actually_served_where_we_point_it(self):
        cfg = Config()
        served = PROBED.get((cfg.model, cfg.gcp_location))
        assert served == 200, (
            f"{cfg.model} is not served from {cfg.gcp_location} — every model "
            "call will 404 while the service still reports itself healthy"
        )

    def test_the_vision_model_is_served_there_too(self):
        cfg = Config()
        # Vision is the path that burns quota and the path a demo depends on.
        assert PROBED.get((cfg.vision_model, cfg.gcp_location)) == 200

    @pytest.mark.parametrize("model", ["gemini-3.5-flash", "gemini-3.1-flash-image"])
    def test_a_region_would_have_broken_the_models_we_claim(self, model):
        # Pins the finding itself, so nobody "tidies" the default back to a
        # region on the reasonable-sounding grounds that everything else is
        # deployed to us-central1.
        assert PROBED[(model, "us-central1")] == 404


class TestCredentialsReporting:
    def test_vertex_counts_as_available_without_an_api_key(self):
        # genai_available gates whether workflows use the real model or a stub.
        cfg = Config()
        assert cfg.genai_available == bool(cfg.api_key) or (
            cfg.use_vertex and bool(cfg.gcp_project)
        )

    def test_the_startup_report_names_the_backend(self):
        # The one line that would have made the 404 obvious on deploy day.
        assert Config().report()["gemini_backend"] in {"vertex-ai", "google-ai-studio"}


class TestSessionIdValidation:
    """A malformed session id used to reach Postgres and 500.

    `session_id` is a uuid column, so `"demo-timing-real"` came back as
    InvalidTextRepresentation with a stack trace — a server error for what is
    plainly a bad request, on the endpoints most likely to be poked by hand.
    """

    def test_a_non_uuid_session_id_is_rejected_as_a_bad_request(self):
        import pytest
        from pydantic import ValidationError

        from main import LeaveScanRequest

        with pytest.raises(ValidationError):
            LeaveScanRequest(destination="work", session_id="demo-timing-real")

    def test_a_real_uuid_passes(self):
        from main import LeaveScanRequest

        sid = "45165286-cd63-497a-bb66-dc06a56eb967"
        assert LeaveScanRequest(session_id=sid).session_id == sid

    def test_an_absent_session_id_is_still_allowed(self):
        from main import LeaveScanRequest

        # The server mints one when the client has no session yet.
        assert LeaveScanRequest().session_id is None
