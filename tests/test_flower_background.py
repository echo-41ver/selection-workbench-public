import pathlib,zipfile
import pytest
from selection_automation.flower_background import (
    CONTENT,FILENAME,PREF,RECOMMENDED,install_defaults,kernel_directory,startup_environment)


def vendor(tmp_path,version="153"):
    directory=tmp_path/"adspower_global/cwd_global"/f"flower_{version}"
    (directory/"defaults/pref").mkdir(parents=True)
    for name in ("flowerbrowser.exe","geckodriver.exe"):(directory/name).touch()
    with zipfile.ZipFile(directory/"omni.ja","w") as archive:
        archive.writestr(RECOMMENDED,f'const prefs = new Map([["{PREF}",false]]);')
    return directory


def test_install_exact_graphics_default_and_idempotently_verify(tmp_path):
    directory=vendor(tmp_path)
    unrelated=directory/"defaults/pref/channel-prefs.js";unrelated.write_text("original")
    first=install_defaults({},environment={"APPDATA":str(tmp_path)})
    path=directory/"defaults/pref"/FILENAME
    stamp=path.stat().st_mtime_ns
    second=install_defaults({},environment={"APPDATA":str(tmp_path)})
    assert path.read_text()==CONTENT and first["created"] and not second["created"]
    assert stamp==path.stat().st_mtime_ns and unrelated.read_text()=="original"
    assert first["applies_to"]=="NEW_BROWSER_STARTS_ONLY" and not first["existing_browsers_changed"]


def test_conflicting_owned_filename_is_not_overwritten(tmp_path):
    directory=vendor(tmp_path);target=directory/"defaults/pref"/FILENAME;target.write_text("foreign content")
    with pytest.raises(RuntimeError,match="CONFLICT"):
        install_defaults({},environment={"APPDATA":str(tmp_path)})
    assert target.read_text()=="foreign content"


def test_system_firefox_or_unverified_vendor_archive_is_refused(tmp_path):
    directory=vendor(tmp_path);(directory/"flowerbrowser.exe").unlink()
    with pytest.raises(RuntimeError,match="VENDOR_KERNEL"):
        install_defaults({},environment={"APPDATA":str(tmp_path)})
    assert not (directory/"defaults/pref"/FILENAME).exists()


def test_future_required_version_is_discovered_without_changing_older_kernel(tmp_path):
    old=vendor(tmp_path);new=vendor(tmp_path,"154")
    result=install_defaults({},environment={"APPDATA":str(tmp_path)},required={"type":"firefox","version":"154"})
    assert result["kernel_version"]=="154" and (new/"defaults/pref"/FILENAME).exists()
    assert not (old/"defaults/pref"/FILENAME).exists()


def test_explicit_vendor_root_handles_custom_kernel_installation(tmp_path):
    directory=vendor(tmp_path)
    assert kernel_directory({"adspower_kernel_root":str(directory.parent)},environment={})==directory


def test_first_adspower_launch_environment_does_not_mutate_callers_environment():
    original={"PATH":"test","MOZ_WINDOW_OCCLUSION":"1"}
    fresh=startup_environment(original)
    assert fresh=={"PATH":"test","MOZ_WINDOW_OCCLUSION":"0"}
    assert original["MOZ_WINDOW_OCCLUSION"]=="1"
