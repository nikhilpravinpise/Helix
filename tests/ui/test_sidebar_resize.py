"""The draggable sidebar: every page with the sidebar renders the resize
handle (a keyboard-reachable separator), and the width is one CSS custom
property shared by the stylesheet and app.js."""

from pathlib import Path

import pytest

UI_STATIC = Path(__file__).resolve().parents[2] / "ui" / "static"


@pytest.mark.parametrize("path", ["/ui/", "/ui/incidents", "/ui/approvals", "/ui/connections"])
def test_sidebar_pages_render_the_resize_separator(client, path):
    resp = client.get(path)
    assert resp.status_code == 200
    html = resp.text
    assert 'role="separator"' in html
    assert 'aria-orientation="vertical"' in html
    assert 'aria-label="Resize sidebar"' in html
    for attr in ("aria-valuenow", "aria-valuemin", "aria-valuemax"):
        assert f"{attr}=" in html
    assert 'tabindex="0"' in html


def test_css_and_js_share_the_sidebar_width_property():
    assert "--sidebar-w" in (UI_STATIC / "style.css").read_text()
    assert "--sidebar-w" in (UI_STATIC / "app.js").read_text()
