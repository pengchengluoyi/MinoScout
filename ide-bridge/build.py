"""Build the companion against a locally installed JetBrains 2026.1 SDK."""
import argparse
import io
from pathlib import Path
import subprocess
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ide-home", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    contents = arguments.ide_home.resolve()
    if (contents / "Contents").is_dir():
        contents /= "Contents"
    compiler = contents / "jbr/Contents/Home/bin/javac"
    if not compiler.is_file():
        compiler = contents / "jbr/bin/javac"
    root = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix="mino-ide-build-") as temporary:
        classes = Path(temporary) / "classes"
        classes.mkdir()
        classpath = ":".join(str(path) for path in sorted((contents / "lib").glob("*.jar")))
        classpath += ":" + ":".join(str(path) for path in sorted((contents / "plugins/mcpserver/lib").glob("*.jar")))
        subprocess.run([str(compiler), "--release", "21", "-encoding", "UTF-8", "-cp", classpath,
                        "-d", str(classes), *map(str, (root / "src").rglob("*.java"))], check=True)
        jar = io.BytesIO()
        with ZipFile(jar, "w", compression=ZIP_DEFLATED) as archive:
            for path in sorted(classes.rglob("*.class")):
                archive.write(path, str(path.relative_to(classes)))
            for path in sorted((root / "resources").rglob("*")):
                if path.is_file():
                    archive.write(path, str(path.relative_to(root / "resources")))
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        if arguments.output.is_symlink() or arguments.output.exists():
            raise FileExistsError("构建输出已存在；请使用新输出路径")
        with ZipFile(arguments.output, "x", compression=ZIP_DEFLATED) as archive:
            archive.writestr("mino-ide-runtime/lib/mino-ide-runtime.jar", jar.getvalue())
        print(arguments.output)


if __name__ == "__main__":
    main()
