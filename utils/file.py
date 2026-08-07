from pathlib import Path


def save_or_create_file(
        file_path: Path,
        content: str | bytes = '',
        mode: str = 'w'
) -> None:

    if type(content) is str:
        encoding = 'utf-8'
    else:
        encoding = None

    file_path.parent.mkdir(parents=True, exist_ok=True)
    with open(
        file=file_path,
        mode=mode,
        encoding=encoding
    ) as f:
        f.write(content)


