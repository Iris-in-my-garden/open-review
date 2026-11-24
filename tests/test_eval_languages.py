from evaluation.languages import allowed_suffixes


def test_allowed_suffixes_maps_manifest_languages():
    assert allowed_suffixes("Go") == (".go",)
    assert allowed_suffixes("go") == (".go",)
    assert allowed_suffixes("Python") == (".py", ".pyi")
    assert allowed_suffixes(" python ") == (".py", ".pyi")


def test_allowed_suffixes_unknown_or_missing_means_no_filter():
    assert allowed_suffixes(None) == ()
    assert allowed_suffixes("") == ()
    assert allowed_suffixes("Rust") == ()
