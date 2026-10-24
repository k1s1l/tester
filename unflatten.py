"""Restores the project folders from flat file names (for uploads through the GitHub web page).

    app--handlers--order.py  ->  app/handlers/order.py
    dot--gitignore           ->  .gitignore

Runs automatically during the Docker build (see Dockerfile). Files that are already inside
folders are left untouched, so it is safe to run more than once.
Local run: python unflatten.py
"""

from pathlib import Path

SEP = "--"
ROOT = Path(__file__).resolve().parent


def main() -> None:
    moved = 0
    for file in sorted(ROOT.iterdir()):
        if not file.is_file():
            continue
        name = file.name
        if name.startswith("dot" + SEP):
            target = ROOT / ("." + name[len("dot" + SEP):])
        elif SEP in name:
            target = ROOT.joinpath(*name.split(SEP))
        else:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        file.replace(target)
        moved += 1
    print(f"unflatten: restored {moved} files into folders")


if __name__ == "__main__":
    main()
