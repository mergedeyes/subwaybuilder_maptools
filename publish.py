import os
import re
import json
import base64
import hashlib
from datetime import datetime
from github import Github
from github.GithubException import GithubException
from dotenv import load_dotenv

# Load variables from .env file
load_dotenv()

# --- Configuration ---
TOKEN = os.getenv("GITHUB_TOKEN")
CITY_CODE = os.getenv("CITY_CODE").upper()

REPO_NAME = "mergedeyes/subwaybuilder_maps"
TARGET_BRANCH = "main"

# File paths
LOCAL_ZIP_PATH = f"./{os.getenv("OUTPUT_DIR")}/{CITY_CODE}/{CITY_CODE}.zip"
CONFIG_PATH = f"./{os.getenv("RAW_BASE_DIR")}/{CITY_CODE}/config.json"
REPO_JSON_PATH = f"releases/{CITY_CODE}_update.json"

def calculate_sha256(file_path):
    """Calculate the SHA-256 checksum of a local file."""
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()

def get_next_global_version(repo, is_new_map):
    """Fetches the latest release tag and increments based on map status."""
    try:
        latest_release = repo.get_latest_release()
        latest_tag = latest_release.tag_name
        
        # Match standard semantic versioning (e.g., v1.0.3)
        match = re.match(r"^v?(\d+)\.(\d+)\.(\d+)$", latest_tag)
        if match:
            major, minor, patch = map(int, match.groups())
            
            if is_new_map:
                # Bump Major version, reset minor and patch
                return f"v{major + 1}.0.0"
            else:
                # Bump Patch version
                return f"v{major}.{minor}.{patch + 1}"
        else:
            print(f"Warning: Latest tag '{latest_tag}' is not standard SemVer. Defaulting to v1.0.0.")
            return "v1.0.0"
            
    except GithubException as e:
        if e.status == 404: # No releases exist yet in the repo
            return "v1.0.0"
        raise

def main():
    if not TOKEN:
        raise ValueError("GITHUB_TOKEN environment variable is not set.")
    if not CITY_CODE:
        raise ValueError("CITY_CODE is not defined in the .env file.")

    # 1. Read local config.json to get the IN-GAME map version
    print(f"Reading configuration from {CONFIG_PATH}...")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config_data = json.load(f)
    
    in_game_version = config_data.get("version")
    if not in_game_version:
        raise ValueError(f"Version not found in {CONFIG_PATH}")

    # Initialize GitHub client
    g = github.Auth.Token(TOKEN)
    repo = g.get_repo(REPO_NAME)

    # 2. Check if the map is new or updated by looking for its JSON file
    print(f"Checking status of {REPO_JSON_PATH} on branch {TARGET_BRANCH}...")
    try:
        json_contents = repo.get_contents(REPO_JSON_PATH, ref=TARGET_BRANCH)
        decoded_json_str = base64.b64decode(json_contents.content).decode('utf-8')
        update_json_data = json.loads(decoded_json_str)
        is_new_map = False
        print(f"Map exists. This is an UPDATE (Patch bump).")
    except GithubException as e:
        if e.status == 404:
            print(f"Map JSON not found. This is a NEW MAP (Major bump).")
            update_json_data = {"schema_version": 1, "versions": []}
            json_contents = None
            is_new_map = True
        else:
            raise

    # 3. Determine Global Repo Version
    global_tag = get_next_global_version(repo, is_new_map)
    release_name = f"Repository Update {global_tag} ({CITY_CODE})"
    
    # 4. Calculate SHA-256 of the local zip file
    print(f"Calculating SHA-256 for {LOCAL_ZIP_PATH}...")
    file_sha256 = calculate_sha256(LOCAL_ZIP_PATH)

    # 5. Create the Release
    print(f"Creating global repository release: {global_tag}...")
    release = repo.create_git_release(
        tag=global_tag,
        name=release_name,
        message=f"Automated global release triggered by an update to the {CITY_CODE} map (In-game version: {in_game_version}).",
        target_commitish=TARGET_BRANCH
    )

    # 6. Upload the Zip File as a Release Asset
    file_name = os.path.basename(LOCAL_ZIP_PATH)
    print(f"Uploading {file_name} to release {global_tag}...")
    
    asset = release.upload_asset(
        path=LOCAL_ZIP_PATH,
        name=file_name,
        content_type="application/zip"
    )
    print(f"Asset uploaded successfully: {asset.browser_download_url}")

    # 7. Construct and commit the updated JSON file
    current_date = datetime.now().strftime("%Y-%m-%d")
    new_version_entry = {
        "version": in_game_version,           # From config.json
        "game_version": ">=1.4.5",            
        "date": current_date,
        "changelog": f"Map {CITY_CODE} update.",
        "download": asset.browser_download_url, # Points to the new vX.Y.Z global tag
        "sha256": file_sha256
    }

    # Prepend the new version to the top of the array
    update_json_data.setdefault("versions", []).insert(0, new_version_entry)

    print(f"Committing updated {REPO_JSON_PATH}...")
    updated_json_str = json.dumps(update_json_data, indent=2, ensure_ascii=False)
    
    commit_message = f"Update {CITY_CODE} metadata pointing to global {global_tag}"
    
    if not is_new_map:
        repo.update_file(
            path=json_contents.path,
            message=commit_message,
            content=updated_json_str,
            sha=json_contents.sha,
            branch=TARGET_BRANCH
        )
    else:
        repo.create_file(
            path=REPO_JSON_PATH,
            message=commit_message,
            content=updated_json_str,
            branch=TARGET_BRANCH
        )
        
    print("Process complete.")

if __name__ == "__main__":
    main()