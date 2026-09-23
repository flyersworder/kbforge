"""Tests for phrase matching and keyword tagging."""

from kbforge.tagging import keyword_tags, phrase_pattern

VOCAB = {"sic": ["SiC", "silicon carbide"], "800v": ["800 V"], "cpp": ["C++"],
         "dc": ["800 V (DC)"], "packaging": []}  # fmt: skip


def test_a_phrase_matches_on_word_boundaries_case_insensitively():
    assert phrase_pattern("SiC").search("New sic MOSFETs")
    assert not phrase_pattern("SiC").search("a basic design")


def test_regex_metacharacters_are_literal():
    assert phrase_pattern("C++").search("written in C++ today")
    assert not phrase_pattern("C++").search("written in C today")
    assert phrase_pattern("800 V (DC)").search("rated 800 V (DC) bus")
    assert not phrase_pattern("800 V (DC)").search("rated 800 V DC bus")


def test_keyword_tags_are_the_sorted_tags_whose_phrases_match():
    tags = keyword_tags(VOCAB, "Q3 report", "Silicon carbide at 800 V, in C++.")
    assert tags == ["800v", "cpp", "sic"]


def test_a_tag_with_no_phrases_is_never_assigned_by_keyword():
    assert keyword_tags(VOCAB, "packaging", "packaging packaging") == []


def test_no_vocabulary_means_no_keyword_tags():
    assert keyword_tags(None, "SiC", "SiC") == []


def test_the_title_counts():
    assert keyword_tags(VOCAB, "SiC roadmap", "") == ["sic"]
