"""Filesystem policy shared by Blue/Yellow Workers and the automatic updater.

Only non-secret diagnostic files are public-readable, never public-writable.
Credentials, updater transactions and isolated Test Worker state stay private.
"""

import stat

DIAGNOSTIC_FILE_MODE = 0o644
DIAGNOSTIC_DIR_MODE = 0o755
WORKER_UMASK = 0o022
PRIVATE_FILE_MODE = 0o600
PRIVATE_DIR_MODE = 0o700
PRIVATE_UMASK = 0o077
EXECUTABLE_FILE_MODE = 0o755
TRAVERSE_DIR_MODE = 0o711
BROKER_SOCKET_MODE = 0o666
NON_OWNER_WRITE_BITS = stat.S_IWGRP | stat.S_IWOTH
EXECUTE_BITS = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH


if __name__ == "__main__":
    # Fixed, whitespace-delimited values for install_auto_update.sh; no eval.
    print(
        *(
            f"{mode:04o}"
            for mode in (
                PRIVATE_FILE_MODE,
                PRIVATE_DIR_MODE,
                DIAGNOSTIC_FILE_MODE,
                DIAGNOSTIC_DIR_MODE,
                EXECUTABLE_FILE_MODE,
                PRIVATE_UMASK,
            )
        )
    )
