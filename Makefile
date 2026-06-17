.PHONY: lint lint-fix format format-check test clean validate

lint:
	uv run ruff check .

lint-fix:
	uv run ruff check --fix .

format:
	uv run ruff format .

format-check:
	uv run ruff format --check .

test:
	uv run pytest --tb=short -q --no-header --disable-warnings

clean:
	rm -rf .pytest_cache .ruff_cache

validate: lint test
	@echo "✅ All local validation checks passed!"
