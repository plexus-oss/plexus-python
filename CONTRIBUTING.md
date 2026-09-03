# Contributing to plexus-python

Thanks for your interest in contributing! Here's how to get started.

## Development Setup

```bash
git clone https://github.com/plexus-oss/plexus-python.git
cd plexus-python
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Running Tests

```bash
pytest
pytest --cov=plexus    # with coverage
```

## Linting

```bash
ruff check .
ruff check --fix .     # auto-fix
```

## Submitting Changes

1. Fork the repo and create a branch from `main`
2. Make your changes — add tests for new functionality
3. Run `pytest` and `ruff check .` and make sure both pass
4. Open a pull request with a clear description of what and why

## Reporting Bugs

Open an issue at [GitHub Issues](https://github.com/plexus-oss/plexus-python/issues) with:
- Python version and OS
- Steps to reproduce
- Expected vs actual behavior
- Relevant logs or tracebacks

## Code Style

- Follow existing patterns in the codebase
- Run `ruff` before committing
- Add docstrings to public functions
- Keep dependencies minimal — optional extras go in `pyproject.toml`

## License

By contributing, you agree that your contributions will be licensed under the [Apache License 2.0](LICENSE).
