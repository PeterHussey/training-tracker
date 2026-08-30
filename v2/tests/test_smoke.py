# tests/test_smoke.py
def test_environment():
    import sys
    assert sys.version_info >= (3, 11)
    import garminconnect, pandas  # noqa: F401