Uses uv. Run tests like this:

    uv run pytest

Run the development version of the tool like this:

    uv run cad --help

Always practice TDD: write a failing test, watch it fail, then make it pass.

Commit early and often. Commits should bundle the test, implementation, and documentation changes together.

Run Black to format code before you commit:

    uv run black .

The picker, provider abstraction, `cad live`, and everything around the HTML renderer are this project's work; the renderer itself came from simonw/claude-code-transcripts (see README). When touching `cad json` / `cad all`, expect to see his original code shape; everywhere else, the conventions in `src/cad/__init__.py` are what to follow.
