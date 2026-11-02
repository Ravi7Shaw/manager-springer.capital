.PHONY: check

check:
	ruff check .
	ruff format --check .
	mypy app
	pytest -q
