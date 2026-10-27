import sys
from pathlib import Path

from validatedpatterns_tests.interop.conftest_openshift import *  # noqa: F401, F403, RUF100

# The k-orc feature tree uses a hyphenated directory name (matching the
# upstream project name), which is not importable as a Python package.
# Make its test module importable from tests/ci/test_standalone.py.
sys.path.insert(0, str(Path(__file__).parent / "k-orc"))
