"""测试 Linux 自启动 .desktop 文件生成 / 启用 / 禁用。

针对 src/daobidao/backends/autostart_linux.py。

测试隔离策略:用 monkeypatch 把 AUTOSTART_DIR / AUTOSTART_FILE 指向 tmp_path,
绝不允许触碰真实的 ~/.config/autostart/。
"""

from daobidao.backends import autostart_linux as al


def test_load_desktop_template_contains_required_fields():
    """模板里必须包含 [Desktop Entry] / Exec / Name 三个关键字段。"""
    text = al._load_desktop_template()
    assert "[Desktop Entry]" in text
    assert "Exec=daobidao" in text
    assert "Name=" in text
    assert "Type=Application" in text


def test_set_autostart_true_writes_template(tmp_path, monkeypatch):
    target_dir = tmp_path / "autostart"
    target_file = target_dir / "daobidao.desktop"
    monkeypatch.setattr(al, "AUTOSTART_DIR", str(target_dir))
    monkeypatch.setattr(al, "AUTOSTART_FILE", str(target_file))

    assert not al.is_autostart_enabled()
    al.set_autostart(True)
    assert target_file.is_file()
    assert al.is_autostart_enabled()
    # 写入的内容就是模板
    assert (
        target_file.read_text(encoding="utf-8") == al._load_desktop_template()
    )


def test_set_autostart_false_removes_file(tmp_path, monkeypatch):
    target_dir = tmp_path / "autostart"
    target_dir.mkdir()
    target_file = target_dir / "daobidao.desktop"
    target_file.write_text("[Desktop Entry]\n", encoding="utf-8")

    monkeypatch.setattr(al, "AUTOSTART_DIR", str(target_dir))
    monkeypatch.setattr(al, "AUTOSTART_FILE", str(target_file))

    assert al.is_autostart_enabled()
    al.set_autostart(False)
    assert not target_file.exists()
    assert not al.is_autostart_enabled()


def test_set_autostart_false_idempotent(tmp_path, monkeypatch):
    """重复禁用不报错。"""
    target_dir = tmp_path / "autostart"
    target_file = target_dir / "daobidao.desktop"
    monkeypatch.setattr(al, "AUTOSTART_DIR", str(target_dir))
    monkeypatch.setattr(al, "AUTOSTART_FILE", str(target_file))

    al.set_autostart(False)  # 不抛
    assert not target_file.exists()
