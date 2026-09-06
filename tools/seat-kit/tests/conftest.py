import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "real_interpreter_check: run seat_new's real interpreter dependency probe",
    )
