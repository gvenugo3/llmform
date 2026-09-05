from __future__ import annotations


def pytest_addoption(parser) -> None:
    parser.addoption(
        "--update-goldens",
        action="store_true",
        default=False,
        help="rewrite diagnostic golden files from the current validator output",
    )
