"""Help > About dialog for the Knights Mod Editor."""

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QDialogButtonBox,
)
from PySide6.QtCore import Qt

from knights_editor import __version__


APP_NAME = "Knights Mod Editor"
APP_VERSION = __version__

LUA_DOCS_URL = "https://www.knightsgame.org.uk/lua"

LINKS = [
    ("Knights home page", "https://www.knightsgame.org.uk/"),
    ("Knights Steam store page",
     "https://store.steampowered.com/app/4146180/Knights/"),
    ("Knights Community Hub on Steam",
     "https://steamcommunity.com/app/4146180"),
    ("Knights on GitHub", "https://github.com/sdthompson1/knights"),
]

DESCRIPTION = (
    "<p>{name} is a tool for creating and editing mods for "
    "<b>Knights</b>, the multiplayer dungeon game.</p>"
    "<p>Copyright &copy; 2026 Stephen Thompson.</p>"
)


class AboutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"About {APP_NAME}")
        self.setModal(True)

        layout = QVBoxLayout(self)

        title = QLabel(f"<h2>{APP_NAME}</h2>"
                       f"<p>Version {APP_VERSION}</p>")
        title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(title)

        desc = QLabel(DESCRIPTION.format(name=APP_NAME))
        desc.setWordWrap(True)
        desc.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(desc)

        links_html = "".join(
            f'<li><a href="{url}">{text}</a></li>' for text, url in LINKS)
        links = QLabel(f"<p><b>Links</b></p><ul>{links_html}</ul>")
        links.setTextFormat(Qt.TextFormat.RichText)
        links.setOpenExternalLinks(True)
        links.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction)
        layout.addWidget(links)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

        self.setMinimumWidth(420)
