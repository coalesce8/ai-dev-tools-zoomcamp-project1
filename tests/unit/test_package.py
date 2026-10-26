import runwatch


def test_version_is_non_empty_string():
    assert isinstance(runwatch.__version__, str)
    assert runwatch.__version__
