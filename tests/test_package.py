def test_package_exposes_version() -> None:
    import stolon

    assert stolon.__version__ == "0.0.1"
