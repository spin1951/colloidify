# colloidify

Gives app icons that the [Colloid icon theme](https://github.com/vinceliuice/Colloid-icon-theme) doesn't include a matching Colloid-style icon.

- Icons with a background get it extended into the Colloid rounded tile.
- Bare logos are placed on a white tile.
- Icons are written to a user overlay in `~/.local/share/icons`, so system files are never touched.

## Requirements

    sudo pacman -S python-pillow librsvg

## Usage

    ./colloidify.py --all --dry-run     # list apps Colloid doesn't cover
    ./colloidify.py --all               # theme all of them
    ./colloidify.py "App Name"          # theme one app
    ./colloidify.py --restore "App Name"
    ./colloidify.py --ignore 'brave-*'  # blacklist apps
