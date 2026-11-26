# aria2-gtkGUI

A download manager for Linux built on [aria2](https://aria2.github.io/), written in Python with GTK4 and libadwaita. The whole app is a single file, `aria2gtk.py`.

It starts its own private aria2c (JSON-RPC on a random localhost port, with a random secret) and controls it over RPC. Unfinished downloads are saved in a session file and resume the next time the app starts.

## Features

- **Downloads:** HTTP(S), FTP, Metalink, `.torrent` files and magnet links, in tabs for Downloading, Queued and Finished.
- **File choice:** pick which files of a torrent or magnet to download before it starts.
- **Seeding:** keep finished torrents seeding, seed existing folders, and create new torrents with `mktorrent`.
- **Peers window:** clients, speeds, progress and country flags. Flags come from a local database (DB-IP Country Lite), so no peer IP addresses leave your PC.
- **Console panel:** a dockable live view of aria2's log, colour-coded by level.
- **Preferences:** the important aria2 options, plus a Storage page that shows large log files and empties them, and automatic updates for the country database.
- **Desktop integration:** tray icon (StatusNotifierItem), handler for torrent files and magnet links, an animated header banner, adjustable window transparency (blurred by KDE's Better Blur).

## Requirements

On Arch-based distributions:

```bash
sudo pacman -S --needed aria2 python-gobject python-cairo gtk4 libadwaita mktorrent
```

GTK 4.16+ and libadwaita 1.6+ are needed. `mktorrent` is only needed for creating torrents.

## Running

```bash
./aria2gtk.py
```

`--hidden` starts it in the tray. Files or magnet links given on the command line are added.

To open `.torrent` files and magnet links with it, copy `io.github.aria2gtk.desktop` to `~/.local/share/applications/`. Edit its `Exec=` line to the full path of `aria2gtk.py`, then run:

```bash
update-desktop-database ~/.local/share/applications
```

## Where things are kept

`~/.local/share/aria2gtk/` holds:
- `settings.json`
- `session`: unfinished downloads
- `aria2.log`: trimmed automatically above 100 MB
- `torrents/`: copies of added torrents
- the country database

Your own `~/.aria2/aria2.conf` is read too. Options you set in Preferences take priority over it.

## Credits

IP geolocation by [DB-IP](https://db-ip.com), licensed under CC BY 4.0.

## License

GPL-3.0. See [LICENSE](LICENSE).
