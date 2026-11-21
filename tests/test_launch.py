import builtins
import sys

from music_agent import launch


def test_normalizing_never_imports_qt_or_the_desktop(tmp_path, monkeypatch):
    script = tmp_path / "normalizer.py"
    script.write_text("import sys\n\ndef main():\n    print(sys.argv[1])\n    print('stderr', file=sys.stderr)\n")
    report = tmp_path / "report.txt"
    monkeypatch.setattr(sys, "argv", ["music-agent", "--normalize", str(report), str(script), "folder"])
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    original_import = builtins.__import__

    def import_module(name, *args, **kwargs):
        assert name != "music_agent.desktop" and not name.startswith("PySide6")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_module)

    assert launch.main() == 0
    assert report.read_text() == "folder\nstderr\n"
