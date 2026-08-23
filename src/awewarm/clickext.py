"""Click plumbing shared by the awewarm and awewarm-hub CLIs."""
import click


class WrapGroup(click.Group):
    """Group whose `Commands:` listing wraps long one-liners.

    Click's default listing truncates each description to the terminal
    width with a trailing `...`; the formatter can wrap the same text,
    so this passes the full first help paragraph through and narrow
    terminals see the whole sentence.
    """

    def format_commands(self, ctx, formatter):
        rows = []
        for name in self.list_commands(ctx):
            cmd = self.get_command(ctx, name)
            if cmd is None or cmd.hidden:
                continue
            rows.append((name, cmd.short_help or _first_paragraph(cmd)))
        if rows:
            with formatter.section("Commands"):
                formatter.write_dl(rows)


def _first_paragraph(cmd):
    """A command's first help paragraph collapsed to one line."""
    return " ".join((cmd.help or "").partition("\n\n")[0].split())
