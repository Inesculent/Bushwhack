"""Find Python sources that emit SyntaxWarning for invalid escape sequences."""
from __future__ import annotations

import pathlib
import warnings


def main() -> None:
    root = pathlib.Path(__file__).resolve().parents[1] / "src"
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", SyntaxWarning)
        for path in sorted(root.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            compile(source, str(path), "exec", dont_inherit=True)
    for w in caught:
        if issubclass(w.category, SyntaxWarning):
            print(w.filename, w.lineno, w.message)


if __name__ == "__main__":
    main()
