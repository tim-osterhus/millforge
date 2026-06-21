from src.app.main import answer


def test_answer() -> None:
    assert answer() == 42
