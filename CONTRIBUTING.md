# Contributing

Contributions are welcome. For a small fix, open a pull request. For anything
larger, open an issue first so the change can be discussed before the work is
done.

## Steps

- Fork the repository and clone your fork with `--recurse-submodules`, or run
  `git submodule update --init` afterwards. The `meter-driver-spec` submodule
  pins the spec release this emulator implements, and the build converts its
  YAML into the package's `openapi.json`, so nothing builds without it.
- Create a branch for your change.
- Keep commits small and independently correct: each should pass the checks
  below on its own.
- Push the branch to your fork and open a pull request against `main`.

## Before submitting

```sh
uv sync --group dev
uv run pre-commit install   # once; the hooks then run on every commit
uv run pytest
uv run ruff format .
uv run ruff check .
```

Formatting and linting are done by [Ruff](https://docs.astral.sh/ruff/),
configured in `pyproject.toml` with a line length of 110 and the pyflakes
and import-sorting rules. Docstrings are checked against PEP 257 by
`pydocstyle`; that check is advisory. CI runs all of the above plus the
tests and builds the container image.

## Scope

The emulator implements the
[Meter Driver Specification](https://github.com/EarthSpark/meter-driver-spec)
and nothing else. Behavior that is not defined by the spec, or that would only
make sense for one particular application, belongs elsewhere. Changes to what
the spec requires go to the spec repository first.

## Reporting issues

File a GitHub issue with as much detail as possible: the command you ran, the
request you sent, the response and events you got, and what you expected.
