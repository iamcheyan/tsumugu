"""
NAS Mount Manager - Mounts SMB/NFS shares to local directories.
"""
import os
import subprocess
import tempfile
import logging
import getpass

logger = logging.getLogger(__name__)

MOUNT_BASE = "/tmp/nas_mnt"
CREDS_DIR = "/tmp/nas_creds"


def _get_mount_point(share: str) -> str:
    """Generate a local mount point path from the share name."""
    safe_name = share.replace("/", "_").replace("\\", "_").strip("_") or "default"
    return os.path.join(MOUNT_BASE, safe_name)


def _write_credentials(username: str, password: str) -> str:
    """Write SMB credentials to a temporary file. Returns the file path."""
    os.makedirs(CREDS_DIR, exist_ok=True)
    creds_path = os.path.join(CREDS_DIR, "smb_credentials")
    with open(creds_path, "w") as f:
        f.write(f"username={username}\n")
        f.write(f"password={password}\n")
    os.chmod(creds_path, 0o600)
    return creds_path


def mount_nas(
    address: str,
    protocol: str,
    share: str,
    username: str = "",
    password: str = "",
    port: str = "",
) -> dict:
    """
    Mount a NAS share to a local directory.
    Returns {"success": bool, "mount_point": str, "message": str}
    """
    if not address or not share:
        return {"success": False, "mount_point": "", "message": "Address and share are required"}

    mount_point = _get_mount_point(share)
    os.makedirs(mount_point, exist_ok=True)

    if os.path.ismount(mount_point):
        return {"success": True, "mount_point": mount_point, "message": f"Already mounted at {mount_point}"}

    try:
        if protocol == "nfs":
            return _mount_nfs(address, share, mount_point, port or "2049")
        else:
            return _mount_smb(address, share, mount_point, username, password, port or "445")
    except Exception as e:
        logger.error(f"Mount failed: {e}")
        return {"success": False, "mount_point": mount_point, "message": f"Mount failed: {str(e)}"}


def _run(cmd: list, timeout: int = 15) -> subprocess.CompletedProcess:
    """Run a command with sudo."""
    sudo_cmd = ["sudo"] + cmd
    logger.info(f"Running: {' '.join(sudo_cmd)}")
    return subprocess.run(sudo_cmd, capture_output=True, text=True, timeout=timeout)


def _get_uid_gid() -> tuple:
    """Get the current user's UID and GID."""
    try:
        import pwd
        user = getpass.getuser()
        pw = pwd.getpwnam(user)
        return pw.pw_uid, pw.pw_gid
    except Exception:
        return 1000, 1000  # fallback


def _mount_smb(
    address: str, share: str, mount_point: str,
    username: str, password: str, port: str
) -> dict:
    """Mount an SMB/CIFS share using a credentials file or guest access."""
    source = f"//{address}/{share}"

    uid, gid = _get_uid_gid()
    opts = [f"port={port}", f"uid={uid}", f"gid={gid}", "file_mode=0664", "dir_mode=0775"]

    if username and password:
        creds_path = _write_credentials(username, password)
        opts.append(f"credentials={creds_path}")
    elif username:
        opts.append(f"username={username}")
    else:
        opts.append("guest")

    opts.extend(["vers=3.0", "nodev", "nosuid"])

    cmd = ["mount", "-t", "cifs", source, mount_point, "-o", ",".join(opts)]
    result = _run(cmd)

    if result.returncode == 0:
        return {"success": True, "mount_point": mount_point, "message": f"Mounted {source}"}
    else:
        error = result.stderr.strip() or result.stdout.strip()
        return {"success": False, "mount_point": mount_point, "message": f"Mount failed: {error}"}


def _mount_nfs(
    address: str, share: str, mount_point: str, port: str
) -> dict:
    """Mount an NFS share."""
    source = f"{address}:/{share}"

    cmd = ["mount", "-t", "nfs", "-o", f"port={port},nfsvers=3,tcp,soft,timeo=10", source, mount_point]
    result = _run(cmd)

    if result.returncode == 0:
        return {"success": True, "mount_point": mount_point, "message": f"Mounted {source}"}
    else:
        error = result.stderr.strip() or result.stdout.strip()
        return {"success": False, "mount_point": mount_point, "message": f"Mount failed: {error}"}


def unmount_nas(share: str) -> dict:
    """Unmount a NAS share."""
    mount_point = _get_mount_point(share)

    if not os.path.ismount(mount_point):
        return {"success": True, "message": "Not currently mounted"}

    result = _run(["umount", mount_point])
    if result.returncode == 0:
        return {"success": True, "message": f"Unmounted {mount_point}"}
    else:
        return {"success": False, "message": f"Unmount failed: {result.stderr.strip()}"}


def get_mount_status(share: str) -> dict:
    """Check if a share is currently mounted."""
    mount_point = _get_mount_point(share)
    return {
        "mounted": os.path.ismount(mount_point),
        "mount_point": mount_point,
        "exists": os.path.isdir(mount_point),
    }
