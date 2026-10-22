from openreview.retriever.factory import build_retriever
from openreview.retriever.grep_retriever import GrepRetriever
from openreview.retriever.tree_sitter_retriever import TreeSitterLocalRetriever
from openreview.review.aspect_reviewer import ReviewOrchestrator
from openreview.review.finding_validator import FindingValidator
from openreview.review.llm_client import MockLLMClient
from openreview.review.output_parser import OutputParser
from openreview.review.prompt_builder import PromptBuilder
from openreview.review.reviewer_factory import build_reviewer
from openreview.review.simple_reviewer import Reviewer


def _reviewer_kwargs():
    return {
        "prompt_builder": PromptBuilder(),
        "llm_client": MockLLMClient(),
        "output_parser": OutputParser(),
        "validator": FindingValidator(),
    }


def test_retriever_factory_builds_ast_and_grep_baseline():
    assert isinstance(build_retriever("tree_sitter"), TreeSitterLocalRetriever)
    assert isinstance(build_retriever("grep"), GrepRetriever)


def test_retriever_factory_rejects_unknown_name():
    try:
        build_retriever("unknown")
    except ValueError as exc:
        assert "Unknown retriever" in str(exc)
    else:
        raise AssertionError("Expected ValueError")


def test_reviewer_factory_builds_multi_and_single_modes():
    multi = build_reviewer("multi", **_reviewer_kwargs())
    single = build_reviewer("single", **_reviewer_kwargs())

    assert isinstance(multi, ReviewOrchestrator)
    assert isinstance(single, Reviewer)
    assert single.aspects == ["single_prompt"]


def test_reviewer_factory_rejects_unknown_name():
    try:
        build_reviewer("unknown", **_reviewer_kwargs())
    except ValueError as exc:
        assert "Unknown reviewer" in str(exc)
    else:
        raise AssertionError("Expected ValueError")
