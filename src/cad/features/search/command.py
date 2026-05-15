"""
features/search/command.py — the ``cad search`` click subcommand.

Runs :func:`search_sessions` and drops the hits into the regular
session picker, with the snippet appended to each row's display line
so the user can see what matched without having to open the session.

Hits carry the standard session-dict shape so the picker's existing
actions (Enter=resume, p=peek, h=html, r=rename, etc.) work on them
without any special-casing. The shared dependency injection through
``cad.__dict__`` (same pattern as features/live/command and
features/local/command) keeps test patchability intact.
"""

import click

from ...core.util import _loading_message
from .find import search_sessions


def _lookup(name):
    """Resolve ``name`` on the top-level ``cad`` module at call time."""
    from ... import __dict__ as cad_ns

    return cad_ns[name]


def _hydrate_hit(hit, load_session_summary):
    """Build the picker display string for a search hit.

    Calls the standard summary loader so the row matches the regular
    session picker, then appends the snippet (and a ``[N]`` match
    badge when there's more than one) so the user sees the context
    that justified the hit."""
    load_session_summary(hit)
    snippet = hit.get("snippet", "")
    matches = hit.get("match_count", 1)
    suffix = f'  → "{snippet}"' if snippet else ""
    if matches > 1:
        suffix += f"  [{matches} matches]"
    hit["display"] = hit["display"] + suffix


@click.command("search")
@click.argument("query")
@click.option(
    "--cwd",
    help="Only search sessions in this project (exact cwd match).",
)
@click.option(
    "--provider",
    type=click.Choice(["claude", "codex", "pi", "opencode", "forge"]),
    help="Restrict to one provider's sessions.",
)
@click.option(
    "--limit",
    type=int,
    help="Cap the number of hits (newest first).",
)
def search_cmd(query, cwd, provider, limit):
    """Find local sessions whose content matches QUERY (case-insensitive).

    Title-based search (``/`` inside the picker) only matches each
    row's summary line. ``cad search`` walks the actual conversation
    text — useful when you remember a phrase but not the title.

    \b
    Examples:
        cad search "viewport culling"
        cad search dda --cwd /Users/x/Code/raycaster
        cad search 'TODO' --limit 5

    Hits land in the regular picker — Enter resumes, p peeks, h
    renders to HTML, r/s/m/d edit the title / move / archive.
    """
    load_session_summary = _lookup("load_session_summary")
    select_entry = _lookup("select_entry")
    resume_session = _lookup("resume_session")
    peek_session = _lookup("peek_session")

    with _loading_message(f"Searching for {query!r}..."):
        hits = search_sessions(query, cwd=cwd, provider=provider, limit=limit)

    if not hits:
        click.echo(f"No sessions match {query!r}.")
        return

    for h in hits:
        _hydrate_hit(h, load_session_summary)

    # The picker. Enter on a hit resumes that session — same UX as
    # finding it in `cad local` and pressing Enter. Peek + html are
    # there for when the snippet alone isn't enough context.
    picked = select_entry(
        hits,
        actions={
            "enter": "resume",
            "p": "peek",
            "h": "html",
        },
    )
    if picked is None:
        return
    session, action = picked
    if action == "peek":
        peek_session(session)
        return
    if action == "html":
        # Inline the same `cad json <file>` invocation the local
        # picker uses — write to the default temp dir and open.
        generate_html = _lookup("generate_html")
        _temp_output_dir = _lookup("_temp_output_dir")
        import webbrowser

        out = _temp_output_dir(f"claude-session-{session['filepath'].stem}")
        generate_html(session["filepath"], out)
        click.echo(f"Output: {out.resolve()}")
        webbrowser.open((out / "index.html").resolve().as_uri())
        return
    # action == "resume" — replaces the process, doesn't return.
    resume_session(session)
