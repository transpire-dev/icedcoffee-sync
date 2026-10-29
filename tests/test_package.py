def test_package_exposes_version() -> None:
    import icedcoffee_sync

    assert icedcoffee_sync.__version__ == "0.0.1"
