from llmform.policy.transforms import InMemoryTokenVault, Origin, detokenize, transform


def test_tokenization_hides_plaintext_and_is_stable_per_run() -> None:
    vault = InMemoryTokenVault()
    first = transform({"ssn": "123"}, ["ssn"], "tokenize", vault, "run", Origin.REQUEST)
    second = transform({"ssn": "123"}, ["ssn"], "tokenize", vault, "run", Origin.REQUEST)
    assert first["ssn"] == second["ssn"]
    assert "123" not in first["ssn"]


def test_tool_result_tokens_are_not_restored_to_caller() -> None:
    vault = InMemoryTokenVault()
    tokenized = transform({"card": "4111"}, ["card"], "tokenize", vault, "run", Origin.TOOL_RESULT)
    assert detokenize(tokenized, ["card"], vault, "run", caller=True) == tokenized
    assert detokenize(tokenized, ["card"], vault, "run") == {"card": "4111"}


def test_detokenize_requires_declared_whole_field() -> None:
    vault = InMemoryTokenVault()
    tokenized = transform({"secret": "value"}, ["secret"], "tokenize", vault, "run", Origin.REQUEST)
    assert detokenize(tokenized, [], vault, "run") == tokenized
