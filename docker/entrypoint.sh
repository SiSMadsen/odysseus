#!/bin/sh
# Entrypoint that fixes the #1 self-host footgun: a Docker container
# that runs as root writes root-owned files into bind-mounted host
# volumes, and the host user (or a non-root service user) then can't
# update them — silently breaking skill extraction, prefs saves, mail
# attachments, etc.
#
# Standard PUID/PGID pattern: pick the UID/GID we should drop to,
# chown the writable bind-mounts so existing root-owned content gets
# repaired on every start (idempotent), then exec the real command
# as that user via gosu.
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
GOSU_BIN="$(command -v gosu)"
PYTHON_BIN="$(command -v python)"

# Reuse an existing matching group/user if the host's UID/GID already
# corresponds to one in /etc/passwd (e.g. when the image is rebuilt
# and "odysseus" already exists at the same id). Otherwise create.
if ! getent group "$PGID" >/dev/null 2>&1; then
    groupadd -g "$PGID" odysseus
fi
if ! getent passwd "$PUID" >/dev/null 2>&1; then
    useradd -u "$PUID" -g "$PGID" -M -s /bin/sh -d /app odysseus
fi

ODY_USER="$(getent passwd "$PUID" | cut -d: -f1)"
[ -z "$ODY_USER" ] && ODY_USER=odysseus

# Docker-socket group plumbing for the explicit host-Docker overlay. When
# opted in, the socket is owned by root:<host docker gid>. Add the app user
# to that group and later call gosu by username so supplementary groups are
# retained.
DOCKER_SOCK="${DOCKER_SOCK:-/var/run/docker.sock}"
if [ "${ODYSSEUS_ENABLE_HOST_DOCKER:-}" = "true" ] && [ -S "$DOCKER_SOCK" ]; then
    SOCK_GID="$(stat -c '%g' "$DOCKER_SOCK" 2>/dev/null || echo '')"
    if [ -n "$SOCK_GID" ] && [ "$SOCK_GID" != "0" ]; then
        if ! getent group "$SOCK_GID" >/dev/null 2>&1; then
            groupadd -g "$SOCK_GID" docker_host || true
        fi
        SOCK_GROUP="$(getent group "$SOCK_GID" | cut -d: -f1)"
        if [ -n "$SOCK_GROUP" ]; then
            usermod -aG "$SOCK_GROUP" "$ODY_USER" 2>/dev/null || true
        fi
    fi
fi

mount_root_for() {
    awk -v target="$1" '$5 == target { print $4; exit }' /proc/self/mountinfo 2>/dev/null || true
}

is_broad_mount_root() {
    case "$1" in
        /|/home|/srv|/var|/usr|/opt|/tmp|/mnt|/media)
            return 0
            ;;
    esac
    return 1
}

repair_tree_ownership() {
    dir="$1"
    if [ -d "$dir" ]; then
        find "$dir" -xdev -not -uid "$PUID" -print0 2>/dev/null \
            | xargs -0 -r chown "$PUID:$PGID" 2>/dev/null || true
    fi
}

repair_app_tree_ownership() {
    if [ -d /app ]; then
        find /app -xdev \
            \( -path /app/data -o -path /app/logs -o -path /app/.ssh -o -path /app/.cache -o -path /app/.local \) -prune \
            -o -not -uid "$PUID" -print0 2>/dev/null \
            | xargs -0 -r chown "$PUID:$PGID" 2>/dev/null || true
    fi
}

repair_bind_mount_ownership() {
    dir="$1"
    if [ ! -d "$dir" ]; then
        return
    fi

    mount_root="$(mount_root_for "$dir")"
    if is_broad_mount_root "$mount_root"; then
        echo "Skipping recursive ownership repair for $dir because it maps to broad host path $mount_root" >&2
        chown "$PUID:$PGID" "$dir" 2>/dev/null || true
        return
    fi

    repair_tree_ownership "$dir"
}

# Repair image-owned writable paths without walking into bind-mounted host
# trees, then repair the app-owned mount roots separately.
repair_app_tree_ownership
# Docker creates the parent of the HuggingFace bind mount as root before this
# entrypoint runs. Repair only the parent directory itself so app-user caches
# such as /app/.cache/vllm and /app/.cache/flashinfer can be created without
# recursively walking the mounted model cache.
chown "$PUID:$PGID" /app/.cache 2>/dev/null || true
# The Hugging Face cache can contain hundreds of gigabytes and is a nested
# mount with its own ownership contract. Repair its mount root so new cache
# entries are writable, but never traverse or rewrite existing model files.
chown "$PUID:$PGID" /app/.cache/huggingface 2>/dev/null || true
for dir in /app/data /app/logs /app/.ssh /app/.local; do
    repair_bind_mount_ownership "$dir"
done

# ── Phase 5a (docs/creator-plan.md): a separate user for the agent's tools ──
# The agent's bash/python run as TOOL_USER, not as the app user, so they
# can't read the app's key, database, settings or memory, or reach the host
# helpers (which accept only the app user's uid). The single sudo rule lets
# the app user run commands as TOOL_USER and nothing else; TOOL_USER gets no
# sudo rights. The app finds the tool user through ODYSSEUS_TOOL_USER.
# Set ODYSSEUS_TOOL_USER_ENABLED=false to keep the old behaviour.
TOOL_USER="${ODYSSEUS_TOOL_USER:-odytools}"
TOOL_UID="${ODYSSEUS_TOOL_UID:-1001}"
TOOL_GROUP="${ODYSSEUS_TOOL_GROUP:-odyshare}"
WORKSPACE_DIR=/app/data/agent_workspace
unset ODYSSEUS_TOOL_USER ODYSSEUS_TOOL_GROUP
if [ "${ODYSSEUS_TOOL_USER_ENABLED:-true}" = "true" ] && command -v sudo >/dev/null 2>&1; then
    if [ "$TOOL_UID" = "$PUID" ]; then
        echo "entrypoint: ODYSSEUS_TOOL_UID must differ from PUID ($PUID); tool user not set up" >&2
    else
        # Every step tolerates failure (this script runs with set -e): a
        # problem here must never stop Odysseus from starting. It then runs
        # tools the old way, and says so in the log.
        tool_ok=true
        getent group "$TOOL_GROUP" >/dev/null 2>&1 || groupadd -r "$TOOL_GROUP" || tool_ok=false
        if ! getent passwd "$TOOL_USER" >/dev/null 2>&1; then
            useradd -u "$TOOL_UID" -U -m -d "/home/$TOOL_USER" -s /bin/bash "$TOOL_USER" || tool_ok=false
        fi
        usermod -aG "$TOOL_GROUP" "$TOOL_USER" 2>/dev/null || tool_ok=false
        usermod -aG "$TOOL_GROUP" "$ODY_USER" 2>/dev/null || true
        mkdir -p "/home/$TOOL_USER" && chown "$TOOL_USER:" "/home/$TOOL_USER" \
            && chmod 0700 "/home/$TOOL_USER" || tool_ok=false

        SUDOERS=/etc/sudoers.d/odysseus-tools
        {
            echo "# Written by docker/entrypoint.sh (Phase 5a). The app user may run"
            echo "# commands as the tool user, with a clean environment; nothing else."
            echo "Defaults:$ODY_USER !requiretty, env_reset, !use_pty, !lecture"
            echo "$ODY_USER ALL=($TOOL_USER) NOPASSWD: ALL"
        } > "$SUDOERS.tmp"
        chmod 0440 "$SUDOERS.tmp" || tool_ok=false
        if [ "$tool_ok" = true ] && visudo -cf "$SUDOERS.tmp" >/dev/null 2>&1; then
            mv "$SUDOERS.tmp" "$SUDOERS"
            export ODYSSEUS_TOOL_USER="$TOOL_USER"
            export ODYSSEUS_TOOL_GROUP="$TOOL_GROUP"
        else
            rm -f "$SUDOERS.tmp" "$SUDOERS"
            echo "entrypoint: WARNING: setting up the tool user failed; the agent's tools run as $ODY_USER (the old way)" >&2
        fi

        # Nothing under data/ is readable by "other" users (the tool user is
        # one), except passing through data/ itself to reach the shared work
        # folder. Symlinks are left alone. Skipped for a broad host mount.
        data_root="$(mount_root_for /app/data)"
        if [ -d /app/data ] && ! is_broad_mount_root "$data_root"; then
            find /app/data -xdev ! -type l \( -perm -o=r -o -perm -o=w -o -perm -o=x \) \
                -exec chmod o-rwx {} + 2>/dev/null || true
            chmod o+x /app/data || true
        fi

        # The work folder is shared through the group: the app and the tool
        # user can both read and write everything in it, now and later.
        mkdir -p "$WORKSPACE_DIR" || true
        chown "$PUID:$TOOL_GROUP" "$WORKSPACE_DIR" 2>/dev/null || true
        chmod 2770 "$WORKSPACE_DIR" 2>/dev/null || true
        chgrp -R "$TOOL_GROUP" "$WORKSPACE_DIR" 2>/dev/null || true
        chmod -R g+rwX "$WORKSPACE_DIR" 2>/dev/null || true
        if command -v setfacl >/dev/null 2>&1; then
            setfacl -R -m "g:$TOOL_GROUP:rwX" "$WORKSPACE_DIR" 2>/dev/null || true
            setfacl -R -d -m "g:$TOOL_GROUP:rwX" "$WORKSPACE_DIR" 2>/dev/null || true
        fi
    fi
fi

# Cookbook installs vllm/etc. via `pip install --user`, which pulls
# nvidia-cuda-* wheels into /app/.local but does not set CUDA_HOME or
# symlink /usr/local/cuda. vllm 0.22+ then crashes during engine init
# when FlashInfer tries to JIT a sampler kernel ("Could not find nvcc",
# then "CUDA compiler and toolkit headers are incompatible" on the
# mixed cuda-nvcc 13.3 / cuda-runtime 13.0 wheel combo).
#
# Auto-set CUDA_HOME if a pip-installed nvcc is present, and disable the
# FlashInfer JIT sampler — sampler only, no impact on attention path.
# No-op when vllm isn't installed.
#
# Checked layouts (all are real pip-wheel install paths):
#   nvidia/cu13        — nvidia-nvcc-cu13 (CUDA 13.x wheel style)
#   nvidia/cu12        — nvidia-nvcc-cu12 (CUDA 12.x wheel style)
#   nvidia/cuda_nvcc   — nvidia-cuda-nvcc-cu12 (older cu12 sub-package style)
for cu in \
    /app/.local/lib/python*/site-packages/nvidia/cu13 \
    /app/.local/lib/python*/site-packages/nvidia/cu12 \
    /app/.local/lib/python*/site-packages/nvidia/cuda_nvcc; do
    if [ -x "$cu/bin/nvcc" ]; then
        export CUDA_HOME="$cu"
        break
    fi
done

# Disable the FlashInfer JIT sampler unconditionally — it is sampler-only
# and has no impact on the attention path, but requires nvcc + matching
# CUDA headers at startup. Without this, vLLM crashes with "Could not find
# nvcc" even when the GPU itself is fully visible to the container.
export VLLM_USE_FLASHINFER_SAMPLER="${VLLM_USE_FLASHINFER_SAMPLER:-0}"

# Make Cookbook-installed Python CLIs visible after `pip install --user`.
# vLLM and helper scripts land here because /app is the non-root user's HOME.
export PATH="/app/.local/bin:$PATH"

# New files the app writes are not readable by other users (the tool user
# among them): Phase 5a. The shared work folder has a default ACL instead.
umask 027

# Run first-time setup as the app user so data/ files get the right ownership.
# setup.py is idempotent — skips auth.json / .env if they already exist.
# || true so a setup failure never prevents the container from starting.
"$GOSU_BIN" "$ODY_USER" "$PYTHON_BIN" /app/setup.py || true

# Drop root and run the actual app. `gosu` is preferred over `su` /
# `sudo` because it cleans up the process tree (no extra shell layer)
# so signals (SIGTERM from `docker stop`) reach uvicorn directly.
exec "$GOSU_BIN" "$ODY_USER" "$@"
