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


def test_filter_comments_by_language_drops_cross_language_references():
    from evaluation.languages import filter_comments_by_language

    comments = [
        {"path": "src/main.go", "note": "go bug"},
        {"path": "src/native.cpp", "note": "cpp bug"},
        {"path": "src/util.py", "note": "py bug"},
        {"note": "general comment"},
    ]

    kept = filter_comments_by_language(comments, "Go")

    assert [item.get("path") for item in kept] == ["src/main.go", None]
