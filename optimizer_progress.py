"""Small console progress renderer shared by optimizer components."""


def _show_loading_progress(label, completed, total):
    """Render one in-place startup progress line for potentially large histories."""
    if total < 5:
        return
    percent = 100.0 * completed / max(1, total)
    end = "\n" if completed >= total else ""
    print(
        f"\r{label}: {percent:6.2f}% ({completed:,}/{total:,})",
        end=end,
        flush=True,
    )
