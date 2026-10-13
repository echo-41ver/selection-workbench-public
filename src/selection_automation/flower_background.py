"""Application-owned graphics preference for newly started vendor FlowerBrowser."""
import os
from pathlib import Path
import zipfile
from .adspower_kernel import REQUIRED_KERNEL

PREF = "widget.windows.window_occlusion_tracking.enabled"
FILENAME = "selection-background-execution.js"
CONTENT = 'pref("widget.windows.window_occlusion_tracking.enabled", false);\n'
RECOMMENDED = "chrome/remote/content/shared/RecommendedPreferences.sys.mjs"


def kernel_directory(company, *, environment=None, required=REQUIRED_KERNEL):
    environment=os.environ if environment is None else environment
    if required.get("type")!="firefox" or not str(required.get("version","")).isdigit():
        raise RuntimeError("BACKGROUND_KERNEL_REQUIREMENT_INVALID")
    parent=company.get("adspower_kernel_root")
    if parent:
        root=Path(parent)
    else:
        roaming=environment.get("APPDATA")
        if not roaming:raise RuntimeError("BACKGROUND_KERNEL_LOCATION_UNAVAILABLE")
        root=Path(roaming)/"adspower_global/cwd_global"
    directory=root/f"flower_{required['version']}"
    # Exact vendor engine/version only: never discover or change a system Firefox.
    if not all((directory/name).is_file() for name in ("flowerbrowser.exe","geckodriver.exe","omni.ja")):
        raise RuntimeError("BACKGROUND_VENDOR_KERNEL_UNVERIFIED")
    try:
        with zipfile.ZipFile(directory/"omni.ja") as archive:
            source=archive.read(RECOMMENDED).decode("utf-8")
    except (OSError,KeyError,ValueError,zipfile.BadZipFile) as error:
        raise RuntimeError("BACKGROUND_RECOMMENDED_PREF_UNVERIFIED") from error
    if PREF not in source:
        raise RuntimeError("BACKGROUND_RECOMMENDED_PREF_UNVERIFIED")
    return directory


def install_defaults(company, *, environment=None, required=REQUIRED_KERNEL):
    directory=kernel_directory(company,environment=environment,required=required)
    prefs=directory/"defaults/pref"
    if not prefs.is_dir():raise RuntimeError("BACKGROUND_VENDOR_PREF_DIRECTORY_UNVERIFIED")
    target=prefs/FILENAME
    # Refuse symlinks and conflicting pre-existing content. Other pref files and
    # all profile data remain outside this helper's write scope.
    if target.is_symlink():raise RuntimeError("BACKGROUND_OWNED_PREF_CONFLICT")
    created=False
    try:
        with target.open("x",encoding="utf-8",newline="\n") as output:
            output.write(CONTENT)
        created=True
    except FileExistsError:
        if target.read_text("utf-8")!=CONTENT:
            raise RuntimeError("BACKGROUND_OWNED_PREF_CONFLICT")
    return {"kernel_version":str(required["version"]),"preference":PREF,"value":False,
            "file":str(target),"created":created,"applies_to":"NEW_BROWSER_STARTS_ONLY",
            "existing_browsers_changed":False}


def startup_environment(environment):
    return {**environment,"MOZ_WINDOW_OCCLUSION":"0"}
