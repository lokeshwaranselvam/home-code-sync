import os
import json
import time
import base64
import hashlib
from pathlib import Path

import requests


# ============================================================
# CONFIGURATION
# ============================================================

ROOT = Path(__file__).resolve().parent.parent

CONFIG_FILE = ROOT / "agent" / "config.json"
STATE_FILE = ROOT / "agent" / "state.json"


with open(CONFIG_FILE, "r", encoding="utf-8") as f:
    config = json.load(f)


SYSTEM_ID = config["system_id"]
REPO_OWNER = config["repo_owner"]
REPO_NAME = config["repo_name"]
BRANCH = config.get("branch", "main")
POLL_SECONDS = config.get("poll_seconds", 2)


# ============================================================
# SYSTEM-SPECIFIC RULES
# ============================================================

if SYSTEM_ID == "S1":
    OWN_FOLDER = "C1"

elif SYSTEM_ID == "S2":
    OWN_FOLDER = "C2"

elif SYSTEM_ID == "S3":
    OWN_FOLDER = "C3"

else:
    raise ValueError(
        f"Invalid system_id: {SYSTEM_ID}. "
        f"Use S1, S2 or S3."
    )


# These are the ONLY folders this system is allowed
# to synchronize.
SYNC_FOLDERS = [
    "common",
    OWN_FOLDER,
]


# ============================================================
# GITHUB TOKEN
# ============================================================

TOKEN = os.environ.get("GITHUB_TOKEN")

if not TOKEN:
    raise RuntimeError(
        "GITHUB_TOKEN is not set.\n\n"
        "Run in PowerShell:\n"
        '$env:GITHUB_TOKEN="YOUR_TOKEN"'
    )


# ============================================================
# GITHUB API
# ============================================================

BASE_URL = (
    f"https://api.github.com/repos/"
    f"{REPO_OWNER}/{REPO_NAME}"
)

HEADERS = {
    "Accept": "application/vnd.github+json",
    "Authorization": f"Bearer {TOKEN}",
    "X-GitHub-Api-Version": "2022-11-28",
}


# ============================================================
# STATE
# ============================================================

def load_state():

    if not STATE_FILE.exists():
        return {}

    try:

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            return json.load(f)

    except Exception:

        return {}


def save_state(state):

    with open(
        STATE_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            state,
            f,
            indent=2
        )


state = load_state()


# ============================================================
# PATH HELPERS
# ============================================================

def is_sync_path(path):

    path = path.replace("\\", "/")

    for folder in SYNC_FOLDERS:

        folder = folder.replace("\\", "/")

        if (
            path == folder
            or path.startswith(folder + "/")
        ):

            return True

    return False


def get_local_files():

    files = {}

    for folder in SYNC_FOLDERS:

        folder_path = ROOT / folder

        if not folder_path.exists():
            continue

        for file_path in folder_path.rglob("*"):

            if not file_path.is_file():
                continue

            relative = file_path.relative_to(ROOT)

            relative = str(relative).replace(
                "\\",
                "/"
            )

            files[relative] = file_path

    return files


# ============================================================
# GIT SHA
# ============================================================

def calculate_git_sha(data):

    header = (
        f"blob {len(data)}\0"
    ).encode("utf-8")

    return hashlib.sha1(
        header + data
    ).hexdigest()


# ============================================================
# GITHUB REQUEST
# ============================================================

def github_get(url, **kwargs):

    response = requests.get(
        url,
        headers=HEADERS,
        timeout=20,
        **kwargs
    )

    if response.status_code != 200:

        raise RuntimeError(
            f"GitHub GET failed "
            f"{response.status_code}: "
            f"{response.text}"
        )

    return response.json()


# ============================================================
# GET REMOTE TREE
# ============================================================

def get_remote_tree():

    url = f"{BASE_URL}/git/trees/{BRANCH}"

    data = github_get(
        url,
        params={
            "recursive": "1"
        }
    )

    remote_files = {}

    for item in data.get("tree", []):

        if item.get("type") != "blob":
            continue

        path = item["path"]

        # IMPORTANT:
        # We only inspect files belonging to
        # common/ or this system's own folder.
        if not is_sync_path(path):
            continue

        remote_files[path] = item["sha"]

    return remote_files


# ============================================================
# DOWNLOAD REMOTE FILE
# ============================================================

def download_remote_file(path):

    url = (
        f"{BASE_URL}/contents/"
        f"{path}"
    )

    data = github_get(
        url,
        params={
            "ref": BRANCH
        }
    )

    content = base64.b64decode(
        data["content"].replace(
            "\n",
            ""
        )
    )

    return content, data["sha"]


# ============================================================
# UPLOAD FILE
# ============================================================

def upload_file(
    path,
    content,
    remote_sha=None
):

    url = (
        f"{BASE_URL}/contents/"
        f"{path}"
    )

    encoded = base64.b64encode(
        content
    ).decode("utf-8")

    payload = {
        "message": (
            f"[{SYSTEM_ID}] "
            f"Update {path}"
        ),
        "content": encoded,
        "branch": BRANCH,
    }

    if remote_sha:
        payload["sha"] = remote_sha

    response = requests.put(
        url,
        headers=HEADERS,
        json=payload,
        timeout=30
    )

    if response.status_code not in (
        200,
        201
    ):

        raise RuntimeError(
            f"GitHub upload failed "
            f"{response.status_code}: "
            f"{response.text}"
        )

    return response.json()


# ============================================================
# DELETE REMOTE FILE
# ============================================================

def delete_remote_file(
    path,
    remote_sha
):

    url = (
        f"{BASE_URL}/contents/"
        f"{path}"
    )

    payload = {
        "message": (
            f"[{SYSTEM_ID}] "
            f"Delete {path}"
        ),
        "sha": remote_sha,
        "branch": BRANCH,
    }

    response = requests.delete(
        url,
        headers=HEADERS,
        json=payload,
        timeout=30
    )

    if response.status_code != 200:

        raise RuntimeError(
            f"GitHub delete failed "
            f"{response.status_code}: "
            f"{response.text}"
        )


# ============================================================
# INITIALIZE STATE
# ============================================================

def initialize_state(remote_tree):

    global state

    if state:
        return

    print()
    print("Initializing sync state...")

    local_files = get_local_files()

    for path, local_path in local_files.items():

        try:

            content = local_path.read_bytes()

        except Exception:

            continue

        local_sha = calculate_git_sha(
            content
        )

        if path in remote_tree:

            remote_sha = remote_tree[path]

            if local_sha == remote_sha:

                state[path] = remote_sha

            else:

                print(
                    f"[INITIAL] Local file differs: "
                    f"{path}"
                )

                print(
                    "           Keeping local file."
                )

                # We mark the remote version as
                # the baseline. On the next loop,
                # the local difference will be pushed.
                state[path] = remote_sha

        else:

            # New local file
            state[path] = None

    save_state(state)


# ============================================================
# PUSH LOCAL CHANGES
# ============================================================

def push_local_changes(remote_tree):

    global state

    local_files = get_local_files()

    # --------------------------------------------------------
    # NEW / MODIFIED FILES
    # --------------------------------------------------------

    for path, local_path in local_files.items():

        try:

            content = local_path.read_bytes()

        except Exception:

            continue

        local_sha = calculate_git_sha(
            content
        )

        previous_sha = state.get(path)

        remote_sha = remote_tree.get(path)

        # No local change
        if local_sha == previous_sha:
            continue

        # ----------------------------------------------------
        # CONFLICT DETECTION
        # ----------------------------------------------------

        if (
            previous_sha is not None
            and remote_sha is not None
            and remote_sha != previous_sha
        ):

            print()
            print(
                f"[CONFLICT] {path}"
            )

            print(
                "Local and remote were both changed."
            )

            print(
                "Automatic overwrite prevented."
            )

            continue

        # ----------------------------------------------------
        # PUSH
        # ----------------------------------------------------

        print(
            f"[PUSH] {path}"
        )

        try:

            result = upload_file(
                path,
                content,
                remote_sha
            )

            new_sha = (
                result
                .get("content", {})
                .get("sha")
            )

            if new_sha:

                state[path] = new_sha

                print(
                    f"[PUSHED] {path}"
                )

        except Exception as e:

            print(
                f"[PUSH ERROR] "
                f"{path}: {e}"
            )

    # --------------------------------------------------------
    # DELETED LOCAL FILES
    # --------------------------------------------------------

    for path in list(state.keys()):

        if not is_sync_path(path):
            continue

        if path in local_files:
            continue

        previous_sha = state.get(path)

        if previous_sha is None:
            continue

        remote_sha = remote_tree.get(path)

        # Already deleted remotely
        if remote_sha is None:

            state[path] = None

            continue

        # Remote changed after our last sync
        if remote_sha != previous_sha:

            print()
            print(
                f"[CONFLICT] "
                f"Local deletion: {path}"
            )

            continue

        print(
            f"[DELETE] {path}"
        )

        try:

            delete_remote_file(
                path,
                remote_sha
            )

            state[path] = None

        except Exception as e:

            print(
                f"[DELETE ERROR] "
                f"{path}: {e}"
            )

    save_state(state)


# ============================================================
# PULL REMOTE CHANGES
# ============================================================

def pull_remote_changes(remote_tree):

    global state

    local_files = get_local_files()

    # --------------------------------------------------------
    # REMOTE NEW / MODIFIED FILES
    # --------------------------------------------------------

    for path, remote_sha in remote_tree.items():

        previous_sha = state.get(path)

        local_path = ROOT / path

        # ----------------------------------------------------
        # FILE NOT KNOWN TO AGENT
        # ----------------------------------------------------

        if path not in state:

            try:

                content, actual_sha = (
                    download_remote_file(path)
                )

                local_path.parent.mkdir(
                    parents=True,
                    exist_ok=True
                )

                local_path.write_bytes(
                    content
                )

                state[path] = actual_sha

                print(
                    f"[PULL NEW] {path}"
                )

            except Exception as e:

                print(
                    f"[PULL ERROR] "
                    f"{path}: {e}"
                )

            continue

        # ----------------------------------------------------
        # NO REMOTE CHANGE
        # ----------------------------------------------------

        if remote_sha == previous_sha:
            continue

        # ----------------------------------------------------
        # REMOTE CHANGED
        # ----------------------------------------------------

        if local_path.exists():

            try:

                local_content = (
                    local_path.read_bytes()
                )

                local_sha = calculate_git_sha(
                    local_content
                )

            except Exception:

                continue

            # Local has NOT changed.
            # Safe to replace with remote.
            if local_sha == previous_sha:

                try:

                    content, actual_sha = (
                        download_remote_file(path)
                    )

                    local_path.parent.mkdir(
                        parents=True,
                        exist_ok=True
                    )

                    local_path.write_bytes(
                        content
                    )

                    state[path] = actual_sha

                    print(
                        f"[PULL] {path}"
                    )

                except Exception as e:

                    print(
                        f"[PULL ERROR] "
                        f"{path}: {e}"
                    )

            else:

                print()
                print(
                    f"[CONFLICT] {path}"
                )

                print(
                    "Local changes detected."
                )

                print(
                    "Remote changes detected."
                )

                print(
                    "Automatic overwrite prevented."
                )

        else:

            try:

                content, actual_sha = (
                    download_remote_file(path)
                )

                local_path.parent.mkdir(
                    parents=True,
                    exist_ok=True
                )

                local_path.write_bytes(
                    content
                )

                state[path] = actual_sha

                print(
                    f"[PULL] {path}"
                )

            except Exception as e:

                print(
                    f"[PULL ERROR] "
                    f"{path}: {e}"
                )

    # --------------------------------------------------------
    # REMOTE DELETIONS
    # --------------------------------------------------------

    for path in list(state.keys()):

        if not is_sync_path(path):
            continue

        previous_sha = state.get(path)

        if previous_sha is None:
            continue

        if path in remote_tree:
            continue

        local_path = ROOT / path

        if not local_path.exists():

            state[path] = None

            continue

        try:

            local_content = (
                local_path.read_bytes()
            )

            local_sha = calculate_git_sha(
                local_content
            )

        except Exception:

            continue

        # Local file has not changed.
        # Safe to delete because remote deleted it.
        if local_sha == previous_sha:

            try:

                local_path.unlink()

                state[path] = None

                print(
                    f"[REMOTE DELETE] {path}"
                )

            except Exception as e:

                print(
                    f"[DELETE LOCAL ERROR] "
                    f"{path}: {e}"
                )

        else:

            print()
            print(
                f"[CONFLICT] "
                f"Remote deleted {path}"
            )

            print(
                "Local changes preserved."
            )

    save_state(state)


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 60)
    print("HOME CODE SYNC AGENT")
    print("=" * 60)

    print(
        f"System       : {SYSTEM_ID}"
    )

    print(
        f"Private folder: {OWN_FOLDER}"
    )

    print(
        f"Shared folder : common"
    )

    print(
        f"Repository   : "
        f"{REPO_OWNER}/{REPO_NAME}"
    )

    print(
        f"Branch       : {BRANCH}"
    )

    print(
        f"Poll time    : "
        f"{POLL_SECONDS} seconds"
    )

    print("=" * 60)

    while True:

        try:

            remote_tree = get_remote_tree()

            initialize_state(
                remote_tree
            )

            # Push only:
            # common + own system folder
            push_local_changes(
                remote_tree
            )

            # Get newest GitHub state
            remote_tree = get_remote_tree()

            # Pull only:
            # common + own system folder
            pull_remote_changes(
                remote_tree
            )

        except KeyboardInterrupt:

            print()
            print(
                "HOME CODE SYNC STOPPED."
            )

            break

        except Exception as e:

            print()
            print(
                f"[ERROR] {e}"
            )

        time.sleep(
            POLL_SECONDS
        )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    main()