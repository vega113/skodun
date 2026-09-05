"""OpenAI API conformance in the registration gate's discoverable namespace.

HTTP runner and spend tests stay in test_openai_api.py. This module supplies
exactly the shared adapter rules and fixtures, without making API requests.
"""

from pathlib import Path

from skodun.adapters.openai_api import PROVIDER_ID, OpenAIAPIAdapter
from skodun.config import Reviewer
from tests.adapter_conformance import (  # noqa: F401 - collected below
    AdapterConformance,
    test_coverage_gate_fails_without_a_conformance_subclass,
    test_every_registered_adapter_has_conformance_coverage,
    test_load_fixture_rejects_a_malformed_rc,
)

MODEL = "gpt-5.6-luna"
FIXTURES = Path(__file__).parent / "fixtures" / "adapters" / "openai_api"


class TestOpenAIAPIConformance(AdapterConformance):
    provider_id = PROVIDER_ID
    fixture_dir = FIXTURES

    def adapter(self):
        return OpenAIAPIAdapter()

    def effort_reject_case(self):
        r = Reviewer(name="f", provider=PROVIDER_ID, model=MODEL,
                     role="finder", effort="max")
        return r, "effort"

