import pytest

from condo_classifier.backends import BaselineBackend


@pytest.fixture(scope="session")
def baseline():
    return BaselineBackend()
